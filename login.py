"""B站官网扫码登录；凭据通过 Windows DPAPI 在本机加密保存。"""

import ctypes
import json
import os
import threading
import time
from ctypes import wintypes
from http.cookiejar import Cookie, CookieJar
from pathlib import Path


class LoginError(Exception):
    pass


def protect(data, decrypt=False):
    if os.name != "nt":
        raise LoginError("当前登录态存储仅支持 Windows。")

    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]

    buffer = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    result = Blob()
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    function = crypt32.CryptUnprotectData if decrypt else crypt32.CryptProtectData
    function.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                         ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    function.restype = wintypes.BOOL
    if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(result)):
        raise LoginError("无法使用当前 Windows 用户读写登录态，请在本机重新登录。")
    try:
        return ctypes.string_at(result.data, result.size)
    finally:
        free = ctypes.WinDLL("kernel32").LocalFree
        free.argtypes = [ctypes.c_void_p]
        free.restype = ctypes.c_void_p
        free(result.data)


def usable_cookies(cookies):
    if not isinstance(cookies, list):
        raise LoginError("保存的登录态格式无效，请重新登录。")
    result = []
    for cookie in cookies:
        if not isinstance(cookie, dict):
            raise LoginError("保存的登录态格式无效，请重新登录。")
        domain = cookie.get("domain", "").lstrip(".")
        if domain != "bilibili.com" and not domain.endswith(".bilibili.com"):
            continue
        if not all(isinstance(cookie.get(key), str) for key in ("name", "value", "path")):
            raise LoginError("保存的登录态格式无效，请重新登录。")
        if any(ord(char) < 32 for char in cookie["name"] + cookie["value"]):
            raise LoginError("保存的登录态格式无效，请重新登录。")
        expires = cookie.get("expires", -1)
        if not isinstance(expires, (int, float)):
            raise LoginError("保存的登录态格式无效，请重新登录。")
        if expires > 0 and expires <= time.time():
            continue
        result.append(cookie.copy())
    return result


class BilibiliLogin:
    def __init__(self, session_path):
        self.path = Path(session_path)
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.thread = None
        self.cookies = []
        self.username = ""
        self.state = "logged_out"
        self.message = "未登录；登录后可使用自己的账号访问公开内容。"
        try:
            if self.path.exists():
                saved = json.loads(protect(self.path.read_bytes(), decrypt=True))
                self.cookies = usable_cookies(saved["cookies"])
                self.username = str(saved.get("username", ""))
                self.restore_status()
        except (LoginError, OSError, ValueError, KeyError, TypeError, AttributeError):
            self.state = "error"
            self.message = "无法读取本机登录态，请重新登录或清除后重试。"

    def restore_status(self):
        self.cookies = usable_cookies(self.cookies)
        if any(cookie["name"] == "SESSDATA" and cookie["value"] for cookie in self.cookies):
            self.state = "saved"
            self.message = "已保存本机登录态，搜索时会自动使用；如已失效，请重新登录。"
        else:
            self.cookies = []
            self.username = ""
            self.state = "logged_out"
            self.message = "未登录；请在 B站官网扫码登录。"

    def status(self):
        with self.lock:
            if self.state in {"saved", "authenticated"}:
                current = usable_cookies(self.cookies)
                if not any(cookie["name"] == "SESSDATA" and cookie["value"] for cookie in current):
                    self.restore_status()
            return {"state": self.state, "message": self.message, "username": self.username,
                    "has_session": bool(self.cookies)}

    def cookie_jar(self):
        with self.lock:
            jar = CookieJar()
            for item in usable_cookies(self.cookies):
                expires = item.get("expires", -1)
                domain = item["domain"]
                jar.set_cookie(Cookie(0, item["name"], item["value"], None, False,
                                      domain, domain.startswith("."), domain.startswith("."),
                                      item["path"], True, item.get("secure", True),
                                      int(expires) if expires > 0 else None, expires <= 0,
                                      None, None, {}, False))
            return jar

    def start(self):
        with self.lock:
            if self.thread and self.thread.is_alive():
                return self.status()
            self.stop = threading.Event()
            self.state, self.message = "starting", "正在打开 B站官方登录窗口…"
            self.thread = threading.Thread(target=self.run, args=(self.stop,), daemon=True)
            self.thread.start()
            return self.status()

    def cancel(self):
        with self.lock:
            self.stop.set()
            if self.thread and self.thread.is_alive():
                self.state, self.message = "cancelling", "正在关闭登录窗口…"
            else:
                self.restore_status()
            return self.status()

    def clear(self):
        with self.lock:
            try:
                self.path.unlink(missing_ok=True)
            except OSError:
                raise LoginError("无法删除本机登录态文件，请检查文件权限后重试。") from None
            self.cookies, self.username = [], ""
            os.environ.pop("BILIBILI_COOKIE", None)
            return self.cancel()

    def save(self, cookies, username, stop):
        cookies = usable_cookies(cookies)
        if not any(cookie["name"] == "SESSDATA" and cookie["value"] for cookie in cookies):
            raise LoginError("B站未返回有效会话，请重新扫码。")
        with self.lock:
            if stop.is_set():
                return
            encrypted = protect(json.dumps({"cookies": cookies, "username": username}).encode("utf-8"))
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(".tmp")
            try:
                temporary.write_bytes(encrypted)
                temporary.replace(self.path)
            finally:
                temporary.unlink(missing_ok=True)
            self.cookies, self.username = cookies, username
            self.state, self.message = "authenticated", "登录成功，搜索时将自动使用当前账号。"

    def run(self, stop):
        try:
            try:
                from playwright.sync_api import sync_playwright
            except ImportError:
                raise LoginError("请先按使用说明安装依赖，并使用 .venv 中的 Python 启动服务。") from None
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(channel="msedge", headless=False)
                try:
                    page = browser.new_page()
                    page.goto("https://passport.bilibili.com/login", wait_until="domcontentloaded", timeout=30000)
                    with self.lock:
                        if not stop.is_set():
                            self.state, self.message = "waiting", "请在打开的 B站官网窗口，用 B站 App 扫码并确认登录。"
                    deadline = time.monotonic() + 180
                    while not stop.is_set() and time.monotonic() < deadline:
                        if page.is_closed():
                            raise LoginError("登录窗口已关闭，可点击登录重新打开。")
                        cookies = usable_cookies(page.context.cookies())
                        if any(cookie["name"] == "SESSDATA" and cookie["value"] for cookie in cookies):
                            response = page.context.request.get("https://api.bilibili.com/x/web-interface/nav", timeout=12000)
                            if not response.ok:
                                raise LoginError("B站登录校验未通过，请稍后重新登录。")
                            body = response.json()
                            data = body.get("data") or {}
                            if body.get("code") == 0 and data.get("isLogin") is True:
                                self.save(page.context.cookies(), str(data.get("uname") or "B站用户"), stop)
                                return
                            raise LoginError("B站未确认登录成功，请重新扫码。")
                        stop.wait(2)
                    if not stop.is_set():
                        raise LoginError("等待扫码超过 3 分钟，请重新登录。")
                finally:
                    browser.close()
        except Exception as exc:
            with self.lock:
                if not stop.is_set():
                    self.state = "error"
                    self.message = str(exc) if isinstance(exc, LoginError) else "登录未完成，请确认已安装 Edge、网络正常，且使用普通本机终端启动服务。"
        finally:
            with self.lock:
                if stop.is_set():
                    self.restore_status()
