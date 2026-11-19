"""模型输出护栏回归：标签不许漏进说说/评论，普通尖括号文案不许被误伤。

对应实机故障（2026-11-19）：
    qzone_publish args: {'text': '正文...</text>\\n<image_indices" string="false">[1, 2, 3]'}
结果标签被原样发到了 QQ 空间。
"""
import time
import unittest

import _bootstrap as B

main = B.main
guard = main.sanitize_text

LEAKED = (
    "快一点了，群里安静得像没人。这个点说什么都不会被打断，"
    "天亮以后就又轮不到我说了。</text>\n<image_indices\" string=\"false\">[1, 2, 3]"
)


class TestSanitizeText(unittest.TestCase):
    def test_observed_leak(self):
        out = guard(LEAKED)
        self.assertEqual(
            out.text,
            "快一点了，群里安静得像没人。这个点说什么都不会被打断，"
            "天亮以后就又轮不到我说了。",
        )
        self.assertEqual(out.image_indices, [1, 2, 3])
        self.assertTrue(out.changed)

    def test_msg_envelope(self):
        out = guard("<msg>\n    <text>今天很好</text>\n</msg>")
        self.assertEqual(out.text, "今天很好")

    def test_multiple_msg_blocks(self):
        out = guard("<msg><text>第一段</text></msg>\n<msg><text>第二段</text></msg>")
        self.assertEqual(out.text, "第一段\n第二段")

    def test_text_pair_with_attributes(self):
        out = guard('<text string="false">正文</text>')
        self.assertEqual(out.text, "正文")

    def test_truncated_closing_tag(self):
        """模型输出被截断（只有 </text 没有 >）时也不能漏标签。"""
        out = guard("正文</text")
        self.assertEqual(out.text, "正文")
        self.assertTrue(out.changed)

    def test_closing_tag_with_attributes(self):
        """闭合标签被模型塞了属性（</text string="false">）也不能漏进正文。"""
        out = guard('正文</text string="false">')
        self.assertEqual(out.text, "正文")
        self.assertTrue(out.changed)

    def test_closed_param_block(self):
        out = guard("正文<image_indices>[1,2,3]</image_indices>")
        self.assertEqual(out.text, "正文")
        self.assertEqual(out.image_indices, [1, 2, 3])

    def test_json_wrapper(self):
        out = guard('{"text": "JSON 里的正文", "image_indices": [2, 3]}')
        self.assertEqual(out.text, "JSON 里的正文")
        self.assertEqual(out.image_indices, [2, 3])

    def test_images_json_recovery(self):
        out = guard('正文\n<images>["https://a.com/1.jpg", "https://a.com/2.jpg"]</images>')
        self.assertEqual(out.text, "正文")
        self.assertEqual(out.images, ["https://a.com/1.jpg", "https://a.com/2.jpg"])

    def test_want_images_recovery(self):
        out = guard("先看看图\n<want_images>true</want_images>")
        self.assertEqual(out.text, "先看看图")
        self.assertIs(out.want_images, True)

    def test_silence_marker_becomes_empty(self):
        out = guard("<msg/>")
        self.assertEqual(out.text, "")

    def test_plain_angle_brackets_untouched(self):
        for text in (
            "今天心情 <3 真的",
            "1 < 2 > 0，这是数学",
            "他说“<text>”是一个标签",
            "普通的英文 a<b and c>d",
            "代码片段 if (a<b) return c>d;",
        ):
            out = guard(text)
            self.assertEqual(out.text, text, text)
            self.assertFalse(out.changed, text)

    def test_whitespace_trim_is_not_reported_as_cleanup(self):
        """普通文本的首尾空白整理不应被当成"清理了协议标签"（避免误导日志）。"""
        out = guard("  正文  ")
        self.assertEqual(out.text, "正文")
        self.assertFalse(out.changed)

    def test_json_unwrap_is_reported_as_cleanup(self):
        out = guard('{"text": "正文"}')
        self.assertEqual(out.text, "正文")
        self.assertTrue(out.changed)

    def test_plain_text_internal_spacing_untouched(self):
        text = "第一行  第二列\n第三行"
        self.assertEqual(guard(text).text, text)

    def test_idempotent(self):
        once = guard(LEAKED)
        twice = guard(once.text)
        self.assertEqual(twice.text, once.text)
        self.assertFalse(twice.changed)

    def test_non_string_input(self):
        self.assertEqual(guard({"text": "字典里的正文", "image_indices": [1]}).text, "字典里的正文")
        self.assertEqual(guard(["第一段", "第二段"]).text, "第一段\n第二段")
        self.assertEqual(guard(None).text, "")

    def test_body_after_unclosed_tag_is_kept(self):
        """未闭合标签后面还有正文时，只摘标签，不吃正文。"""
        out = guard("正文\n<image_indices>[1,2]\n后面还有一句")
        self.assertIn("后面还有一句", out.text)
        self.assertEqual(out.image_indices, [1, 2])


class FakeApi:
    def __init__(self):
        self.published = []
        self.comments = []

    async def publish(self, post, allow_image_drop=False):
        self.published.append((post.text, list(post.images)))
        return B._Simple(ok=True, code=0, message=None,
                         data={"tid": "t1", "image_md5s": []}, raw={})

    async def get_detail(self, post):
        return B._Simple(ok=True, code=0, message=None, data={
            "tid": "t1", "uin": 10001, "name": "我", "content": "原说说",
            "created_time": int(time.time()), "pic": [], "commentlist": [],
        }, raw={})

    async def comment(self, post, content):
        self.comments.append(content)
        return B._Simple(ok=True, code=0, message=None, data={}, raw={})


