"""需求 2 验收：适配器引用卫生、失败状态机、软重置、真重载节流、假成功修正。"""
import asyncio
import json
import time

import _bootstrap as B

main = B.main
LOG = main.logger.name


class TestAdapterReferenceHygiene(B.LoopTestCase):
    def test_replaced_adapter_is_picked_up_immediately(self):
        """适配器被替换后，下一次调用必须自动用新对象（无需任何重载）。"""
        ada_a = B.FakeAdapter('qq_ada', error=TimeoutError('请求 get_cookies 超时'))
        self.plugin, self.ctx = B.make_plugin(adapters={'qq_ada': ada_a},
                                              cfg={'cookie_self_heal': False})
        with self.assertRaises(TimeoutError):
            self.run_(self.plugin._call_onebot_action('get_cookies', {}))
        # 框架在适配器更新时会 new 一个新实例覆盖注册表
        ada_b = B.FakeAdapter('qq_ada')
        self.ctx.adapter_mgr._adapters['qq_ada'] = ada_b
        res = self.run_(self.plugin._call_onebot_action('get_cookies', {}))
        self.assertEqual(res['status'], 'ok')
        self.assertTrue(ada_b.sent, "新适配器应收到调用")
        self.assertIs(self.plugin._ada_obj, ada_b)

    def test_permanently_disconnected_reports_precisely(self):
        ada = B.FakeAdapter('qq_ada')
        ada.permanently_disconnected = True
        self.plugin, _ = B.make_plugin(adapters={'qq_ada': ada}, cfg={'cookie_self_heal': False})
        with self.assertRaises(RuntimeError) as ctx:
            self.run_(self.plugin._call_onebot_action('get_cookies', {}))
        self.assertIn('永久失败', str(ctx.exception))

    def test_missing_adapter_does_not_crash(self):
        self.plugin, _ = B.make_plugin(adapters={'other': B.FakeAdapter('other', platform='TG')},
                                       cfg={'cookie_self_heal': False})
        with self.assertRaises(RuntimeError):
            self.run_(self.plugin._call_onebot_action('get_cookies', {}))


class TestHealthStateMachine(B.LoopTestCase):
    def setUp(self):
        super().setUp()
        self.ada = B.FakeAdapter('qq_ada', error=TimeoutError('请求 get_cookies 超时'))
        self.plugin, self.ctx = B.make_plugin(adapters={'qq_ada': self.ada},
                                              cfg={'cookie_self_heal': False,
                                                   'cookie_self_heal_threshold': 3})

    def _fail(self):
        with self.assertRaises(Exception):
            self.run_(self.plugin._call_onebot_action('get_cookies', {}))

    def test_logs_only_on_state_change(self):
        with self.assertLogs(LOG, level='DEBUG') as ctx:
            self._fail()
            self._fail()
            self._fail()
            self._fail()
            self._fail()
        warnings = [r for r in ctx.records if r.levelno >= 30]
        self.assertEqual(len(warnings), 1, f"连续失败只应有一条 WARNING，实际 {len(warnings)}")
        self.assertIn('连续失败 3 次', warnings[0].getMessage())
        self.assertIn('诊断', warnings[0].getMessage())
        self.assertIn('引用一致', warnings[0].getMessage())

    def test_recovery_logs_once(self):
        self._fail()
        self._fail()
        self._fail()
        self.ada._error = None
        with self.assertLogs(LOG, level='DEBUG') as ctx:
            self.run_(self.plugin._call_onebot_action('get_cookies', {}))
            self.run_(self.plugin._call_onebot_action('get_cookies', {}))
        infos = [r for r in ctx.records if r.levelno == 20 and '已恢复' in r.getMessage()]
        self.assertEqual(len(infos), 1)

    def test_diagnostics_flags_stale_reference(self):
        """诊断串必须能区分"引用不一致"（陈旧引用主因）与其它形态。"""
        self._fail()
        self.plugin._ada_obj = B.FakeAdapter('qq_ada')  # 模拟插件手里的旧对象
        diag = self.plugin._onebot_diagnostics('get_cookies')
        self.assertIn('引用一致=False', diag)
        self.assertIn('websocket=已连接', diag)


