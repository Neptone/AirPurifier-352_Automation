#!/usr/bin/env python3
"""352 空气净化器 (H301 加湿净化一体机) 控制 + AQI 自动化

命令:
  python3 purifier.py status          查看设备状态和 AQI
  python3 purifier.py on              开机
  python3 purifier.py off             关机
  python3 purifier.py auto            执行一次自动化检查 (start_time~end_time 窗口内且 AQI>=阈值 则开机)
  python3 purifier.py auto --daemon   持续运行自动化 (每 10 分钟检查一次)
  python3 purifier.py discover        发现局域网设备 (CoAP device-info)
  python3 purifier.py stations [省/市] 列出 IQAir 可选监测站 (如 stations guangdong/guangzhou)

时间窗口支持 HH:MM 精确到分钟, 且支持跨天 (如 19:00~08:00 表示晚 7 点到次日早 8 点)。
窗口内 AQI≥阈值自动开机; end_time 之后自动关机 (end_time 为空则不自动关)。
AQI 数据源: IQAir (逆向其网站数据接口, 无需密钥)。

凭据 (352Life 账号/密码) 不存程序目录: 依次读环境变量 AIR352_ACCOUNT/AIR352_PASSWORD、
~/.352life/credentials.json (chmod 600, 删除该文件即撤销)、交互终端临时输入。
无人值守 (定时任务/自动化) 直接执行 `python3 purifier.py auto` 即可。
"""

import json
import os
import secrets
import socket
import sys
import time
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from air352 import Air352Client, H301_PRODUCT_KEY, Air352Error, Air352AuthError

CONFIG_PATH = Path(__file__).parent / "config.json"
CREDS_PATH = Path.home() / ".352life" / "credentials.json"  # 程序目录外, 不落盘到项目里
CST = timezone(timedelta(hours=8))  # 北京时间

# 中国 HJ 633-2012 PM2.5 → IAQI 断点
_PM25_BP = [0, 35, 75, 115, 150, 250, 350, 500]
_IAQI_BP = [0, 50, 100, 150, 200, 300, 400, 500]


def chinese_aqi_from_pm25(pm25: float) -> int:
    """按中国标准由 PM2.5 实时浓度(μg/m³)计算 AQI。"""
    if pm25 <= 0:
        return 0
    if pm25 >= 500:
        return 500
    for i in range(len(_PM25_BP) - 1):
        lo, hi = _PM25_BP[i], _PM25_BP[i + 1]
        if lo <= pm25 <= hi:
            aqi = (_IAQI_BP[i + 1] - _IAQI_BP[i]) / (hi - lo) * (pm25 - lo) + _IAQI_BP[i]
            return int(round(aqi))
    return 500


