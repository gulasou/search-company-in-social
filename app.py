"""公司求职口碑检索，支持 B站与抖音。"""

import json
import os
import re
import threading
import time
from datetime import datetime, timezone
from html import unescape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, urlencode, urlsplit
from urllib.request import HTTPCookieProcessor, Request, build_opener

from login import BilibiliLogin, LoginError
from playwright.sync_api import Error as BrowserError, TimeoutError as BrowserTimeout, sync_playwright


ROOT = Path(__file__).resolve().parent
TOPICS = {"salary": "待遇", "overtime": "加班", "interview": "面试", "experience": "入职体验"}
BUSY = threading.Lock()
LOGIN = BilibiliLogin(ROOT / ".local" / "bilibili-session.bin")


class PlatformError(Exception):
    def __init__(self, message, blocked=False):
        super().__init__(message)
        self.blocked = blocked


def plain_text(value):
    return unescape(re.sub(r"<[^>]*>", "", str(value or "")))


def validate(payload):
    if not isinstance(payload, dict):
        raise ValueError("请求必须是 JSON 对象。")
    company = payload.get("company", "")
    topic = payload.get("topic", "all")
    limit = payload.get("limit", 6)
    if not isinstance(company, str) or not 2 <= len(company.strip()) <= 80:
        raise ValueError("请输入 2–80 个字符的公司名。")
    if any(ord(char) < 32 for char in company) or "," in company or "，" in company:
        raise ValueError("请一次输入一家公司，且不要包含换行或控制字符。")
    if not isinstance(topic, str) or topic not in {"all", *TOPICS}:
        raise ValueError("请选择有效的求职话题。")
    if type(limit) is not int or not 1 <= limit <= 8:
        raise ValueError("视频数量必须是 1–8 的整数。")
    platform = payload.get("platform", "bilibili")
    if not isinstance(platform, str) or platform not in {"bilibili", "douyin"}:
        raise ValueError("请选择 B站或抖音。")
    return company.strip(), topic, limit


class Bilibili:
    def __init__(self):
        jar = LOGIN.cookie_jar()
        self.opener = build_opener(HTTPCookieProcessor(jar))
        self.last_request = 0
        self.cookie = "" if any(cookie.name == "SESSDATA" for cookie in jar) else os.environ.get("BILIBILI_COOKIE", "")
        if "\r" in self.cookie or "\n" in self.cookie:
            raise PlatformError("BILIBILI_COOKIE 不能包含换行。")

    def get(self, path, params):
        time.sleep(max(0, 0.6 - (time.monotonic() - self.last_request)))
        self.last_request = time.monotonic()
        headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://www.bilibili.com/"}
        if self.cookie:
            headers["Cookie"] = self.cookie
        request = Request("https://api.bilibili.com" + path + "?" + urlencode(params), headers=headers)
        try:
            with self.opener.open(request, timeout=12) as response:
                body = json.load(response)
        except HTTPError as exc:
            if exc.code in {401, 403, 412, 429}:
                raise PlatformError("B站限制了访问，请稍后重试，或点击页面上的「登录 B站」后再试；登录不能保证解除限制。", True) from None
            raise PlatformError(f"B站返回 HTTP {exc.code}，请稍后重试。") from None
        except (URLError, TimeoutError, OSError):
            raise PlatformError("连接 B站失败或超时，请检查网络后重试。") from None
        except (ValueError, UnicodeError):
            raise PlatformError("B站响应格式异常，未能读取数据。") from None
        if not isinstance(body, dict):
            raise PlatformError("B站响应格式异常。")
        code = body.get("code")
        if code != 0:
            if code in {-101, -111, -352, -412, -509}:
                raise PlatformError("B站要求登录、验证或限制了请求；本次采集已停止，请在官网检查账号状态后重试。", True)
            if code in {12002, 12009}:
                raise PlatformError("该视频评论已关闭或不可见。")
            raise PlatformError(f"B站接口未成功返回数据（错误码 {code}）。")
        data = body.get("data")
        if not isinstance(data, dict) or data.get("v_voucher"):
            raise PlatformError("B站未返回可用数据，可能需要在官网完成验证。", True)
        return data

    def search(self, keyword):
        return self.get("/x/web-interface/search/type", {
            "search_type": "video", "keyword": keyword, "page": 1,
            "page_size": 5,
        }).get("result") or []

    def comments(self, aid):
        data = self.get("/x/v2/reply", {"type": 1, "oid": aid, "sort": 2, "ps": 10, "pn": 1})
        return data.get("replies") or []


