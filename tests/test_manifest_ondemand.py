"""on_demand 注入策略 + 描述截断 + 按需提供候选清单。"""
import time
import unittest

import _bootstrap as B

main = B.main
SID = "qq:gm:123"


def entries(n=3, long_desc=False):
    now = int(time.time())
    desc = ("一张手机截图，内容是聊天界面：顶部显示群名，中间有多条消息，文字包括" * 3) if long_desc else "一只橘猫"
    return [{"source": "url", "url": f"https://x/{i}.jpg", "sender": f"用户{i}",
             "time": now, "desc": desc, "msg_id": None} for i in range(n)]


def event(text=None, publish_task=False):
    msgs = []
    if text is not None or publish_task:
        extra = {"qzone_publish_task": True, "qzone_target_image_count": 1,
                 "qzone_max_image_count": 3} if publish_task else {}
        chain = [B.Text(text)] if text else []
        msgs.append(B.KiraIMMessage(chain=chain, extra=extra, sender=None))
    return B.KiraMessageBatchEvent(sid=SID, messages=msgs, session=None)


class TestInjectMode(B.LoopTestCase):
    def test_default_is_on_demand(self):
        plugin, _ = B.make_plugin(cfg={'manifest_inject_mode': None})
        self.assertEqual(plugin.manifest_inject_mode, "on_demand")

    def test_on_demand_skips_plain_chat_turn(self):
        plugin, _ = B.make_plugin()
        plugin.manifest_inject_mode = "on_demand"
        plugin._image_registry[SID] = entries()
        req = B.LLMRequest()
        self.run_(plugin._inject_image_manifest(event(text="今天天气不错"), req, None))
        self.assertEqual(req.user_prompt, [], "普通聊天轮次不应注入图片清单")

    def test_on_demand_does_not_inject_on_user_chat(self):
        """用户聊天里让她发说说时不需要清单：她自己能从消息文本里看到图片路径。

        真要一份清单时，发布工具会按需返回（见 TestOfferOnDemand）。
        """
        plugin, _ = B.make_plugin()
        plugin.manifest_inject_mode = "on_demand"
        plugin._image_registry[SID] = entries()
        for text in ("帮我发条说说", "配张图发个动态", "看下 QQ空间"):
            req = B.LLMRequest()
            self.run_(plugin._inject_image_manifest(event(text=text), req, None))
            self.assertEqual(req.user_prompt, [], f'"{text}" 不该注入清单')

    def test_on_demand_injects_for_scheduled_publish_task(self):
        plugin, _ = B.make_plugin()
        plugin.manifest_inject_mode = "on_demand"
        plugin._image_registry[SID] = entries()
        req = B.LLMRequest()
        self.run_(plugin._inject_image_manifest(event(publish_task=True), req, None))
        self.assertEqual(len(req.user_prompt), 1, "定时发布任务必须注入（这一轮确实要发）")

    def test_always_mode_unchanged(self):
        plugin, _ = B.make_plugin(cfg={'manifest_inject_mode': 'always'})
        plugin._image_registry[SID] = entries()
        req = B.LLMRequest()
        self.run_(plugin._inject_image_manifest(event(text="随便聊聊"), req, None))
        self.assertEqual(len(req.user_prompt), 1)

    def test_no_images_no_injection(self):
        plugin, _ = B.make_plugin(cfg={'manifest_inject_mode': 'always'})
        req = B.LLMRequest()
        self.run_(plugin._inject_image_manifest(event(text="发个说说"), req, None))
        self.assertEqual(req.user_prompt, [])


class TestTruncation(B.LoopTestCase):
    def test_long_description_is_truncated(self):
        plugin, _ = B.make_plugin(cfg={'manifest_inject_mode': 'always', 'image_desc_max_chars': 40})
        plugin._image_registry[SID] = entries(1, long_desc=True)
        req = B.LLMRequest()
        self.run_(plugin._inject_image_manifest(event(), req, None))
        text = req.user_prompt[0].text
        line = [l for l in text.splitlines() if l.startswith("1.")][0]
        desc = line.split("] ", 1)[1]
        self.assertLessEqual(len(desc), 40)
        self.assertTrue(desc.endswith("…"))

    def test_zero_means_no_truncation(self):
        long_desc = "很长很长的描述" * 20
        plugin, _ = B.make_plugin(cfg={'manifest_inject_mode': 'always', 'image_desc_max_chars': 0})
        plugin._image_registry[SID] = [
            {"source": "url", "url": "https://x/1.jpg", "sender": "A",
             "time": int(time.time()), "desc": long_desc, "msg_id": None}
        ]
        req = B.LLMRequest()
        self.run_(plugin._inject_image_manifest(event(), req, None))
        self.assertIn(long_desc, req.user_prompt[0].text)