class GuardFlowCase(B.LoopTestCase):
    def setUp(self):
        super().setUp()
        self.plugin, self.ctx = B.make_plugin(
            cfg={"action_interval": "0-0s", "like_delay": "0-0s"}
        )
        self.plugin.my_uin = 10001
        self.api = FakeApi()
        self.plugin.api = self.api
        self.plugin.session = object()

        async def noop():
            return None

        self.plugin._ensure_api = noop
        self.event = B.KiraMessageBatchEvent(sid="qq:gm:1", messages=[], session=None)

    def entry(self, url, desc="一只猫"):
        return {"source": "url", "url": url, "sender": "A",
                "time": int(time.time()), "desc": desc, "msg_id": None}


class TestPublishGuard(GuardFlowCase):
    def test_leaked_text_is_published_clean(self):
        out = self.run_(self.plugin.tool_publish(self.event, text=LEAKED))
        self.assertIn("说说发布成功", out)
        text, _ = self.api.published[-1]
        self.assertNotIn("</text>", text)
        self.assertNotIn("image_indices", text)
        self.assertTrue(text.endswith("轮不到我说了。"))

    def test_recovered_indices_still_attach_images(self):
        self.plugin._image_registry["qq:gm:1"] = [
            self.entry("https://x/1.jpg"),
            self.entry("https://x/2.jpg"),
            self.entry("https://x/3.jpg"),
        ]
        out = self.run_(self.plugin.tool_publish(self.event, text=LEAKED))
        self.assertIn("说说发布成功", out)
        _, images = self.api.published[-1]
        self.assertEqual(images, ["https://x/1.jpg", "https://x/2.jpg", "https://x/3.jpg"])

    def test_explicit_indices_win_over_recovered(self):
        self.plugin._image_registry["qq:gm:1"] = [
            self.entry("https://x/1.jpg"),
            self.entry("https://x/2.jpg"),
        ]
        out = self.run_(self.plugin.tool_publish(
            self.event, text="正文</text>\n<image_indices>[1,2,3]", image_indices=[2]))
        self.assertIn("说说发布成功", out)
        _, images = self.api.published[-1]
        self.assertEqual(images, ["https://x/2.jpg"])

    def test_empty_text_with_images_keeps_old_behavior(self):
        """原本就是空正文 + 配图（只发图）不能被护栏误伤。"""
        out = self.run_(self.plugin.tool_publish(
            self.event, text="", images=["data/temp/local.jpg"]))
        self.assertIn("说说发布成功", out)
        text, images = self.api.published[-1]
        self.assertEqual(text, "")
        self.assertEqual(images, ["data/temp/local.jpg"])

    def test_empty_text_without_images_is_rejected(self):
        out = self.run_(self.plugin.tool_publish(self.event, text=""))
        self.assertIn("发布失败", out)
        self.assertEqual(self.api.published, [])

    def test_tag_only_text_is_not_published(self):
        out = self.run_(self.plugin.tool_publish(self.event, text="<msg/>"))
        self.assertIn("说说正文为空", out)
        self.assertEqual(self.api.published, [])

    def test_publish_layer_is_last_gate(self):
        """即使绕过工具层直接调 _publish，正文也不会带标签出去。"""
        self.run_(self.plugin._publish("正文</text>\n<image_indices>[1]", []))
        text, _ = self.api.published[-1]
        self.assertEqual(text, "正文")

    def test_scheduled_task_scenario_end_to_end(self):
        """实机场景：定时发布 + 清单 3 张图 + 正文尾部漏标签。"""
        self.plugin._image_registry["qq:gm:1"] = [
            self.entry("https://x/1.jpg"),
            self.entry("https://x/2.jpg"),
            self.entry("https://x/3.jpg"),
        ]
        event = B.KiraMessageBatchEvent(
            sid="qq:gm:1",
            messages=[B.KiraIMMessage(
                chain=[], group=None, sender=None,
                extra={"qzone_publish_task": True,
                       "qzone_target_image_count": 3,
                       "qzone_max_image_count": 3},
            )],
            session=None,
        )
        out = self.run_(self.plugin.tool_publish(event, text=LEAKED))
        self.assertIn("说说发布成功", out)
        text, images = self.api.published[-1]
        self.assertEqual(text, LEAKED.split("</text>")[0])
        self.assertEqual(len(images), 3)

    def test_plain_text_is_unchanged(self):
        self.run_(self.plugin.tool_publish(self.event, text="1 < 2 > 0，心情 <3"))
        text, _ = self.api.published[-1]
        self.assertEqual(text, "1 < 2 > 0，心情 <3")


class TestCommentGuard(GuardFlowCase):
    def test_comment_text_is_cleaned(self):
        out = self.run_(self.plugin.tool_comment(
            self.event, target_id="10001", tid="t1",
            content="好看</text>\n<text>真的"))
        self.assertIn("评论成功", out)
        self.assertEqual(self.api.comments[-1], "好看\n真的")

    def test_tag_only_comment_is_rejected(self):
        out = self.run_(self.plugin.tool_comment(
            self.event, target_id="10001", tid="t1", content="<msg/>"))
        self.assertIn("评论失败", out)
        self.assertEqual(self.api.comments, [])


if __name__ == '__main__':
    unittest.main()