class TestSoftReset(B.LoopTestCase):
    def setUp(self):
        super().setUp()
        self.plugin, self.ctx = B.make_plugin()

    def test_soft_reset_clears_plugin_state_without_blocking(self):
        self.plugin._init_failed = True
        self.plugin._last_cookie_refresh = time.time()
        self.plugin._ada_obj = self.ctx.adapter_mgr.get_adapter('qq_ada')
        self.plugin._recent_images_cache['x'] = (0.0, [])
        start = time.monotonic()
        self.run_(self.plugin._soft_reset_connection('测试'))
        elapsed = time.monotonic() - start
        self.assertLess(elapsed, 0.2, f"软重置不应阻塞调用方（耗时 {elapsed:.3f}s）")
        self.assertIsNone(self.plugin._ada_obj)
        self.assertIsNone(self.plugin.session)
        self.assertIsNone(self.plugin.api)
        self.assertFalse(self.plugin._init_failed)
        self.assertEqual(self.plugin._last_cookie_refresh, 0.0)
        self.assertEqual(self.plugin._recent_images_cache, {})
        self.assertIsNotNone(self.plugin._startup_retry_task)

    def test_self_heal_escalates_to_hard_reload(self):
        """软重置后仍未恢复 → 自动调用与 WebUI 重载等价的 reload。"""
        self.plugin.self_heal_probe_timeout = 0.3   # 测试里不必等满 60s
        self.run_(self.plugin._self_heal('测试'))
        self.run_(asyncio.sleep(0.1))
        self.assertEqual(self.ctx.plugin_mgr.reloads, ['qzone_plugin'])

    def test_hard_reload_throttled_and_persisted(self):
        self.plugin._last_auto_reload_ts = 0.0

        async def trigger(tag):
            self.plugin._schedule_hard_reload(tag)
            await asyncio.sleep(0.1)

        self.run_(trigger('第一次'))
        self.assertEqual(len(self.ctx.plugin_mgr.reloads), 1)
        # 冷却期内再次触发：不应重复重载（防止重载风暴）
        self.plugin._reload_inflight = False
        self.run_(trigger('第二次'))
        self.assertEqual(len(self.ctx.plugin_mgr.reloads), 1, "冷却期内不应重复重载")
        # 冷却时间必须持久化（跨重载依然生效）
        self.run_(asyncio.sleep(0.1))
        state = json.loads((self.ctx._data_dir / 'state.json').read_text(encoding='utf-8'))
        self.assertGreater(state.get('last_auto_reload_ts', 0), 0)

    def test_self_heal_disabled_by_config(self):
        plugin, ctx = B.make_plugin(cfg={'cookie_self_heal': False})
        plugin._trigger_self_heal('测试')
        self.run_(asyncio.sleep(0.05))
        self.assertEqual(ctx.plugin_mgr.reloads, [])


class TestHonestRefresh(B.LoopTestCase):
    def test_throttled_force_refresh_does_not_lie(self):
        """被节流跳过时必须返回 False（旧实现返回 True 会让 HTTP 层拿旧凭证空重试）。"""
        self.plugin, _ = B.make_plugin(cfg={'auto_refresh_cookie': True})
        self.plugin._last_cookie_refresh = time.time()
        got = self.run_(self.plugin._refresh_cookie(force=True))
        self.assertFalse(got)

    def test_background_on_use_refresh_does_not_block(self):
        """用即刷必须走后台：_ensure_api 立刻返回。"""
        self.plugin, _ = B.make_plugin()
        self.plugin.api = _FakeApi()
        self.plugin.session = object()
        self.plugin._last_cookie_refresh = 0.0
        self.plugin.cookie_refresh_on_use = 1
        called = []

        async def fake_refresh(force=False):
            called.append(force)
            await asyncio.sleep(0.2)
            return True

        self.plugin._refresh_cookie = fake_refresh
        start = time.monotonic()
        self.run_(self.plugin._ensure_api())
        elapsed = time.monotonic() - start
        self.assertLess(elapsed, 0.1, f"_ensure_api 不应等待刷新（耗时 {elapsed:.3f}s）")
        self.run_(asyncio.sleep(0.3))
        self.assertEqual(called, [False], "刷新应在后台被调用一次")


class _FakeApi:
    async def close(self):
        pass


class TestAdapterRestart(B.LoopTestCase):
    def test_restart_adapter_optin(self):
        ada = B.FakeAdapter('qq_ada')
        self.plugin, self.ctx = B.make_plugin(
            adapters={'qq_ada': ada},
            cfg={'cookie_self_heal_restart_adapter': True, 'cookie_self_heal_reload': False},
        )
        self.plugin._ada_obj = ada
        ok = self.run_(self.plugin._restart_adapter())
        self.assertTrue(ok)
        self.assertIsNone(self.plugin._ada_obj)
        # 重启后注册表里必须是新实例
        self.assertIsNot(self.ctx.adapter_mgr.get_adapter('qq_ada'), ada)

    def test_restart_adapter_disabled_by_default(self):
        self.plugin, self.ctx = B.make_plugin()

        async def trigger():
            self.plugin._schedule_adapter_restart('测试')
            await asyncio.sleep(0.05)

        self.run_(trigger())
        # 默认关闭：适配器实例不变
        self.assertIn('qq_ada', self.ctx.adapter_mgr.get_adapters())

class TestCookieFetchTimeout(B.LoopTestCase):
    """get_cookies 使用独立超时，绝不再被 5s 的通用动作超时误杀。"""

    def test_default_is_25s(self):
        plugin, _ = B.make_plugin(cfg={'cookie_self_heal': False})
        self.assertEqual(plugin.cookie_fetch_timeout, 25)
        self.assertEqual(plugin.onebot_action_timeout, 5)

    def test_override(self):
        plugin, _ = B.make_plugin(
            cfg={'cookie_self_heal': False, 'cookie_fetch_timeout': '40s'}
        )
        self.assertEqual(plugin.cookie_fetch_timeout, 40)

    def test_get_cookies_uses_dedicated_timeout(self):
        plugin, _ = B.make_plugin(
            cfg={'cookie_self_heal': False, 'cookie_fetch_timeout': '40s'}
        )
        seen = {}

        async def fake_call(action, params, timeout=None):
            seen['action'] = action
            seen['timeout'] = timeout
            return {'status': 'ok', 'data': {'cookies': 'uin=o10001; skey=abc; p_skey=def'}}

        plugin._call_onebot_action = fake_call
        got = self.run_(plugin._get_cookie_from_onebot())
        self.assertEqual(got, 'uin=o10001; skey=abc; p_skey=def')
        self.assertEqual(seen['action'], 'get_cookies')
        self.assertEqual(seen['timeout'], 40)
