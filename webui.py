#!/usr/bin/env python3
"""352 空气净化器 Web 控制台 — 本地 REST API + 静态页面

启动:  python3 webui.py [端口, 默认 8765]
浏览器: http://127.0.0.1:8765
"""

import json
import sys
import threading
import urllib.request
from datetime import datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, str(Path(__file__).parent))
import purifier
from air352 import Air352Client, Air352Error, Air352AuthError

BASE = Path(__file__).parent
STATIC = BASE / "static"
CST = timezone(timedelta(hours=8))

# ── 中国大陆主要城市 → IQAir 城市路径 (省拼音/市拼音) ──
CITIES = [
    {"name": "北京", "path": "beijing/beijing", "prov": "北京"},
    {"name": "上海", "path": "shanghai/shanghai", "prov": "上海"},
    {"name": "广州", "path": "guangdong/guangzhou", "prov": "广东"},
    {"name": "深圳", "path": "guangdong/shenzhen", "prov": "广东"},
    {"name": "东莞", "path": "guangdong/dongguan", "prov": "广东"},
    {"name": "佛山", "path": "guangdong/foshan", "prov": "广东"},
    {"name": "珠海", "path": "guangdong/zhuhai", "prov": "广东"},
    {"name": "成都", "path": "sichuan/chengdu", "prov": "四川"},
    {"name": "重庆", "path": "chongqing/chongqing", "prov": "重庆"},
    {"name": "天津", "path": "tianjin/tianjin", "prov": "天津"},
    {"name": "杭州", "path": "zhejiang/hangzhou", "prov": "浙江"},
    {"name": "宁波", "path": "zhejiang/ningbo", "prov": "浙江"},
    {"name": "温州", "path": "zhejiang/wenzhou", "prov": "浙江"},
    {"name": "南京", "path": "jiangsu/nanjing", "prov": "江苏"},
    {"name": "苏州", "path": "jiangsu/suzhou", "prov": "江苏"},
    {"name": "无锡", "path": "jiangsu/wuxi", "prov": "江苏"},
    {"name": "武汉", "path": "hubei/wuhan", "prov": "湖北"},
    {"name": "西安", "path": "shaanxi/xian", "prov": "陕西"},
    {"name": "长沙", "path": "hunan/changsha", "prov": "湖南"},
    {"name": "郑州", "path": "henan/zhengzhou", "prov": "河南"},
    {"name": "青岛", "path": "shandong/qingdao", "prov": "山东"},
    {"name": "济南", "path": "shandong/jinan", "prov": "山东"},
    {"name": "沈阳", "path": "liaoning/shenyang", "prov": "辽宁"},
    {"name": "大连", "path": "liaoning/dalian", "prov": "辽宁"},
    {"name": "哈尔滨", "path": "heilongjiang/harbin", "prov": "黑龙江"},
    {"name": "长春", "path": "jilin/changchun", "prov": "吉林"},
    {"name": "石家庄", "path": "hebei/shijiazhuang", "prov": "河北"},
    {"name": "太原", "path": "shanxi/taiyuan", "prov": "山西"},
    {"name": "合肥", "path": "anhui/hefei", "prov": "安徽"},
    {"name": "福州", "path": "fujian/fuzhou", "prov": "福建"},
    {"name": "厦门", "path": "fujian/xiamen", "prov": "福建"},
    {"name": "南昌", "path": "jiangxi/nanchang", "prov": "江西"},
    {"name": "昆明", "path": "yunnan/kunming", "prov": "云南"},
    {"name": "贵阳", "path": "guizhou/guiyang", "prov": "贵州"},
    {"name": "南宁", "path": "guangxi/nanning", "prov": "广西"},
    {"name": "海口", "path": "hainan/haikou", "prov": "海南"},
    {"name": "兰州", "path": "gansu/lanzhou", "prov": "甘肃"},
    {"name": "银川", "path": "ningxia/yinchuan", "prov": "宁夏"},
    {"name": "西宁", "path": "qinghai/xining", "prov": "青海"},
    {"name": "乌鲁木齐", "path": "xinjiang/wulumuqi", "prov": "新疆"},
    {"name": "呼和浩特", "path": "neimenggu/huhehaote", "prov": "内蒙古"},
    {"name": "拉萨", "path": "xizang/lasa", "prov": "西藏"},
]
CITY_BY_NAME = {c["name"]: c for c in CITIES}


