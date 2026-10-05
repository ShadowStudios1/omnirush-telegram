import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import urllib.error

from telegram_bridge import account


class Response:
    def __init__(self, status, payload):
        self.status = status
        self.payload = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def getcode(self):
        return self.status

    def read(self, _limit):
        return self.payload


class AccountTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "native" / "omnirush-account.json"
        self.ui = Mock()

    def tearDown(self):
        self.temporary.cleanup()

    def test_device_authorize_and_pending_poll_store_only_after_success(self):
        authorize = {"device_code": "device-secret", "user_code": "ABCD-EFGH",
                     "verification_uri_complete": "https://omnirush.ai/verify?user_code=ABCD-EFGH",
                     "interval": 2, "expires_in": 60}
        token = {"gateway_url": "https://omnirush.ai/omnirush/v1",
                 "access_token": "access-secret", "refresh_token": "refresh-secret"}
        requests = []

        class Opener:
            def open(self, request, timeout):
                requests.append((request.full_url, json.loads(request.data), timeout,
                                 dict(request.header_items()).get("X-omnirush-client")))
                if len(requests) == 1:
                    return Response(200, authorize)
                if len(requests) == 2:
                    raise urllib.error.HTTPError(request.full_url, 428, "pending", {}, io.BytesIO(b"{}"))
                return Response(200, token)

        with patch.object(account, "ACCOUNT_PATH", self.path), \
             patch.object(account.urllib.request, "build_opener", return_value=Opener()), \
             patch.object(account.webbrowser, "open") as browser, \
             patch.object(account.time, "monotonic", return_value=0), \
             patch.object(account.time, "sleep") as sleep:
            result = account.login(self.ui)

        self.assertEqual(requests[0][0], "https://omnirush.ai/omnirush/device/authorize")
        self.assertEqual(requests[0][1], {"device_name": "omnirush-telegram-portable", "platform": "linux"})
        self.assertEqual(requests[1][0], "https://omnirush.ai/omnirush/device/token")
        self.assertEqual(requests[1][1], {"device_code": "device-secret"})
        self.assertEqual(requests[0][2], account.HTTP_TIMEOUT)
        self.assertEqual(requests[0][3], account.CLIENT_VALUE)
        browser.assert_called_once_with(authorize["verification_uri_complete"])
        sleep.assert_called_once_with(2.0)
        self.assertEqual(result, token)
        self.assertNotIn("device-secret", " ".join(str(call) for call in self.ui.say.call_args_list))
        self.assertNotIn("access-secret", " ".join(str(call) for call in self.ui.say.call_args_list))
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        with patch.object(account, "ACCOUNT_PATH", self.path):
            self.assertEqual(account.load_credentials(), token)

    def test_failed_replacement_preserves_old_credentials(self):
        old = {"gateway_url": "https://omnirush.ai/omnirush/v1",
               "access_token": "old-access", "refresh_token": "old-refresh"}
        new = {"gateway_url": "https://omnirush.ai/omnirush/v1",
               "access_token": "new-access", "refresh_token": "new-refresh"}
        with patch.object(account, "ACCOUNT_PATH", self.path):
            account._store_credentials(old)
            with patch.object(account, "_request", side_effect=[
                (200, {"device_code": "device", "user_code": "CODE",
                       "verification_uri_complete": "https://omnirush.ai/verify",
                       "interval": 1, "expires_in": 60}), (200, new)]), \
                 patch.object(account.webbrowser, "open"), \
                 patch.object(account.os, "replace", side_effect=OSError):
                with self.assertRaisesRegex(account.AccountError, "old credentials were preserved"):
                    account.login(self.ui)
            self.assertEqual(account.load_credentials(), old)

    def test_authenticated_environment_requires_account_and_strips_inherited_overrides(self):
        credentials = {"gateway_url": "https://omnirush.ai/omnirush/v1",
                       "access_token": "access", "refresh_token": "refresh"}
        with patch.object(account, "load_credentials", return_value=credentials):
            environment = account.authenticated_environment({
                "PATH": "/usr/bin", "OMNIRUSH_GATEWAY_URL": "https://wrong",
                "OMNIRUSH_ACCESS_TOKEN": "wrong", "XDG_CONFIG_HOME": "/wrong",
            })
        self.assertEqual(environment, {"PATH": "/usr/bin",
                                       "OMNIRUSH_GATEWAY_URL": credentials["gateway_url"],
                                       "OMNIRUSH_ACCESS_TOKEN": "access"})
        with patch.object(account, "load_credentials", return_value=None):
            with self.assertRaisesRegex(account.AccountError, "not signed in"):
                account.authenticated_environment({})

    def test_invalid_urls_and_cross_host_redirect_are_rejected(self):
        for value in ("http://omnirush.ai/omnirush/v1", "https://user:secret@omnirush.ai/v1",
                      "https://omnirush.ai/v1?token=bad", "https://omnirush.ai/v1#bad",
                      "https://omnirush.ai/../v1", "https://omnirush.ai:bad/v1"):
            with self.subTest(value=value), self.assertRaises(account.AccountError):
                account._url(value)
        handler = account._ApprovedRedirectHandler({"omnirush.ai"})
        with self.assertRaises(account.AccountError):
            handler.redirect_request(Mock(), None, 302, "redirect", {}, "https://attacker.example/device/token")

    def test_storage_refuses_symlinks_unowned_or_readable_files(self):
        with patch.object(account, "ACCOUNT_PATH", self.path):
            data = {"gateway_url": account.DEFAULT_GATEWAY_URL,
                    "access_token": "access", "refresh_token": "refresh"}
            account._store_credentials(data)
            self.path.chmod(0o644)
            with self.assertRaises(account.AccountError):
                account.load_credentials()
            self.path.chmod(0o600)
            with patch.object(account.os, "getuid", return_value=-1), self.assertRaises(account.AccountError):
                account.load_credentials()
            real = self.path.with_name("real.json")
            self.path.rename(real)
            self.path.symlink_to(real)
            with self.assertRaises(account.AccountError):
                account._store_credentials(data)
            with self.assertRaises(account.AccountError):
                account.load_credentials()
            self.assertEqual(json.loads(real.read_text()), data)

    def test_preexisting_temporary_file_is_not_removed(self):
        self.path.parent.mkdir(mode=0o700)
        temporary = self.path.with_name(self.path.name + ".new")
        temporary.write_text("unrelated")
        with patch.object(account, "ACCOUNT_PATH", self.path), self.assertRaises(account.AccountError):
            account._store_credentials({"gateway_url": account.DEFAULT_GATEWAY_URL,
                                        "access_token": "access", "refresh_token": "refresh"})
        self.assertEqual(temporary.read_text(), "unrelated")

    def test_timeout_and_malformed_success_preserve_old_credentials(self):
        authorization = {"device_code": "device-secret", "user_code": "CODE",
                         "verification_uri_complete": "https://omnirush.ai/verify",
                         "interval": 2, "expires_in": 3}
        old = {"gateway_url": account.DEFAULT_GATEWAY_URL,
               "access_token": "old-access", "refresh_token": "old-refresh"}
        with patch.object(account, "ACCOUNT_PATH", self.path):
            account._store_credentials(old)
            with patch.object(account, "_request", return_value=(200, authorization)) as request, \
                 patch.object(account.webbrowser, "open", side_effect=OSError), \
                 patch.object(account.time, "monotonic", side_effect=[0, 3]):
                with self.assertRaisesRegex(account.AccountError, "timed out"):
                    account.login(self.ui)
                self.assertEqual(request.call_count, 1)
            with patch.object(account, "_request", side_effect=[(200, authorization), (200, {"access_token": "secret"})]), \
                 patch.object(account.webbrowser, "open"), patch.object(account.time, "monotonic", return_value=0):
                with self.assertRaises(account.AccountError):
                    account.login(self.ui)
            self.assertEqual(account.load_credentials(), old)

    def test_request_size_cap_and_safe_network_error(self):
        opener = Mock()
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.getcode.return_value = 200
        response.read.return_value = b"x" * (account.MAX_RESPONSE_BYTES + 1)
        opener.open.return_value = response
        with patch.object(account.urllib.request, "build_opener", return_value=opener):
            with self.assertRaisesRegex(account.AccountError, "too large"):
                account._request("https://omnirush.ai/device/token", {}, {"omnirush.ai"})
            opener.open.side_effect = urllib.error.URLError("PRIVATE-RESPONSE")
            with self.assertRaises(account.AccountError) as error:
                account._request("https://omnirush.ai/device/token", {}, {"omnirush.ai"})
            self.assertNotIn("PRIVATE-RESPONSE", str(error.exception))


if __name__ == "__main__":
    unittest.main()
