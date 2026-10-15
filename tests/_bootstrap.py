"""自检脚手架：在没有 KiraAI 本体的环境下加载插件，并用桩件驱动关键路径。

设计要点：
- 用一个"运行时构造出来的假 core 包"顶替框架导入，避免依赖真实框架；
- 用 package-alias 方式加载 main.py，让它的相对导入（from .qzone...）正常工作；
- 所有网络、VLM、OneBot 调用都由测试注入桩件，绝不发真实请求。
"""
import asyncio
import importlib.util
import os
import pathlib
import sys
import tempfile
import types
from enum import IntEnum

# 宿主时区可能不是 zoneinfo 可用格式（如 LCL-8），会让 APScheduler 初始化失败
os.environ['TZ'] = 'UTC'

ROOT = pathlib.Path(__file__).resolve().parent.parent
TESTS_DIR = pathlib.Path(__file__).resolve().parent

# 在临时目录里跑，避免插件建 data/temp 污染仓库
os.chdir(tempfile.mkdtemp(prefix='qzone_selftest_'))


# --------------------------------------------------------------------------
# 假 core 包
# --------------------------------------------------------------------------
def _mod(name, path=None):
    mod = types.ModuleType(name)
    if path is not None:
        mod.__path__ = list(path)
    sys.modules[name] = mod
    return mod


class Priority(IntEnum):
    SYS_LOW = -100
    LOW = -50
    MEDIUM = 0
    HIGH = 50
    SYS_HIGH = 100


class _On:
    """@on.xxx() 装饰器桩件：原样返回函数，并记录被注册的钩子。"""

    hooks = []

    def __getattr__(self, name):
        def factory(*a, **kw):
            def deco(fn):
                _On.hooks.append((name, fn))
                return fn
            return deco
        return factory


class _ToolRegistry:
    tools = {}

    def __call__(self, **kwargs):
        def deco(fn):
            _ToolRegistry.tools[kwargs.get('name')] = fn
            return fn
        return deco


class BasePlugin:
    def __init__(self, ctx, cfg):
        self.ctx = ctx
        self.cfg = cfg


class LLMRequest:
    def __init__(self, messages=None, **kw):
        self.messages = messages or []
        self.user_prompt = []
        self.system_prompt = []
        self.tool_set = None


class Prompt:
    def __init__(self, text, name=None, source=None, persist=True, render_template=True, **kw):
        self.text = text
        self.name = name
        self.source = source
        self.persist = persist
        self.kwargs = kw


_STUB_IMG_DIR = pathlib.Path(tempfile.mkdtemp(prefix='qzone_stub_img_'))


def _write_stub_image(name='stub.jpg'):
    p = _STUB_IMG_DIR / name
    if not p.exists():
        p.write_bytes(b"\xff\xd8\xff" + b"\x00" * 64)
    return str(p)


class Image:
    """桩件 Image。

    - hash_image() 会记录调用并抛错：框架里它对 URL 型元素会真下载，
      任何热路径（尤其 llm_request 钩子）都不应该碰它；
    - to_path() 返回一个真的本地图片文件，让**后台**识图流程可以正常跑完。
    """

    def __init__(self, image=None, mime=None, name=None, caption=None):
        self.image = image
        self.file = image
        self.mime = mime
        self.name = name
        self.caption = caption
        self.md5 = None
        self.image_type = 'data_url' if str(image).startswith('data:') else 'url'
        self._temp_path = None
        self.hash_image_calls = 0
        self.to_path_calls = 0

    async def hash_image(self):
        self.hash_image_calls += 1
        raise AssertionError('热路径不应调用 Image.hash_image()（框架里它会真下载图片）')

    async def to_path(self):
        self.to_path_calls += 1
        return _write_stub_image()


class Text:
    def __init__(self, text=''):
        self.text = text


class _Simple:
    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


class KiraIMMessage(_Simple):
    pass


class KiraMessageEvent(_Simple):
    pass


class KiraMessageBatchEvent(_Simple):
    def is_group_message(self):
        for m in getattr(self, "messages", None) or []:
            if getattr(m, "group", None) is not None:
                return True
        sess = getattr(self, "session", None)
        return getattr(sess, "session_type", "") == "gm"

    def is_private_message(self):
        return not self.is_group_message()


class MessageChain(list):
    pass


class User(_Simple):
    pass


class Group(_Simple):
    pass


class Session(_Simple):
    pass


def desc_img(client=None, image=None, prompt=None, lang=None):
    raise AssertionError('VLM 桩件未被替换：不应在测试中真调 desc_img')


