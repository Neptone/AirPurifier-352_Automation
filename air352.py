#!/usr/bin/env python3
"""352Life (352 H301 等) 云端 API 客户端 —— 基于 hass-air352 逆向实现的纯标准库版本。

用法:
    from air352 import Air352Client
    c = Air352Client("手机号", "密码")
    c.login()
    devices = c.device_list()
    props = c.get_props(iot_id)
    c.set_props(iot_id, {"PowerSwitch": 1})
"""

import base64
import gzip
import hashlib
import hmac
import json
import time
import uuid
from email.utils import formatdate
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

# ── 协议常量 (来自 352Life APK 逆向 / hass-air352) ──
APPID_352 = "8d5018f2bc0f11ea8e6388e9fe5ac5b6"
BASE_URL_352 = "https://app.352air.com"
ALI_APP_KEY = "27554844"
ALI_APP_SECRET = "b66d2c9767cd15a7c5a088341055d134"
ALI_DOMAIN = "api.link.aliyun.com"
ALI_OA_DOMAIN = "living-account.cn-shanghai.aliyuncs.com"

# 352 H301 加湿净化一体机的产品密钥
H301_PRODUCT_KEY = "a1eLtLlIKYR"

TIMEOUT = 15


class Air352Error(Exception):
    pass


class Air352AuthError(Air352Error):
    pass