# ── 自动化守护 (线程内可控) ──
class Automation:
    def __init__(self):
        self.thread = None
        self.stop_event = threading.Event()
        self.logs = []          # [(ts, line), ...] 最近 200 条
        self.lock = threading.Lock()

    def log(self, line: str):
        with self.lock:
            self.logs.append((datetime.now(CST).strftime("%H:%M:%S"), line))
            if len(self.logs) > 200:
                self.logs = self.logs[-200:]

    @property
    def running(self):
        # stop() 置事件后立即视为已停止 (即使线程还在收尾当前轮), 按钮即时响应
        return (self.thread is not None and self.thread.is_alive()
                and not self.stop_event.is_set())

    def start(self):
        if self.thread is not None and self.thread.is_alive():
            if self.stop_event.is_set():
                self.thread.join(timeout=30)  # 等上一轮收尾, 防止双线程并发
            else:
                return False
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()
        return True

    def stop(self):
        self.stop_event.set()
        return True

    def _loop(self):
        self.log("自动化守护已启动 (每 %d 分钟检查)" % self._interval())
        while not self.stop_event.is_set():
            try:
                cfg = purifier.load_config()
                client = _SESS.get("client")  # 内存会话客户端 (凭据不落盘)
                if client is None:
                    self.log("未登录 352Life 账号, 本轮跳过 (请在页面登录后使用自动化)")
                else:
                    for ln in purifier.auto_once(cfg, client=client):
                        self.log(ln)
            except Exception as e:
                self.log(f"错误: {e}")
            self.stop_event.wait(self._interval() * 60)
        self.log("自动化守护已停止")

    @staticmethod
    def _interval():
        return purifier.load_config().get("check_interval_min", 10) or 10


AUTO = Automation()


# ── 位置识别 (IP 地理定位, 作为城市默认值) ──
def detect_location() -> dict:
    """通过 IP 地理定位识别所在城市 (仅取城市名, 不涉及地图)。"""
    for url in (
        "http://ip-api.com/json/?lang=zh-CN&fields=status,country,regionName,city,isp",
        "https://ipapi.co/json/",
    ):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "purifier-webui/1.0"})
            with urllib.request.urlopen(req, timeout=6) as r:
                d = json.loads(r.read())
            city = d.get("city") or ""
            region = d.get("regionName") or d.get("region") or ""
            # ip-api 返回 "北京市" 等, 去掉后缀匹配
            for cand in (city.rstrip("市"), region.rstrip("省市")):
                if cand in CITY_BY_NAME:
                    c = CITY_BY_NAME[cand]
                    return {"ok": True, "city": c["name"], "city_path": c["path"],
                            "raw_city": city, "raw_region": region, "source": url.split("/")[2]}
            return {"ok": True, "city": None, "raw_city": city, "raw_region": region,
                    "source": url.split("/")[2], "hint": "未能映射到内置城市列表, 请手动选择"}
        except Exception:
            continue
    return {"ok": False, "error": "IP 定位失败, 请手动选择城市"}


# ── 会话缓存: 复用登录态/IoT token/设备ID, 开关机免重复全链路登录 ──
# 凭据 (account/password) 仅存于进程内存, 不写入任何文件; 服务重启后需重新登录
# ThreadingHTTPServer 多线程共享, 必须加锁防止并发重复登录/互相覆盖
_SESS = {"account": None, "password": None, "client": None, "iot_id": None, "dev": None}
_SESS_LOCK = threading.Lock()


def _sess_invalidate():
    """作废缓存登录态/令牌 (保留内存凭据用于自动重登)。"""
    with _SESS_LOCK:
        _SESS.update(client=None, iot_id=None, dev=None)


def _get_client(cfg) -> Air352Client:
    """内存会话客户端: 登录后复用 (IoT token 约 20h, 过期自动续)。凭据不落盘。"""
    with _SESS_LOCK:
        c = _SESS.get("client")
        if c is not None:
            c._ensure_token()  # 无网络请求, 仅在过期时才重登
            return c
        account, password = _SESS.get("account"), _SESS.get("password")
        if not account or not password:
            raise Air352Error("未登录 352Life 账号 (凭据仅保存在内存中, 服务重启后需重新登录)")
        c = Air352Client(account, password)
        c.login()
        _SESS.update(client=c, iot_id=None, dev=None)
        return c