def fetch_aqi(cfg: dict = None, **_ignored) -> dict:
    """从 IQAir (AirVisual) 获取当地监测站实时 AQI 数据。

    数据源: IQAir  (DongSi, Dongcheng District, Beijing)
    通过其网站 React Router .data 接口逆向获取, 无需 API Key。
    """
    station = (cfg or {}).get("iqair_station") or "dongsi"
    city_path = (cfg or {}).get("iqair_city_path") or "beijing/beijing"  # 省/市, 如 guangdong/guangzhou
    url = f"https://www.iqair.com/air-quality/china/{city_path}/{station}.data"
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
            "Accept": "*/*",
        },
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        raw = resp.read().decode("utf-8", errors="replace")

    # React Router devalue 流式格式: 多段拼接的 JSON, 取第一段扁平数组
    dec = json.JSONDecoder()
    pos = 0
    flat = None
    while pos < len(raw):
        s = raw[pos:].lstrip()
        if not s:
            break
        pos = len(raw) - len(s)
        try:
            obj, end = dec.raw_decode(raw, pos)
        except json.JSONDecodeError:
            break
        if isinstance(obj, list):
            flat = obj
            break
        pos = end
    if flat is None:
        raise RuntimeError("IQAir 响应解析失败")

    # 解引用: 对象 {_N: idx} 表示 key=flat[N], value=flat[idx]
    def deref(idx, depth=0, seen=None):
        if seen is None:
            seen = set()
        if depth > 40 or idx in seen or not isinstance(idx, int) or not (0 <= idx < len(flat)):
            return idx if not isinstance(idx, int) else None
        seen = seen | {idx}
        v = flat[idx]
        if isinstance(v, dict) and v and all(isinstance(k, str) and k.startswith("_") for k in v):
            out = {}
            for k, val in v.items():
                key = deref(int(k[1:]), depth + 1, seen)
                out[str(key)] = deref(val, depth + 1, seen) if isinstance(val, int) else val
            return out
        if isinstance(v, list) and v and v[0] == "P":
            return None  # deferred, 不需要
        return v

    root = deref(0)
    details = (root.get("routes/$(locale).air-quality.$") or {}).get("data", {}).get("details", {})
    current = details.get("current", {})
    aqi = current.get("aqi")
    if aqi is None:
        raise RuntimeError(f"IQAir 数据异常: current={current}")
    return {
        "pm25": current.get("concentration"),
        "aqi_cn": aqi,  # IQAir 站点报告的 AQI
        "us_aqi": aqi,
        "time": current.get("ts"),
        "station": _zh_station_name(city_path, station, details.get("name", station)),
        "neighborhood": details.get("neighborhood", ""),
        "source": "IQAir",
    }


# ── 局域网 CoAP 发现 (352Life device-info) ──
def discover_local(timeout: float = 3.0) -> list:
    import struct

    def build_req():
        token = secrets.token_bytes(8)
        mid = secrets.randbits(16)
        rid = str(secrets.randbits(32))
        payload = json.dumps(
            {"id": rid, "version": "1.0", "method": "device.info.get", "params": {}},
            separators=(",", ":"),
        ).encode()
        pkt = bytearray(b"\x58\x01" + mid.to_bytes(2, "big") + token)
        prev = 0
        for seg in b"sys/device/info/get".split(b"/"):
            pkt.append(((11 - prev) << 4) | len(seg))
            pkt.extend(seg)
            prev = 11
        pkt.extend(b"\x11\x32\xff")
        pkt.extend(payload)
        return bytes(pkt), token

    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    s.bind(("", 0))
    s.settimeout(timeout)
    pkt, token = build_req()
    s.sendto(pkt, ("255.255.255.255", 5683))
    found = []
    end = time.time() + timeout
    while time.time() < end:
        s.settimeout(max(0.1, end - time.time()))
        try:
            data, addr = s.recvfrom(8192)
        except socket.timeout:
            break
        idx = data.find(b"\xff")
        if idx < 0:
            continue
        try:
            body = json.loads(data[idx + 1:])
        except json.JSONDecodeError:
            continue
        d = body.get("data", {})
        if d.get("productKey"):
            found.append({
                "ip": addr[0],
                "productKey": d.get("productKey"),
                "deviceName": d.get("deviceName"),
                "mac": d.get("mac"),
                "fw": d.get("fwVersion"),
            })
    s.close()
    return found


# ── 配置 ──
def load_config() -> dict:
    if CONFIG_PATH.exists():
        return json.loads(CONFIG_PATH.read_text())
    return {}


def save_config(cfg: dict):
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2))


# ── 主流程 ──
def load_creds():
    """读取 352Life 凭据 (优先环境变量, 其次 ~/.352life/credentials.json)。

    凭据不存程序目录/不进 config.json; 删除 ~/.352life/credentials.json 即撤销。
    """
    acct = (os.environ.get("AIR352_ACCOUNT") or "").strip()
    pwd = os.environ.get("AIR352_PASSWORD") or ""
    if acct and pwd:
        return acct, pwd
    if CREDS_PATH.exists():
        try:
            d = json.loads(CREDS_PATH.read_text())
            if d.get("account") and d.get("password"):
                return str(d["account"]).strip(), str(d["password"])
        except (OSError, json.JSONDecodeError):
            pass
    return None, None