def _http(method, url, headers=None, data=None, timeout=TIMEOUT):
    """发起 HTTP 请求, 自动解压 gzip, 返回 JSON dict。"""
    req = Request(url, data=data, headers=headers or {}, method=method)
    try:
        with urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except HTTPError as e:
        raw = e.read()
        try:
            body = json.loads(raw)
            raise Air352Error(f"HTTP {e.code}: {body.get('message', body)}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise Air352Error(f"HTTP {e.code}") from e
    except URLError as e:
        raise Air352Error(f"网络错误: {e.reason}") from e
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise Air352Error(f"响应不是 JSON: {raw[:200]!r}") from e


class Air352Client:
    def __init__(self, username: str, password: str):
        self.username = username
        self.password = password
        self._access_token = None
        self._iot_token = None
        self._iot_token_ts = 0
        self._iot_token_expire = 0

    # ── 352 自有 API 签名: md5(APPID + path + ts) ──
    @staticmethod
    def _352_headers(path: str, token: str = "") -> dict:
        ts = str(int(time.time()))
        sign = hashlib.md5((APPID_352 + path + ts).encode()).hexdigest()
        return {
            "Content-Type": "application/json;charset=utf-8",
            "Authorization": f"Token {token}",
            "ts": ts,
            "sign": sign,
        }

    # ── 阿里云 API 网关 x-ca 签名 (HmacSHA1) ──
    def _ali_sign(self, method, path, params, api_ver, iot_token=None):
        body_obj = {
            "id": str(uuid.uuid4()),
            "version": "1.0",
            "request": {"apiVer": api_ver, "language": "zh-CN"},
            "params": params,
        }
        if iot_token:
            body_obj["request"]["iotToken"] = iot_token
        body = json.dumps(body_obj, separators=(",", ":")).encode()

        nonce = str(uuid.uuid4())
        ts_ms = str(int(time.time() * 1000))
        ct = "application/json; charset=utf-8"
        accept = "application/json; charset=utf-8"
        content_md5 = base64.b64encode(hashlib.md5(body).digest()).decode()
        sign_headers = {"x-ca-key": ALI_APP_KEY, "x-ca-nonce": nonce, "x-ca-timestamp": ts_ms}
        sh_str = "".join(f"{k}:{v}\n" for k, v in sorted(sign_headers.items()))
        sts = f"{method}\n{accept}\n{content_md5}\n{ct}\n\n{sh_str}{path}"
        sig = base64.b64encode(
            hmac.new(ALI_APP_SECRET.encode(), sts.encode(), hashlib.sha1).digest()
        ).decode()
        headers = {
            "content-type": ct, "accept": accept,
            "x-ca-key": ALI_APP_KEY, "x-ca-nonce": nonce, "x-ca-timestamp": ts_ms,
            "x-ca-signature": sig,
            "x-ca-signature-headers": ",".join(sorted(sign_headers)),
            "x-ca-signature-method": "HmacSHA1",
            "content-md5": content_md5,
        }
        return headers, body

    def _ali_request(self, path, params, api_ver="1.0.2", iot_token=None):
        headers, body = self._ali_sign("POST", path, params, api_ver, iot_token)
        data = _http("POST", f"https://{ALI_DOMAIN}{path}", headers, body)
        code = data.get("code", -1)
        if code == 200:
            return data
        msg = data.get("message", data.get("localizedMsg", str(data)))
        # 29003 "identityId is blank" 等同鉴权失效 (IoT token 被服务端提前作废)
        if code in (401, 2001, 2002, 2459, 26101, 26102, 29003):
            raise Air352AuthError(f"鉴权失败({code}): {msg}")
        raise Air352Error(f"API 错误({code}): {msg}")

    # ── 登录全链路: 352 登录 → 区域 → OA 登录 → IoT 会话 ──
    def login(self):
        # 1) 352 账号登录
        path = "/api/v1/enduser/login"
        data = _http(
            "POST", BASE_URL_352 + path,
            self._352_headers(path),
            json.dumps({"account": self.username, "password": self.password}).encode(),
        )
        if data.get("code") not in (0, 200):
            raise Air352AuthError(f"登录失败: {data.get('message', data)}")
        self._access_token = data["data"]["access_token"]

        # 2) 区域确认
        self._ali_request(
            "/living/account/region/get",
            {"type": "THIRD_AUTHCODE", "countryCode": "CN", "authCode": self._access_token},
        )

        # 3) OA 登录 → sid
        sid = self._oa_login()

        # 4) 创建 IoT 会话 → iotToken
        r = self._ali_request(
            "/account/createSessionByAuthCode",
            {"request": {"authCode": sid, "accountType": "OA_SESSION", "appKey": ALI_APP_KEY}},
            api_ver="1.0.4",
        )
        self._iot_token = r["data"]["iotToken"]
        self._iot_token_expire = r["data"].get("iotTokenExpire", 72000)
        self._iot_token_ts = time.time()
        return True

    def _oa_login(self) -> str:
        nonce = str(uuid.uuid4())
        ts_ms = str(int(time.time() * 1000))
        date_str = formatdate(timeval=None, localtime=False, usegmt=True)
        ct = "application/x-www-form-urlencoded; charset=utf-8"
        accept = "application/json; charset=utf-8"
        path = "/api/prd/loginbyoauth.json"

        oa_json = json.dumps({
            "country": "CN", "authCode": self._access_token,
            "oauthPlateform": 23, "oauthAppKey": ALI_APP_KEY,
            "riskControlInfo": {
                "appVersion": "1070001", "USE_OA_PWD_ENCRYPT": "true",
                "utdid": "ffffffffffffffffffffffff", "netType": "wifi",
                "umidToken": "", "locale": "zh_CN", "appVersionName": "1.7.1",
                "deviceId": str(uuid.uuid4()), "routerMac": "02:00:00:00:00:00",
                "platformVersion": "36", "appAuthToken": "",
                "appID": "com.mxchip.project352", "signType": "RSA",
                "sdkVersion": "3.4.2", "model": "HA",
                "USE_H5_NC": "true", "platformName": "android",
                "brand": "HomeAssistant", "yunOSId": "",
            },
        }, separators=(",", ":"))
        body = ("loginByOauthRequest=" + quote(oa_json, safe="")).encode()

        sign_headers = {
            "x-ca-key": ALI_APP_KEY, "x-ca-nonce": nonce,
            "x-ca-signature-method": "HmacSHA1", "x-ca-timestamp": ts_ms,
        }
        sh_str = "".join(f"{k}:{v}\n" for k, v in sorted(sign_headers.items()))
        sts = f"POST\n{accept}\n\n{ct}\n{date_str}\n{sh_str}{path}?loginByOauthRequest={oa_json}"
        sig = base64.b64encode(
            hmac.new(ALI_APP_SECRET.encode(), sts.encode(), hashlib.sha1).digest()
        ).decode()
        headers = {
            "content-type": ct, "accept": accept,
            "x-ca-key": ALI_APP_KEY, "x-ca-nonce": nonce, "x-ca-timestamp": ts_ms,
            "x-ca-signature": sig,
            "x-ca-signature-headers": ",".join(sorted(sign_headers)),
            "x-ca-signature-method": "HmacSHA1",
            "ca_version": "1", "user-agent": "ALIYUN-ANDROID-DEMO",
            "date": date_str, "Accept-Encoding": "gzip",
        }
        data = _http("POST", f"https://{ALI_OA_DOMAIN}{path}", headers, body)
        try:
            return data["data"]["data"]["loginSuccessResult"]["sid"]
        except (KeyError, TypeError) as e:
            raise Air352AuthError(f"OA 登录失败: {data}") from e

    def _ensure_token(self):
        if not self._iot_token:
            self.login()
            return
        elapsed = time.time() - self._iot_token_ts
        if elapsed >= self._iot_token_expire - 3600:
            self.login()

    def relogin(self):
        """强制作废本地 token 并重新走全链路登录 (用于服务端提前作废 token 的场景)。"""
        self._iot_token = None
        self._iot_token_ts = 0
        self.login()

    # ── 公开接口 ──
    def device_list(self) -> list:
        self._ensure_token()
        r = self._ali_request(
            "/uc/listBindingByAccount",
            {"pageNo": 1, "pageSize": 50},
            iot_token=self._iot_token,
        )
        return r.get("data", {}).get("data", [])

    def get_props(self, iot_id: str) -> dict:
        self._ensure_token()
        r = self._ali_request(
            "/thing/properties/get", {"iotId": iot_id}, iot_token=self._iot_token
        )
        return r.get("data", {})

    def set_props(self, iot_id: str, items: dict):
        self._ensure_token()
        self._ali_request(
            "/thing/properties/set",
            {"iotId": iot_id, "items": items},
            iot_token=self._iot_token,
        )

    def find_device(self, product_key: str = H301_PRODUCT_KEY) -> dict | None:
        """按产品密钥查找设备(默认找 H301), 找不到就返回列表里第一个。"""
        devices = self.device_list()
        for d in devices:
            if d.get("productKey") == product_key:
                return d
        return devices[0] if devices else None


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 3:
        print("用法: python3 air352.py <手机号> <密码>")
        sys.exit(1)
    c = Air352Client(sys.argv[1], sys.argv[2])
    print("登录中...")
    c.login()
    print("登录成功! 设备列表:")
    for d in c.device_list():
        print(f"  {d.get('name','?')} | productKey={d.get('productKey')} | iotId={d.get('iotId')}")
        print(f"    {json.dumps(d, ensure_ascii=False)[:300]}")