# 组装 core 包树
_mod('core')
_mod('core.plugin', [])
_mod('core.chat', [])
_mod('core.provider', [])
_mod('core.prompt_manager')
_mod('core.utils')
_mod('core.utils.common_utils')

sys.modules['core.plugin'].BasePlugin = BasePlugin
sys.modules['core.plugin'].register_tool = _ToolRegistry()
sys.modules['core.plugin'].on = _On()
sys.modules['core.plugin'].Priority = Priority
sys.modules['core.chat'].MessageChain = MessageChain
sys.modules['core.chat'].KiraIMMessage = KiraIMMessage
sys.modules['core.chat'].User = User
sys.modules['core.chat'].Group = Group
sys.modules['core.chat'].Session = Session
sys.modules['core.chat.message_utils'] = _mod('core.chat.message_utils')
sys.modules['core.chat.message_utils'].KiraMessageEvent = KiraMessageEvent
sys.modules['core.chat.message_utils'].KiraMessageBatchEvent = KiraMessageBatchEvent
sys.modules['core.chat.message_elements'] = _mod('core.chat.message_elements')
sys.modules['core.chat.message_elements'].Image = Image
sys.modules['core.chat.message_elements'].Text = Text
sys.modules['core.provider'].LLMRequest = LLMRequest
sys.modules['core.prompt_manager'].Prompt = Prompt
sys.modules['core.utils.common_utils'].desc_img = desc_img

# --------------------------------------------------------------------------
# 加载插件
# --------------------------------------------------------------------------
_pkg = types.ModuleType('qzone_plugin')
_pkg.__path__ = [str(ROOT)]
sys.modules['qzone_plugin'] = _pkg
_spec = importlib.util.spec_from_file_location('qzone_plugin.main', ROOT / 'main.py')
main = importlib.util.module_from_spec(_spec)
sys.modules['qzone_plugin.main'] = main
_spec.loader.exec_module(main)

qzone_utils = sys.modules['qzone_plugin.qzone.utils']
qzone_parser = sys.modules['qzone_plugin.qzone.parser']
qzone_image_policy = sys.modules['qzone_plugin.qzone.image_policy']


# --------------------------------------------------------------------------
# 测试用桩件
# --------------------------------------------------------------------------
class FakeResponse:
    def __init__(self, status=200, body=b'', headers=None):
        self.status = status
        self._body = body
        self.headers = headers or {}
        self.content = self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def iter_chunked(self, size):
        for i in range(0, len(self._body), size):
            yield self._body[i:i + size]

    async def read(self):
        return self._body


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kw):
        self.calls.append(url)
        if self.responses:
            return self.responses.pop(0)
        return FakeResponse(status=500, body=b'')

    @property
    def closed(self):
        return False


class FakeAdapter:
    def __init__(self, name='qq_ada', send_result=None, error=None, platform='QQ'):
        self.info = _Simple(name=name, platform=platform, adapter_id='aid-' + name, enabled=True)
        self.config = {"self_id": "10001"}          # 真实适配器有 config（合成事件读 self_id）
        self.message_types = ["text", "img"]
        self.permanently_disconnected = False
        self._send_result = send_result if send_result is not None else {'status': 'ok', 'data': {}}
        self._error = error
        self.sent = []

    def get_client(self):
        return self

    @property
    def websocket(self):
        return object()

    class _Ev:
        def __init__(self, flag):
            self._flag = flag

        def is_set(self):
            return self._flag

        async def wait(self):
            return True

    @property
    def login_success_event(self):
        return FakeAdapter._Ev(True)

    @property
    def shutdown_event(self):
        return FakeAdapter._Ev(False)

    async def send_action(self, action, params, timeout=10.0):
        self.sent.append((action, params))
        if self._error is not None:
            raise self._error
        return self._send_result


class FakeAdapterManager:
    def __init__(self, adapters=None):
        self._adapters = dict(adapters or {})
        # 真实的 AdapterManager 把"已保存的配置(info)"与"运行中的实例"分开存，
        # 所以 stop_adapter 之后 get_adapter_info 依然拿得到 → 桩件必须一致。
        self._infos = {a.info.adapter_id: a.info for a in self._adapters.values()}

    def get_adapter(self, name):
        return self._adapters.get(name)

    def get_adapters(self):
        return self._adapters

    def get_adapter_info(self, adapter_id):
        return self._infos.get(adapter_id)

    async def stop_adapter(self, name):
        self._adapters.pop(name, None)

    async def register_adapter(self, info):
        self._infos[info.adapter_id] = info
        self._adapters[info.name] = FakeAdapter(name=info.name)