def get_client(cfg) -> Air352Client:
    account = (cfg.get("account") or "").strip() or None
    password = cfg.get("password") or None
    if not account or not password:
        account, password = load_creds()
    if not account or not password:
        if sys.stdin.isatty():
            account = account or input("352Life 账号(手机号): ").strip()
            password = password or input("352Life 密码: ").strip()
        else:
            raise Air352Error(
                "缺少 352Life 凭据: 请设置环境变量 AIR352_ACCOUNT/AIR352_PASSWORD, "
                '或写入 ~/.352life/credentials.json (格式 {"account":..., "password":...}, chmod 600)'
            )
    return Air352Client(account, password)


def pick_device(client: Air352Client, cfg) -> tuple[str, dict]:
    """返回 (iot_id, device_dict)。优先 H301, 其次配置里存的 iot_id, 再次第一个设备。"""
    devices = client.device_list()
    if not devices:
        raise Air352Error("账号下没有绑定设备")
    # 配置里记录的 iot_id 优先
    saved = cfg.get("iot_id")
    if saved:
        for d in devices:
            if d.get("iotId") == saved:
                return saved, d
    for d in devices:
        if d.get("productKey") == H301_PRODUCT_KEY:
            return d["iotId"], d
    d = devices[0]
    return d["iotId"], d


def extract_power(props: dict):
    """从属性字典中提取电源状态, 兼容多种属性名。"""
    for key in ("PowerSwitch", "power", "Power", "switch", "OnOff", "power_switch"):
        if key in props:
            v = props[key]
            if isinstance(v, dict):
                v = v.get("value", v)
            if isinstance(v, (int, float)):
                return bool(int(v)), key
            if isinstance(v, bool):
                return v, key
            if isinstance(v, str) and v.isdigit():
                return bool(int(v)), key
    return None, None


def cmd_status(cfg):
    print("── 局域网发现 ──")
    for dev in discover_local(timeout=2.5):
        print(f"  {dev['ip']}  {dev['mac']}  {dev['productKey']}  fw={dev['fw']}")
    print("\n── AQI ──")
    aqi = fetch_aqi(cfg)
    print(f"  PM2.5: {aqi['pm25']} μg/m³  →  IQAir AQI: {aqi['aqi_cn']}  (站: {aqi.get('station','?')})")
    print("\n── 设备状态 ──")
    client = get_client(cfg)
    client.login()
    iot_id, dev = pick_device(client, cfg)
    print(f"  设备: {dev.get('name', '?')}  iotId={iot_id}")
    props = client.get_props(iot_id)
    power, key = extract_power(props)
    print(f"  电源: {'开' if power else '关'} (属性 {key})")
    print(f"  全部属性: {json.dumps(props, ensure_ascii=False)[:600]}")
    return props


def cmd_power(cfg, on: bool):
    client = get_client(cfg)
    client.login()
    iot_id, dev = pick_device(client, cfg)
    props = client.get_props(iot_id)
    power, key = extract_power(props)
    if power is not None and power == on:
        print(f"设备已{'开启' if on else '关闭'}, 无需操作。")
        return
    # 尝试常见属性名
    tried = []
    for prop_name in ("PowerSwitch", "power", "Power", "switch", "OnOff"):
        try:
            client.set_props(iot_id, {prop_name: 1 if on else 0})
            tried.append(prop_name)
            # 验证
            time.sleep(2)
            props2 = client.get_props(iot_id)
            p2, k2 = extract_power(props2)
            if p2 is not None and p2 == on:
                print(f"✅ 已{'开机' if on else '关机'} (属性 {prop_name})")
                return
        except Air352Error as e:
            tried.append(f"{prop_name}: {e}")
    print(f"⚠️  尝试属性 {tried} 后未能确认状态变化, 请检查设备面板。")


