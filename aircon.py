#!/usr/bin/env python3
"""Neptune air-conditioning client reconstructed from the control web bundle.

No credentials are embedded or written to disk. A cached login is read only
for the explicitly selected asset. Remote operations require running this
program in an environment with network access.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import json
import os
from pathlib import Path
import re
import struct
import sys
import threading
import time
from urllib import error, parse, request
import zlib


BASE_URL = "https://gateway.hzxwwl.com/api/"
LOGIN_PATH = "asset/ac/tourist/auth/login"
LOGIN_FIELDS = ("xm", "xh", "qsId", "timestamp", "platformType", "sign")
MODES = {"auto": 1, "cool": 2, "dry": 3, "heat": 4, "fan": 5}
WINDS = {"auto": 0, "low": 1, "medium": 2, "high": 3, "turbo": 4}
MAX_BYTES = 2 * 1024 * 1024


class AirconError(Exception):
    """An error whose text is safe to display without exposing credentials."""


@dataclass(repr=False)
class Session:
    asset_code: str
    token: str
    expires_at: float
    signed_params: dict[str, str]

    def require_valid(self) -> None:
        if self.expires_at <= time.time() + 10:
            raise AirconError(
                "临时授权已过期。请从 ihome 重新进入空调遥控后再运行；"
                "原签名也可能已失效，需从 ihome 取得新的签名入口。"
            )


def session_from_login(payload: dict, asset_code: str, received_at: float,
                       signed_params: dict[str, str]) -> Session:
    if payload.get("code") != "0000" or payload.get("success") is not True:
        raise AirconError("登录未成功，请从 ihome 重新进入遥控页面。")
    data = payload.get("data")
    if not isinstance(data, dict) or data.get("assetCode") != asset_code:
        raise AirconError("登录响应与指定空调不匹配。")
    if data.get("isIot") is not True:
        raise AirconError("指定设备不是该网页支持的物联网空调。")
    token = data.get("tmpAuthToken", "")
    if not isinstance(token, str) or not re.fullmatch(r"[0-9a-fA-F]{32}", token):
        raise AirconError("临时授权格式与本次观察到的协议不一致。")
    ttl = data.get("expireTime")
    if isinstance(ttl, bool) or not isinstance(ttl, (int, float)) or ttl <= 0:
        raise AirconError("登录响应没有有效的授权期限。")
    return Session(asset_code, token, received_at + ttl, signed_params)


def read_cached_login(path: Path, asset_code: str) -> Session | None:
    """Read only matching login entries from Chromium Simple Cache v5.

    The gzip end and CRC identify the response-body boundary. The server Date
    header determines expiry; file mtime is deliberately not used as issuance
    time, and the signed request timestamp is preserved without interpretation.
    """
    try:
        with path.open("rb") as f:
            header = f.read(24)
            if len(header) != 24 or header[:8] != bytes.fromhex("305c72a71b6dfbfc"):
                return None
            _, version, key_size, _ = struct.unpack_from("<QIII", header)
            if version != 5 or not 0 < key_size <= 8192:
                return None
            key = f.read(key_size).decode("utf-8", errors="replace")
            match = re.search(r"https://gateway\.hzxwwl\.com/api/asset/ac/tourist/auth/login\?[^\s\x00]+", key)
            if not match:
                return None
            parsed = parse.urlsplit(match.group())
            query = parse.parse_qs(parsed.query)
            if any(len(query.get(field, [])) != 1 for field in LOGIN_FIELDS):
                return None
            remainder = f.read(MAX_BYTES + 1)
            if len(remainder) > MAX_BYTES or not remainder.startswith(b"\x1f\x8b"):
                return None
        decoder = zlib.decompressobj(31)
        body = decoder.decompress(remainder, MAX_BYTES)
        if not decoder.eof:
            return None
        date = re.search(rb"(?:\x00|\r\n)date:\s*([^\x00\r\n]+)",
                         decoder.unused_data, re.IGNORECASE)
        if not date:
            return None
        issued = parsedate_to_datetime(date.group(1).decode("ascii")).timestamp()
        payload = json.loads(body)
        if not isinstance(payload, dict):
            return None
        return session_from_login(payload, asset_code, issued,
                                  {field: query[field][0] for field in LOGIN_FIELDS})
    except (OSError, ValueError, TypeError, zlib.error, AirconError):
        return None


def discover_session(asset_code: str, cache_root: Path | None = None,
                     *, oldest: bool = False) -> Session:
    root = cache_root or (
        Path.home() / "Library/Containers/com.tencent.xinWeChat/Data/Documents/"
        "app_data/radium/web/profiles"
    )
    selected = None
    for path in root.glob("*/Cache/Cache_Data/*_0"):
        candidate = read_cached_login(path, asset_code)
        if candidate is not None and (
            selected is None or
            (candidate.expires_at < selected.expires_at if oldest
             else candidate.expires_at > selected.expires_at)
        ):
            selected = candidate
    if selected is None:
        raise AirconError("未找到该空调的成功登录缓存，请从 Mac 微信 ihome 重新进入遥控页面。")
    return selected


class NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise AirconError("API 返回了重定向，本客户端没有跟随或转发授权信息。") from None


class AirconClient:
    def __init__(self, session: Session, *, direct: bool = False,
                 auto_refresh: bool = True):
        self.session = session
        self.auto_refresh = auto_refresh
        self._session_lock = threading.RLock()
        handlers = [NoRedirect()]
        if direct:
            handlers.append(request.ProxyHandler({}))
        self.opener = request.build_opener(*handlers)

    def _request(self, method: str, path: str, fields: dict,
                 *, login: bool = False) -> dict:
        url = BASE_URL + path
        encoded = parse.urlencode(fields).encode("utf-8")
        headers = {"Accept": "application/json", "Origin": "https://wx.hzxwwl.com",
                   "Referer": "https://wx.hzxwwl.com/prod/air/"}
        # The original interceptor optionally adds this normal-account token;
        # its necessity for the temporary-token endpoints is not yet verified.
        npd_token = os.environ.get("REA_AIR_NPD_TOKEN")
        if npd_token and not login:
            headers["REQ-NPD-TOKEN"] = npd_token
        if method == "GET":
            url += "?" + encoded.decode("utf-8")
            body = None
        else:
            body = encoded
            headers["Content-Type"] = "application/x-www-form-urlencoded;charset=UTF-8"
        req = request.Request(url, data=body, headers=headers, method=method)
        try:
            with self.opener.open(req, timeout=20) as response:
                raw = response.read(MAX_BYTES + 1)
            if len(raw) > MAX_BYTES:
                raise AirconError("API 响应超过本客户端的读取上限。")
            payload = json.loads(raw)
        except error.HTTPError as exc:
            # HTTPError's usual message contains the credential-bearing URL.
            raise AirconError(f"API 返回 HTTP {exc.code}。") from None
        except (error.URLError, TimeoutError, OSError):
            raise AirconError("连接 API 失败，请检查本机网络和代理设置。") from None
        except (ValueError, UnicodeError):
            raise AirconError("API 响应不是预期的 JSON。") from None
        if not isinstance(payload, dict) or payload.get("code") != "0000" or payload.get("success") is not True:
            code = str(payload.get("code", "unknown")) if isinstance(payload, dict) else "unknown"
            # Never echo arbitrary server text: it can contain credentials.
            if not re.fullmatch(r"[0-9]{1,6}", code):
                code = "unknown"
            raise AirconError(f"API 拒绝请求（code={code}），请检查授权是否仍有效。")
        data = payload.get("data")
        if isinstance(data, dict) and data.get("success") is False:
            raise AirconError("设备返回操作失败；网络请求成功不代表空调已执行。")
        return payload

    def refresh(self) -> None:
        """Try the unchanged, legitimately issued signed login once.

        Does not forge/recompute a signature or assume the original URL remains
        valid. The returned session exists only in this process.
        """
        with self._session_lock:
            payload = self._request("GET", LOGIN_PATH, self.session.signed_params, login=True)
            self.session = session_from_login(payload, self.session.asset_code,
                                              time.time(), self.session.signed_params)

    def call(self, endpoint: str, fields: dict | None = None, *, method="GET") -> dict:
        with self._session_lock:
            if self.auto_refresh and self.session.expires_at <= time.time() + 60:
                self.refresh()
            self.session.require_valid()
            path = f"iot/device/dynamic/control/{self.session.token}/{endpoint}"
            params = {**(fields or {}), "assetCode": self.session.asset_code}
            return self._request(method, path, params)["data"]

    def status(self) -> dict:
        result = self.call("getRunningStatusInfo")
        if not isinstance(result, dict) or result.get("success") is not True:
            raise AirconError("状态响应缺少预期的设备数据。")
        return result

    def profile(self) -> dict:
        result = self.call("getProfile")
        if not isinstance(result, dict) or result.get("success") is not True:
            raise AirconError("能力响应缺少预期的设备数据。")
        return result

    def command(self, endpoint: str, fields: dict) -> dict:
        state = self.status()
        if state.get("locked"):
            raise AirconError("空调报告已锁定，停止控制请求。")
        if state.get("online") is False:
            raise AirconError("空调报告离线，停止控制请求。")
        if endpoint == "temperature":
            mode = state.get("mode")
            prefix = {1: "auto", 2: "cool", 4: "heat"}.get(mode)
            if prefix is None:
                raise AirconError("当前模式没有观察到可调温度范围。")
            profile = self.profile()
            lower, upper = profile.get(prefix + "ModeTempMin"), profile.get(prefix + "ModeTempMax")
            if lower is None or upper is None or not lower <= fields["temperature"] <= upper:
                raise AirconError("温度超出设备能力接口返回的范围。")
        result = self.call(endpoint, fields, method="POST")
        if not isinstance(result, dict) or result.get("success") is not True:
            raise AirconError("控制响应缺少预期的设备应答。")
        return result


def public_status(data: dict) -> dict:
    fields = ("success", "run", "mode", "temperatureSet", "temperatureIn", "wind",
              "windLeftRight", "windUpDown", "locked", "online", "elecHeat", "eco",
              "screenDisplay", "powerSaving", "selfClean")
    return {field: data[field] for field in fields
            if isinstance(data.get(field), (bool, int, float))}


def public_profile(data: dict) -> dict:
    """Expose numeric temperature capabilities without device identifiers."""
    fields = ("success", "autoModeTempMin", "autoModeTempMax", "coolModeTempMin",
              "coolModeTempMax", "heatModeTempMin", "heatModeTempMax")
    return {field: data[field] for field in fields
            if isinstance(data.get(field), (bool, int, float))}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--asset-code", required=True, help="只操作明确指定的空调资产")
    parser.add_argument("--cache-root", type=Path, help="覆盖 Mac 微信网页 profiles 目录")
    parser.add_argument("--refresh", action="store_true", help="尝试原始签名登录一次，不生成新签名")
    parser.add_argument("--no-auto-refresh", action="store_true", help="关闭到期前的自动重新授权")
    parser.add_argument("--direct", action="store_true", help="该进程直接联网，不使用环境变量中的代理")
    sub = parser.add_subparsers(dest="action", required=True)
    for name in ("cache-info", "status", "profile"):
        sub.add_parser(name)
    sub.add_parser("check-refresh").add_argument(
        "--oldest", action="store_true", help="验证最早缓存的合法签名，而非最新签名")
    sub.add_parser("set-temp").add_argument("temperature", type=int)
    sub.add_parser("set-mode").add_argument("mode", choices=MODES)
    sub.add_parser("set-wind").add_argument("wind", choices=WINDS)
    sub.add_parser("power").add_argument("state", choices=("on", "off"))
    args = parser.parse_args()
    try:
        session = discover_session(args.asset_code, args.cache_root,
                                   oldest=getattr(args, "oldest", False))
        if args.action == "cache-info":
            if args.refresh:
                raise AirconError("cache-info 只读取本地缓存，不能与 --refresh 同时使用。")
            result = {"assetCode": session.asset_code,
                      "expiresAtUTC": datetime.fromtimestamp(session.expires_at, timezone.utc).isoformat(),
                      "expired": session.expires_at <= time.time() + 10,
                      "source": "local-wechat-login-cache", "token": "[redacted]"}
        else:
            client = AirconClient(session, direct=args.direct,
                                  auto_refresh=not args.no_auto_refresh)
            original_expiry = session.expires_at
            original_token = session.token
            original_was_expired = original_expiry <= time.time()
            if args.refresh or args.action == "check-refresh":
                client.refresh()
            if args.action == "check-refresh":
                result = {
                    "originalExpiresAtUTC": datetime.fromtimestamp(original_expiry, timezone.utc).isoformat(),
                    "refreshedExpiresAtUTC": datetime.fromtimestamp(client.session.expires_at, timezone.utc).isoformat(),
                    "expiryExtensionSeconds": round(client.session.expires_at - original_expiry, 1),
                    "remainingSeconds": max(0, int(client.session.expires_at - time.time())),
                    "tokenChanged": client.session.token != original_token,
                    "originalSessionAlreadyExpired": original_was_expired,
                    "verifiedStatus": public_status(client.status()),
                    "note": "登录成功或令牌改变不代表续期；到期时间需明显延后。数秒差异可能来自时钟和传输误差。",
                }
            elif args.action == "status":
                result = public_status(client.status())
            elif args.action == "profile":
                result = public_profile(client.profile())
            else:
                operations = {"set-temp": ("temperature", {"temperature": getattr(args, "temperature", None)}),
                              "set-mode": ("model", {"model": MODES.get(getattr(args, "mode", ""))}),
                              "set-wind": ("windSpeed", {"windSpeed": WINDS.get(getattr(args, "wind", ""))}),
                              "power": ("switching", {"run": int(getattr(args, "state", "") == "on")})}
                endpoint, fields = operations[args.action]
                ack = client.command(endpoint, fields)
                result = {"commandAcknowledgement": public_status(ack),
                          "observedStatus": public_status(client.status()),
                          "note": "即时查询结果可能尚未反映异步设备执行；仍需观察实际空调。"}
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except AirconError as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
