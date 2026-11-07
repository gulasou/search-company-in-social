import unittest
from unittest.mock import MagicMock, patch
from urllib.parse import quote

from app import Douyin, PlatformError, collect, validate


AWEME = "7420000000000000123"
COMMENT = "7420000000000000456"


class DouyinTests(unittest.TestCase):
    def test_platform_validation(self):
        for platform in (None, [], "other"):
            with self.subTest(platform=platform), self.assertRaises(ValueError):
                validate({"company": "示例公司", "platform": platform})

    def test_video_and_comment_mapping_preserves_long_ids(self):
        client = Douyin()
        items = [{"aweme_info": {"aweme_id": AWEME, "desc": "示例公司 面试",
                                  "author": {"nickname": "测试作者"}, "create_time": 1700000000}}]
        replies = [{"cid": COMMENT, "text": "原始评论 <script>文本</script>", "digg_count": 3}]
        with patch.object(client, "read_page", side_effect=[items, replies]):
            result = collect("示例公司", "interview", client=client, platform="douyin")
        video = result["videos"][0]
        self.assertEqual(result["platform"], "抖音")
        self.assertEqual(video["aid"], AWEME)
        self.assertEqual(video["url"], f"https://www.douyin.com/video/{AWEME}")
        self.assertIsNone(video["views"])
        self.assertEqual(video["comments"][0]["id"], COMMENT)
        self.assertEqual(video["comments"][0]["url"], f"{video['url']}?comment_id={COMMENT}")
        self.assertIn("<script>", video["comments"][0]["text"])

    def test_search_limits_candidates_and_skips_non_video_results(self):
        client = Douyin()
        items = [{"user_info": {}}, {"aweme_info": "invalid"},
                 {"aweme_info": {"aweme_id": "invalid"}}]
        items += [{"aweme_info": {"aweme_id": str(int(AWEME) + i), "desc": "示例公司"}} for i in range(8)]
        with patch.object(client, "read_page", return_value=items):
            self.assertEqual(len(client.search("示例公司")), 5)

    def read(self, body, status=200, response_url=None):
        client = Douyin()
        client.page = MagicMock()
        response = MagicMock(status=status, ok=status == 200)
        response.url = response_url or "https://www.douyin.com/aweme/v1/web/search/item/?keyword=" + quote("示例公司")
        response.json.return_value = body
        client.page.goto.side_effect = lambda *a, **kw: client.page.on.call_args.args[1](response)
        # 限定等待循环，测试不访问网络也不实际等待。
        with patch("app.time.monotonic", side_effect=[0, 121]):
            return client.read_page("https://www.douyin.com/search/test", {"/aweme/v1/web/search/item/"},
                                    "keyword", "示例公司", "data")

    def test_explicit_empty_results_are_success(self):
        self.assertEqual(self.read({"status_code": 0, "data": []}), [])

    def test_restricted_or_invalid_response_is_not_empty_success(self):
        for body, status in (({"status_code": 0, "data": []}, 403),
                             ({"status_code": 8}, 200),
                             ({"status_code": 0}, 200), ([], 200)):
            with self.subTest(body=body, status=status), self.assertRaises(PlatformError) as caught:
                self.read(body, status)
            self.assertTrue(caught.exception.blocked)

    def test_ignores_other_hosts_keywords_and_pages(self):
        base = "https://www.douyin.com/aweme/v1/web/search/item/"
        for url in (base.replace("www.douyin.com", "www.douyin.com.evil.example") + "?keyword=" + quote("示例公司"),
                    base + "?keyword=other", base + "?keyword=" + quote("示例公司") + "&offset=10"):
            with self.subTest(url=url), self.assertRaises(PlatformError):
                self.read({"status_code": 0, "data": []}, response_url=url)

    def test_browser_is_closed_when_collection_fails(self):
        with patch("app.Douyin") as factory:
            client = factory.return_value.__enter__.return_value
            client.search.side_effect = PlatformError("需要验证", True)
            with self.assertRaises(PlatformError):
                collect("示例公司", platform="douyin")
            client.search.assert_called_once()
            factory.return_value.__exit__.assert_called_once()

    def test_comment_restriction_stops_later_videos(self):
        client = Douyin()
        items = [{"aweme_info": {"aweme_id": str(int(AWEME) + i), "desc": "示例公司 面试"}} for i in range(2)]
        with patch.object(client, "read_page", side_effect=[items, PlatformError("需要验证", True)]) as read:
            result = collect("示例公司", "interview", client=client, platform="douyin")
        self.assertEqual(read.call_count, 2)
        self.assertTrue(result["partial"])
        self.assertTrue(all(v["comment_status"] == "unavailable" for v in result["videos"]))


if __name__ == "__main__":
    unittest.main()