class TestPlaceholderCaption(B.LoopTestCase):
    def test_native_mode_image_still_qualifies(self):
        """框架 native 模式写的是占位串 "attached image"：不是文字描述，
        但图确实随消息发给了模型（她亲眼看过），所以**仍然进清单**（按原样展示）。"""
        plugin, _ = B.make_plugin(cfg={'manifest_inject_mode': 'always'})
        elem = B.Image("https://x/rt.jpg")
        elem.caption = "attached image"
        plugin._image_registry[SID] = [
            {"elem": elem, "sender": "A", "time": int(time.time()), "desc": None, "msg_id": 1}
        ]
        req = B.LLMRequest()
        self.run_(plugin._inject_image_manifest(event(), req, None))
        self.assertEqual(len(req.user_prompt), 1, 'native 模式的图必须能进清单，否则发不了配图')
        text = req.user_prompt[0].text
        self.assertIn("attached image", text, '占位串按原样展示，不改写')
        # 序号也要能正常解析（发布时用得到）
        resolved = self.run_(plugin._resolve_manifest_images(SID, [1]))
        self.assertEqual(len(resolved), 1)

    def test_native_mode_caption_seeded_at_registration(self):
        """登记时抓到的 caption 也要能用（不能提前被过滤掉）。"""
        plugin, _ = B.make_plugin(cfg={'manifest_inject_mode': 'always'})
        elem = B.Image("https://x/rt.jpg")
        elem.caption = "attached image"
        plugin.run_ = None
        plugin._image_registry[SID] = [
            {"elem": elem, "sender": "A", "time": int(time.time()),
             "desc": elem.caption, "msg_id": 1}
        ]
        req = B.LLMRequest()
        self.run_(plugin._inject_image_manifest(event(), req, None))
        self.assertIn("attached image", req.user_prompt[0].text)


class TestNoAutoDescribe(B.LoopTestCase):
    """登记图片时绝不识图、绝不下载（只登记，描述等框架给）。"""

    def setUp(self):
        super().setUp()
        self.plugin, self.ctx = B.make_plugin()
        self.plugin._image_registry.clear()
        self.downloads = []

        async def fake_fetch(url, **kw):
            self.downloads.append(url)
            return B.fetch_result(True, data=b"\xff\xd8\xff" + b"\x00" * 64)

        async def fake_desc(**kw):
            raise AssertionError('登记路径不该调用 VLM')

        B.patch_fetch_bytes(fake_fetch)
        B.patch_desc_img(fake_desc)

    def _send_image(self):
        return self.run_(self.plugin._collect_images(
            B.KiraMessageEvent(message=B.KiraIMMessage(chain=[B.Image("https://x/a.jpg")],
                                                       sender=None, timestamp=int(time.time())),
                               session=B.Session(sid=SID))))

    def test_registration_does_not_describe_or_download(self):
        self._send_image()
        self.assertEqual(self.downloads, [], '登记图片不该下载')
        self.assertEqual(self.plugin._bg_tasks, set(), '登记图片不该起后台任务')
        entry = self.plugin._image_registry[SID][0]
        self.assertIsNone(entry["desc"], '没有框架描述时 desc 保持 None')


class FakeApi:
    def __init__(self):
        self.published = []

    async def publish(self, post, allow_image_drop=False):
        self.published.append(list(post.images))
        return B._Simple(ok=True, code=0, message=None, data={"tid": "t1"}, raw={})