class FakeDb:
    def __init__(self):
        self.store = {}

    async def get_image_desc_cache(self, md5):
        item = self.store.get(md5)
        return dict(item) if item else None

    async def add_image_desc_cache(self, md5, description, count=1, last_seen=0):
        self.store[md5] = {'md5': md5, 'description': description, 'count': count, 'last_seen': last_seen}

    async def update_image_desc_cache(self, md5, description=None, count=None, last_seen=None):
        item = self.store.setdefault(md5, {'md5': md5, 'description': '', 'count': 0, 'last_seen': 0})
        if description is not None:
            item['description'] = description
        if count is not None:
            item['count'] = count
        if last_seen is not None:
            item['last_seen'] = last_seen
        return True


class FakePluginManager:
    def __init__(self):
        self.reloads = []

    async def reload(self, plugin_id):
        self.reloads.append(plugin_id)


class FakeProviderManager:
    """提供默认 VLM 桩件（返回非 None 即可，真正的 desc_img 由测试打桩）。"""

    def get_default_vlm(self):
        return object()


class _FakeEventBus:
    """镜像真框架接线：lifecycle 把 KiraMessageEvent 订阅到处理管线。

    真框架里 `ctx.event_bus.publish(event)` 会路由给 message_processor.handle_event
    （2.x）/ workflow.handle_event（3.0）；测试里统一委托给当前 ctx.message_processor
    的 handle_im_message（各用例会把它替换成自己的记录桩）。
    """

    def __init__(self, ctx):
        self._ctx = ctx

    async def publish(self, event):
        mp = self._ctx.message_processor
        handler = getattr(mp, "handle_im_message", None)
        if handler is not None:
            return await handler(event)
        raise AssertionError("event_bus.publish 被调用，但 message_processor 未就绪")


class FakeCtx:
    def __init__(self, adapters=None, data_dir=None):
        self.adapter_mgr = FakeAdapterManager(adapters)
        self.db = FakeDb()
        self.plugin_mgr = FakePluginManager()
        self.provider_mgr = FakeProviderManager()
        self.persona_mgr = None
        self.message_processor = None
        self.event_bus = _FakeEventBus(self)
        self._data_dir = pathlib.Path(data_dir or tempfile.mkdtemp(prefix='qzone_data_'))

    def get_plugin_data_dir(self):
        return self._data_dir

    def get_llm_client(self, model_uuid=None, llm_type=None):
        return None

    def get_default_fast_llm_client(self):
        return None


DEFAULT_CFG = {
    # 既有用例按"每轮注入"的旧语义写；on_demand 由专门用例覆盖
    'manifest_inject_mode': 'always',
    'auto_refresh_cookie': True,
    'image_manifest_enabled': True,
    'image_manifest_count': 5,
    'auto_publish_image_dedupe_interval': '3d',
}


def make_plugin(cfg=None, adapters=None, data_dir=None):
    merged = dict(DEFAULT_CFG)
    merged.update(cfg or {})
    if adapters is None:
        adapters = {'qq_ada': FakeAdapter()}
    ctx = FakeCtx(adapters, data_dir)
    plugin = main.QzonePlugin(ctx, merged)
    return plugin, ctx


def run(coro):
    """在独立事件循环里跑一段协程（每次新建，避免 loop 复用带来的串扰）。"""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        try:
            loop.run_until_complete(loop.shutdown_asyncgens())
        except Exception:
            pass
        loop.close()


import unittest as _unittest


class LoopTestCase(_unittest.TestCase):
    """同一个事件循环跑完整个用例：后台任务才能跨多次 run 真正跑完。

    每个用例结束会取消并回收仍挂起的任务，避免"Task was destroyed but pending"。
    """

    def setUp(self):
        super().setUp()
        self.loop = asyncio.new_event_loop()

    def tearDown(self):
        pending = [t for t in asyncio.all_tasks(self.loop) if not t.done()]
        for task in pending:
            task.cancel()
        if pending:
            self.loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        try:
            self.loop.run_until_complete(self.loop.shutdown_asyncgens())
        except Exception:
            pass
        self.loop.close()
        self.loop = None
        super().tearDown()

    def run_(self, coro):
        return self.loop.run_until_complete(coro)


def patch_shared_session(responses):
    """把 qzone.utils 的共享会话换成脚本化的假会话。"""
    session = FakeSession(responses)

    async def _get():
        return session

    qzone_utils._get_shared_session = _get
    return session


def patch_fetch_bytes(func):
    """替换 main 里已导入的 fetch_bytes（直接改模块属性才生效）。"""
    main.fetch_bytes = func


def patch_desc_img(func):
    main.desc_img = func


def fetch_result(ok, data=None, status=None, reason=''):
    return qzone_utils.FetchResult(ok, data=data, status=status, reason=reason)
