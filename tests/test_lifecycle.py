"""启动与卸载冒烟：插件能正常建立会话、能干净地停止（不残留后台任务）。"""
import asyncio
import json

import _bootstrap as B

COOKIES = "uin=o10001; skey=abc; p_skey=def; p_uin=o10001"


class TestStartup(B.LoopTestCase):
    def test_initialize_builds_session_from_onebot_cookie(self):
        ada = B.FakeAdapter('qq_ada', send_result={'status': 'ok', 'data': {'cookies': COOKIES}})
        plugin, ctx = B.make_plugin(adapters={'qq_ada': ada})
        self.run_(plugin.initialize())
        self.assertEqual(plugin.my_uin, 10001)
        self.assertIsNotNone(plugin.api)
        self.assertFalse(plugin._init_failed)
        self.run_(plugin.terminate())

    def test_initialize_without_cookie_schedules_retry(self):
        ada = B.FakeAdapter('qq_ada', error=TimeoutError('请求 get_cookies 超时'))
        plugin, ctx = B.make_plugin(adapters={'qq_ada': ada})
        self.run_(plugin.initialize())
        self.assertIsNotNone(plugin._startup_retry_task, "应安排延迟自动重试而不是直接判死")
        self.run_(plugin.terminate())

    def test_initialize_with_manual_cookie_only(self):
        plugin, ctx = B.make_plugin(cfg={'auto_refresh_cookie': False, 'cookies_str': COOKIES})
        self.run_(plugin.initialize())
        self.assertEqual(plugin.my_uin, 10001)
        self.run_(plugin.terminate())

    def test_terminate_cancels_background_tasks_and_flushes_state(self):
        ada = B.FakeAdapter('qq_ada', send_result={'status': 'ok', 'data': {'cookies': COOKIES}})
        plugin, ctx = B.make_plugin(adapters={'qq_ada': ada})

        self.run_(plugin.initialize())

        # 制造一个进行中的后台任务 + 一个待落盘的状态变更
        async def long_wait():
            await asyncio.sleep(30)

        async def setup():
            plugin._save_state()                # 制造一个待落盘的状态变更
            plugin._spawn_task(long_wait())     # 制造一个进行中的后台任务

        self.run_(setup())
        self.run_(asyncio.sleep(0.05))
        remaining = [t for t in plugin._bg_tasks if not t.done()]
        self.assertTrue(remaining, "应存在进行中的后台任务")

        self.run_(plugin.terminate())
        self.assertEqual(len(plugin._bg_tasks), 0, "卸载后不应残留后台任务")
        self.assertIsNone(plugin.api)
        state = json.loads((ctx._data_dir / 'state.json').read_text(encoding='utf-8'))
        self.assertIn('published_image_history', state)

    def test_initialize_survives_broken_scheduler(self):
        """宿主时区异常导致调度器构造失败时，插件仍应可用（只是没有定时任务）。"""
        plugin, ctx = B.make_plugin()
        plugin.scheduler = None
        self.run_(plugin._setup_scheduled_jobs())   # 不应抛异常
        self.run_(plugin.terminate())


class AsyncMetaAdapter(B.FakeAdapter):
    """模拟 3.x 适配器：get_message_metadata() 是异步的。"""

    def get_capability(self, cls):
        class Cap:
            async def get_message_metadata(self):
                return B._Simple(supported_elements=["text", "img", "face"])
        return Cap()


class TestAdapterProbe(B.LoopTestCase):
    def test_async_metadata_is_awaited(self):
        """3.x 的元数据接口是协程：必须 await，否则退化为弃用的 message_types。"""
        plugin, _ = B.make_plugin(adapters={"qq_ada": AsyncMetaAdapter("qq_ada")})
        els = self.run_(B.main.adapter_supported_elements(plugin._resolve_ada()))
        self.assertEqual(els, ["text", "img", "face"])

    def test_synthetic_task_event_carries_elements(self):
        plugin, ctx = B.make_plugin(adapters={"qq_ada": AsyncMetaAdapter("qq_ada")})
        plugin.task_group_ids = ["123"]
        captured = []

        async def publish(ev):
            captured.append(ev)

        ctx.event_bus.publish = publish
        self.assertTrue(self.run_(plugin._send_task_instruction("hello")))
        self.assertEqual(list(captured[0].message_types), ["text", "img", "face"])