def _get_iot_id(client, cfg) -> str:
    with _SESS_LOCK:
        if _SESS.get("iot_id"):
            return _SESS["iot_id"]
    iot_id, dev = purifier.pick_device(client, cfg)
    with _SESS_LOCK:
        _SESS["iot_id"] = iot_id
        _SESS["dev"] = dev
    return iot_id


def _sess_call(cfg, fn):
    """执行 fn(client, iot_id)。鉴权失效 (token 被服务端提前作废) 时自动重登并重试一次。

    这是 'identityId is blank' 类故障的自愈入口: 旧缓存永久失效导致所有请求失败,
    必须强制重新走登录链路才能恢复。
    """
    def _once():
        client = _get_client(cfg)
        iot_id = _get_iot_id(client, cfg)
        return fn(client, iot_id)
    try:
        return _once()
    except Air352AuthError:
        _sess_invalidate()
        return _once()  # 重登后重试一次


def _pv(props, *names):
    """从属性字典取原始值。"""
    for n in names:
        if n in props:
            v = props[n]
            if isinstance(v, dict):
                v = v.get("value", v)
            return v
    return None


def _device_snapshot(cfg):
    def _do(client, iot_id):
        dev = _SESS.get("dev") or {}
        props = client.get_props(iot_id)
        power, key = purifier.extract_power(props)
        pm25 = _pv(props, "PM25")
        metrics = {
            "power": bool(power) if power is not None else None,
            "wind_speed": _pv(props, "WindSpeed", "WindValue"),
            "work_mode": _pv(props, "WorkMode"),
            "pm25": pm25,
            "air_quality": _pv(props, "AirQuality"),
            "temperature": _pv(props, "CurrentTemperature"),
            "humidity": _pv(props, "RelativeHumidity"),
            "humidifier_on": bool(_pv(props, "HumidificationFunction") or 0),
            "uv_on": bool(_pv(props, "UVLEDSwitch") or 0),
            "filter_life": [
                _pv(props, "FilterLifeTimePercent_1"),
                _pv(props, "FilterLifeTimePercent_2"),
                _pv(props, "FilterLifeTimePercent_3"),
            ],
        }
        if pm25 is not None:
            try:
                metrics["aqi_device"] = purifier.chinese_aqi_from_pm25(float(pm25))
            except (TypeError, ValueError):
                metrics["aqi_device"] = None
        else:
            metrics["aqi_device"] = None
        return {
            "iot_id": iot_id,
            "name": dev.get("name", "352 净化器"),
            "product_key": dev.get("productKey"),
            "power": bool(power) if power is not None else None,
            "power_attr": key,
            "metrics": metrics,
            "props": props,
        }
    return _sess_call(cfg, _do)