class Douyin:
    def __enter__(self):
        self.playwright = sync_playwright().start()
        self.context = None
        try:
            self.context = self.playwright.chromium.launch_persistent_context(
                user_data_dir=str(ROOT / ".local" / "douyin-browser"), channel="msedge", headless=False, chromium_sandbox=True,
                viewport={"width": 1280, "height": 900})
            self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
        except BrowserError:
            self.__exit__(None, None, None)
            raise PlatformError("无法打开抖音搜索窗口，请关闭上次未退出的本程序抖音窗口，并确认已安装 Edge、从普通本机终端启动服务。", True) from None
        return self

    def __exit__(self, *args):
        try:
            if self.context:
                self.context.close()
        finally:
            self.playwright.stop()

    def read_page(self, url, paths, parameter, value, field, timeout=120000):
        responses = []

        def receive(response):
            address = urlsplit(response.url)
            host = address.hostname or ""
            params = parse_qs(address.query)
            if ((host == "douyin.com" or host.endswith(".douyin.com")) and address.path in paths
                    and params.get(parameter) == [str(value)]
                    and params.get("offset", ["0"]) == ["0"]
                    and params.get("cursor", ["0"]) == ["0"]):
                responses.append(response)

        self.page.on("response", receive)
        try:
            self.page.goto(url, wait_until="domcontentloaded", timeout=30000)
            deadline = time.monotonic() + timeout / 1000
            while (not responses or self.page.locator("#login-panel-new").is_visible()) and time.monotonic() < deadline:
                if self.page.is_closed():
                    raise PlatformError("抖音搜索窗口已关闭，本次采集停止。", True)
                if self.page.locator("#login-panel-new").is_visible():
                    responses.clear()
                self.page.wait_for_timeout(250)
            if not responses or self.page.locator("#login-panel-new").is_visible():
                raise PlatformError("抖音等待超时，未返回可用数据，本次采集已停止。请重新搜索，并在弹出的抖音窗口中完成登录或安全验证；如仍失败，可能是平台限制了访问。", True)
            response = responses[-1]
            if response.status in {401, 403, 412, 429}:
                raise PlatformError("抖音要求登录、验证或限制了访问，本次采集已停止。", True)
            if not response.ok:
                raise PlatformError(f"抖音返回 HTTP {response.status}，请稍后重试。")
            try:
                body = response.json()
            except (ValueError, BrowserError):
                raise PlatformError("抖音返回了无法读取的数据，可能需要在官网完成验证。", True) from None
            if not isinstance(body, dict) or body.get("status_code") != 0:
                raise PlatformError("抖音未成功返回数据，可能需要登录或验证，本次采集已停止。", True)
            items = body.get(field)
            if items is None and field == "comments" and body.get("total") == 0:
                return []
            if not isinstance(items, list):
                raise PlatformError("抖音响应格式已变化或需要验证，本次采集已停止。", True)
            return items
        except BrowserTimeout:
            raise PlatformError("打开抖音页面超时，本次采集已停止，请检查网络后重试。", True) from None
        except BrowserError:
            raise PlatformError("抖音窗口已关闭或页面连接失败，本次采集已停止。", True) from None
        finally:
            self.page.remove_listener("response", receive)

    def search(self, keyword):
        items = self.read_page("https://www.douyin.com/search/" + quote(keyword, safe="") + "?type=video",
                               {"/aweme/v1/web/search/item/", "/aweme/v1/web/general/search/single/"},
                               "keyword", keyword, "data", timeout=120000)
        videos = []
        for item in items:
            if not isinstance(item, dict):
                continue
            info = item.get("aweme_info") or {}
            if not isinstance(info, dict):
                continue
            author, statistics = info.get("author") or {}, info.get("statistics") or {}
            if not isinstance(author, dict) or not isinstance(statistics, dict):
                continue
            aid = str(info.get("aweme_id") or "")
            if not aid.isascii() or not aid.isdigit() or int(aid) <= 0:
                continue
            videos.append({"aid": int(aid), "title": info.get("desc"), "description": "",
                           "author": author.get("nickname"),
                           "pubdate": info.get("create_time"),
                           "play": statistics.get("play_count")})
            if len(videos) == 5:
                break
        return videos

    def comments(self, aid):
        items = self.read_page(f"https://www.douyin.com/video/{aid}",
                               {"/aweme/v1/web/comment/list/"}, "aweme_id", aid, "comments")
        replies = []
        for item in items[:10]:
            if not isinstance(item, dict):
                continue
            cid = str(item.get("cid") or "")
            if not cid.isascii() or not cid.isdigit() or int(cid) <= 0:
                continue
            replies.append({"rpid": int(cid), "content": {"message": item.get("text")},
                            "like": item.get("digg_count", 0), "ctime": item.get("create_time")})
        return replies


def collect(company, topic="all", limit=6, client=None, platform="bilibili"):
    company, topic, limit = validate({"company": company, "topic": topic, "limit": limit, "platform": platform})
    if client is not None:
        return collect_videos(company, topic, limit, client, platform)
    if platform == "douyin":
        with Douyin() as client:
            return collect_videos(company, topic, limit, client, platform)
    return collect_videos(company, topic, limit, Bilibili(), platform)