def _parse_hhmm(val, default=None):
    """解析 'HH:MM' (或纯小时数) 为当日分钟数。None → default。"""
    if val is None or val == "":
        return default
    if isinstance(val, (int, float)):
        return int(val) * 60
    s = str(val).strip()
    if ":" in s:
        h, m = s.split(":", 1)
        return int(h) * 60 + int(m)
    return int(s) * 60


def _in_time_window(now_min: int, start_min: int, end_min: int | None) -> bool:
    """判断 now_min(当日分钟数) 是否在 [start_min, end_min) 窗口内, 支持跨天窗口。"""
    if end_min is None:
        return now_min >= start_min
    if start_min <= end_min:  # 当天窗口, 如 08:00~17:00
        return start_min <= now_min < end_min
    return now_min >= start_min or now_min < end_min  # 跨天窗口, 如 19:00~07:00


def _is_after_end(now_min: int, start_min: int, end_min: int | None) -> bool:
    """判断是否处于 end_time 之后的收尾段 (窗口外、end 之后到下一轮 start 之前) → 该自动关机了。"""
    if end_min is None:
        return False  # 未设置 end_time, 不自动关
    if start_min <= end_min:  # 当天窗口: end 之后到午夜
        return now_min >= end_min
    return end_min <= now_min < start_min  # 跨天窗口: 如 08:00~19:00


def _set_power(client: Air352Client, iot_id: str, on: bool, log=print) -> bool:
    """开关机, 优先使用已知属性 PowerSwitch, 失败再尝试别名。

    - 鉴权错误 (Air352AuthError) 直接上抛, 由调用方重登后重试;
    - set 成功即视为指令已送达 (设备会执行), 验证仅作确认, 不再盲试其他属性名
      (旧行为逐个试 5 个属性 × sleep(2) 造成长时间无响应)。
    """
    val = 1 if on else 0
    last_err = None
    for prop_name in ("PowerSwitch", "power", "Power", "switch", "OnOff"):
        try:
            client.set_props(iot_id, {prop_name: val})
        except Air352AuthError:
            raise
        except Air352Error as e:
            last_err = e
            continue
        # 指令已送达 → 短暂等待后验证状态
        time.sleep(1.2)
        try:
            props2 = client.get_props(iot_id)
            p2, _ = extract_power(props2)
        except Air352AuthError:
            raise
        except Air352Error:
            p2 = None  # 读取失败不影响"指令已送达"的事实
        if p2 is not None and p2 == on:
            log(f"  ✅ 已{'开机' if on else '关机'} (属性 {prop_name})")
        else:
            log(f"  ✅ {'开机' if on else '关机'}指令已送达 (属性 {prop_name}), 状态待设备确认")
        return True
    if last_err:
        raise Air352Error(f"开关机指令发送失败: {last_err}") from last_err
    return False