# ── HTTP ──
class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # 静默访问日志

    # ---- helpers ----
    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n))
        except Exception:
            return {}

    def _file(self, path: Path, ctype=None):
        if not path.exists() or not path.is_file():
            self._json({"error": "not found"}, 404)
            return
        data = path.read_bytes()
        if ctype is None:
            ctype = {
                ".html": "text/html; charset=utf-8",
                ".js": "application/javascript; charset=utf-8",
                ".css": "text/css; charset=utf-8",
                ".svg": "image/svg+xml",
                ".png": "image/png",
                ".ico": "image/x-icon",
            }.get(path.suffix, "application/octet-stream")
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        # 防止浏览器缓存旧版页面/图片 (改版后用户仍看到旧 UI)
        self.send_header("Cache-Control", "no-store, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    # ---- routes ----
    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        try:
            if u.path in ("/", "/index.html"):
                self._file(STATIC / "index.html")
            elif u.path.startswith("/static/"):
                self._file(STATIC / u.path[len("/static/"):])

            elif u.path == "/api/config":
                cfg = purifier.load_config()
                # 账号/密码永不落盘, 也不通过接口回传
                out = {k: v for k, v in cfg.items() if k not in ("password", "account")}
                self._json(out)

            elif u.path == "/api/cities":
                self._json({"cities": CITIES})

            elif u.path == "/api/location":
                self._json(detect_location())

            elif u.path == "/api/stations":
                city_path = (q.get("city_path") or ["beijing/beijing"])[0]
                try:
                    stations = purifier.list_stations(city_path)
                    self._json({"city_path": city_path, "stations": stations})
                except Exception as e:
                    self._json({"error": f"站点枚举失败: {e}"}, 502)

            elif u.path == "/api/aqi":
                cfg = purifier.load_config()
                # 允许前端传入下拉框当前选中值 (未保存也可预览对应站点), 空值回退已保存配置
                qp_station = (q.get("station") or [""])[0].strip()
                qp_city = (q.get("city_path") or [""])[0].strip()
                if qp_station:
                    cfg["iqair_station"] = qp_station
                if qp_city:
                    cfg["iqair_city_path"] = qp_city
                self._json(purifier.fetch_aqi(cfg))

            elif u.path == "/api/status":
                cfg = purifier.load_config()
                try:
                    self._json({"ok": True, "device": _device_snapshot(cfg)})
                except Exception as e:
                    self._json({"ok": False, "error": str(e)}, 502)

            elif u.path == "/api/auto/status":
                with AUTO.lock:
                    logs = [{"t": t, "line": ln} for t, ln in AUTO.logs[-50:]]
                self._json({"running": AUTO.running, "logs": logs})

            else:
                self._json({"error": "not found"}, 404)
        except Exception as e:
            self._json({"error": str(e)}, 500)

    def do_POST(self):
        u = urlparse(self.path)
        body = self._body()
        try:
            if u.path == "/api/config":
                cfg = purifier.load_config()
                # 仅接受非凭据字段; account/password 一律忽略 (凭据只经 /api/login 进内存)
                for key in ("iqair_station", "iqair_city_path",
                            "aqi_threshold", "start_time", "end_time", "check_interval_min",
                            "city_explicit"):
                    if key in body:
                        cfg[key] = body[key]
                # 数值字段归一化
                if "aqi_threshold" in cfg:
                    cfg["aqi_threshold"] = int(cfg["aqi_threshold"])
                if "check_interval_min" in cfg:
                    cfg["check_interval_min"] = int(cfg["check_interval_min"])
                purifier.save_config(cfg)
                out = {k: v for k, v in cfg.items() if k not in ("password", "account")}
                self._json({"ok": True, "config": out})

            elif u.path == "/api/login":
                account = (body.get("account") or "").strip()
                password = body.get("password") or ""
                if not account or not password:
                    self._json({"error": "请输入账号和密码"}, 400)
                    return
                client = Air352Client(account, password)
                client.login()
                # 凭据仅保存在进程内存 (不写入任何文件), 服务重启后需重新登录
                with _SESS_LOCK:
                    _SESS.update(account=account, password=password,
                                 client=client, iot_id=None, dev=None)
                self._json({"ok": True, "account": account})

            elif u.path == "/api/power":
                cfg = purifier.load_config()
                on = bool(body.get("on"))
                def _do(client, iot_id):
                    return purifier._set_power(client, iot_id, on, log=lambda *_: None)
                ok = _sess_call(cfg, _do)
                if ok:
                    self._json({"ok": True, "on": on})
                else:
                    # 明确失败必须用非 200 状态码, 否则前端会误报"指令已送达"
                    self._json({"ok": False, "on": on,
                                "error": "指令发送失败, 未能确认设备状态"}, 502)

            elif u.path == "/api/auto/once":
                cfg = purifier.load_config()
                logs = purifier.auto_once(cfg)
                self._json({"ok": True, "logs": logs})

            elif u.path == "/api/auto/start":
                started = AUTO.start()
                self._json({"ok": True, "running": AUTO.running, "already": not started})

            elif u.path == "/api/auto/stop":
                AUTO.stop()
                self._json({"ok": True, "running": False})

            else:
                self._json({"error": "not found"}, 404)
        except Exception as e:
            self._json({"error": str(e)}, 500)


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"352 净化器控制台: http://127.0.0.1:{port}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        AUTO.stop()
        srv.shutdown()


if __name__ == "__main__":
    main()