class TestWantImages(B.LoopTestCase):
    """显式参数 want_images：她要清单才给，不要就正常发布（不打扰、不多调用）。"""

    def setUp(self):
        super().setUp()
        self.plugin, self.ctx = B.make_plugin()
        self.plugin.manifest_inject_mode = "on_demand"
        self.plugin.my_uin = 10001
        self.plugin.api = FakeApi()
        self.plugin.session = object()

        async def noop():
            return None

        self.plugin._ensure_api = noop
        self.plugin._image_registry[SID] = entries(3)

    def test_plain_publish_is_not_interrupted(self):
        """不带 want_images：直接发布，不会被打断去要清单。"""
        out = self.run_(self.plugin.tool_publish(event(text="发个说说"), text="今天不错"))
        self.assertIn("说说发布成功", out)
        self.assertEqual(self.plugin.api.published, [[]])

    def test_want_images_returns_list_without_publishing(self):
        out = self.run_(self.plugin.tool_publish(event(text="发个说说"), text="今天不错",
                                                want_images=True))
        self.assertIn("说说未发布", out)
        self.assertIn("image_indices", out)
        self.assertIn("1. ", out)
        self.assertEqual(self.plugin.api.published, [], "取清单时不应真的发布")

    def test_second_call_publishes(self):
        self.run_(self.plugin.tool_publish(event(text="发个说说"), text="今天不错",
                                           want_images=True))
        out = self.run_(self.plugin.tool_publish(event(text="发个说说"), text="今天不错"))
        self.assertIn("说说发布成功", out)
        self.assertEqual(self.plugin.api.published, [[]])

    def test_want_images_with_empty_candidates_publishes_directly(self):
        """清单为空时不要让她白跑一趟：直接按纯文字发布。"""
        self.plugin._image_registry[SID] = []
        out = self.run_(self.plugin.tool_publish(event(text="发个说说"), text="空",
                                                 want_images=True))
        self.assertIn("说说发布成功", out)

    def test_want_images_uses_native_mode_images(self):
        """native 模式的图（框架只给占位串）也要能出现在清单里 —— 这是她唯一的配图入口。"""
        elem = B.Image("https://x/rt.jpg")
        elem.caption = "attached image"
        self.plugin._image_registry[SID] = [
            {"elem": elem, "sender": "A", "time": int(time.time()), "desc": None, "msg_id": 1}
        ]
        out = self.run_(self.plugin.tool_publish(event(text="发个说说"), text="今日",
                                                 want_images=True))
        self.assertIn("说说未发布", out)
        self.assertIn("attached image", out)

    def test_want_images_ignored_when_images_already_given(self):
        out = self.run_(self.plugin.tool_publish(
            event(text="发个说说"), text="带图", want_images=True,
            images=["https://cdn.qq.com/real.jpg?rkey=1"]))
        self.assertIn("说说发布成功", out)
        self.assertEqual(self.plugin.api.published, [["https://cdn.qq.com/real.jpg?rkey=1"]])

    def test_want_images_ignored_when_list_already_injected(self):
        """清单已经进过上下文（例如定时发布任务那一轮）→ 这个参数应被忽略、直接发布。

        这是 v1.4.9 的闸门：不让"多传一个参数"白白推迟一轮。
        """
        self.run_(self.plugin._inject_image_manifest(event(publish_task=True), B.LLMRequest(), None))
        self.assertIn(SID, self.plugin._manifest_fresh_ts, '注入后应留下标记')
        out = self.run_(self.plugin.tool_publish(event(text="发个说说"), text="今日",
                                                 want_images=True))
        self.assertIn("说说发布成功", out, "清单已在上下文里时不该再返回清单")
        self.assertEqual(self.plugin.api.published, [[]])

    def test_flag_cleared_when_new_turn_has_no_list(self):
        """新一轮没有注入清单 → 标记清除，want_images 恢复生效。"""
        self.run_(self.plugin._inject_image_manifest(event(publish_task=True), B.LLMRequest(), None))
        self.assertIn(SID, self.plugin._manifest_fresh_ts)
        self.run_(self.plugin._inject_image_manifest(event(text="随便聊聊"), B.LLMRequest(), None))
        self.assertNotIn(SID, self.plugin._manifest_fresh_ts, '没有清单的轮次应清掉标记')
        out = self.run_(self.plugin.tool_publish(event(text="发个说说"), text="今日",
                                                 want_images=True))
        self.assertIn("说说未发布", out, '没有清单时这个参数应重新生效')

    def test_timer_turn_publishes_normally(self):
        """定时任务那一轮已经注入过清单，直接发布即可。"""
        out = self.run_(self.plugin.tool_publish(event(publish_task=True), text="定时发"))
        self.assertIn("说说发布成功", out)

    def test_always_mode_does_not_change_tool_behaviour(self):
        self.plugin.manifest_inject_mode = "always"
        out = self.run_(self.plugin.tool_publish(event(text="发个说说"), text="随便"))
        self.assertIn("说说发布成功", out)