def auto_once(cfg, client: Air352Client | None = None) -> list:
    """执行一次自动化检查, 返回日志行列表 (供 CLI 与 WebUI 复用)。

    client: 可选, 复用已登录客户端 (WebUI 内存会话, 凭据不落盘);
    缺省时按 cfg 构建并在运行时输入凭据 (CLI)。
    """
    logs = []
    threshold = cfg.get("aqi_threshold", 100)
    # 兼容旧配置 start_hour/end_hour (整点), 新配置 start_time/end_time (HH:MM)
    start_min = _parse_hhmm(cfg.get("start_time", cfg.get("start_hour", 19)), 19 * 60)
    end_val = cfg.get("end_time", cfg.get("end_hour"))
    end_min = _parse_hhmm(end_val) if end_val is not None else None

    def fmt(m):
        return f"{m // 60:02d}:{m % 60:02d}" if m is not None else "不限"

    def log(s):
        logs.append(s)

    def _dev(fn):
        """执行设备操作 fn(c); 鉴权失效时重登重试一次。"""
        c = client
        if c is None:
            c = get_client(cfg)  # cfg 无凭据时运行时输入, 不落盘
            c.login()
        try:
            return fn(c)
        except Air352AuthError:
            c.relogin()
            return fn(c)

    now = datetime.now(CST)
    now_min = now.hour * 60 + now.minute
    in_window = _in_time_window(now_min, start_min, end_min)
    aqi = fetch_aqi(cfg)
    line = f"[{now:%H:%M}] AQI={aqi['aqi_cn']} (PM2.5={aqi['pm25']})"

    if not in_window:
        if _is_after_end(now_min, start_min, end_min):
            # end_time 已过 → 自动关机
            log(f"{line} | 已过 {fmt(end_min)}, 自动关机检查...")

            def _off(c):
                iot_id, _ = pick_device(c, cfg)
                props = c.get_props(iot_id)
                power, _ = extract_power(props)
                if power is None:
                    log("  ⚠️  无法读取电源状态")
                elif not power:
                    log("  设备已关闭, 无需操作。")
                elif _set_power(c, iot_id, False, log=log):
                    pass  # 成功日志由 _set_power 写入
                else:
                    log("  ⚠️  关机指令已发送但未能确认状态")

            _dev(_off)
        else:
            log(f"{line} | 未到 {fmt(start_min)}, 跳过")
        return logs

    if aqi["aqi_cn"] < threshold:
        log(f"{line} | AQI < {threshold}, 不开机")
        return logs
    log(f"{line} | 满足条件 (窗口 {fmt(start_min)}~{fmt(end_min)} 内且 AQI≥{threshold}), 检查设备...")

    def _on(c):
        iot_id, _ = pick_device(c, cfg)
        props = c.get_props(iot_id)
        power, _ = extract_power(props)
        if power:
            log("  设备已开启, 保持运行。")
        else:
            if not _set_power(c, iot_id, True, log=log):
                log("  ⚠️  开机指令已发送但未能确认状态")

    _dev(_on)
    return logs


def cmd_auto(cfg, daemon=False, interval_min=None):
    threshold = cfg.get("aqi_threshold", 100)
    start_min = _parse_hhmm(cfg.get("start_time", cfg.get("start_hour", 19)), 19 * 60)
    end_val = cfg.get("end_time", cfg.get("end_hour"))
    end_min = _parse_hhmm(end_val) if end_val is not None else None
    interval = interval_min or cfg.get("check_interval_min", 10)

    def fmt(m):
        return f"{m // 60:02d}:{m % 60:02d}" if m is not None else "不限"

    if not daemon:
        for ln in auto_once(cfg):
            print(ln)
        return
    off_note = f" | {fmt(end_min)} 后自动关机" if end_min is not None else ""
    print(f"自动化已启动: 每 {interval} 分钟检查 | 窗口 {fmt(start_min)}~{fmt(end_min)} 内 AQI≥{threshold} 自动开机{off_note}")
    while True:
        try:
            for ln in auto_once(cfg):
                print(ln)
        except Exception as e:
            print(f"  错误: {e}")
        time.sleep(interval * 60)


# slug → 中文站名缓存 (list_stations 填充, fetch_aqi 查用)
_STATION_ZH = {}


def _zh_station_name(city_path: str, slug: str, fallback: str) -> str:
    """查询站点中文名 (缓存未命中时拉取一次城市站点表)。"""
    zh = _STATION_ZH.get(slug)
    if zh is None:
        try:
            list_stations(city_path)
        except Exception:
            pass
        zh = _STATION_ZH.get(slug)
    return zh or fallback