def collect_videos(company, topic, limit, client, platform):
    queries = [f"{company} {word}" for key, word in TOPICS.items() if topic == "all" or key == topic]
    groups, warnings = [], []
    blocked = False
    needle = re.sub(r"\s+", "", company).casefold()
    for query in queries:
        try:
            groups.append(client.search(query))
        except PlatformError as exc:
            warnings.append(f"搜索「{query}」：{exc}")
            if exc.blocked:
                blocked = True
                break
    videos, seen = [], set()
    # 各话题轮流取结果，避免综合搜索被第一个话题占满。
    for index in range(max((len(group) for group in groups), default=0)):
        for group in groups:
            if index >= len(group) or len(videos) >= limit:
                continue
            item = group[index]
            aid = item.get("aid")
            title = plain_text(item.get("title"))
            description = plain_text(item.get("description"))
            searchable = re.sub(r"\s+", "", title + description).casefold()
            if type(aid) is not int or aid <= 0 or aid in seen or needle not in searchable:
                continue
            seen.add(aid)
            url = f"https://www.douyin.com/video/{aid}" if platform == "douyin" else f"https://www.bilibili.com/video/av{aid}"
            video = {"aid": str(aid) if platform == "douyin" else aid, "title": title, "description": description,
                     "author": str(item.get("author") or "未知作者"), "url": url,
                     "published_at": item.get("pubdate"), "views": item.get("play", 0),
                     "comments": [], "comment_status": "pending", "comment_error": ""}
            videos.append(video)
    for video in videos:
        if blocked:
            video["comment_status"] = "unavailable"
            video["comment_error"] = "本次采集因平台访问限制停止，未请求此视频的评论。"
            continue
        try:
            replies = client.comments(video["aid"])
            for reply in replies[:10]:
                message = (reply.get("content") or {}).get("message", "")
                rpid = reply.get("rpid")
                if not message or type(rpid) is not int or rpid <= 0:
                    continue
                video["comments"].append({
                    "id": str(rpid), "text": str(message), "likes": reply.get("like", 0),
                    "published_at": reply.get("ctime"),
                    "url": f"{video['url']}?comment_id={rpid}" if platform == "douyin" else f"{video['url']}#reply{rpid}",
                })
            video["comment_status"] = "ok" if video["comments"] else "empty"
        except PlatformError as exc:
            video["comment_status"] = "unavailable"
            video["comment_error"] = str(exc)
            warnings.append(f"「{video['title']}」评论：{exc}")
            blocked = exc.blocked
    if not groups and warnings:
        raise PlatformError(warnings[0], blocked)
    return {"company": company, "topic": topic, "platform": "抖音" if platform == "douyin" else "B站", "queries": queries,
            "fetched_at": datetime.now(timezone.utc).isoformat(), "videos": videos,
            "warnings": warnings, "partial": bool(warnings)}


class Handler(BaseHTTPRequestHandler):
    def local_request(self):
        port = self.server.server_port
        hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        host = self.headers.get("Host")
        origin = self.headers.get("Origin")
        return host in hosts and (not origin or origin == f"http://{host}")

    def respond(self, status, body, content_type="application/json; charset=utf-8"):
        raw = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        try:
            self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        if not self.local_request():
            self.respond(403, {"error": "仅允许本机访问。"})
        elif self.path == "/":
            self.respond(200, (ROOT / "index.html").read_bytes(), "text/html; charset=utf-8")
        elif self.path == "/api/health":
            self.respond(200, {"ok": True, "platform": "bilibili", "platforms": ["bilibili", "douyin"]})
        elif self.path == "/api/login/status":
            self.respond(200, LOGIN.status())
        else:
            self.respond(404, {"error": "页面不存在。"})

    def do_POST(self):
        if not self.local_request():
            self.respond(403, {"error": "仅允许同源的本机请求。"})
            return
        if self.path not in {"/api/search", "/api/login/start", "/api/login/cancel", "/api/login/clear"}:
            self.respond(404, {"error": "接口不存在。"})
            return
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= 4096 or self.headers.get_content_type() != "application/json":
                raise ValueError("请发送不超过 4 KB 的 JSON 请求。")
            payload = json.loads(self.rfile.read(size))
            if self.path == "/api/search":
                company, topic, limit = validate(payload)
            elif not isinstance(payload, dict):
                raise ValueError("请求必须是 JSON 对象。")
        except (ValueError, UnicodeError) as exc:
            self.respond(400, {"error": str(exc) if not isinstance(exc, json.JSONDecodeError) else "JSON 格式不正确。"})
            return
        if self.path.startswith("/api/login/"):
            action = {"/api/login/start": LOGIN.start, "/api/login/cancel": LOGIN.cancel,
                      "/api/login/clear": LOGIN.clear}[self.path]
            try:
                self.respond(200, action())
            except LoginError as exc:
                self.respond(500, {"error": str(exc)})
            return
        if not BUSY.acquire(blocking=False):
            self.respond(429, {"error": "已有采集任务正在运行，请等待完成。"})
            return
        try:
            self.respond(200, collect(company, topic, limit, platform=payload.get("platform", "bilibili")))
        except PlatformError as exc:
            self.respond(502, {"error": str(exc)})
        except Exception:
            self.respond(500, {"error": "处理结果时发生异常，请重试；若持续出现，请检查平台接口是否变化。"})
        finally:
            BUSY.release()

    def log_message(self, format, *args):
        pass


if __name__ == "__main__":
    server = ThreadingHTTPServer(("127.0.0.1", 8765), Handler)
    print("Eye of Truth: http://127.0.0.1:8765 (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
