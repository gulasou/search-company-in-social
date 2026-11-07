"""真实 Edge + 构造平台响应，验证抖音采集及页面；不访问抖音。"""

import json
import sys
import tempfile
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, quote, unquote, urlsplit

from playwright.sync_api import Browser, BrowserType, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import Handler, collect


AWEME = "7420000000000000123"
COMMENT = "7420000000000000456"
original_launch = BrowserType.launch
original_page = Browser.new_page
requests = []


def headless_launch(self, **kwargs):
    kwargs["headless"] = True
    return original_launch(self, **kwargs)


def fixture_page(self, **kwargs):
    page = original_page(self, **kwargs)

    def platform(route):
        address = urlsplit(route.request.url)
        requests.append(address.path)
        if address.path.startswith("/search/"):
            keyword = unquote(address.path.removeprefix("/search/"))
            api = "/aweme/v1/web/search/item/?keyword=" + quote(keyword)
        elif address.path.startswith("/video/"):
            api = "/aweme/v1/web/comment/list/?aweme_id=" + AWEME + "&cursor=0"
        elif address.path == "/aweme/v1/web/search/item/":
            keyword = parse_qs(address.query)["keyword"][0]
            body = {"status_code": 0, "data": [{"aweme_info": {
                "aweme_id": AWEME, "desc": keyword, "author": {"nickname": "测试作者"},
                "create_time": 1700000000, "statistics": {}}}]}
            route.fulfill(json=body)
            return
        elif address.path == "/aweme/v1/web/comment/list/":
            route.fulfill(json={"status_code": 0, "comments": [{"cid": COMMENT,
                "text": '<img src=x onerror="window.__xss=true"> 原始评论', "digg_count": 3}]})
            return
        else:
            route.abort()
            return
        route.fulfill(content_type="text/html", body=f"<title>抖音测试</title><script>fetch({json.dumps(api)})</script>")

    page.route("**/*", platform)
    return page


with patch.object(BrowserType, "launch", headless_launch), patch.object(Browser, "new_page", fixture_page):
    result = collect("示例公司", "interview", platform="douyin")
assert result["videos"][0]["aid"] == AWEME
assert result["videos"][0]["comments"][0]["id"] == COMMENT
assert len(requests) == 4, requests
print("Real Edge: search navigation, response capture, comment navigation, long IDs and browser cleanup passed with fixtures.")

server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
thread = threading.Thread(target=server.serve_forever, daemon=True)
thread.start()
try:
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="msedge", headless=True)
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
            assert "抖音官网窗口" in page.locator("#search-hint").inner_text()
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
