"""真实 Edge + 构造平台响应，验证抖音采集及页面；不访问抖音。"""

import json
import sys
import tempfile
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, quote, unquote, urlsplit

from playwright.sync_api import BrowserType, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import Handler, collect


AWEME = "7420000000000000123"
COMMENT = "7420000000000000456"
original_persistent = BrowserType.launch_persistent_context
requests = []
restored_sessions = []


def fixture_context(self, user_data_dir, **kwargs):
    kwargs["headless"] = True
    context = original_persistent(self, user_data_dir, **kwargs)
    page = context.pages[0]
    page.goto("edge://version")
    if "--no-sandbox" in page.locator("#command_line").inner_text().split():
        context.close()
        raise AssertionError("The app must launch Edge with Chromium sandboxing enabled")
    page.goto("about:blank")

    def platform(route):
        address = urlsplit(route.request.url)
        requests.append(address.path)
        if address.path.startswith("/search/"):
            keyword = unquote(address.path.removeprefix("/search/"))
            api = "/aweme/v1/web/search/item/?keyword=" + quote(keyword)
            saved = "fixture-session=persisted" in (route.request.header_value("cookie") or "")
            restored_sessions.append(saved)
        elif address.path.startswith("/video/"):
            api = "https://www-hj.douyin.com/aweme/v1/web/comment/list/?aweme_id=" + AWEME + "&cursor=0"
        elif address.path == "/aweme/v1/web/search/item/":
            if "fixture-session=persisted" not in (route.request.header_value("cookie") or ""):
                route.fulfill(status=401, json={"status_code": 8})
                return
            keyword = parse_qs(address.query)["keyword"][0]
            body = {"status_code": 0, "data": [{"aweme_info": {
                "aweme_id": AWEME, "desc": keyword, "author": {"nickname": "测试作者"},
                "create_time": 1700000000, "statistics": {}}}]}
            route.fulfill(json=body)
            return
        elif address.path == "/aweme/v1/web/comment/list/":
            route.fulfill(headers={"Access-Control-Allow-Origin": "https://www.douyin.com"},
                          json={"status_code": 0, "comments": [{"cid": COMMENT,
                "text": '<img src=x onerror="window.__xss=true"> 原始评论', "digg_count": 3}]})
            return
        else:
            route.abort()
            return
        if address.path.startswith("/search/"):
            if saved:
                body = (f'<title>抖音测试</title><script>if (localStorage.getItem("fixture-login") === "yes") '
                        f'fetch({json.dumps(api)});</script>')
            else:
                # 模拟用户确认登录，期间不得切页或消费登录前的错误响应。
                body = ('<title>安全验证中间页</title><div id="login-panel-new"><button id="verify">模拟登录完成</button></div><script>'
                        f'fetch({json.dumps(api)}); document.getElementById("verify").onclick = () => {{'
                        'document.cookie = "fixture-session=persisted; Max-Age=3600; Path=/; Secure; SameSite=Lax";'
                        'localStorage.setItem("fixture-login", "yes"); document.title = "抖音测试";'
                        f'document.getElementById("login-panel-new").remove(); setTimeout(() => fetch({json.dumps(api)}), 300);}};'
                        'setTimeout(() => document.getElementById("verify").click(), 500);</script>')
        else:
            body = f"<title>抖音测试</title><script>fetch({json.dumps(api)})</script>"
        route.fulfill(content_type="text/html", body=body)

    context.route("**/*", platform)
    return context


with tempfile.TemporaryDirectory() as directory, patch("app.ROOT", Path(directory)), patch.object(BrowserType, "launch_persistent_context", fixture_context):
    result = collect("示例公司", "interview", platform="douyin")
    reopened = collect("示例公司", "interview", platform="douyin")
    assert reopened["videos"][0]["aid"] == AWEME
assert restored_sessions == [False, True], restored_sessions
assert result["videos"][0]["aid"] == AWEME
assert result["videos"][0]["comments"][0]["id"] == COMMENT
assert len(requests) == 9, requests
print("Real Edge: no --no-sandbox flag, login waiting, delayed response after login, cookies/localStorage restored after browser restart, comments and long IDs passed with fixtures.")

server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
thread = threading.Thread(target=server.serve_forever, daemon=True)
thread.start()
try:
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="msedge", headless=True, chromium_sandbox=True)
        try:
            page = browser.new_page(viewport={"width": 1365, "height": 1000})
            errors, payloads = [], []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.route("**/api/login/status", lambda route: route.fulfill(json={
                "state": "logged_out", "message": "测试：未登录", "username": "", "has_session": False}))

            def search(route):
                payloads.append(route.request.post_data_json)
                route.fulfill(json=result)

            page.route("**/api/search", search)
            page.goto(f"http://127.0.0.1:{server.server_port}/")
            page.get_by_text("未登录 B站", exact=True).wait_for()
            page.locator("#platform").select_option("douyin")
            assert page.locator(".account").is_hidden()
            assert "复用本机保存的登录态" in page.locator("#search-hint").inner_text()
            page.locator("#company").fill("示例公司")
            page.locator("#topic").select_option("interview")
            page.locator("#search").click()
            page.locator("#results").wait_for()
            assert payloads[0]["platform"] == "douyin"
            assert "抖音 · 1 个视频 · 1 条评论" in page.locator("#summary").inner_text()
            assert page.locator("h3 a").get_attribute("href") == f"https://www.douyin.com/video/{AWEME}"
            assert page.locator(".comment a").get_attribute("href").endswith("comment_id=" + COMMENT)
            assert "播放量未公开" in page.locator(".video").inner_text()
            assert page.locator(".comment img").count() == 0
            assert not page.evaluate("Boolean(window.__xss)")
            with page.expect_download() as download:
                page.locator("#download").click()
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "result.json"
                download.value.save_as(path)
                assert json.loads(path.read_text(encoding="utf-8"))["videos"][0]["aid"] == AWEME
            page.screenshot(path="test-results/douyin-desktop.png", full_page=True)
            for width in (390, 700, 768):
                page.set_viewport_size({"width": width, "height": 844})
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), width
            page.set_viewport_size({"width": 390, "height": 844})
            page.screenshot(path="test-results/douyin-mobile.png", full_page=True)
            page.locator("#platform").select_option("bilibili")
            assert page.locator(".account").is_visible()
            assert page.locator(".pill").inner_text() == "抖音 · 视频线索"
            page.route("**/api/search", lambda route: route.fulfill(status=502, json={"error": "抖音要求验证，本次采集已停止。"}))
            page.locator("#platform").select_option("douyin")
            page.locator("#search").click()
            page.get_by_text("抖音要求验证，本次采集已停止。", exact=True).wait_for()
            assert page.locator("#results").is_hidden()
            assert not page.locator("#search").is_disabled()
            assert not errors, errors
            print("UI: platform selection, rendering, source links, JSON export, HTML safety, desktop/mobile layout and error display passed with fixtures.")
        finally:
            browser.close()
finally:
    server.shutdown()
    server.server_close()
    thread.join()
