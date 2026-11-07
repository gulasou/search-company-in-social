import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.request import Request

from login import BilibiliLogin, usable_cookies


def session_cookie(**overrides):
    return {"name": "SESSDATA", "value": "unit-test-session", "domain": ".bilibili.com",
            "path": "/", "secure": True, "expires": time.time() + 3600, **overrides}


class LoginTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "session.bin"
        self.login = BilibiliLogin(self.path)

    def tearDown(self):
        self.directory.cleanup()

    @unittest.skipUnless(os.name == "nt", "Windows DPAPI")
    def test_encrypted_session_roundtrip_and_restart(self):
        self.login.save([session_cookie()], "测试账号", threading.Event())
        raw = self.path.read_bytes()
        self.assertNotIn(b"unit-test-session", raw)
        restored = BilibiliLogin(self.path)
        self.assertEqual(restored.status()["state"], "saved")
        self.assertEqual(restored.status()["username"], "测试账号")
        self.assertEqual(list(restored.cookie_jar())[0].value, "unit-test-session")
        self.assertNotIn("unit-test-session", json.dumps(restored.status()))

    @unittest.skipUnless(os.name == "nt", "Windows DPAPI")
    def test_corrupt_encrypted_state_is_recoverable(self):
        self.path.write_bytes(b"corrupt-session")
        restored = BilibiliLogin(self.path)
        self.assertEqual(restored.status()["state"], "error")
        self.assertFalse(restored.status()["has_session"])

    def test_filters_expired_and_unrelated_cookies(self):
        cookies = [session_cookie(), session_cookie(expires=time.time() - 5),
                   session_cookie(domain="bilibili.com.evil.example")]
        self.assertEqual(len(usable_cookies(cookies)), 1)
        self.login.cookies = usable_cookies(cookies)
        api = Request("https://api.bilibili.com/x/web-interface/nav")
        evil = Request("https://evil.example/")
        jar = self.login.cookie_jar()
        jar.add_cookie_header(api)
        jar.add_cookie_header(evil)
        self.assertIn("unit-test-session", api.get_header("Cookie"))
        self.assertIsNone(evil.get_header("Cookie"))

    def test_cancelled_worker_cannot_restore_credentials(self):
        stop = threading.Event()
        stop.set()
        self.login.save([session_cookie()], "测试账号", stop)
        self.assertFalse(self.path.exists())
        self.assertFalse(self.login.status()["has_session"])

    def test_clear_removes_disk_memory_and_environment_credentials(self):
        self.path.write_bytes(b"placeholder")
        self.login.cookies = [session_cookie()]
        with patch.dict(os.environ, {"BILIBILI_COOKIE": "manual-test-session"}):
            result = self.login.clear()
            self.assertNotIn("BILIBILI_COOKIE", os.environ)
        self.assertFalse(self.path.exists())
        self.assertFalse(result["has_session"])
        self.assertEqual(result["state"], "logged_out")

    def test_expired_saved_session_is_not_shown_as_logged_in(self):
        self.login.state = "authenticated"
        self.login.cookies = [session_cookie(expires=time.time() - 10)]
        self.assertEqual(self.login.status()["state"], "logged_out")

    def test_new_login_does_not_accept_cookie_without_server_confirmation(self):
        with patch("playwright.sync_api.sync_playwright") as factory, patch.object(self.login, "save") as save:
            page = factory.return_value.__enter__.return_value.chromium.launch.return_value.new_page.return_value
            page.is_closed.return_value = False
            page.context.cookies.return_value = [session_cookie()]
            page.context.request.get.return_value.ok = True
            page.context.request.get.return_value.json.return_value = {"code": -101, "data": {"isLogin": False}}
            self.login.run(threading.Event())
            save.assert_not_called()
        self.assertEqual(self.login.status()["state"], "error")

    def test_confirmed_login_saves_cookies_without_exposing_them(self):
        with patch("playwright.sync_api.sync_playwright") as factory, patch("login.protect", return_value=b"encrypted"):
            page = factory.return_value.__enter__.return_value.chromium.launch.return_value.new_page.return_value
            page.is_closed.return_value = False
            page.context.cookies.return_value = [session_cookie()]
            page.context.request.get.return_value.ok = True
            page.context.request.get.return_value.json.return_value = {"code": 0, "data": {"isLogin": True, "uname": "测试账号"}}
            self.login.run(threading.Event())
        self.assertEqual(self.login.status()["state"], "authenticated")
        self.assertNotIn("unit-test-session", json.dumps(self.login.status()))

    def test_close_window_is_recoverable(self):
        with patch("playwright.sync_api.sync_playwright") as factory:
            page = factory.return_value.__enter__.return_value.chromium.launch.return_value.new_page.return_value
            page.is_closed.return_value = True
            self.login.run(threading.Event())
        self.assertEqual(self.login.status()["state"], "error")
        self.assertFalse(self.login.status()["has_session"])

    def test_existing_login_task_is_not_started_twice(self):
        self.login.thread = MagicMock()
        self.login.thread.is_alive.return_value = True
        with patch("login.threading.Thread") as thread:
            self.login.start()
            thread.assert_not_called()

    def test_search_client_automatically_uses_saved_session(self):
        from app import Bilibili
        self.login.cookies = [session_cookie()]
        with patch("app.LOGIN", self.login), patch.dict(os.environ, {"BILIBILI_COOKIE": "obsolete-session"}):
            client = Bilibili()
        request = Request("https://api.bilibili.com/x/web-interface/nav")
        client.opener.addheaders = []
        for handler in client.opener.handlers:
            if hasattr(handler, "cookiejar"):
                handler.cookiejar.add_cookie_header(request)
        self.assertIn("unit-test-session", request.get_header("Cookie"))
        self.assertEqual(client.cookie, "")


if __name__ == "__main__":
    unittest.main()
