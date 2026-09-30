"""silent 模式的契约（v1.4.10）：静默只在**指令层**达成，插件不拦截任何消息。

背景（真实故障，2026-09-30）：
    v1.4.9 在 AFTER_XML_PARSE 里对定时任务轮无条件 `actions.clear()`。框架的
    send_xml_messages 是唯一发送入口，解析后**没有**任何撤回/补发通道 ——
    清空 = 这一轮模型产出的全部消息永久丢失。定时发布要跑几分钟，期间 Midflight
    注入的群友插话、模型按引导语产出的回应都落在同一批 actions 里，被一起吞掉。
    这就是用户报的「明明没发空间，消息也消失了」。

v1.4.10 的最终形态：插件**彻底退出消息拦截**。
    - 静默改由指令层达成：silent 轮次明确要求模型不要汇报，需要不说话时输出
      框架原生的 `<msg/>`（解析后 MessageChain 为空，框架直接跳过）；
    - 「关键词压制」那版半吊子实现已删除 —— 它既漏（关键词覆盖不到就照发）、
      又误伤（纯文本含关键词的正常应答会被压掉），两头不讨好，
      却要付出一张永远维护不全的词表。

所以本文件的重点是**钉死「插件不得再碰 AFTER_XML_PARSE」**。
"""
import inspect
import json

import _bootstrap as B

main = B.main
QzonePlugin = main.QzonePlugin


class TestPluginNeverInterceptsMessages(B.LoopTestCase):
    def test_no_after_xml_parse_hook_registered(self):
        """★ 核心回归：插件不得注册 AFTER_XML_PARSE 钩子。

        那是解析后的最后可干预点、也是唯一发送入口；在这里删改 actions
        就是「这一轮的消息永久丢失」，框架没有任何补偿通道。
        """
        names = [name for name, _fn in B._On.hooks]
        self.assertNotIn(
            'after_xml_parse', names,
            '插件不得再注册 AFTER_XML_PARSE 钩子 —— 这是 v1.4.9 吞消息的根因')

    def test_legacy_guard_implementation_is_gone(self):
        """旧的清空/关键词压制实现必须彻底移除，不留半截。"""
        self.assertFalse(hasattr(QzonePlugin, '_silent_task_guard'))
        self.assertFalse(hasattr(QzonePlugin, '_is_task_batch'))
        self.assertFalse(hasattr(QzonePlugin, 'TASK_NARRATION_KEYWORDS'))
        self.assertFalse(hasattr(main, 'TASK_NARRATION_KEYWORDS'))

    def test_source_never_touches_actions(self):
        """源码里不得再出现任何删改 actions 的写法。"""
        src = inspect.getsource(main)
        for bad in ('actions.clear()', 'actions[:] =', 'del actions'):
            self.assertNotIn(bad, src, f'源码里不该再出现 {bad!r}')

    def test_other_hooks_still_registered(self):
        """去掉守卫后，其它钩子必须还在（别误删）。"""
        names = [name for name, _fn in B._On.hooks]
        self.assertIn('im_message', names)
        self.assertIn('llm_request', names)


class TestSilentIsPromptOnly(B.LoopTestCase):
    def _instruction(self, style: str) -> str:
        plug, ctx = B.make_plugin({'task_message_style': style})
        plug.task_group_ids = ['427674145']
        plug.task_private_ids = []
        captured = []

        class _MP:
            async def handle_im_message(self, event):
                captured.append(event)

        ctx.message_processor = _MP()
        ada = ctx.adapter_mgr.get_adapter('qq_ada')
        plug._resolve_ada = lambda: ada
        plug._ada_obj = ada
        ok = self.run_(QzonePlugin._send_task_instruction(
            plug, '【定时任务】请根据最近聊天发布一条说说。', with_place=False))
        self.assertTrue(ok, '指令事件没有投递成功')
        self.assertTrue(captured, '指令没有送进 message_processor')
        return ''.join(e.text for e in captured[-1].message.chain)

    def test_silent_instruction_asks_model_to_stay_quiet(self):
        """silent 轮次的指令必须写明「静默执行」并给出框架原生的 <msg/> 语法。"""
        text = self._instruction('silent')
        self.assertIn('静默执行', text)
        self.assertIn('<msg/>', text)
        self.assertIn('照常调用工具完成任务', text,
                      '给模型的否定约束不能把「要做什么」挤掉')

    def test_notify_instruction_has_no_silence_clause(self):
        """notify 轮次不得附加任何静默要求。"""
        text = self._instruction('notify')
        self.assertNotIn('静默执行', text)
        self.assertNotIn('<msg/>', text)

    def test_silent_clause_allows_replying_to_people(self):
        """静默不等于「不许说话」：指令要允许回应群友，避免模型连插话都不回。"""
        text = self._instruction('silent')
        self.assertIn('回应群友', text)


class TestTaskMarkersStillEmitted(B.LoopTestCase):
    """去掉消息拦截后，任务标记必须保留（配图清单等下游逻辑依赖它们）。"""

    def test_markers_present_in_source(self):
        src = inspect.getsource(main)
        self.assertIn('qzone_publish_task', src)
        self.assertIn('extra={"qzone_task": True', src)


class TestDocsMatchBehaviour(B.LoopTestCase):
    """把这次踩过的坑钉在文档上，防止再次漂移。"""

    def test_changelog_does_not_use_message_id_as_evidence(self):
        txt = (B.ROOT / '更新记录.txt').read_text(encoding='utf-8')
        self.assertNotIn('被清空的轮次 message_id 为空', txt,
                         '已被推翻的 message_id 判据不许再出现在更新记录里')
        self.assertIn('ON_MESSAGE_SENT', txt, '更新记录应给出可靠判据')

    def test_readme_title_version_matches_manifest(self):
        version = json.loads(
            (B.ROOT / 'manifest.json').read_text(encoding='utf-8'))['version']
        first_line = (B.ROOT / 'README.md').read_text(
            encoding='utf-8').splitlines()[0]
        self.assertIn(f'v{version}', first_line,
                      f'README 标题版本号与 manifest({version}) 不一致')

    def test_schema_hint_is_short(self):
        """hint 要简洁（配置面板里显示，太长没人看）。"""
        schema = json.loads(
            (B.ROOT / 'schema.json').read_text(encoding='utf-8'))
        hint = schema['task_message_style']['hint']
        self.assertLessEqual(len(hint), 60, f'hint 过长（{len(hint)} 字）: {hint}')
        self.assertNotIn('message_id', hint)
