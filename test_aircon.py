import gzip
import io
import json
from pathlib import Path
import struct
import tempfile
import time
import traceback
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError

from aircon import AirconClient, AirconError, NoRedirect, Session, main, public_status, read_cached_login


ASSET = "TEST-ASSET"
TOKEN = "a" * 32


def reply(data, *, success=True, code="0000"):
    return io.BytesIO(json.dumps({"success": success, "code": code, "data": data}).encode())


class ProtocolTests(unittest.TestCase):
    def client(self):
        client = AirconClient(Session(ASSET, TOKEN, time.time() + 600, {}))
        client.opener = Mock()
        return client

    def test_status_matches_observed_dynamic_get(self):
        client = self.client()
        client.opener.open.return_value = reply({"success": True, "temperatureSet": 24})
        self.assertEqual(client.status()["temperatureSet"], 24)
        req = client.opener.open.call_args.args[0]
        self.assertEqual(req.get_method(), "GET")
        self.assertEqual(req.full_url,
                         f"https://gateway.hzxwwl.com/api/iot/device/dynamic/control/{TOKEN}/getRunningStatusInfo?assetCode={ASSET}")

    def test_temperature_uses_profile_limits_and_form_post(self):
        client = self.client()
        client.opener.open.side_effect = [
            reply({"success": True, "locked": False, "online": True, "mode": 2}),
            reply({"success": True, "coolModeTempMin": 16, "coolModeTempMax": 30}),
            reply({"success": True}),
        ]
        client.command("temperature", {"temperature": 25})
        req = client.opener.open.call_args.args[0]
        self.assertEqual(req.get_method(), "POST")
        self.assertTrue(req.full_url.endswith("/temperature"))
        from urllib.parse import parse_qs
        self.assertEqual(parse_qs(req.data.decode()),
                         {"assetCode": [ASSET], "temperature": ["25"]})
        self.assertEqual(req.get_header("Content-type"),
                         "application/x-www-form-urlencoded;charset=UTF-8")

    def test_locked_device_and_out_of_range_never_send_control(self):
        client = self.client()
        client.opener.open.return_value = reply({"success": True, "locked": True})
        with self.assertRaises(AirconError):
            client.command("switching", {"run": 1})
        self.assertEqual(client.opener.open.call_count, 1)
        client = self.client()
        client.opener.open.side_effect = [
            reply({"success": True, "locked": False, "online": True, "mode": 2}),
            reply({"success": True, "coolModeTempMin": 16, "coolModeTempMax": 30}),
        ]
        with self.assertRaises(AirconError):
            client.command("temperature", {"temperature": 31})
        self.assertEqual(client.opener.open.call_count, 2)

    def test_expired_session_and_device_error_are_not_success(self):
        client = self.client()
        client.session.expires_at = time.time() - 1
        client.auto_refresh = False
        with self.assertRaises(AirconError):
            client.status()
        client.opener.open.assert_not_called()
        client = self.client()
        client.opener.open.return_value = reply({"success": False, "errorMsg": "offline"})
        with self.assertRaises(AirconError):
            client.status()

    def test_refresh_preserves_all_original_signed_fields(self):
        client = self.client()
        fields = {"xm": "name", "xh": "student", "qsId": "room",
                  "timestamp": "1792039805471", "platformType": "platform", "sign": "issued-sign"}
        client.session.signed_params = fields
        client.opener.open.return_value = reply({"assetCode": ASSET, "isIot": True,
                                                 "tmpAuthToken": "b" * 32, "expireTime": 895})
        client.refresh()
        req = client.opener.open.call_args.args[0]
        from urllib.parse import urlsplit, parse_qs
        self.assertEqual(parse_qs(urlsplit(req.full_url).query),
                         {k: [v] for k, v in fields.items()})
        self.assertEqual(client.session.token, "b" * 32)

    def test_expired_authorization_is_renewed_before_query(self):
        client = self.client()
        client.session.expires_at = time.time() - 1
        client.opener.open.side_effect = [
            reply({"assetCode": ASSET, "isIot": True,
                   "tmpAuthToken": "b" * 32, "expireTime": 895}),
            reply({"success": True, "temperatureSet": 23}),
        ]
        self.assertEqual(client.status()["temperatureSet"], 23)
        calls = client.opener.open.call_args_list
        self.assertEqual(len(calls), 2)
        self.assertIn("/asset/ac/tourist/auth/login?", calls[0].args[0].full_url)
        self.assertIn("/" + "b" * 32 + "/getRunningStatusInfo", calls[1].args[0].full_url)

    def test_failed_renewal_never_sends_device_request(self):
        client = self.client()
        client.session.expires_at = time.time() - 1
        client.opener.open.return_value = reply(None, success=False, code="5001")
        with self.assertRaises(AirconError):
            client.status()
        self.assertEqual(client.opener.open.call_count, 1)
        self.assertIn("/asset/ac/tourist/auth/login?",
                      client.opener.open.call_args.args[0].full_url)