def list_stations(city_path: str = "beijing/beijing") -> list:
    """从 IQAir 城市页枚举监测站 (slug, 中文名, 当前AQI)。city_path 形如 '省/市'。

    优先抓取 /cn/ 中文页 (站名为中文); 失败时回退英文页。
    """
    import re
    pat = re.compile(
        r">([^<>]+)</p></div></td><td[^>]*>.*?aqi-bg-\w+[^>]*><p[^>]*>(\d+)</p>"
        r'.*?href="(?:/cn)?/air-quality/china/'
        + re.escape(city_path)
        + r'/([a-z0-9-]+)"',
        re.S,
    )
    rows, last_err = [], None
    for url in (
        f"https://www.iqair.com/cn/air-quality/china/{city_path}",
        f"https://www.iqair.com/air-quality/china/{city_path}",
    ):
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
                "Accept": "text/html,application/xhtml+xml",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                html = resp.read().decode("utf-8", errors="replace")
        except Exception as e:
            last_err = e
            continue
        rows = []
        for tr in re.findall(r"<tr[^>]*>.*?</tr>", html, re.S):
            m = pat.search(tr)
            if m:
                rows.append((m.group(1), m.group(2), m.group(3)))
        if rows:
            break
    if not rows and last_err:
        raise last_err
    seen, out = set(), []
    for name, aqi, slug in rows:
        if slug in seen:
            continue
        seen.add(slug)
        name = name.strip()
        out.append({"slug": slug, "name": name, "aqi": int(aqi)})
        _STATION_ZH[slug] = name
    return out


def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return
    cmd = args[0]
    cfg = load_config()

    if cmd == "discover":
        for dev in discover_local():
            print(json.dumps(dev, ensure_ascii=False))
        return

    if cmd == "stations":
        # 枚举可选站点: python3 purifier.py stations [省/市, 默认 beijing/beijing]
        city_path = args[1] if len(args) > 1 else cfg.get("iqair_city_path", "beijing/beijing")
        print(f"IQAir 站点列表 (china/{city_path}):")
        for s in list_stations(city_path):
            print(f"  {s['slug']:32s} {s['name']:28s} AQI={s['aqi']}")
        return

    if cmd == "config":
        # 交互式配置 (账号/密码不再保存到文件, 运行时按需输入, 仅存内存)
        cfg.pop("account", None)
        cfg.pop("password", None)
        cfg["iqair_station"] = input("IQAir 站点 slug [%s]: " % cfg.get("iqair_station", "daxing-huangcun")) or cfg.get("iqair_station", "daxing-huangcun")
        cfg["iqair_city_path"] = input("IQAir 城市路径 省/市 (如 guangdong/guangzhou) [%s]: " % cfg.get("iqair_city_path", "beijing/beijing")) or cfg.get("iqair_city_path", "beijing/beijing")
        cfg["aqi_threshold"] = int(input("AQI 阈值 [%s]: " % cfg.get("aqi_threshold", 100)) or cfg.get("aqi_threshold", 100))
        cfg["start_time"] = input("生效起始时间 HH:MM (支持分钟, 如 19:00) [%s]: " % cfg.get("start_time", "19:00")) or cfg.get("start_time", "19:00")
        end_in = input("生效结束时间 HH:MM (之后自动关机; 回车=不自动关) [%s]: " % (cfg.get("end_time") or "不自动关")).strip()
        cfg["end_time"] = end_in if end_in else None
        cfg["check_interval_min"] = int(input("检查间隔(分钟) [%s]: " % cfg.get("check_interval_min", 10)) or cfg.get("check_interval_min", 10))
        save_config(cfg)
        print(f"已保存到 {CONFIG_PATH} (账号/密码不落盘, 每次运行时输入)")
        return

    # AQI 数据源: IQAir 大兴监测站 (无需坐标配置)

    if cmd == "status":
        cmd_status(cfg)
    elif cmd == "on":
        cmd_power(cfg, True)
    elif cmd == "off":
        cmd_power(cfg, False)
    elif cmd == "auto":
        daemon = "--daemon" in args
        cmd_auto(cfg, daemon=daemon)
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
