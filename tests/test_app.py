import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from app import Bilibili, Handler, PlatformError, collect, plain_text, validate


def video(aid=1, title="示例公司 面试经验"):
    return {"aid": aid, "title": title, "description": "测试用简介", "author": "测试作者"}


class FakeBilibili:
    def __init__(self):
        self.calls = []

    def search(self, keyword):
        self.calls.append(keyword)
        return [video(), video(2, "其他公司 面试"), video(3, "示例公司 加班")]

    def comments(self, aid):
        self.calls.append(aid)
        return [{"rpid": 100 + aid, "content": {"message": "测试评论 <script>文本</script>"}, "like": 2}]


class CollectionTests(unittest.TestCase):
    def test_validation(self):
        for payload in [[], {}, {"company": "a"}, {"company": "示例公司\n"},
                        {"company": "示例公司", "topic": []},
                        {"company": "示例公司", "limit": True},
                        {"company": "示例公司", "limit": 9}]:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                validate(payload)

    def test_cleanup_preserves_encoded_text(self):
        self.assertEqual(plain_text('<em class="keyword">公司</em> &amp; &lt;test&gt;'), "公司 & <test>")

    def test_deduplicates_filters_and_retains_sources(self):
        client = FakeBilibili()
        result = collect("示例公司", client=client)
        self.assertEqual(len(result["videos"]), 2)
        self.assertEqual(len([call for call in client.calls if isinstance(call, str)]), 4)
        self.assertEqual([v["aid"] for v in result["videos"]], [1, 3])
        self.assertEqual(result["videos"][0]["comments"][0]["url"], "https://www.bilibili.com/video/av1#reply101")
        self.assertIn("<script>", result["videos"][0]["comments"][0]["text"])
        self.assertFalse(result["partial"])

    def test_selected_topic_and_limit(self):
        client = FakeBilibili()
        result = collect("示例公司", "interview", 1, client)
        self.assertEqual(client.calls, ["示例公司 面试", 1])
        self.assertEqual(len(result["videos"]), 1)

    def test_round_robin_topics(self):
        client = FakeBilibili()
        client.search = lambda keyword: [video(10 if "待遇" in keyword else 20), video(30)]
        result = collect("示例公司", limit=2, client=client)
        self.assertEqual([v["aid"] for v in result["videos"]], [10, 20])

    def test_empty_comments_are_distinct_from_failure(self):
        client = FakeBilibili()
        client.comments = lambda aid: []
        result = collect("示例公司", "salary", 1, client)
        self.assertEqual(result["videos"][0]["comment_status"], "empty")
        self.assertFalse(result["partial"])

    def test_comment_restriction_stops_further_requests(self):
        client = FakeBilibili()
        def restricted(aid):
            client.calls.append(aid)
            raise PlatformError("需要验证", True)
        client.comments = restricted
        result = collect("示例公司", "salary", 2, client)
        self.assertEqual(client.calls, ["示例公司 待遇", 1])
        self.assertTrue(result["partial"])
        self.assertTrue(all(v["comment_status"] == "unavailable" for v in result["videos"]))

    def test_search_restriction_is_not_empty_success(self):
        client = FakeBilibili()
        client.search = lambda keyword: (_ for _ in ()).throw(PlatformError("限制访问", True))
        with self.assertRaises(PlatformError):
            collect("示例公司", client=client)

    def test_api_requires_success_code(self):
        client = Bilibili()
        with patch.object(client.opener, "open") as mocked:
            mocked.return_value.__enter__.return_value.read.return_value = b'{"code":-352,"data":null}'
            with self.assertRaises(PlatformError) as caught:
                client.comments(1)
        self.assertTrue(caught.exception.blocked)


class HttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def test_page_and_health(self):
        with urlopen(self.base + "/") as response:
            self.assertIn("公司名称".encode(), response.read())
        with urlopen(self.base + "/api/health") as response:
            self.assertTrue(json.load(response)["ok"])

    def test_rejects_invalid_payload_before_network(self):
        req = Request(self.base + "/api/search", data=b'{}', headers={"Content-Type": "application/json"})
        with self.assertRaises(HTTPError) as caught:
            urlopen(req)
        self.assertEqual(caught.exception.code, 400)

    def test_rejects_cross_origin(self):
        req = Request(self.base + "/api/search", data=b'{}', headers={
            "Content-Type": "application/json", "Origin": "https://example.com"})
        with self.assertRaises(HTTPError) as caught:
            urlopen(req)
        self.assertEqual(caught.exception.code, 403)

    def test_search_response(self):
        req = Request(self.base + "/api/search", data=json.dumps({"company": "示例公司"}).encode(),
                      headers={"Content-Type": "application/json"})
        with patch("app.Bilibili", return_value=FakeBilibili()), urlopen(req) as response:
            result = json.load(response)
        self.assertEqual(len(result["videos"]), 2)

    def test_login_status_never_exposes_credentials(self):
        with urlopen(self.base + "/api/login/status") as response:
            status = json.load(response)
        self.assertEqual(set(status), {"state", "message", "username", "has_session"})

    def test_douyin_search_dispatch(self):
        req = Request(self.base + "/api/search", data=json.dumps({"company": "示例公司", "platform": "douyin"}).encode(),
                      headers={"Content-Type": "application/json"})
        with patch("app.Douyin") as factory:
            factory.return_value.__enter__.return_value.search.return_value = []
            with urlopen(req) as response:
                self.assertEqual(json.load(response)["platform"], "抖音")
            factory.return_value.__exit__.assert_called_once()

    def test_invalid_platform_rejected_before_browser_launch(self):
        req = Request(self.base + "/api/search", data=json.dumps({"company": "示例公司", "platform": "other"}).encode(),
                      headers={"Content-Type": "application/json"})
        with patch("app.Douyin") as factory, self.assertRaises(HTTPError) as caught:
            urlopen(req)
        self.assertEqual(caught.exception.code, 400)
        factory.assert_not_called()

    def test_login_controls_require_same_origin(self):
        for action in ("start", "cancel", "clear"):
            req = Request(self.base + "/api/login/" + action, data=b'{}', headers={
                "Content-Type": "application/json", "Origin": "https://example.com"})
            with self.subTest(action=action), self.assertRaises(HTTPError) as caught:
                urlopen(req)
            self.assertEqual(caught.exception.code, 403)

    def test_login_start_calls_manager(self):
        req = Request(self.base + "/api/login/start", data=b'{}', headers={"Content-Type": "application/json"})
        with patch("app.LOGIN") as login:
            login.start.return_value = {"state": "starting", "has_session": False}
            with urlopen(req) as response:
                self.assertEqual(json.load(response)["state"], "starting")
            login.start.assert_called_once()


if __name__ == "__main__":
    unittest.main()