class CacheTests(unittest.TestCase):
    def test_cache_uses_server_date_and_selected_asset(self):
        key = ("https://gateway.hzxwwl.com/api/asset/ac/tourist/auth/login?"
               "xm=name&xh=student&qsId=room&timestamp=9999999999999&platformType=p&sign=s").encode()
        body = json.dumps({"success": True, "code": "0000", "data": {
            "assetCode": ASSET, "isIot": True, "tmpAuthToken": TOKEN, "expireTime": 895}}).encode()
        header = struct.pack("<QIII", 0xFCFB6D1BA7725C30, 5, len(key), 0) + b"\0" * 4
        cache = header + key + gzip.compress(body) + b"\0Date: Wed, 14 Oct 2026 20:50:06 GMT\0"
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "entry_0"
            path.write_bytes(cache)
            session = read_cached_login(path, ASSET)
            self.assertIsNotNone(session)
            self.assertEqual(session.token, TOKEN)
            from datetime import datetime, timezone
            self.assertEqual(datetime.fromtimestamp(session.expires_at, timezone.utc).isoformat(),
                             "2026-10-14T21:05:01+00:00")
            self.assertIsNone(read_cached_login(path, "ANOTHER-ASSET"))
            path.write_bytes(cache[:-1].replace(b"Date:", b"xxxx:"))
            self.assertIsNone(read_cached_login(path, ASSET))


class PrivacyTests(unittest.TestCase):
    def test_profile_cli_filters_identifiers_and_nested_unknown_data(self):
        session = Session(ASSET, TOKEN, time.time() + 600, {})
        with patch("sys.argv", ["aircon.py", "--asset-code", ASSET, "profile"]), \
                patch("aircon.discover_session", return_value=session), \
                patch("aircon.AirconClient.profile", return_value={
                    "success": True, "coolModeTempMin": 16, "coolModeTempMax": 30,
                    "assetCode": ASSET, "tmpAuthToken": TOKEN,
                    "owner": {"xh": "private-student"}, "heatModeTempMin": {"token": TOKEN},
                }), patch("sys.stdout", new_callable=io.StringIO) as output:
            self.assertEqual(main(), 0)
            self.assertEqual(json.loads(output.getvalue()), {
                "success": True, "coolModeTempMin": 16, "coolModeTempMax": 30,
            })

    def test_status_filters_unknown_fields_and_non_numeric_values(self):
        self.assertEqual(public_status({"success": True, "temperatureSet": 23,
                                        "tmpAuthToken": TOKEN, "assetCode": ASSET,
                                        "wind": {"secret": TOKEN}}),
                         {"success": True, "temperatureSet": 23})

    def test_http_error_and_server_code_do_not_echo_credentials(self):
        client = ProtocolTests().client()
        url = f"https://gateway.hzxwwl.com/api/private/{TOKEN}?sign=private-sign"
        client.opener.open.side_effect = HTTPError(url, 403, "private-sign", {}, None)
        try:
            client.status()
        except AirconError as exc:
            rendered = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
            self.assertNotIn(TOKEN, rendered)
            self.assertNotIn("private-sign", rendered)
            self.assertEqual(str(exc), "API 返回 HTTP 403。")
        else:
            self.fail("HTTP failure must raise AirconError")
        client = ProtocolTests().client()
        client.opener.open.return_value = reply(None, success=False, code=TOKEN)
        with self.assertRaises(AirconError) as raised:
            client.status()
        self.assertNotIn(TOKEN, str(raised.exception))

    def test_redirect_is_blocked_without_echoing_target(self):
        with self.assertRaises(AirconError) as raised:
            NoRedirect().redirect_request(None, None, 302, "", {},
                                          f"https://example.invalid/{TOKEN}")
        self.assertNotIn(TOKEN, str(raised.exception))

    def test_refresh_for_another_asset_does_not_replace_session(self):
        client = ProtocolTests().client()
        original = client.session
        client.opener.open.return_value = reply({"assetCode": "ANOTHER-ASSET", "isIot": True,
                                                 "tmpAuthToken": "b" * 32, "expireTime": 895})
        with self.assertRaises(AirconError):
            client.refresh()
        self.assertIs(client.session, original)

    def test_control_acknowledgement_must_be_structured_success(self):
        client = ProtocolTests().client()
        client.opener.open.side_effect = [
            reply({"success": True, "online": True, "locked": False}),
            reply({"tmpAuthToken": TOKEN}),
        ]
        with self.assertRaises(AirconError):
            client.command("switching", {"run": 1})


if __name__ == "__main__":
    unittest.main()
