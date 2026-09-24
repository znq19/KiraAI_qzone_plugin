import asyncio
import base64
import hashlib
import html
import json
import logging
import random
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Optional, List

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from core.plugin import BasePlugin, register_tool, on, Priority
from core.chat.message_utils import KiraMessageEvent, KiraMessageBatchEvent
from core.chat.message_elements import Image, Text
from core.chat import MessageChain, KiraIMMessage, User, Group, Session
from core.prompt_manager import Prompt
from core.provider import LLMRequest

from .qzone.api import QzoneAPI
from .qzone.session import QzoneSession
from .qzone.utils import (
    clean_url,
    close_shared_session,
    fetch_bytes,
    is_safe_public_url,
    looks_like_image,
)
from .qzone.image_policy import (
    build_instruction as build_image_instruction,
    candidate_label,
    dedupe_sources,
    draw_target as draw_image_target,
    resolve_described_sources,
)
from .qzone.parser import QzoneParser
from .qzone.model import Post as QzonePost, Comment as QzoneComment

try:
    from core.utils.common_utils import desc_img
except Exception:  # 核心路径变更时不影响插件加载
    desc_img = None

logger = logging.getLogger(__name__)


def _read_head_bytes(path, size: int = 16) -> bytes:
    with open(path, "rb") as f:
        return f.read(size)


def _to_float(value, default: float = 0.0) -> float:
    try:
        if value is None or isinstance(value, bool):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default

MAX_HISTORY = 10
MAX_REPLIED_CACHE = 1000
IMAGE_REGISTRY_CAP = 20
# 图片去重历史容量：与图片清单共用 20 条会被很快挤掉（3 天窗口内可能发十几条说说），
# 导致"去重间隔 3d"形同虚设。这里给去重历史独立的、足够大的容量。
DEDUPE_HISTORY_MAX = 500
# 去重历史保留时长下限（即使去重间隔配得很短，也至少留这么久）
DEDUPE_HISTORY_MIN_TTL = 7 * 86400
# 图片清单保留的会话数上限（防止 _image_registry 按会话无限增长）
IMAGE_REGISTRY_MAX_SESSIONS = 50
# URL 映射表容量上限
URL_MD5_MAX = 500
# 近期图片短缓存 TTL（避免同一轮内重复拉取 OneBot 历史）
RECENT_IMAGES_TTL = 20.0
# 框架只在"处理过这张图"时才会写 caption：
# - 常规模式写的是 VLM 文字描述；
# - native 模式写的是占位串（如 "attached image"）—— 那不算文字描述，但图确实随消息
#   发给了多模态模型（她亲眼看过），所以**一样算"框架处理过的图"**，可以进清单；
#   展示就按原样，不擅自改写。
# 状态落盘节流（合并多次写，异步落盘）
STATE_FLUSH_DELAY = 2.0
# 清单"刚注入过"的有效期（秒）。钩子每轮都会维护该标记（注入时置位、未注入时清除），
# 这里只是安全兜底，避免异常路径下标记残留导致 want_images 被长期忽略。
MANIFEST_FRESH_TTL = 300.0

# 启动/重载时的凭证重试退避（绝对时刻，不是累加 sleep）
STARTUP_RETRY_DELAYS = (15, 30, 60, 120)
# 软重置后等待恢复的观察窗口（秒），超时才升级到"真重载"
SELF_HEAL_PROBE_TIMEOUT = 60.0

# 登录失效后强制刷新的最小间隔（防失效风暴）
FORCE_REFRESH_MIN_INTERVAL = 3
# 常规刷新失败冷却：失败后该秒数内不再主动骚扰 OneBot（避免 get 风暴），
# 但下个用即刷/周期任务到来时会自动再试，OneBot 恢复后即可续回来。
REFRESH_THROTTLE = 10


class QzonePlugin(BasePlugin):
    def __init__(self, ctx, cfg):
        super().__init__(ctx, cfg)
        self.cfg = cfg
        self.cookies_str = cfg.get("cookies_str", "")
        self.qq_ada = cfg.get("qq_ada", "")
        self.auto_refresh = cfg.get("auto_refresh_cookie", True)
        self.timeout = cfg.get("timeout", 10)
        self.temp_dir = Path(cfg.get("temp_dir", "data/temp"))
        self.temp_dir.mkdir(parents=True, exist_ok=True)

        # 主人白名单
        master_ids_str = cfg.get("master_ids", "")
        self.master_ids = [x.strip() for x in master_ids_str.split(",") if x.strip()]
        # 代码层权限检查总开关（关闭后完全依赖 persona 提示词层控权，AI 任何主动调用都放行）
        self.master_check_enabled = cfg.get("master_check_enabled", False)

        # 解析通用任务目标
        task_group_ids_str = cfg.get("task_group_ids", "")
        self.task_group_ids = [x.strip() for x in task_group_ids_str.split(",") if x.strip()]
        task_private_ids_str = cfg.get("task_private_ids", "")
        self.task_private_ids = [x.strip() for x in task_private_ids_str.split(",") if x.strip()]
        self.task_message_style = cfg.get("task_message_style", "silent")

        # 后台模式数据源
        self.auto_publish_group_id = cfg.get("auto_publish_group_id", "")
        self.auto_publish_user_id = cfg.get("auto_publish_user_id", "")
        self.auto_publish_image_prob = self._clamp_float(cfg.get("auto_publish_image_prob", 1.0), 0.0, 1.0)
        self.auto_publish_image_min = max(0, int(cfg.get("auto_publish_image_min", 0) or 0))
        self.auto_publish_image_max = max(self.auto_publish_image_min, int(cfg.get("auto_publish_image_max", 3) or 3))
        self.auto_publish_image_fallback = bool(cfg.get("auto_publish_image_fallback", False))
        self.auto_publish_image_dedupe_interval = self._parse_interval_seconds(
            cfg.get("auto_publish_image_dedupe_interval", "3d"), default_unit="h"
        )

        self.session: Optional[QzoneSession] = None
        self.api: Optional[QzoneAPI] = None
        self.my_uin: Optional[int] = None
        # 自己的昵称缓存（从 OneBot get_stranger_info 懒加载；用于点赞列表展示自己昵称，防「我」昵称诈骗）
        self._my_nickname: str = ""

        # 调度器构造受宿主机时区配置影响，可能抛异常；而 __init__ 抛异常会被框架
        # 判定为"插件加载失败"而整个禁用。这里兜住：失败只是没有定时任务，其它功能照常。
        self.scheduler = self._new_scheduler()

        # 定时配置
        self.auto_publish_schedule = cfg.get("auto_publish_schedule", "")
        self.auto_comment_schedule = cfg.get("auto_comment_schedule", "")
        self.auto_reply_schedule = cfg.get("auto_reply_schedule", "")
        self.auto_reply_enabled = cfg.get("auto_reply_enabled", False)
        self.like_when_comment = cfg.get("like_when_comment", False)

        # 写操作透明节流与自动点赞延迟（"0.5-1.5s" 格式，解析为 (min, jitter)）
        self.action_interval_min, self.action_interval_jitter = self._parse_delay_range(
            cfg.get("action_interval", "0.5-1.5s")
        )
        self.like_delay_min, self.like_delay_jitter = self._parse_delay_range(
            cfg.get("like_delay", "0.5-1.5s")
        )
        # 评论后回读确认开关（诊断模式，默认关）：只写日志提示，不阻断成功判定
        self.comment_verify = bool(cfg.get("comment_verify", False))
        # QQ 号黑白名单（全插件功能生效，默认空 = 不限制）
        self.qzone_blacklist = [
            x.strip() for x in str(cfg.get("qzone_blacklist", "") or "").split(",") if x.strip()
        ]
        self.qzone_whitelist = [
            x.strip() for x in str(cfg.get("qzone_whitelist", "") or "").split(",") if x.strip()
        ]
        # view 展示点赞人昵称的数量上限（默认 5，模拟空间页「xx等人觉得很赞」）
        try:
            self.like_users_display_max = max(1, int(cfg.get("like_users_display_max", 5) or 5))
        except (TypeError, ValueError):
            self.like_users_display_max = 5

        # 旧定时配置（向后兼容）
        self.auto_publish_cron = cfg.get("auto_publish_cron", "")
        self.auto_comment_cron = cfg.get("auto_comment_cron", "")
        self.auto_reply_cron = cfg.get("auto_reply_cron", "")

        self.auto_publish_trigger_dict = self._parse_schedule(self.auto_publish_schedule) if self.auto_publish_schedule else None
        self.auto_comment_trigger_dict = self._parse_schedule(self.auto_comment_schedule) if self.auto_comment_schedule else None
        self.auto_reply_trigger_dict = self._parse_schedule(self.auto_reply_schedule) if self.auto_reply_schedule else None

        self.max_comments_per_cycle = cfg.get("max_comments_per_cycle", 3)
        self.max_replies_per_cycle = cfg.get("max_replies_per_cycle", 5)

        # Cookie 周期刷新间隔（秒），0/None 表示不周期刷
        # 注意 default_unit="s"：文档写明"支持 2h、30m、7200（秒）"，
        # 因此裸数字按秒解释（旧实现按分钟，用户填 7200 实际成了 5 天）。
        self.cookie_refresh_interval = self._parse_interval_seconds(
            cfg.get("cookie_refresh_interval", "2h"), default_unit="s"
        )
        # 用即刷节流：调用空间功能时，若距上次刷新超过该间隔则顺手刷新（秒），0/None 关闭
        self.cookie_refresh_on_use = self._parse_interval_seconds(cfg.get("cookie_refresh_on_use", "10m"))

        # 图片识图相关配置
        self.image_manifest_enabled = cfg.get("image_manifest_enabled", True)
        self.image_manifest_count = max(1, int(cfg.get("image_manifest_count", 5) or 5))
        # ---- 非阻塞识图 / 自愈相关配置 ----
        self.image_fetch_fail_ttl = self._parse_interval_seconds(
            cfg.get("image_fetch_fail_ttl", "10m"), default_unit="m"
        ) or 600
        self.image_download_timeout = max(5, int(cfg.get("image_download_timeout", 20) or 20))
        # 清单注入策略：on_demand（默认，只在确实要发说说时注入）/ always（旧行为）
        mode = str(cfg.get("manifest_inject_mode", "on_demand") or "on_demand").strip().lower()
        self.manifest_inject_mode = mode if mode in ("on_demand", "always") else "on_demand"
        # 展示用描述的长度上限（0 = 不截断）
        self.image_desc_max_chars = max(0, int(cfg.get("image_desc_max_chars", 80) or 0))
        # OneBot 动作默认超时（秒）
        self.onebot_action_timeout = self._parse_interval_seconds(
            cfg.get("onebot_action_timeout", "5s"), default_unit="s"
        ) or 5
        # Cookie 连接自愈
        self.cookie_self_heal = bool(cfg.get("cookie_self_heal", True))
        self.cookie_self_heal_threshold = max(1, int(cfg.get("cookie_self_heal_threshold", 3) or 3))
        self.cookie_self_heal_reload = bool(cfg.get("cookie_self_heal_reload", True))
        self.cookie_self_heal_reload_cooldown = self._parse_interval_seconds(
            cfg.get("cookie_self_heal_reload_cooldown", "10m"), default_unit="m"
        ) or 600
        self.cookie_self_heal_restart_adapter = bool(cfg.get("cookie_self_heal_restart_adapter", False))
        # 软重置后等待恢复的观察窗口（超时才升级到"真重载"）
        self.self_heal_probe_timeout = float(self._parse_interval_seconds(
            cfg.get("cookie_self_heal_probe", "60s"), default_unit="s"
        ) or SELF_HEAL_PROBE_TIMEOUT)
        self.visitor_limit = max(1, min(50, int(cfg.get("visitor_limit", 20) or 20)))
        self.qzone_image_desc_enabled = cfg.get("qzone_image_desc_enabled", True)
        self.auto_comment_image_desc = cfg.get("auto_comment_image_desc", False)
        self.image_desc_model = cfg.get("image_desc_model", "")
        # 吸附模式：发说说未指定图片时自动抓最近一张图（不看内容）
        self.auto_attach_recent_image = cfg.get("auto_attach_recent_image", False)
        # 是否允许对自己空间的说说识图
        self.qzone_image_desc_own = cfg.get("qzone_image_desc_own", False)

        self.replied_comments = set()
        self.my_posts_history: List[str] = []
        self.last_auto_publish_time: Optional[datetime] = None
        self._jobs_added = False

        # ---- 后台任务 / 识图调度 / 负缓存 / 自愈状态 ----
        # 统一登记的后台任务（terminate 时统一取消，避免热重载后旧任务继续跑）
        self._bg_tasks: set = set()
        # 识图负缓存：key -> 失败时间戳（TTL 内不再重试、不再刷日志）
        self._desc_failed: dict[str, float] = {}
        # 该会话最近一次"清单被注入到请求里"的时间；用来判断 want_images 是否多余
        self._manifest_fresh_ts: dict[str, float] = {}
        # 图片缓存键 -> 已就绪描述（只放"已知描述"，供免费路径复用）
        self._entry_desc: dict[str, str] = {}
        # 近期图片短缓存：sid -> (时间戳, [url])
        self._recent_images_cache: dict[str, tuple[float, list]] = {}
        # 软重置时摘下来的旧 HTTP 会话（延迟关闭；卸载时兜底关闭）
        self._detached_apis: list = []
        # OneBot 失败状态机
        self._onebot_fail_streak = 0
        self._onebot_state = "ok"
        self._onebot_fail_since = 0.0
        # 自愈/重载互斥与冷却
        self._resetting = False
        self._reload_inflight = False
        self._last_auto_reload_ts = 0.0
        self._refresh_inflight = False
        self._adapter_restart_inflight = False
        self._last_adapter_restart_ts = 0.0
        # 状态落盘节流
        self._state_dirty = False
        self._state_flush_task: Optional[asyncio.Task] = None

        # Cookie 刷新控制
        self._cookie_refresh_lock = asyncio.Lock()
        self._last_cookie_refresh = 0.0
        self._cookie_refresh_task: Optional[asyncio.Task] = None
        # 写操作透明节流状态（只包 sleep，不包 HTTP）
        self._write_lock = asyncio.Lock()
        self._last_write_ts = 0.0
        # 启动/重载时 get_cookies 失败的延迟自动重试任务（避免重载瞬间 OneBot 未就绪导致插件判死）
        self._startup_retry_task: Optional[asyncio.Task] = None

        # 群名缓存：{gid: (name, timestamp)}
        self._group_name_cache: dict[str, tuple[str, float]] = {}
        # 用户昵称缓存：{uid: (name, timestamp)}
        self._user_name_cache: dict[str, tuple[str, float]] = {}

        # QQ适配器对象
        self._ada_obj = None

        # 初始化失败标记
        self._init_failed = False

        self.backend_llm_model = cfg.get("backend_llm_model", "")
        # 后台模式人设（空 = 跟随 WebUI 当前激活人设）
        self.backend_persona = cfg.get("backend_persona", "")
        self.blackout_schedules = cfg.get("blackout_schedules", [])

        # 近期图片注册表：sid -> [{"elem": Image, "sender": str, "time": int, "desc": Optional[str]}]
        self._image_registry: dict[str, list[dict]] = {}

        # 空间图片 url -> md5 映射（避免重复下载）
        self._url_md5: dict[str, str] = {}
        # 主动发布成功使用过的图片指纹及时间，仅在发布成功后写入。
        self._published_image_history: list[dict] = []

    @staticmethod
    def _new_scheduler():
        try:
            return AsyncIOScheduler()
        except Exception as e:
            logger.error(f"初始化定时调度器失败（定时任务将不可用，其它功能正常）: {e}")
            return None

    # ---------- 状态持久化 ----------
    def _state_path(self) -> Path:
        return self.ctx.get_plugin_data_dir() / "state.json"

    def _load_state(self):
        try:
            path = self._state_path()
            if not path.exists():
                return
            data = json.loads(path.read_text(encoding="utf-8"))
            self.replied_comments = set(data.get("replied_comments", [])[-MAX_REPLIED_CACHE:])
            self.my_posts_history = list(data.get("my_posts_history", [])[-MAX_HISTORY:])
            self._published_image_history = [
                {
                    "identity": item.get("identity") or item.get("source", ""),
                    "source": item.get("source", ""),
                    "time": _to_float(item.get("time"), 0.0),
                }
                for item in data.get("published_image_history", [])[-DEDUPE_HISTORY_MAX:]
                if item.get("identity") or item.get("source")
            ]
            self._last_auto_reload_ts = _to_float(data.get("last_auto_reload_ts"), 0.0)
            self._prune_dedupe_history()
            logger.info(
                f"已加载持久化状态：历史说说 {len(self.my_posts_history)} 条，"
                f"已回复评论 {len(self.replied_comments)} 条，"
                f"图片去重历史 {len(self._published_image_history)} 条"
            )
        except Exception as e:
            logger.warning(f"加载插件状态失败: {e}")

    def _save_state(self, force: bool = False):
        """标记状态待落盘。

        默认节流合并（多条状态变更合并成一次异步写盘），避免每次评论/回复/发布
        都在事件循环里同步写文件；force=True 用于插件卸载前强制落盘。
        """
        self._state_dirty = True
        self._schedule_state_flush(immediate=force)

    def _schedule_state_flush(self, immediate: bool = False):
        # 无事件循环时留给下一次或 terminate 强制落盘
        task = self._state_flush_task
        if task is not None and not task.done() and not immediate:
            return  # 已有待执行的合并写，直接搭车
        self._state_flush_task = self._spawn_task(
            self._flush_state_later(0.0 if immediate else STATE_FLUSH_DELAY)
        )

    async def _flush_state_later(self, delay: float = STATE_FLUSH_DELAY):
        try:
            if delay:
                await asyncio.sleep(delay)
            await self._flush_state()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"状态落盘失败: {e}")

    async def _flush_state(self):
        if not self._state_dirty:
            return
        self._state_dirty = False
        payload = {
            "replied_comments": list(self.replied_comments)[-MAX_REPLIED_CACHE:],
            "my_posts_history": self.my_posts_history[-MAX_HISTORY:],
            "published_image_history": self._prune_dedupe_history(),
            "last_auto_reload_ts": self._last_auto_reload_ts,
        }
        try:
            await asyncio.to_thread(self._write_state_file, self._state_path(), payload)
        except Exception as e:
            logger.warning(f"保存插件状态失败: {e}")
            self._state_dirty = True

    @staticmethod
    def _write_state_file(path: Path, payload: dict):
        """原子写入（先写临时文件再替换），避免异常中断留下半截 JSON。"""
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)

    def _track_task(self, task: asyncio.Task) -> asyncio.Task:
        """登记后台任务，terminate 时统一取消。"""
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)
        return task

    def _spawn_task(self, coro, track: bool = True):
        """在当前事件循环里起一个后台任务。

        没有运行中的事件循环时安全降级（返回 None），不让"起不来任务"这种
        边缘情况把调用方整条链路打挂。
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.debug("当前没有运行中的事件循环，已跳过后台任务创建")
            close = getattr(coro, "close", None)
            if callable(close):
                close()  # 明确关闭，避免 "coroutine was never awaited"
            return None
        task = loop.create_task(coro)
        return self._track_task(task) if track else task

    @staticmethod
    def _clamp_float(value, minimum: float, maximum: float) -> float:
        try:
            return max(minimum, min(maximum, float(value)))
        except (TypeError, ValueError):
            return minimum

    def _draw_auto_publish_image_target(self) -> int:
        """为一次定时发布抽取配图目标；0 表示交给 AI 自主决定。"""
        return draw_image_target(
            self.auto_publish_image_min,
            self.auto_publish_image_max,
        )

    def _auto_publish_image_instruction(self, target: int) -> str:
        return build_image_instruction(target, self.auto_publish_image_max)

    @staticmethod
    def _dedupe_sources(sources: list[str]) -> list[str]:
        return dedupe_sources(sources)

    def _scheduled_publish_policy(self, event) -> tuple[int, int] | None:
        """读取合成定时发布事件的任务级配图策略，普通发布不受影响。"""
        for message in getattr(event, "messages", None) or []:
            extra = getattr(message, "extra", None) or {}
            if not extra.get("qzone_publish_task"):
                continue
            try:
                target = int(extra["qzone_target_image_count"])
                maximum = int(extra["qzone_max_image_count"])
            except (KeyError, TypeError, ValueError):
                return None
            return max(0, min(target, maximum)), max(0, maximum)
        return None

    async def _fill_scheduled_publish_sources(
        self,
        sid: str,
        selected: list[str],
        target: int,
    ) -> list[str]:
        """用当前会话清单补足自动任务的正数目标；候选耗尽后允许自然降级。"""
        selected = self._dedupe_sources(selected)
        if target <= 0 or len(selected) >= target:
            return selected[:target] if target > 0 else selected
        entries = self._manifest_entries(sid, apply_dedupe=True)
        for index in range(1, len(entries) + 1):
            resolved = await self._resolve_manifest_images(sid, [index], apply_dedupe=True)
            for source in resolved:
                if source not in selected:
                    selected.append(source)
                    if len(selected) >= target:
                        return selected[:target]
        return selected

    async def _get_persona_content(self) -> str:
        """获取人设内容：配置 backend_persona 时用指定人设（id 或名称均可），否则用当前激活人设"""
        try:
            if self.backend_persona:
                persona_info = await self.ctx.persona_mgr.get_persona(self.backend_persona)
                if persona_info is None:
                    # 按名称兑底匹配
                    try:
                        for p in await self.ctx.persona_mgr.list_personas():
                            if p.name == self.backend_persona:
                                persona_info = p
                                break
                    except Exception:
                        pass
                if persona_info is not None:
                    return persona_info.content or ""
                logger.warning(f"配置的人设 {self.backend_persona} 不存在，回退当前激活人设")
            persona_info = await self.ctx.persona_mgr.get_active_persona()
            if persona_info is not None:
                return persona_info.content or ""
        except Exception as e:
            logger.warning(f"获取人设失败: {e}")
        # 旧版兼容兑底
        try:
            return self.ctx.persona_mgr.get_persona() or ""
        except Exception:
            return ""

    # ---------- Cookie 管理 ----------
    async def _refresh_cookie(self, force: bool = False) -> bool:
        """从 OneBot 获取最新 Cookie 并原地更新会话。

        返回 True 表示凭证已更新。失败时保留现有会话（last-good），不置失败标记。
        无论成败都会推进 _last_cookie_refresh（冷却），防止 get_cookies 失败风暴：
        失败后 REFRESH_THROTTLE(10s) 内不再主动骚扰 OneBot，让现有 Cookie 继续干活；
        下个用即刷/周期任务/启动重试到来时会自动再试，OneBot 恢复后即可续回来。
        """
        if not self.auto_refresh:
            return False
        async with self._cookie_refresh_lock:
            now = time.time()
            min_interval = FORCE_REFRESH_MIN_INTERVAL if force else REFRESH_THROTTLE
            if (now - self._last_cookie_refresh) < min_interval:
                # 刚刷新过：不再骚扰 OneBot。这里必须返回 False 表示"本次没有刷新"——
                # 旧实现返回 True（谎报已刷新），会让 HTTP 层拿着旧凭证再重试 4 次
                # （每次 sleep 1.5s）才报错，白白多等好几秒且误导排查。
                logger.debug("距上次刷新过近，跳过实际刷新（交由上层瞬态重试）")
                return False
            new_cookie = await self._get_cookie_from_onebot()
            if not new_cookie:
                # 失败同样推进冷却：避免每次调用都打爆 OneBot（get_cookies 超时会阻塞调用链）
                self._last_cookie_refresh = now
                # 失败细节由 _note_onebot_failure 统一按状态机去噪，这里不重复刷屏
                logger.debug("从 OneBot 获取 Cookie 失败，保留现有会话继续工作")
                return False
            try:
                self.cookies_str = new_cookie
                if self.session is not None:
                    await self.session.update_cookies(new_cookie)
                    self.my_uin = (await self.session.get_ctx()).uin
                else:
                    await self._reinit_session()
                self._last_cookie_refresh = now
                self._init_failed = False
                logger.info("已从 OneBot 获取最新 Cookie 并原地更新会话")
                return True
            except Exception as e:
                self._last_cookie_refresh = now
                logger.error(f"应用新 Cookie 失败: {e}")
                return False

    async def _retry_startup_cookie(self):
        """启动/重载时 get_cookies 失败后的延迟自动重试（15s/30s/60s/120s 递增，最多 4 次）。

        重载瞬间 OneBot 适配器常尚未就绪（login_success_event 未触发），get_cookies 快速失败；
        该任务在后台按递增间隔重试，OneBot 一恢复即可续回，成功后自动停止。
        """
        try:
            started = time.monotonic()
            for attempt, delay in enumerate(STARTUP_RETRY_DELAYS, 1):
                # 绝对时刻调度：旧实现是累加 sleep（实际 15/45/105/225s），
                # 与日志/文档宣称的 15/30/60/120s 不符，自愈窗口被拖长一倍。
                remain = (started + delay) - time.monotonic()
                if remain > 0:
                    await asyncio.sleep(remain)
                if self.session is not None and not self._init_failed:
                    return  # 会话已就绪（可能被其它路径恢复），无需再试
                try:
                    if await self._refresh_cookie(force=False):
                        logger.info(f"启动 Cookie 延迟重试成功（第 {attempt} 次，累计 {delay}s）")
                        return
                except Exception as e:
                    logger.debug(f"启动 Cookie 延迟重试失败（第 {attempt} 次）: {e}")
        except asyncio.CancelledError:
            return
        except Exception as e:
            logger.warning(f"启动 Cookie 延迟重试任务异常退出: {e}")

    async def _handle_auth_expired(self) -> bool:
        """提供给 QzoneHttpClient 的登录失效回调"""
        return await self._refresh_cookie(force=True)

    async def _check_session_alive(self) -> bool:
        """用轻量接口验证当前会话是否仍可用"""
        if self.api is None:
            return False
        try:
            resp = await self.api.get_visitor()
            return bool(resp.ok)
        except Exception:
            return False

    async def _ensure_api(self):
        """确保 API 可用：健康则直接返回；按需刷新/重建，失败时验证旧会话兜底"""
        if self.api is not None and not self._init_failed:
            # 非阻塞原则：已有可用会话就**立刻返回**，"用即刷"挪到后台执行。
            # 旧实现在这里 await 刷新（最长 5s 的 get_cookies 超时 + 锁等待），
            # 每次工具调用都要先陪等一次，纯属把延迟加到用户头上。
            self._schedule_refresh()
            return
        if self.auto_refresh:
            try:
                if await self._refresh_cookie(force=False):
                    return
            except Exception as e:
                logger.error(f"刷新 Cookie 异常: {e}")
        if self.api is None:
            # 启动自愈窗口内（后台重试任务仍在跑）：明确提示稍后再试，
            # 不要用空 Cookie 反复构建注定失败的会话。
            if (
                self.auto_refresh
                and self._startup_retry_task is not None
                and not self._startup_retry_task.done()
            ):
                raise RuntimeError("QQ空间 Cookie 正在后台自动获取中，请稍后再试")
            await self._reinit_session()
            return
        # api 存在但曾被标记失败：先验证旧会话是否其实还可用
        if self._init_failed:
            if await self._check_session_alive():
                logger.info("现有 QQ 空间会话验证通过，继续使用")
                self._init_failed = False
                return
            raise RuntimeError("QQ空间会话不可用：Cookie 失效且自动刷新失败，请检查 OneBot 连接或手动更新 Cookie")

    async def _reinit_session(self):
        """根据当前 self.cookies_str 构建 session 和 api（仅在尚无会话时调用）"""
        if self.api:
            try:
                await self.api.close()
            except Exception as e:
                logger.warning(f"关闭旧 API 时出错: {e}")
            self.api = None
            self.session = None
        try:
            config = type("Config", (), {
                "cookies_str": self.cookies_str,
                "timeout": self.timeout
            })()
            self.session = QzoneSession(config)
            self.api = QzoneAPI(self.session, config)
            self.api.on_auth_expired = self._handle_auth_expired
            ctx = await self.session.get_ctx()
            self.my_uin = ctx.uin
            # 空/无效 Cookie 也能构造出 session，但解析不到 uin 时接口全部不可用；
            # 必须在这里显式失败并保持 _init_failed，让 _ensure_api 持续走刷新路径自愈，
            # 避免"空会话假成功"导致功能静默不可用。
            if not self.my_uin:
                raise RuntimeError("Cookie 中未解析到有效 QQ 号（uin），会话不可用")
            logger.info(f"QQ空间 API 初始化成功，当前账号: {self.my_uin}")
            self._init_failed = False
        except Exception as e:
            logger.error(f"初始化失败: {e}")
            self._init_failed = True
            raise

    def _schedule_refresh(self):
        """后台执行"用即刷"（去重 + 节流），绝不阻塞调用方。"""
        if not self.auto_refresh or self.cookie_refresh_on_use is None:
            return
        if self._refresh_inflight or self._resetting:
            return
        if (time.time() - self._last_cookie_refresh) <= self.cookie_refresh_on_use:
            return
        self._refresh_inflight = True

        async def _run():
            try:
                await self._refresh_cookie(force=False)
            except Exception as e:
                logger.debug(f"后台用即刷失败（不影响调用）: {e}")
            finally:
                self._refresh_inflight = False

        self._spawn_task(_run())

    # ---------- 连接自愈（等价"重载一次"的效果） ----------
    def _trigger_self_heal(self, reason: str):
        """在后台触发连接自愈；调用方零阻塞。"""
        if not self.auto_refresh or not self.cookie_self_heal or self._resetting:
            return
        self._spawn_task(self._self_heal(reason))

    async def _self_heal(self, reason: str):
        if self._resetting:
            return
        self._resetting = True
        try:
            logger.info(f"开始连接自愈（{reason}）")
            await self._soft_reset_connection(reason)
            if await self._wait_cookie_recovered(self.self_heal_probe_timeout):
                logger.info("连接自愈成功（软重置后凭证已恢复）")
                return
            if self.cookie_self_heal_reload:
                # 首选"插件重载"：完全复刻 WebUI 那一次点击，且不中断 QQ 消息
                self._schedule_hard_reload(reason)
            elif self.cookie_self_heal_restart_adapter:
                # 用户显式关闭重载时才用适配器重启兜底（会短暂中断消息）
                self._schedule_adapter_restart(reason)
            else:
                logger.warning("连接自愈未恢复，且自动重载已关闭；将继续按周期重试")
        except Exception as e:
            logger.error(f"连接自愈过程异常: {e}")
        finally:
            self._resetting = False

    async def _soft_reset_connection(self, reason: str = ""):
        """软重置：等价于 WebUI"插件重载"对插件侧状态的影响，但不重载模块。

        只清插件自己缓存的、会失效的外部状态；不动用户的配置与业务数据。
        """
        self._ada_obj = None                      # 1. 丢弃适配器引用（下次重新解析）
        # 2. 摘掉旧会话。注意：**不要当场 close** —— 此刻可能还有在途请求
        #   （例如正在发布/评论的 HTTP 调用），当场关掉会把它们打断。
        #   改为交给后台延迟关闭，插件卸载时也会兜底关。
        old_api, self.api = self.api, None
        self.session = None
        if old_api is not None:
            self._detached_apis.append(old_api)
            self._spawn_task(self._close_api_later(old_api))
        self._init_failed = False                 # 2. 清失败标记
        self._last_cookie_refresh = 0.0           # 3. 解除节流，允许立刻重试
        self._recent_images_cache.clear()         # 4. 清与会话绑定的短缓存
        self._cancel_startup_retry()              # 5. 重排启动退避重试
        self._startup_retry_task = self._spawn_task(self._retry_startup_cookie())
        logger.info(f"已执行连接软重置（{reason or '手动'}），正在后台重新获取凭证")

    async def _close_api_later(self, api, delay: float = None):
        """延迟关闭被摘掉的旧会话：给在途请求留出跑完的时间，避免打断它们。"""
        try:
            await asyncio.sleep(self.timeout * 2 if delay is None else delay)
        except asyncio.CancelledError:
            # 被取消（例如插件卸载）：这里**不能**把 api 从登记表摘掉，
            # 否则 terminate 的兜底循环就找不到它、会话永远不会被关闭。
            raise
        try:
            await api.close()
        except Exception as e:
            logger.debug(f"关闭旧会话失败: {e}")
        finally:
            if api in self._detached_apis:
                self._detached_apis.remove(api)

    def _cancel_startup_retry(self):
        task = self._startup_retry_task
        if task is not None and not task.done():
            task.cancel()
        self._startup_retry_task = None

    async def _wait_cookie_recovered(self, timeout: float) -> bool:
        """软重置后等待恢复：拿到可用会话即视为恢复。"""
        deadline = time.time() + max(0.0, timeout)
        while True:
            if self.session is not None and not self._init_failed:
                return True
            remain = deadline - time.time()
            if remain <= 0:
                return False
            await asyncio.sleep(min(0.25, remain))

    def _schedule_hard_reload(self, reason: str):
        """真重载：完全复刻 WebUI 的"插件重载"。

        两个必须遵守的约束（否则会自杀式取消/重载风暴）：
        1. 必须在**独立 task**里发起，且不登记到 _bg_tasks —— reload 会调用本插件的
           terminate()，若在插件自己的任务里 await 它，会把自己一起取消掉；
        2. 冷却时间**持久化**到 state.json，跨重载生效。
        """
        mgr = getattr(self.ctx, "plugin_mgr", None)
        if mgr is None:
            logger.warning("无法获取 plugin_mgr，跳过自动重载（已做软重置）")
            return
        if self._reload_inflight:
            return
        cooldown = self.cookie_self_heal_reload_cooldown
        now = time.time()
        if self._last_auto_reload_ts and (now - self._last_auto_reload_ts) < cooldown:
            logger.info(
                f"距上次自动重载不足 {int(cooldown)}s，改为继续后台重试（避免重载风暴）"
            )
            return
        self._reload_inflight = True
        self._last_auto_reload_ts = now
        self._save_state(force=True)             # 冷却持久化

        async def _run():
            await asyncio.sleep(0)               # 先让当前调用栈退出
            try:
                logger.warning(f"连接自愈未恢复，执行插件自动重载（{reason}）")
                await mgr.reload("qzone_plugin")
            except Exception as e:
                logger.error(f"插件自动重载失败: {e}")
            finally:
                # 无论成败都解锁：成功的重载会终结本实例；万一 reload 没真正生效
                # （例如插件目录缺失），也不至于把后续重试永久锁死。
                self._reload_inflight = False

        self._spawn_task(_run(), track=False)   # 独立 task，不登记（否则会被自身 terminate 取消）

    def _schedule_adapter_restart(self, reason: str):
        """最后手段：重启 QQ 适配器（真正重建 socket）。

        代价：该适配器上所有 QQ 消息会短暂中断数秒，所以默认关闭，
        且必须满足冷却与"仍是同一个适配器"两个前提。
        """
        if not self.cookie_self_heal_restart_adapter or self._adapter_restart_inflight:
            return
        cooldown = self.cookie_self_heal_reload_cooldown
        if self._last_adapter_restart_ts and (time.time() - self._last_adapter_restart_ts) < cooldown:
            return
        self._adapter_restart_inflight = True
        self._last_adapter_restart_ts = time.time()

        async def _run():
            try:
                await self._restart_adapter()
            except Exception as e:
                logger.error(f"适配器重启失败: {e}")
            finally:
                self._adapter_restart_inflight = False

        self._spawn_task(_run())

    async def _restart_adapter(self) -> bool:
        mgr = self.ctx.adapter_mgr
        ada = self._ada_obj or self._resolve_ada()
        info = getattr(ada, "info", None) if ada else None
        if info is None:
            return False
        name = getattr(info, "name", "") or self.qq_ada
        adapter_id = getattr(info, "adapter_id", None)
        # 只在"仍是同一个适配器"时动手，避免把刚被新建的适配器干掉
        if mgr.get_adapter(name) is not ada:
            return False
        logger.warning(f"执行适配器重启（{name}），期间该适配器消息会短暂中断")
        await mgr.stop_adapter(name)
        fresh = mgr.get_adapter_info(adapter_id) if adapter_id else None
        if fresh is None:
            logger.error(f"适配器 {name} 的配置已不存在，无法重启")
            return False
        await mgr.register_adapter(fresh)
        self._ada_obj = None
        self._last_cookie_refresh = 0.0
        logger.info(f"适配器 {name} 已重启，将重新解析并刷新凭证")
        return True

    async def _cookie_refresh_loop(self):
        """周期刷新 Cookie（带 ±10% jitter），不干扰正常调用"""
        try:
            while True:
                interval = self.cookie_refresh_interval or 6 * 3600
                jittered = interval * random.uniform(0.9, 1.1)
                await asyncio.sleep(max(300, jittered))
                try:
                    await self._refresh_cookie(force=False)
                except Exception as e:
                    logger.warning(f"周期刷新 Cookie 失败: {e}")
        except asyncio.CancelledError:
            return

    @staticmethod
    def _parse_interval_seconds(s, default_unit: str = "m") -> Optional[int]:
        """解析 3d/6h/30m/7200 等间隔；空或 0 返回 None。"""
        if s is None:
            return None
        s = str(s).strip().lower()
        if not s or s in ("0", "0s", "0m", "0h", "0d"):
            return None
        m = re.match(r"^(\d+(?:\.\d+)?)([dhms]?)$", s)
        if not m:
            logger.warning(f"无法解析间隔: {s}")
            return None
        val = float(m.group(1))
        unit = m.group(2) or default_unit
        multipliers = {"d": 86400, "h": 3600, "m": 60, "s": 1}
        val *= multipliers[unit]
        return int(val) if val > 0 else None

    @staticmethod
    def _parse_delay_range(s, default: str = "0.5-1.5s") -> tuple[float, float]:
        """解析 '0.5-1.5s' / '1-2' 为 (min, jitter)；解析失败回退默认值。"""
        def _split(text: str):
            text = str(text or "").strip().lower()
            if text.endswith("s"):
                text = text[:-1]
            if "-" in text:
                lo, hi = text.split("-", 1)
                return float(lo), float(hi)
            return None
        for cand in (s, default):
            try:
                r = _split(cand)
                if r is not None and r[0] >= 0 and r[1] >= r[0]:
                    return r[0], r[1] - r[0]
            except (TypeError, ValueError):
                continue
        return 0.5, 1.0

    async def _throttle_write(self):
        """写操作透明节流：仅延迟发送时机，绝不拒绝/跳过/失败，AI 无感知。

        锁只包 sleep 段（毫秒级），不包 HTTP 请求，不同会话的写操作互不卡死；
        框架工具调用本身串行，锁不会死锁。相邻写操作实际间隔 = 最小间隔 + 随机抖动。
        """
        async with self._write_lock:
            now = time.monotonic()
            wait = self.action_interval_min - (now - self._last_write_ts)
            if wait > 0:
                await asyncio.sleep(wait)
            await asyncio.sleep(random.uniform(0, self.action_interval_jitter))
            self._last_write_ts = time.monotonic()

    def _target_block_reason(self, target_id) -> Optional[str]:
        """校验目标 QQ 是否被黑白名单限制（全插件功能生效）。

        返回 None=放行；返回 str=拒绝原因。黑名单最优先（含自己）；
        白名单非空时仅放行名单内 + 自己（除非自己进黑名单）。
        """
        target = str(target_id or "").strip()
        if not target:
            return None
        if target in self.qzone_blacklist:
            return "该 QQ 已被加入插件黑名单，禁止操作"
        if self.qzone_whitelist and target not in self.qzone_whitelist:
            if self.my_uin and target == str(self.my_uin):
                return None
            return "该 QQ 不在插件白名单内，禁止操作"
        return None

    def _resolve_ada(self):
        """解析并返回**当前**的 QQ 适配器实例（兼容新版 KiraAI）。

        关键：不再长期缓存适配器对象。框架在适配器配置更新/重启时会 new 一个新实例
        覆盖注册表（core/adapter/adapter_registry.py:547），长期缓存旧对象会让插件
        握着一个已废弃的连接——请求发出去永远等不到应答，表现为"请求 get_cookies 超时"，
        且只有"插件重载"才能恢复。这里每次重新解析并做身份校验，成本仅一次 dict 查找。
        """
        ada = None
        ada_name = self.qq_ada
        if ada_name:
            ada = self.ctx.adapter_mgr.get_adapter(ada_name)
            if ada is None:
                logger.warning(f"未找到配置的适配器: {ada_name}，将自动查找")
        if ada is None:
            ada = self._find_qq_adapter()
        if ada is None:
            self._ada_obj = None
            return None
        if self._ada_obj is not ada:
            logger.info(
                f"QQ 适配器{'已更新' if self._ada_obj is not None else '已就绪'}: "
                f"{getattr(getattr(ada, 'info', None), 'name', ada_name)}"
            )
        self._ada_obj = ada
        return ada

    def _find_qq_adapter(self):
        """自动查找第一个平台为 QQ 的适配器（兼容新旧框架）。"""
        try:
            adapters = None
            if hasattr(self.ctx.adapter_mgr, 'get_adapters'):
                adapters = self.ctx.adapter_mgr.get_adapters()
            elif hasattr(self.ctx.adapter_mgr, '_adapters'):
                adapters = self.ctx.adapter_mgr._adapters
            if not adapters:
                logger.error("未找到平台为 QQ 的适配器，无法调用 OneBot 接口")
                return None
            for name, ada in adapters.items():
                if hasattr(ada, 'info') and ada.info.platform == "QQ":
                    if self._ada_obj is not ada:
                        logger.info(f"自动找到 QQ 适配器: {name}")
                    return ada
            logger.error("未找到平台为 QQ 的适配器，无法调用 OneBot 接口")
        except Exception as e:
            logger.error(f"查找 QQ 适配器时出错: {e}")
        return None

    async def _call_onebot_action(self, action: str, params: dict, timeout: float = None):
        """调用 OneBot 动作（带失败状态机与现场诊断）。

        成功/失败会被计数；连续失败到阈值时在**后台**触发连接自愈，
        调用方本身零阻塞（自愈不占用本次调用链）。
        """
        timeout = self.onebot_action_timeout if timeout is None else timeout
        ada = self._resolve_ada()
        if not ada:
            raise RuntimeError("无法获取 QQ 适配器")
        if getattr(ada, "permanently_disconnected", False):
            # 框架已明确标记"重连次数耗尽"，不必再陪着重试到超时
            self._note_onebot_failure(action, "适配器已永久断开（NapCat 重连次数用尽）")
            raise RuntimeError(
                "NapCat 连接已永久失败（重连次数用尽），请检查 NapCat 是否在运行、ws_uri/token 是否正确"
            )
        ob_client = ada.get_client()
        # NapCat send_action 第一步会硬编码等待 login_success_event（最长 10s），
        # 而该事件仅在收到 lifecycle 元事件时 set——NapCat 偶发不发送 lifecycle 时，
        # 所有 send_action 都会白白卡满 10 秒。这里预检：事件未触发则快速失败，
        # 避免 OneBot 偶发抽风拖死插件整条调用链。
        login_ev = getattr(ob_client, "login_success_event", None)
        if login_ev is not None and not login_ev.is_set():
            try:
                # 给 1 秒窗口：若此刻事件即将触发则让它通过，否则快速失败
                await asyncio.wait_for(login_ev.wait(), timeout=1)
            except asyncio.TimeoutError:
                self._note_onebot_failure(action, "OneBot 登录成功事件未触发", ob_client=ob_client)
                raise TimeoutError("OneBot 登录成功事件未触发（适配器可能未就绪或 NapCat 未发送 lifecycle）")
        try:
            res = await ob_client.send_action(action, params, timeout=timeout)
        except Exception as e:
            self._note_onebot_failure(action, str(e), ob_client=ob_client)
            raise
        self._note_onebot_success()
        return res

    # ---------- OneBot 健康状态机（日志只在"跳变"时打，避免刷屏） ----------
    def _note_onebot_success(self):
        if self._onebot_fail_streak or self._onebot_state != "ok":
            lasted = time.time() - self._onebot_fail_since if self._onebot_fail_since else 0.0
            suffix = f"，持续 {int(lasted)}s" if lasted else ""
            logger.info(f"OneBot 连接已恢复（此前连续失败 {self._onebot_fail_streak} 次{suffix}）")
        self._onebot_fail_streak = 0
        self._onebot_state = "ok"
        self._onebot_fail_since = 0.0

    def _note_onebot_failure(self, action: str, reason: str, ob_client=None):
        self._onebot_fail_streak += 1
        if not self._onebot_fail_since:
            self._onebot_fail_since = time.time()
        streak = self._onebot_fail_streak
        threshold = self.cookie_self_heal_threshold
        if streak == 1:
            self._onebot_state = "degraded"
            logger.info(f"OneBot 调用失败（{action}）：{reason}；已转入后台自动重试，现有会话继续工作")
        elif streak == threshold:
            self._onebot_state = "down"
            logger.warning(
                f"OneBot 调用连续失败 {streak} 次（{action}）：{reason}"
                f"｜诊断：{self._onebot_diagnostics(action, ob_client)}"
            )
            if self.auto_refresh and self.cookie_self_heal:
                self._trigger_self_heal(f"连续 {streak} 次 OneBot 调用失败")
        elif streak > threshold:
            # 已经失败过阈值：每再失败 threshold 次重新触发一次自愈。
            # 否则自愈只在"恰好第 threshold 次"尝试一次，失败后就再也不会重试，
            # 插件会一直卡在不可用状态直到某次偶然成功。
            if self.auto_refresh and self.cookie_self_heal and streak % threshold == 0:
                logger.info(f"OneBot 仍未恢复（连续 {streak} 次），再次触发连接自愈")
                self._trigger_self_heal(f"连续 {streak} 次 OneBot 调用失败")
            logger.debug(f"OneBot 仍不可用（连续 {streak} 次）：{reason}")
        else:
            self._onebot_state = "degraded"
            logger.debug(f"OneBot 调用失败（第 {streak} 次）：{reason}")

    def _onebot_diagnostics(self, action: str, ob_client=None) -> str:
        """现场诊断串：一次复现即可判定主因（陈旧引用 / 传输层假死 / 未就绪）。"""
        ada = self._ada_obj
        name = getattr(getattr(ada, "info", None), "name", "") or self.qq_ada
        try:
            current = self.ctx.adapter_mgr.get_adapter(name) if name else None
        except Exception:
            current = None
        client = ob_client if ob_client is not None else (ada.get_client() if ada else None)
        ws = getattr(client, "websocket", None)
        login_ev = getattr(client, "login_success_event", None)
        shutdown_ev = getattr(client, "shutdown_event", None)
        try:
            names = list(self.ctx.adapter_mgr.get_adapters().keys())
        except Exception:
            names = "n/a"
        return (
            f"action={action} 引用一致={ada is current} "
            f"websocket={'已连接' if ws is not None else '未连接'} "
            f"login_event={login_ev.is_set() if login_ev is not None else 'n/a'} "
            f"shutdown_event={shutdown_ev.is_set() if shutdown_ev is not None else 'n/a'} "
            f"连续失败={self._onebot_fail_streak} 适配器列表={names}"
        )

    async def _get_cookie_from_onebot(self) -> Optional[str]:
        try:
            # send_action 默认超时 10s；Cookie 刷新属"锦上添花"，给 5s 快速失败，
            # 避免 OneBot 繁忙时把整条发布/评论链路拖死。
            data = await self._call_onebot_action(
                "get_cookies", {"domain": "user.qzone.qq.com"}, timeout=self.onebot_action_timeout
            )
            if data.get("status") != "ok":
                logger.debug(f"oneBot 返回错误: {str(data)[:200]}")
                return None
            cookie_str = data.get("data", {}).get("cookies")
            if not cookie_str:
                logger.error("返回数据中未找到 cookies 字段")
                return None
            logger.info("成功从 oneBot 获取 Cookie")
            return cookie_str
        except TimeoutError as e:
            # 区分：OneBot 未就绪（login_success_event 未触发）≠ Cookie 失效。
            # 现有会话可能完全可用，只是拿不到新的——保留现有会话继续干活，
            # 下个用即刷/周期任务会再试，OneBot 恢复即续回，无需开关适配器。
            # OneBot 未就绪 ≠ Cookie 失效：现有会话可能完全可用，只是拿不到新的。
            # 失败本身已由 _note_onebot_failure 按状态机去噪，这里只留 debug。
            logger.debug(f"从 OneBot 获取 Cookie 超时（保留现有会话继续工作）: {e}")
            return None
        except Exception as e:
            logger.debug(f"从 OneBot 获取 Cookie 失败: {e}")
            return None

    # ---------- 插件生命周期 ----------
    async def initialize(self):
        self._load_state()

        refresh_ok = False
        if self.auto_refresh:
            try:
                refresh_ok = await self._refresh_cookie(force=True)
            except Exception as e:
                logger.warning(f"启动时刷新 Cookie 失败: {e}")
                refresh_ok = False
            # 启动/重载瞬间 OneBot 适配器可能尚未就绪（login_success_event 未触发），
            # get_cookies 会快速失败——此时不要直接判死：安排延迟自动重试，
            # OneBot 恢复后几十秒内自动续回，无需手动重载。
            if not refresh_ok and self.session is None:
                if self._startup_retry_task is None or self._startup_retry_task.done():
                    self._startup_retry_task = asyncio.create_task(self._retry_startup_cookie())
                    logger.info("启动 Cookie 获取失败，已安排延迟自动重试（15s/30s/60s/120s 递增，最多 4 次）")

        if self.session is None:
            if not self.cookies_str:
                if self._startup_retry_task is None or self._startup_retry_task.done():
                    logger.error("未提供 Cookie 且自动刷新不可用，插件功能将不可用直至 Cookie 就绪")
                    self._init_failed = True
            else:
                try:
                    await self._reinit_session()
                except Exception as e:
                    logger.error(f"初始化 API 失败: {e}")

        if self.auto_refresh and self.cookie_refresh_interval is not None:
            self._cookie_refresh_task = asyncio.create_task(self._cookie_refresh_loop())

        await self._setup_scheduled_jobs()
        logger.info("QQ空间插件初始化完成")

    async def terminate(self):
        try:
            if self._cookie_refresh_task and not self._cookie_refresh_task.done():
                self._cookie_refresh_task.cancel()
                try:
                    await self._cookie_refresh_task
                except asyncio.CancelledError:
                    pass
            self._cookie_refresh_task = None

            if self._startup_retry_task and not self._startup_retry_task.done():
                self._startup_retry_task.cancel()
                try:
                    await self._startup_retry_task
                except asyncio.CancelledError:
                    pass
            self._startup_retry_task = None

            # 取消所有登记过的后台任务（识图、状态落盘、自愈等）：
            # 不取消的话热重载后旧任务仍在跑，还持有旧实例的引用。
            pending = [task for task in self._bg_tasks if not task.done()]
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            self._bg_tasks.clear()
            self._state_flush_task = None

            # 软重置时摘下来的旧会话：延迟关闭任务可能已被取消，这里兜底关掉
            for stale_api in list(self._detached_apis):
                try:
                    await stale_api.close()
                except Exception:
                    pass
            self._detached_apis.clear()

            if self.api:
                await self.api.close()
            self.api = None
            self.session = None
            # 关闭插件共享的 HTTP 会话（图片下载用）
            try:
                await close_shared_session()
            except Exception as e:
                logger.debug(f"关闭共享 HTTP 会话失败: {e}")

            # 卸载前强制把状态落盘：落盘任务刚被取消，脏标记要重新置上，
            # 否则节流窗口内（最多 STATE_FLUSH_DELAY 秒）的状态变更会丢。
            try:
                self._state_dirty = True
                await self._flush_state()
            except Exception as e:
                logger.debug(f"卸载时状态落盘失败: {e}")

            try:
                if self.scheduler is not None:
                    for job in self.scheduler.get_jobs():
                        job.remove()
                    self.scheduler.shutdown(wait=False)
            except Exception:
                pass
            self.scheduler = self._new_scheduler()
            self._jobs_added = False
            logger.info("QQ空间插件已停止")
        except Exception as e:
            logger.error(f"停止QQ空间插件时出错：{e}")

    # ---------- 黑名单检查（仅定时任务） ----------
    def _is_in_blackout(self) -> bool:
        """检查当前时间是否在配置的黑名单时间段内（仅定时任务调用）"""
        if not self.blackout_schedules:
            return False
        now = datetime.now().time()
        for sched in self.blackout_schedules:
            if not sched or '-' not in sched:
                continue
            parts = sched.split('-')
            try:
                start_str, end_str = parts[0].strip(), parts[1].strip()
                start = datetime.strptime(start_str, "%H:%M").time()
                end = datetime.strptime(end_str, "%H:%M").time()
                if start <= end:
                    if start <= now <= end:
                        return True
                else:
                    if now >= start or now <= end:
                        return True
            except ValueError:
                logger.warning(f"无效的黑名单时间段格式: {sched}")
                continue
        return False

    # ---------- 定时任务 ----------
    async def _setup_scheduled_jobs(self):
        if self._jobs_added:
            logger.warning("定时任务已添加，跳过")
            return
        if self.scheduler is None:
            logger.error("定时调度器不可用，定时任务不会运行（其它功能正常）")
            return

        def add_job(job_func, trigger_dict, cron_fallback, job_id):
            # misfire_grace_time：事件循环被长任务占用时，允许延迟 5 分钟内补跑
            job_kwargs = {"id": job_id, "replace_existing": True,
                          "misfire_grace_time": 300, "coalesce": True}
            if trigger_dict:
                if trigger_dict["mode"] == "cron":
                    trigger = CronTrigger.from_crontab(trigger_dict["expr"])
                else:
                    trigger = IntervalTrigger(
                        seconds=trigger_dict["interval_seconds"],
                        jitter=trigger_dict["jitter_seconds"]
                    )
                self.scheduler.add_job(job_func, trigger, **job_kwargs)
                logger.info(f"定时任务 {job_id} 已调度: {trigger_dict}")
            elif cron_fallback:
                try:
                    trigger = CronTrigger.from_crontab(cron_fallback)
                    self.scheduler.add_job(job_func, trigger, **job_kwargs)
                    logger.info(f"定时任务 {job_id} 已调度 (旧配置): {cron_fallback}")
                except Exception as e:
                    logger.error(f"定时任务 {job_id} 旧配置解析失败: {e}")

        add_job(self._auto_publish_job, self.auto_publish_trigger_dict, self.auto_publish_cron, "auto_publish")
        add_job(self._auto_comment_job, self.auto_comment_trigger_dict, self.auto_comment_cron, "auto_comment")
        if self.auto_reply_enabled:
            add_job(self._auto_reply_job, self.auto_reply_trigger_dict, self.auto_reply_cron, "auto_reply")

        if self.scheduler.get_jobs():
            self.scheduler.start()
            logger.info("定时任务调度器已启动")
            self._jobs_added = True

    async def _get_group_name(self, group_id: str) -> str:
        """获取群名（带 1 小时缓存，失败时返回空串）"""
        cached = self._group_name_cache.get(group_id)
        if cached and time.time() - cached[1] < 3600:
            return cached[0]
        name = ""
        try:
            res = await self._call_onebot_action("get_group_info", {"group_id": int(group_id)})
            if res and res.get("status") == "ok":
                name = res.get("data", {}).get("group_name", "") or ""
        except Exception as e:
            logger.debug(f"获取群名失败 ({group_id}): {e}")
        self._group_name_cache[group_id] = (name, time.time())
        return name

    async def _get_user_nickname(self, user_id: str) -> str:
        """获取用户昵称（带 1 小时缓存，失败时返回空串）"""
        cached = self._user_name_cache.get(user_id)
        if cached and time.time() - cached[1] < 3600:
            return cached[0]
        name = ""
        try:
            res = await self._call_onebot_action("get_stranger_info", {"user_id": int(user_id)})
            if res and res.get("status") == "ok":
                name = res.get("data", {}).get("nickname", "") or ""
        except Exception as e:
            logger.debug(f"获取用户昵称失败 ({user_id}): {e}")
        self._user_name_cache[user_id] = (name, time.time())
        return name

    async def _send_task_instruction(
        self,
        instruction_text: str,
        with_place: bool = True,
        task_extra: Optional[dict] = None,
    ) -> bool:
        """发送定时任务指令（合成内部事件，带 qzone_task 标记供 silent 模式识别）

        with_place=False 用于评论/回复任务：操作对象是空间说说，与会话场合无关，
        附加场合信息反而会误导 AI。
        """
        targets = []
        for gid in self.task_group_ids:
            targets.append(("gm", gid))
        for uid in self.task_private_ids:
            targets.append(("dm", uid))

        if not targets:
            return False

        if not self._resolve_ada():
            logger.error("无法获取 QQ 适配器，定时任务指令发送失败")
            return False

        session_type, target_id = random.choice(targets)
        # 补充场合信息，避免 AI 搞错任务发生的会话（评论/回复任务不附加）
        if with_place:
            if session_type == "gm":
                group_name = await self._get_group_name(target_id)
                place = f"群「{group_name}」{target_id}" if group_name else f"群 {target_id}"
                instruction_text += f"\n（当前场合：{place}）"
            else:
                nickname = await self._get_user_nickname(target_id)
                place = f"与「{nickname}」{target_id} 的私聊" if nickname else f"与 {target_id} 的私聊"
                instruction_text += f"\n（当前场合：{place}）"

        adapter = self._ada_obj
        adapter_name = adapter.info.name
        sid = f"{adapter_name}:{session_type}:{target_id}"
        group = Group(group_id=target_id) if session_type == "gm" else None
        t = int(time.time())
        event = KiraMessageEvent(
            adapter=adapter.info,
            message_types=adapter.message_types,
            message=KiraIMMessage(
                timestamp=t,
                sender=User(user_id="system_qzone_task", nickname="系统"),
                group=group,
                message_id="system_message",
                self_id=str(adapter.config.get("self_id", "") or ""),
                chain=MessageChain([Text(instruction_text)]),
                is_notice=True,
                is_mentioned=True,
                extra={"qzone_task": True, **(task_extra or {})},
            ),
            timestamp=t,
        )
        event.session = Session(
            adapter_name=adapter_name,
            session_type=session_type,
            session_id=target_id,
        )
        if with_place and self.image_manifest_enabled and not self.auto_attach_recent_image:
            try:
                await self._fetch_history_messages(
                    "group" if session_type == "gm" else "private", target_id, 20
                )
            except Exception as e:
                logger.debug(f"预取定时任务图片候选失败: {e}")
        await self.ctx.message_processor.handle_im_message(event)
        logger.info(f"已向 {sid} 发送指令: {instruction_text[:30]}...")
        return True

    @on.after_xml_parse()
    async def _silent_task_guard(self, event, actions, *_):
        """silent 模式下，定时任务指令触发的回复不发送到群里（工具调用不受影响）"""
        if self.task_message_style != "silent":
            return
        for m in getattr(event, "messages", None) or []:
            extra = getattr(m, "extra", None) or {}
            if extra.get("qzone_task"):
                actions.clear()
                logger.debug("silent 模式：已抑制定时任务指令的群回复")
                return

    # ---------- 带黑名单检查的定时任务 ----------
    async def _auto_publish_job(self):
        if self._is_in_blackout():
            logger.info("当前时间处于黑名单内，跳过自动发布")
            return
        try:
            if self.last_auto_publish_time and (datetime.now() - self.last_auto_publish_time).total_seconds() < 60:
                logger.warning("距离上次自动发布不足60秒，跳过本次自动发布")
                return

            await self._ensure_api()

            target_image_count = self._draw_auto_publish_image_target()
            logger.info(
                "定时自动发布配图目标: target=%s range=%s-%s",
                target_image_count,
                self.auto_publish_image_min,
                self.auto_publish_image_max,
            )
            if self.task_group_ids or self.task_private_ids:
                instruction = (
                    "【定时任务】请根据最近聊天发布一条说说，自然一点，不要提及这是定时任务。"
                    + self._auto_publish_image_instruction(target_image_count)
                    + "配图时用image_indices选择，也可用images传聊天记录里见过的图片URL或本地路径。"
                )
                if await self._send_task_instruction(
                    instruction,
                    task_extra={
                        "qzone_publish_task": True,
                        "qzone_target_image_count": target_image_count,
                        "qzone_max_image_count": self.auto_publish_image_max,
                    },
                ):
                    self.last_auto_publish_time = datetime.now()
                return

            await self._legacy_auto_publish(target_image_count)
            self.last_auto_publish_time = datetime.now()
        except Exception as e:
            logger.error(f"自动发布任务失败: {e}")

    async def _legacy_auto_publish(self, target_image_count: int):
        source_id = None
        source_type = None
        if self.auto_publish_group_id.strip():
            source_id = self.auto_publish_group_id.strip()
            source_type = "group"
        elif self.auto_publish_user_id.strip():
            source_id = self.auto_publish_user_id.strip()
            source_type = "private"

        context_messages = []
        if source_id:
            try:
                context_messages = await self._fetch_chat_history(source_type, source_id, count=10)
                if context_messages:
                    logger.info(f"从 {source_type} {source_id} 获取到 {len(context_messages)} 条消息作为上下文")
            except Exception as e:
                logger.error(f"获取历史失败: {e}")

        system_prompt = await self._get_persona_content()
        if self.my_posts_history:
            history_str = "\n".join([f"- {post}" for post in self.my_posts_history[-5:]])
            system_prompt += f"\n\n你最近发布的说说是：\n{history_str}"

        if context_messages:
            history_text = "\n".join(context_messages)
            prompt = f"根据以下最近对话，生成一条QQ空间说说（20-50字），要符合你的人设：\n{history_text}"
        else:
            prompt = "请生成一条QQ空间说说，内容可以是心情、日常、段子，20-50字，要符合你的人设。"

        image_urls: list[str] = []
        should_offer_images = (
            source_id
            and (
                target_image_count > 0
                or (
                    self.auto_publish_image_prob > 0
                    and random.random() < self.auto_publish_image_prob
                )
            )
        )
        if should_offer_images:
            fetch_count = max(self.auto_publish_image_max, target_image_count)
            candidates = await self._fetch_recent_images(source_type, source_id, max_count=fetch_count)
            candidates = [url for url in candidates if not self._is_recently_published_image(url)]

            # 只用"已知描述"（免费路径）：没有描述的候选直接排除，
            # 绝不为了配图去识图。顺序与 candidates 保持一致。
            candidates_with_desc = []
            for url in candidates:
                desc = await self._lookup_cached_url_desc(url)
                if desc:
                    candidates_with_desc.append(
                        (url, candidate_label(desc, self.image_desc_max_chars))
                    )
            if candidates_with_desc:
                if target_image_count > 0:
                    choice_rule = (
                        f"本次必须选择恰好{target_image_count}个不同序号；"
                        "候选不足时选择全部可用候选。"
                    )
                else:
                    choice_rule = (
                        f"可按内容自主选择0至{self.auto_publish_image_max}个不同序号；"
                        "不适合配图时不要输出 IMG 行。"
                    )
                prompt += (
                    "\n\n以下是最近聊天中出现的图片及内容描述：\n"
                    + "\n".join(f"{i}. {desc}" for i, (_, desc) in enumerate(candidates_with_desc, 1))
                    + "\n"
                    + choice_rule
                    + "正文后另起一行输出 IMG:序号 或 IMG:序号,序号。"
                )
                text_with_choice = await self._call_llm(prompt, system_prompt, use_backend_model=True)
                text, chosen = self._split_img_choices(text_with_choice)
                image_urls = resolve_described_sources(
                    [url for url, _ in candidates_with_desc],
                    chosen,
                    target_image_count,
                    self.auto_publish_image_max,
                )
                if target_image_count > 0 and len(image_urls) < target_image_count:
                    logger.info(
                        "自动发布图片目标降级: target=%s usable=%s",
                        target_image_count,
                        len(image_urls),
                    )
            else:
                if target_image_count > 0:
                    logger.info(
                        "自动发布图片目标降级为纯文字: target=%s 无可用候选",
                        target_image_count,
                    )
                text = await self._call_llm(prompt, system_prompt, use_backend_model=True)
        else:
            text = await self._call_llm(prompt, system_prompt, use_backend_model=True)

        if not text:
            logger.warning("LLM生成内容为空，跳过自动发布")
            return

        await self._publish(text, image_urls, allow_image_drop=True)
        self._add_post_to_history(text)
        logger.info(f"自动发布说说成功: {text} (图片数: {len(image_urls)})")

    @staticmethod
    def _split_img_choices(text: str) -> tuple[str, list[int]]:
        """解析末尾 IMG:1 或 IMG:1,3 选择行。"""
        if not text:
            return "", []
        matches = re.findall(r"^\s*IMG\s*[:：]\s*([\d\s,，]+)\s*$", text, re.M | re.I)
        if not matches:
            return text.strip(), []
        chosen = []
        for part in re.split(r"[,，\s]+", matches[-1].strip()):
            if part.isdigit():
                chosen.append(int(part))
        cleaned = re.sub(r"^\s*IMG\s*[:：]\s*[\d\s,，]+\s*$", "", text, flags=re.M | re.I).strip()
        return cleaned, chosen

    async def _auto_comment_job(self):
        if self._is_in_blackout():
            logger.info("当前时间处于黑名单内，跳过自动评论")
            return
        try:
            await self._ensure_api()
            if self.task_group_ids or self.task_private_ids:
                instruction = "【评论任务】请对最近的好友（不包括自己）说说进行评论，自然一点和简洁（0-15字内）。严禁内容重复和复读。注意，检查用户昵称来不要评论自己发布的QQ说说，优先没有评论过的内容，该内容时间戳与当前系统时间戳不得超过7天，否则不评论。"
                await self._send_task_instruction(instruction, with_place=False)
                return
            await self._legacy_auto_comment()
        except Exception as e:
            logger.error(f"自动评论任务失败: {e}")

    async def _legacy_auto_comment(self):
        try:
            posts = await self._get_feeds(target_id=None, num=20)
            if not posts:
                return
            # 不评论自己的说说
            posts = [p for p in posts if not self.my_uin or p.uin != self.my_uin]
            # 黑白名单过滤：黑名单作者跳过；白名单非空时只评论白名单内作者
            posts = [p for p in posts if not self._target_block_reason(str(p.uin))]
            if not posts:
                return
            selected = random.sample(posts, min(self.max_comments_per_cycle, len(posts)))
            for post in selected:
                prompt = f"根据以下说说内容，生成一条简洁评论（0-15字）：\n{post.text}"
                # 可选：识图后评论
                if self.auto_comment_image_desc and post.images and self.qzone_image_desc_enabled:
                    desc = await self._describe_image_url(post.images[0])
                    if desc:
                        prompt += f"\n该说说配图内容：{desc}"
                comment_text = await self._call_llm(prompt, await self._get_persona_content(), use_backend_model=True)
                if not comment_text:
                    continue
                try:
                    result = await self._comment(post, comment_text)
                    logger.info(f"自动评论成功: {post.tid} -> {comment_text}")
                except Exception as e:
                    logger.warning(f"自动评论失败: {post.tid} -> {e}")
                    continue
                if self.like_when_comment and "未重复提交" not in result:
                    # 透明延迟：随机 0.5~1.5s 后再点赞，错开连续请求特征
                    await asyncio.sleep(
                        random.uniform(self.like_delay_min, self.like_delay_min + self.like_delay_jitter)
                    )
                    like_resp = await self.api.like(post, abstime=post.create_time)
                    if like_resp.ok:
                        logger.info(f"自动点赞成功: {post.tid}")
                    else:
                        logger.warning(f"自动点赞失败: {post.tid} -> {like_resp.message}")
                # 评论间随机间隔 0.5~1.5s（原固定 2s，改为随机更自然）
                await asyncio.sleep(
                    random.uniform(self.action_interval_min, self.action_interval_min + self.action_interval_jitter)
                )
        except Exception as e:
            logger.error(f"自动评论任务失败: {e}")

    async def _auto_reply_job(self):
        if self._is_in_blackout():
            logger.info("当前时间处于黑名单内，跳过自动回复")
            return
        try:
            await self._ensure_api()
            if self.task_group_ids or self.task_private_ids:
                instruction = "【回复任务】请回复你最近说说下的新评论，使用qzone_reply_comment和评论自身的ID、UIN准确回复，target_id为自己的QQ号。自然一点和简洁（0-15字内），严禁内容重复和复读。根据评论作者UIN不回复自己，优先没有回复过的用户和新回复，否则不回复。"
                await self._send_task_instruction(instruction, with_place=False)
                return
            await self._legacy_auto_reply()
        except Exception as e:
            logger.error(f"自动回复任务失败: {e}")

    async def _legacy_auto_reply(self):
        try:
            if not self.my_uin:
                logger.error("无法获取当前账号的QQ号")
                return
            my_uin_str = str(self.my_uin)
            posts = await self._get_feeds(target_id=my_uin_str, num=10)
            if not posts:
                return
            new_replies = 0
            for post in posts:
                detail_resp = await self.api.get_detail(post)
                if not detail_resp.ok:
                    continue
                parsed_posts = QzoneParser.parse_feeds([detail_resp.data])
                if not parsed_posts:
                    continue
                full_post = parsed_posts[0]
                for comment in full_post.comments:
                    if comment.uin == self.my_uin:
                        continue
                    reply_key = f"{full_post.tid}:{comment.tid}:{comment.uin}"
                    if reply_key in self.replied_comments:
                        continue
                    _, prompt_content = self._parse_comment_content(comment.content)
                    prompt = f"用户 {comment.nickname} 评论了你的说说：{prompt_content}，请生成一条简洁回复（0-15字）。"
                    reply_text = await self._call_llm(prompt, await self._get_persona_content(), use_backend_model=True)
                    if not reply_text:
                        continue
                    root_comment = self._find_root_comment(full_post.comments, comment)
                    resp = await self.api.reply(full_post, comment, reply_text, root_comment=root_comment)
                    if not resp.ok:
                        logger.warning(f"自动回复失败: {comment.tid}/{comment.uin} -> {resp.message}")
                        continue
                    logger.info(f"自动回复成功: {comment.tid}/{comment.uin} -> {reply_text}")
                    self.replied_comments.add(reply_key)
                    self._save_state()
                    new_replies += 1
                    # 回复间随机间隔 0.5~1.5s，避免连续回复特征
                    await asyncio.sleep(
                        random.uniform(self.action_interval_min, self.action_interval_min + self.action_interval_jitter)
                    )
                    if new_replies >= self.max_replies_per_cycle:
                        break
                if new_replies >= self.max_replies_per_cycle:
                    break
            logger.info(f"自动回复任务完成，共回复 {new_replies} 条新评论")
        except Exception as e:
            logger.error(f"自动回复任务失败: {e}")

    def _register_current_event_images(self, event: KiraMessageBatchEvent):
        """登记本轮消息里的图片（同步、无 IO，绝不识图）。

        此时框架已跑完 message_format_to_text（它在 ON_LLM_REQUEST 钩子之前），
        所以本轮每张图都已经有 elem.caption / elem.md5 —— 我们只取这个免费的描述。
        """
        if not self.image_manifest_enabled or self.auto_attach_recent_image:
            return
        sid = getattr(event, "sid", "")
        if not sid:
            return
        registry = self._image_registry.setdefault(sid, [])
        for message in getattr(event, "messages", None) or []:
            sender = ""
            sender_obj = getattr(message, "sender", None)
            if sender_obj is not None:
                sender = sender_obj.nickname or str(sender_obj.user_id or "未知")
            for elem in getattr(message, "chain", None) or []:
                if not isinstance(elem, Image):
                    continue
                if any(e.get("elem") is elem for e in registry):
                    continue
                entry = {"elem": elem, "sender": sender,
                         "time": int(getattr(message, "timestamp", 0) or time.time()),
                         "desc": getattr(elem, "caption", None),
                         "msg_id": getattr(message, "message_id", None)}
                registry.append(entry)
        if len(registry) > IMAGE_REGISTRY_CAP:
            del registry[: len(registry) - IMAGE_REGISTRY_CAP]
        self._prune_image_registry()

    async def _fetch_history_messages(
        self,
        source_type: str,
        source_id: str,
        count: int = 20,
    ) -> List[dict]:
        """读取 OneBot 群聊或私聊历史，并将其中的图片 URL 登记为候选。"""
        action = "get_group_msg_history" if source_type == "group" else "get_friend_msg_history"
        key = "group_id" if source_type == "group" else "user_id"
        result = await self._call_onebot_action(action, {key: int(source_id), "count": count})
        if not result or result.get("status") != "ok":
            logger.error(f"获取历史失败: {result}")
            return []
        messages = result.get("data", {}).get("messages", []) or []
        if self.image_manifest_enabled and not self.auto_attach_recent_image:
            sid = f"qq:{'gm' if source_type == 'group' else 'dm'}:{source_id}"
            registry = self._image_registry.setdefault(sid, [])
            for msg in messages:
                sender_data = msg.get("sender") or {}
                sender = sender_data.get("nickname") or str(sender_data.get("user_id") or "未知")
                msg_id = msg.get("message_id")
                timestamp = int(msg.get("time") or time.time())
                for seg in msg.get("message") or []:
                    if seg.get("type") != "image":
                        continue
                    data = seg.get("data") or {}
                    url = html.unescape(str(data.get("url") or "").strip().strip('"').strip("'"))
                    if url and not any(e.get("url") == url for e in registry):
                        entry = {"source": "url", "url": url, "sender": sender,
                                 "time": timestamp, "desc": None, "msg_id": msg_id}
                        registry.append(entry)
            if len(registry) > IMAGE_REGISTRY_CAP:
                del registry[: len(registry) - IMAGE_REGISTRY_CAP]
            self._prune_image_registry()
        return messages[-count:]

    async def _fetch_chat_history(self, source_type: str, source_id: str, count: int = 10) -> List[str]:
        messages = await self._fetch_history_messages(source_type, source_id, max(count, 20))
        return [
            f"{msg.get('sender', {}).get('nickname', '未知')}: {self._extract_text_simple(msg.get('message', []))}"
            for msg in messages[-count:]
        ]

    async def _fetch_recent_images(self, source_type: str, source_id: str, max_count: int = 1) -> List[str]:
        """从历史登记候选并返回最新图片 URL（兼容吸附模式）。

        带短 TTL 缓存：同一轮里多次调用不再重复拉取 OneBot 历史。
        """
        cache_key = f"{source_type}:{source_id}:{max_count}"
        cached = self._recent_images_cache.get(cache_key)
        if cached and (time.time() - cached[0]) < RECENT_IMAGES_TTL:
            return list(cached[1])
        messages = await self._fetch_history_messages(source_type, source_id, 20)
        urls = []
        for msg in reversed(messages):
            for seg in msg.get("message") or []:
                if seg.get("type") == "image":
                    url = html.unescape(str((seg.get("data") or {}).get("url") or "").strip().strip('"').strip("'"))
                    if url:
                        urls.append(url)
                        if len(urls) >= max_count:
                            break
            if len(urls) >= max_count:
                break
        if len(self._recent_images_cache) > IMAGE_REGISTRY_MAX_SESSIONS:
            self._recent_images_cache.clear()
        self._recent_images_cache[cache_key] = (time.time(), list(urls))
        return urls

    async def _fetch_recent_images_for_event(self, event: KiraMessageBatchEvent, max_count: int = 1) -> List[str]:
        """从触发事件的会话中获取最近图片 URL（兜底配图）"""
        session_type = None
        session_id = None
        if event.is_group_message():
            session_type = "group"
            for m in getattr(event, "messages", None) or []:
                if m.group and m.group.group_id:
                    session_id = str(m.group.group_id)
                    break
        else:
            session_type = "private"
            for m in getattr(event, "messages", None) or []:
                if m.sender and m.sender.user_id and not str(m.sender.user_id).startswith("system"):
                    session_id = str(m.sender.user_id)
                    break
        if not session_id:
            session = getattr(event, "session", None)
            raw_session_id = getattr(session, "session_id", None)
            raw_session_type = getattr(session, "session_type", None)
            if raw_session_id:
                session_id = str(raw_session_id)
                session_type = "group" if raw_session_type == "gm" else "private"
        if not session_id:
            return []
        return await self._fetch_recent_images(session_type, session_id, max_count)

    def _extract_text_simple(self, message_list: List[dict]) -> str:
        texts = []
        for seg in message_list:
            if seg.get("type") == "text":
                texts.append(seg.get("data", {}).get("text", ""))
        return " ".join(texts)

    # ---------- 图片识图（复用核心 image_desc_cache + desc_img） ----------
    def _get_vlm_client(self):
        """获取图片描述用的 VLM 客户端：优先配置的模型，否则默认 VLM"""
        if self.image_desc_model:
            try:
                client = self.ctx.get_llm_client(model_uuid=self.image_desc_model)
            except TypeError:
                client = self.ctx.get_llm_client(self.image_desc_model)
            if client is not None:
                return client
            logger.warning(f"配置的识图模型 {self.image_desc_model} 不可用，回退默认 VLM")
        try:
            return self.ctx.provider_mgr.get_default_vlm()
        except Exception as e:
            logger.error(f"无法获取默认 VLM: {e}")
            return None

    async def _cache_get_desc(self, md5: str) -> str:
        try:
            cached = await self.ctx.db.get_image_desc_cache(md5)
            if cached and cached.get("description"):
                return cached["description"]
        except Exception as e:
            logger.warning(f"读取图片描述缓存失败: {e}")
        return ""

    async def _cache_set_desc(self, md5: str, desc: str):
        try:
            now = int(time.time())
            existing = await self.ctx.db.get_image_desc_cache(md5)
            if existing:
                await self.ctx.db.update_image_desc_cache(
                    md5, description=desc, count=(existing.get("count") or 0) + 1, last_seen=now
                )
            else:
                await self.ctx.db.add_image_desc_cache(md5, desc, count=1, last_seen=now)
        except Exception as e:
            logger.warning(f"写入图片描述缓存失败: {e}")

    async def _describe_image_bytes(self, data: bytes, source_url: str = "") -> str:
        """描述图片：md5 查核心缓存 -> 未命中调 VLM -> 写回缓存"""
        if not data:
            return ""
        md5 = hashlib.md5(data).hexdigest()
        if source_url:
            self._remember_image_identity(source_url, md5)
        cached = await self._cache_get_desc(md5)
        if cached:
            if source_url:
                self._entry_desc[f"url:{clean_url(source_url)}"] = cached
            return cached
        if desc_img is None:
            return ""
        client = self._get_vlm_client()
        if client is None:
            return ""
        try:
            b64 = base64.b64encode(data).decode()
            img = Image(f"data:image/jpeg;base64,{b64}")
            desc = await desc_img(client=client, image=img, prompt=None, lang="zh")
        except Exception as e:
            logger.error(f"VLM 描述图片失败: {e}")
            return ""
        if desc:
            await self._cache_set_desc(md5, desc)
        return desc or ""

    async def _describe_image_url(self, url: str) -> str:
        """描述 URL 图片（内存缓存 → md5 缓存 → 下载 → VLM）。

        失败会写入负缓存：同一张过期图在 TTL 内不再反复下载、也不再刷日志。
        """
        if not url:
            return ""
        key = f"url:{clean_url(url)}"
        cached = self._entry_desc.get(key)
        if cached:
            return cached
        if self._is_desc_failed(key):
            return ""
        known_md5 = self._url_md5.get(url) or self._url_md5.get(clean_url(url))
        if known_md5:
            cached = await self._cache_get_desc(known_md5)
            if cached:
                self._entry_desc[key] = cached
                return cached
        result = await fetch_bytes(url, timeout=self.image_download_timeout)
        if not result.ok:
            self._mark_desc_failed(key, result.reason or "下载失败")
            return ""
        desc = await self._describe_image_bytes(result.data, source_url=url)
        if desc:
            self._entry_desc[key] = desc
            self._prune_entry_caches()
        else:
            self._mark_desc_failed(key, "未取得有效描述")
        return desc

    # ---------- 近期图片清单（manifest） ----------
    @on.im_message(priority=Priority.LOW)
    async def _collect_images(self, event: KiraMessageEvent, *_):
        """观察消息中的图片，登记到会话图片注册表（不改变消息策略）"""
        # 吸附模式开启时彻底回到旧行为，不维护清单
        if not self.image_manifest_enabled or self.auto_attach_recent_image:
            return
        try:
            images = [e for e in event.message.chain if isinstance(e, Image)]
            if not images:
                return
            sid = event.session.sid
            sender = ""
            if event.message.sender:
                sender = event.message.sender.nickname or str(event.message.sender.user_id or "")
            registry = self._image_registry.setdefault(sid, [])
            stamp = int(event.message.timestamp or time.time())
            msg_id = getattr(event.message, "message_id", None)
            for img in images:
                entry = {
                    "elem": img,
                    "sender": sender,
                    "time": stamp,
                    # 保留框架写在元素上的原始 caption（含 native 模式的占位串，
                    # 分类时才知道"框架处理过这张图"）；没有就不进清单
                    "desc": getattr(img, "caption", None),
                    "msg_id": msg_id,
                }
                registry.append(entry)
            if len(registry) > IMAGE_REGISTRY_CAP:
                del registry[: len(registry) - IMAGE_REGISTRY_CAP]
            self._prune_image_registry()
        except Exception as e:
            logger.debug(f"收集图片失败: {e}")

    def _prune_image_registry(self):
        """限制清单保留的会话数，避免 _image_registry 按会话无限增长。"""
        if len(self._image_registry) <= IMAGE_REGISTRY_MAX_SESSIONS:
            return
        overflow = len(self._image_registry) - IMAGE_REGISTRY_MAX_SESSIONS
        for sid in list(self._image_registry)[:overflow]:
            self._image_registry.pop(sid, None)

    def _session_image_mode(self, sid: str) -> str:
        """读该会话的识图模式（框架自己也是这么读的）。

        用来给"框架没写 caption"的 native 图兜底资格：native 模式下图是原样发给
        多模态模型的（她亲眼看过），即使框架以后不写占位串，也仍然应该能进清单。
        """
        try:
            caps = self.ctx.get_session_capabilities(sid) or {}
            rec = caps.get("image_recognition") or {}
            return str(rec.get("mode") or "")
        except Exception:
            return ""

    def _manifest_entries(self, sid: str, apply_dedupe: bool = False) -> list[dict]:
        """取该会话最近 N 张**有框架描述**的图片（注入与序号解析共用同一份）。

        清单的口径（明确约定）：**只列框架已经描述过、bot 真实收到过的图片**。
        - 只走免费路径：读框架写在元素上的 caption / 内存里已知的描述；
        - 拿不到描述 → 这张图**直接不进清单**，既不列表、也不下载、更不识图；
        - 绝不为了"让清单有内容"而识图，也不让"我们自己识出来的图"混进清单。

        apply_dedupe=True 时再剔除去重窗口内已发布过的图片。
        注入（钩子）与序号解析（发布）必须用同一份过滤规则，否则序号会错位。
        """
        registry = self._image_registry.get(sid) or []
        native_mode = self._session_image_mode(sid) == "native"

        def qualifies(entry: dict) -> bool:
            if self._read_entry_desc(entry):
                return True
            # 兜底：native 模式 + 这张图确实是她收到过的（带元素）→ 有资格。
            # 只认带元素的条目：OneBot 历史 URL 她从没收到过，不算。
            return native_mode and entry.get("elem") is not None

        described = [entry for entry in registry if qualifies(entry)]
        if apply_dedupe:
            described = [
                entry for entry in described
                if not self._is_recently_published_image(self._entry_source(entry))
            ]
        return described[-self.image_manifest_count:]

    # ---------- 图片描述：钩子只读，生产前移到"图片到达时" ----------
    def _entry_key(self, entry: dict) -> str:
        """条目去重键：URL 条目用清洗后的 URL，实时 Image 用内容指纹/元素身份。"""
        url = entry.get("url")
        if url:
            return f"url:{clean_url(str(url))}"
        elem = entry.get("elem")
        if elem is not None:
            md5 = getattr(elem, "md5", None)
            if md5:
                return f"md5:{md5}"
            return f"elem:{id(elem)}"
        return ""

    def _entry_source(self, entry: dict) -> str:
        """条目对应的实际来源（URL 或本地路径），用于发布与内容指纹。"""
        if entry.get("url"):
            return clean_url(str(entry.get("url")))
        elem = entry.get("elem")
        if elem is not None:
            return str(getattr(elem, "image", "") or getattr(elem, "file", "") or "")
        return ""

    def _is_desc_failed(self, key: str) -> bool:
        item = self._desc_failed.get(key)
        if item is None:
            return False
        ts, ttl = item
        if time.time() - ts > ttl:
            self._desc_failed.pop(key, None)
            return False
        return True

    def _mark_desc_failed(self, key: str, reason: str, ttl: Optional[float] = None):
        """写入负缓存：TTL 内不再重试、不再刷日志。

        只服务「显式识图」两条路径（AI 主动调识图工具 / 可选的评论前识图）；
        清单本身绝不识图，所以这里不影响清单。
        """
        if not key:
            return
        effective = float(ttl if ttl is not None else self.image_fetch_fail_ttl)
        self._desc_failed[key] = (time.time(), effective)
        if len(self._desc_failed) > URL_MD5_MAX:
            overflow = len(self._desc_failed) - URL_MD5_MAX
            for k, _ in sorted(self._desc_failed.items(), key=lambda kv: kv[1][0])[:overflow]:
                self._desc_failed.pop(k, None)
        logger.debug(f"图片描述暂不可用（{int(effective)}s 内不再重试）: {reason}")

    def _prune_entry_caches(self):
        for cache in (self._entry_desc, self._url_md5):
            if len(cache) > URL_MD5_MAX:
                overflow = len(cache) - URL_MD5_MAX
                for k in list(cache)[:overflow]:
                    cache.pop(k, None)

    @staticmethod
    def _classify_desc(text) -> tuple[str, bool]:
        """把一段候选描述分类成 (展示文案, 是否有资格进清单)。

        判据很朴素：**有 caption 就说明框架处理过这张图**（常规模式给 VLM 描述，
        native 模式给 "attached image" 这类占位串）；展示一律原样，不擅自改写。
        没有 caption（例如框架识图被关掉、或只有 OneBot 历史 URL）→ 不进清单。
        """
        raw = str(text or "").strip()
        if not raw:
            return "", False
        return raw, True

    def _should_inject_manifest(self, event) -> bool:
        """本轮是否注入图片清单。

        - ``on_demand``（默认）：只在**插件自己的定时发布任务**那一轮注入 ——
          那是唯一"我们确定她这就要发说说、并且需要挑图"的时刻。
          其它场合一律不注入：
          * 常规模式下她本来就能从消息文本里看到图片（框架会写成
            ``[Image 描述, file_path: data/temp/xxx.jpg]``），直接传 ``images`` 即可；
          * native 模式下框架不给路径 —— 她可以带 ``want_images=true`` 调用发布工具
            取一份候选清单（清单里包含这些图），所以也不需要我们主动注入。
        - ``always``：保持旧行为（只要该会话有可用候选就每轮注入）。
        """
        if self.manifest_inject_mode == "always":
            return True
        return self._scheduled_publish_policy(event) is not None

    async def _lookup_cached_url_desc(self, url: str) -> str:
        """免费路径查一张 URL 图的已知描述：内存 → 同 URL 的清单条目 → 已知 md5 查共享缓存。

        **绝不下载、绝不识图**：查不到就返回空串，由调用方决定丢弃这张候选。
        """
        if not url:
            return ""
        key = f"url:{clean_url(url)}"
        cached = self._entry_desc.get(key)
        if cached:
            return cached
        # 这张 URL 对应的图如果也被 bot 当消息收到过，清单条目上就有框架的 caption
        entry = self._find_image_registry_entry("", url)
        if entry is not None:
            desc = self._read_entry_desc(entry)
            if desc:
                self._entry_desc[key] = desc
                return desc
        md5 = self._url_md5.get(url) or self._url_md5.get(clean_url(url))
        if not md5:
            return ""
        desc = await self._cache_get_desc(md5)
        if desc:
            self._entry_desc[key] = desc
        return desc

    def _manifest_reply(self, entries: list) -> str:
        """把候选清单格式化成工具回复（不发布）。

        序号交给 AI 引用；真正的取图/续命由发布时在插件侧完成（所以哪怕链接过期也能救，
        不需要把长 URL 塞进上下文）。
        """
        lines = []
        for i, entry in enumerate(entries, 1):
            desc = self._read_entry_desc(entry)
            time_str = self._format_manifest_time(entry.get("time"))
            sender = entry.get("sender") or "未知"
            lines.append(f"{i}. [{time_str} {sender}] "
                         f"{candidate_label(desc, self.image_desc_max_chars)}")
        return (
            "说说未发布：这是当前可用的图片清单（只包含框架已经处理过的图片）。\n"
            + "\n".join(lines)
            + "\n请带上 image_indices=[序号,...] 再次调用 qzone_publish 完成发布"
              "（正文可原样保留或继续改写）；如确认发纯文字，请再次调用本工具且不要传 want_images。"
        )

    def _read_entry_desc(self, entry: dict) -> str:
        """同步、无网络：只读**已经就绪**的描述（钩子唯一允许的取描述方式）。

        绝不调用 hash_image()/to_path()/download_file()/desc_img()：
        框架的 Image.hash_image() 对 URL 型元素会真的下载图片（timeout 60s），
        且失败不缓存 md5，旧实现因此让每一轮 LLM 请求都重下一次。
        """
        elem = entry.get("elem")
        key = self._entry_key(entry)
        for candidate in (
            entry.get("desc"),                                   # 登记时抓到的 caption
            getattr(elem, "caption", None) if elem is not None else None,   # 元素上现成的
            self._entry_desc.get(key) if key else None,          # 进程内已知描述
        ):
            display, ok = self._classify_desc(candidate)
            if ok:
                entry["desc"] = display
                return display
        return ""

    @on.llm_request()
    async def _inject_image_manifest(self, event, req: LLMRequest, tag_set, *_):
        """向本轮请求注入近期图片清单（persist=False，不落记忆）。

        **钩子只读**：这里没有任何 await / 网络 / 磁盘操作，也不识图。
        只把"框架已经描述过的图片"列出来；没有描述的图片直接不列。

        为什么这样做是安全的：框架在 handle_im_batch_message 里先跑
        message_format_to_text（给每张图 hash + 生成描述 + 写缓存 + 填 caption），
        再触发 ON_LLM_REQUEST 钩子 —— 所以进到这里时，本轮图片的描述已经就绪。
        """
        # 吸附模式开启时彻底回到旧行为，不注入清单
        if not self.image_manifest_enabled or self.auto_attach_recent_image:
            return
        try:
            sid = getattr(event, "sid", "")
            if not sid:
                return
            self._register_current_event_images(event)
            # 注入策略：on_demand（默认）只在"这一轮确实要发说说"时注入，
            # 其它轮次一句都不加 —— 既不白占 token，也不在无关话题里误导模型。
            if not self._should_inject_manifest(event):
                # 这一轮没有清单：清掉"刚注入过"的标记，让 want_images 恢复生效
                self._manifest_fresh_ts.pop(sid, None)
                return
            # 定时发布任务：从候选里剔除去重窗口内已发布过的图，
            # 从源头避免连续几条说说配同一张图（用户可见症状）。
            dedupe_active = self._scheduled_publish_policy(event) is not None
            entries = self._manifest_entries(sid, apply_dedupe=dedupe_active)
            if not entries:
                return
            lines = []
            for i, entry in enumerate(entries, 1):
                desc = self._read_entry_desc(entry)          # 同步、零 IO
                time_str = self._format_manifest_time(entry.get("time"))
                sender = entry.get("sender") or "未知"
                lines.append(f"{i}. [{time_str} {sender}] "
                             f"{candidate_label(desc, self.image_desc_max_chars)}")
            if not lines:
                return
            text = (
                "[近期图片] 本群/会话最近出现的图片及内容描述，调用 qzone_publish 发说说时可用 image_indices 参数引用序号配图：\n"
                + "\n".join(lines)
            )
            req.user_prompt.insert(0, Prompt(
                text, name="qzone_images", source="qzone_plugin", persist=False
            ))
            self._manifest_fresh_ts[sid] = time.time()
            if len(self._manifest_fresh_ts) > 50:
                for key in list(self._manifest_fresh_ts)[: len(self._manifest_fresh_ts) - 50]:
                    self._manifest_fresh_ts.pop(key, None)
        except Exception as e:
            logger.debug(f"注入图片清单失败: {e}")

    async def _refresh_image_url(self, entry: dict, quiet: bool = False) -> bool:
        """图片 URL 过期时，用 get_msg 按 message_id 换取新签名 URL（rkey 续命）。

        换到的新 URL 会**继承旧 URL 的内容指纹**：否则同一张图的"去重身份"
        会随着签名变化而改变，表现为去重失效、反复配同一张图。
        """
        msg_id = entry.get("msg_id")
        if not msg_id:
            return False
        old_url = clean_url(str(entry.get("url") or ""))
        try:
            res = await self._call_onebot_action("get_msg", {"message_id": int(msg_id)})
            if not res or res.get("status") != "ok":
                return False
            for seg in (res.get("data") or {}).get("message") or []:
                if not isinstance(seg, dict) or seg.get("type") != "image":
                    continue
                url = clean_url(html.unescape(str((seg.get("data") or {}).get("url") or "")))
                if not url:
                    continue
                entry["url"] = url
                elem = entry.get("elem")
                if elem is not None:
                    elem.image = url
                    elem.file = url
                    elem.image_type = "url"
                    elem._temp_path = None  # 作废可能已污染的缓存文件
                fingerprint = self._url_md5.get(old_url)
                if fingerprint:
                    self._url_md5[url] = fingerprint
                if not quiet:
                    logger.info(f"已通过 get_msg 刷新过期图片 URL (msg_id={msg_id})")
                return True
        except Exception as e:
            logger.debug(f"刷新图片 URL 失败 (msg_id={msg_id}): {e}")
        return False

    async def _resolve_manifest_images(self, sid: str, indices: list,
                                       apply_dedupe: bool = False) -> List[str]:
        """按清单序号解析图片；历史 URL 直接传入，实时 Image 转成本地路径。

        apply_dedupe=True（定时/自动发布）时会跳过处于图片去重间隔内的条目，
        避免连续几条说说配同一张图；用户主动指定图片时不受影响。
        """
        paths = []
        entries = self._manifest_entries(sid, apply_dedupe=apply_dedupe)
        normalized_indices = []
        for idx in indices:
            try:
                normalized_indices.append(int(idx))
            except (TypeError, ValueError):
                return []
        if not normalized_indices:
            return []
        if any(not (1 <= i <= len(entries)) for i in normalized_indices):
            return []
        for i in dict.fromkeys(normalized_indices):
            entry = entries[i - 1]
            if apply_dedupe and self._is_recently_published_image(self._entry_source(entry)):
                logger.info(f"清单图片 {i} 处于图片去重间隔内，已跳过（避免连续配同一张图）")
                continue
            if entry.get("source") == "url":
                url = entry.get("url", "")
                if url:
                    # 历史 URL 的 rkey 有效期约 1 小时，过期后下载必 400；
                    # 先用 get_msg 按 message_id 续命换新签名 URL，失败再退回原 URL。
                    if entry.get("msg_id") and await self._refresh_image_url(entry):
                        url = entry.get("url") or url
                        logger.info(f"历史图片已续命成功: {url[:80]}")
                    paths.append(url)
                continue
            elem: Image = entry["elem"]
            for attempt in range(2):
                try:
                    path = await elem.to_path()
                    if path:
                        head = await asyncio.to_thread(_read_head_bytes, path, 16)
                        if looks_like_image(head):
                            paths.append(str(path))
                            break
                        logger.warning(f"清单图片 {i} 缓存内容不是图片: {path}")
                    raise ValueError("to_path 为空或内容非图片")
                except Exception as e:
                    if attempt == 0 and await self._refresh_image_url(entry):
                        continue
                    logger.warning(f"清单图片 {i} 获取失败: {e}")
                    break
        return paths

    def _find_image_registry_entry(self, sid: str, source: str) -> Optional[dict]:
        """按来源 URL 定位候选，优先当前会话，再检查其他会话的同源条目。"""
        registries = [self._image_registry.get(sid) or []]
        registries.extend(
            entries
            for registry_sid, entries in self._image_registry.items()
            if registry_sid != sid
        )
        for registry in registries:
            for entry in reversed(registry):
                if entry.get("url") == source:
                    return entry
                elem = entry.get("elem")
                if elem is not None and source in {
                    str(getattr(elem, "image", "") or ""),
                    str(getattr(elem, "file", "") or ""),
                }:
                    return entry
        return None

    async def _refresh_explicit_image_sources(self, sid: str, sources: list[str]) -> list[str]:
        """刷新 Bot 通过 images 传回的 QQ 临时 URL；非清单来源保持原值。"""
        refreshed = []
        for source in sources:
            entry = self._find_image_registry_entry(sid, source)
            if entry and await self._refresh_image_url(entry):
                refreshed.append(entry.get("url") or source)
            else:
                refreshed.append(source)
        return refreshed

    # ---------- 图片去重（内容指纹优先，来源串兜底） ----------
    @staticmethod
    def _identity_key(source: str) -> str:
        # 先 strip：AI 传回的 images 参数常带空格/引号，不归一会导致去重身份对不上
        src = str(source or "").strip()
        if src.startswith(("http://", "https://")):
            return clean_url(src)
        return src

    def _image_identity(self, source: str) -> str:
        """内容指纹优先：拿得到 md5 就按**内容**判重（URL 签名变了也认得出来）。"""
        if not source:
            return ""
        key = self._identity_key(source)
        md5 = self._url_md5.get(key) or self._url_md5.get(str(source))
        if md5:
            return f"md5:{md5}"
        return f"src:{key}"

    def _remember_image_identity(self, source: str, md5: str):
        """把来源与内容指纹绑定：URL 续命换签名后仍能认出是同一张图。"""
        if not source or not md5:
            return
        src = str(source)
        self._url_md5[src] = md5
        self._url_md5[self._identity_key(src)] = md5
        self._prune_entry_caches()

    def _prune_dedupe_history(self) -> list:
        """按时间与容量修剪去重历史。

        旧实现把去重历史与图片清单共用 IMAGE_REGISTRY_CAP=20 条上限：
        一条说说配 3 张图时只够记 6 条说说，"3 天去重"实际上几小时后就失效了
        ——这是"连续几条说说配同一张图"的直接原因之一。
        """
        keep = max(self.auto_publish_image_dedupe_interval or 0, DEDUPE_HISTORY_MIN_TTL)
        cutoff = time.time() - keep
        self._published_image_history = [
            item for item in self._published_image_history
            if _to_float(item.get("time"), 0.0) >= cutoff
        ][-DEDUPE_HISTORY_MAX:]
        return self._published_image_history

    def _is_recently_published_image(self, source) -> bool:
        """该图片在去重间隔内是否已经发布过（内容指纹优先，来源串兜底）。"""
        interval = self.auto_publish_image_dedupe_interval
        if not interval or not source:
            return False
        src = str(source)
        identity = self._image_identity(src)
        now = time.time()
        for item in self._published_image_history:
            if now - _to_float(item.get("time"), 0.0) >= interval:
                continue
            if identity and item.get("identity") == identity:
                return True
            # 兜底：任一侧缺内容指纹时，用原始来源串比对
            if item.get("source") and str(item.get("source")) == src:
                return True
        return False

    def _record_published_images(self, sources: list, md5_pairs=None):
        """记录**实际发布成功**的图片（来源 + 内容指纹）。

        md5_pairs 来自发布接口回传的 (来源, md5)；拿不到时退回来源串（与旧版语义一致）。
        旧实现在"部分图片被降级丢弃"时整批不记录，那几张图下一轮又会被配上。
        """
        now = time.time()
        records: list[tuple[str, str]] = []
        if md5_pairs:
            for source, md5 in md5_pairs:
                src = str(source)
                if md5:
                    self._remember_image_identity(src, md5)
                records.append((f"md5:{md5}" if md5 else self._image_identity(src), src))
        elif md5_pairs is None:
            for source in sources:
                src = str(source)
                records.append((self._image_identity(src), src))
        seen: set = set()
        for identity, src in records:
            if not identity or identity in seen:
                continue
            seen.add(identity)
            self._published_image_history.append({"identity": identity, "source": src, "time": now})
        self._prune_dedupe_history()
        self._save_state()

    async def _publish(self, text: str, image_urls: list, allow_image_drop: bool = False) -> str:
        await self._ensure_api()
        post = QzonePost(text=text, images=image_urls)
        resp = await self.api.publish(post, allow_image_drop=allow_image_drop)
        if not resp.ok:
            raise RuntimeError(f"发布失败: {resp.message}")
        if image_urls:
            # 用接口回传的"实际上传成功"的图片记去重历史：部分图片被降级丢弃时，
            # 旧实现会整批不记录，导致那几张图下一轮又被配上。
            uploaded = (resp.data or {}).get("image_md5s")
            if uploaded:
                self._record_published_images([str(x) for x in image_urls], md5_pairs=uploaded)
            elif not (allow_image_drop and resp.message):
                self._record_published_images([str(x) for x in image_urls])
        tid = resp.data.get("tid")
        result = f"说说发布成功！TID: {tid}"
        if resp.message:
            result += f"（注意：{resp.message}）"
        return result

    async def _get_my_nickname(self) -> str:
        """懒加载自己的昵称，失败返回空串（调用方回退「我」）。

        来源优先级：
        1. OneBot get_stranger_info（QQ 主协议数据，NapCat/LLOneBot 必支持，最可靠）
        2. QZone cgi_personal_card（空间接口，部分环境不可用）
        """
        if self._my_nickname:
            return self._my_nickname
        if not self.my_uin:
            return ""
        # 优先 OneBot 主协议
        try:
            data = await self._call_onebot_action(
                "get_stranger_info", {"user_id": int(self.my_uin)}, timeout=5
            )
            if data.get("status") == "ok":
                info = data.get("data") or {}
                nick = str(info.get("nickname") or info.get("nick") or "")
                if nick:
                    self._my_nickname = nick
                    logger.info(f"已获取自己的昵称(OneBot): {nick}")
                    return nick
        except Exception as e:
            logger.debug(f"OneBot 获取自己昵称失败: {e}")
        # 回退 QZone 空间接口
        if self.api is not None:
            try:
                resp = await self.api.get_user_info(str(self.my_uin))
                if resp.ok:
                    nick = str((resp.data or {}).get("nickname") or "")
                    if nick:
                        self._my_nickname = nick
                        logger.info(f"已获取自己的昵称(cgi_personal_card): {nick}")
            except Exception as e:
                logger.debug(f"cgi_personal_card 获取自己昵称失败: {e}")
        return self._my_nickname

    async def _get_feeds(self, target_id: Optional[str] = None, num: int = 1) -> list[QzonePost]:
        await self._ensure_api()
        if target_id:
            resp = await self.api.get_feeds(target_id, pos=0, num=num)
        else:
            resp = await self.api.get_recent_feeds()
        if not resp.ok:
            raise RuntimeError(f"获取说说失败: {resp.message}")
        if target_id:
            msglist = resp.data.get("msglist") or []
            posts = QzoneParser.parse_feeds(msglist)
        else:
            posts = QzoneParser.parse_recent_feeds(resp.data)
        return posts[:num]

    async def _like(self, post: QzonePost) -> str:
        await self._ensure_api()
        resp = await self.api.like(post, abstime=post.create_time)
        if not resp.ok:
            raise RuntimeError(f"点赞失败: {resp.message}")
        return "点赞成功"

    @staticmethod
    def _normalize_comment_text(content: str) -> str:
        """用于落地确认；忽略 QQ 展示层插入的空白，但保留实际字符差异。"""
        return re.sub(r"\s+", "", content or "")

    def _count_own_comment(self, post: QzonePost, content: str) -> int:
        expected = self._normalize_comment_text(content)
        if not expected:
            return 0
        return sum(
            1
            for comment in post.comments
            if str(comment.uin) == str(self.my_uin)
            and self._normalize_comment_text(comment.plain_content) == expected
        )

    async def _safe_detail_post(self, post: QzonePost) -> Optional[QzonePost]:
        """view 用：详情拉取失败返回 None（由上层回退到列表内联评论）。"""
        try:
            return await self._get_detail_post(post)
        except Exception as e:
            logger.debug(f"view 拉取详情评论失败（回退列表评论）: {e}")
            return None

    async def _safe_like_list(self, post: QzonePost):
        """view 用：点赞列表拉取失败返回 None。"""
        try:
            return await self.api.get_like_list(post, query_count=10)
        except Exception as e:
            logger.debug(f"view 拉取点赞列表失败: {e}")
            return None

    async def _get_detail_post(self, post: QzonePost) -> QzonePost:
        detail_resp = await self.api.get_detail(post)
        if not detail_resp.ok:
            raise RuntimeError(f"获取说说详情失败: {detail_resp.message}")
        parsed_posts = QzoneParser.parse_feeds([detail_resp.data])
        if not parsed_posts:
            raise RuntimeError("说说详情解析失败")
        return parsed_posts[0]

    async def _comment(self, post: QzonePost, content: str) -> str:
        """提交评论。接口返回成功即视为成功（与其他操作统一判定，不再回读确认）。

        提交前做幂等检查（本地比对，零 token 消耗）：若该说说下已存在
        自己相同内容的评论，直接返回成功，避免 LLM 重试造成重复评论。
        """
        await self._ensure_api()
        if not self.my_uin:
            raise RuntimeError("无法确认当前登录 QQ，已取消评论")

        # 幂等检查：本地遍历评论比对（不调 LLM，不消耗 token）
        try:
            before_post = await self._get_detail_post(post)
            if self._count_own_comment(before_post, content) > 0:
                logger.info(f"幂等命中：该说说已存在相同评论，跳过重复提交: {post.tid}")
                return "评论成功（该说说已存在相同内容的评论，未重复提交）"
        except Exception as e:
            logger.debug(f"评论前幂等检查失败（继续提交）: {e}")

        resp = await self.api.comment(post, content)
        if not resp.ok:
            raise RuntimeError(f"评论接口失败: {resp.message}")
        logger.info(f"QZone评论提交成功: post={post.tid} content={content[:40]}")
        # 假成功可观测：记录接口原始 code/ret，便于排查"接口说成功但评论没落库"
        raw = resp.raw or {}
        logger.info(
            f"评论接口原始响应: post={post.tid} code={resp.code} ret={raw.get('ret')} "
            f"msg={raw.get('msg') or raw.get('message') or resp.message}"
        )
        # 回读确认（诊断模式，默认关）：只写日志/附注，不阻断成功判定，
        # 避免重蹈 v1.4.4 之前"回读误判失败 -> AI 重试 -> 重复评论"的覆辙。
        if self.comment_verify:
            try:
                await asyncio.sleep(random.uniform(1.5, 2.5))  # 等评论落库
                verify_post = await self._get_detail_post(post)
                if self._count_own_comment(verify_post, content) > 0:
                    logger.info(f"评论回读确认成功: post={post.tid}")
                else:
                    logger.warning(
                        f"评论回读未确认（可能延迟或风控）: post={post.tid} content={content[:40]}"
                    )
            except Exception as e:
                logger.debug(f"评论回读确认失败: {e}")
        return "评论成功"

    async def _delete(self, tid: str) -> str:
        await self._ensure_api()
        resp = await self.api.delete(tid)
        if not resp.ok:
            raise RuntimeError(f"删除失败: {resp.message}")
        return f"说说 {tid} 删除成功"

    @staticmethod
    def _find_root_comment(comments: List[QzoneComment], comment: QzoneComment) -> QzoneComment:
        """返回 API 所需的主评论对象，避免把主评论 ID 与回复作者 UIN 错配。"""
        if comment.parent_tid is None:
            return comment
        roots = [
            item for item in comments
            if item.parent_tid is None and str(item.tid) == str(comment.parent_tid)
        ]
        if len(roots) != 1:
            raise RuntimeError(
                f"无法唯一定位楼中回复 {comment.tid}/{comment.uin} 的主评论 {comment.parent_tid}"
            )
        return roots[0]

    async def _reply_comment(
        self,
        post: QzonePost,
        comment: QzoneComment,
        content: str = "",
        root_comment: Optional[QzoneComment] = None,
    ) -> str:
        await self._ensure_api()
        if not content:
            _, prompt_content = self._parse_comment_content(comment.content)
            prompt = f"用户 {comment.nickname} 评论了你的说说：{prompt_content}，请生成一条简洁回复（0-15字）。"
            content = await self._call_llm(prompt, await self._get_persona_content(), use_backend_model=False)
            if not content:
                raise RuntimeError("生成回复内容为空")
        resp = await self.api.reply(post, comment, content, root_comment=root_comment)
        if not resp.ok:
            raise RuntimeError(f"回复失败: {resp.message}")
        return f"回复成功: {content}"

    # ---------- LLM 调用 ----------
    async def _call_llm(self, prompt: str, system_prompt: Optional[str] = None, use_backend_model: bool = False) -> str:
        """调用 LLM，use_backend_model=True 时使用后台指定模型（仅后台直接生成模式）"""
        try:
            client = None
            if use_backend_model and self.backend_llm_model:
                try:
                    client = self.ctx.get_llm_client(model_uuid=self.backend_llm_model)
                except TypeError:
                    client = self.ctx.get_llm_client(self.backend_llm_model)
                if client is None:
                    logger.warning(f"后台指定模型 {self.backend_llm_model} 不存在，回退到快速模型")
            if client is None:
                client = self.ctx.get_default_fast_llm_client()
            if not client:
                logger.error("无法获取 LLM 客户端")
                return ""
            messages = []
            if system_prompt:
                messages.append({"role": "system", "content": system_prompt})
            messages.append({"role": "user", "content": prompt})
            request = LLMRequest(messages=messages)
            response = await client.chat(request)
            return response.text_response.strip()
        except Exception as e:
            logger.error(f"LLM 调用失败: {e}")
            return ""

    # ---------- 权限检查 ----------
    async def _check_master(self, event: KiraMessageBatchEvent) -> bool:
        """敏感操作权限：批量消息中任一发送者是主人或系统内部调用即放行。

        只读工具（qzone_view / qzone_describe_image）不调用此方法，对所有用户开放。
        """
        if not self.master_check_enabled:
            return True
        if not self.master_ids:
            return True
        senders = []
        for m in getattr(event, "messages", None) or []:
            if m.sender and m.sender.user_id:
                senders.append(str(m.sender.user_id))
        if not senders:
            # 拿不到发送者信息（系统内部调用等），放行
            return True
        for uid in senders:
            if uid.startswith("system") or uid in self.master_ids:
                return True
        logger.warning(f"用户 {senders} 尝试使用QQ空间敏感工具，但不在主人列表中")
        return False

    @staticmethod
    def _parse_comment_content(content: str) -> tuple[str, str]:
        """拆分 QQ 空间原生回复对象标记和正文。"""
        text = content or ""
        match = re.match(r"\s*@\{uin:(\d+),nick:([^,}]+)[^}]*\}\s*", text)
        if not match:
            return "", text
        target = f"{match.group(2)}(UIN:{match.group(1)})"
        return target, text[match.end():]

    @classmethod
    def _format_comment_line(cls, comment: QzoneComment, label: str, indent: str, time_str: str) -> str:
        target, content = cls._parse_comment_content(comment.content)
        relation = f" 回复 {target}" if target else ""
        # 优先展示真实评论 ID（长数字/含 _r_ 的合成 id），删除/回复接口需要它；
        # 短楼层号 tid 仅作为展示辅助，避免 AI 把楼层号当真实 ID 传给删除接口。
        cid = comment.comment_id or str(comment.tid)
        return (
            f"{indent}└ [{label} ID:{cid} UIN:{comment.uin}] "
            f"{comment.nickname}{relation} [{time_str}]: {content}"
        )

    def _add_post_to_history(self, text: str):
        self.my_posts_history.append(text)
        if len(self.my_posts_history) > MAX_HISTORY:
            self.my_posts_history.pop(0)
        self._save_state()

    @staticmethod
    def _format_manifest_time(value) -> str:
        """清单时间格式化：单个脏值不能让整份清单消失（钩子里尤其要稳）。

        脏值/缺失如实显示"时间未知"，不要伪装成"现在"——否则 AI 会把很老的历史图
        当成刚出现的图。
        """
        try:
            ts = _to_float(value, 0.0)
            if ts <= 0:
                return "时间未知"
            return datetime.fromtimestamp(ts).strftime("%m-%d %H:%M")
        except Exception:
            return "时间未知"

    @staticmethod
    def _format_time(ts) -> str:
        if isinstance(ts, (int, float)) and ts > 0:
            dt = datetime.fromtimestamp(ts)
            return dt.strftime("%Y-%m-%d %H:%M")
        elif isinstance(ts, str):
            return ts
        else:
            return "未知时间"

    def _parse_schedule(self, s: str) -> Optional[dict]:
        if not s or not s.strip():
            return None
        s = s.strip()
        if ' ' in s or '*' in s or '/' in s:
            try:
                CronTrigger.from_crontab(s)
                return {"mode": "cron", "expr": s}
            except Exception:
                pass
        pattern = r'^(?P<interval>\d+(?:\.\d+)?[hm]?)(?:/(?P<jitter>\d+(?:\.\d+)?[hm]?))?$'
        match = re.match(pattern, s)
        if match:
            interval_str = match.group('interval')
            jitter_str = match.group('jitter')

            def parse_time(t):
                if t.endswith('h'):
                    return float(t[:-1]) * 3600
                elif t.endswith('m'):
                    return float(t[:-1]) * 60
                else:
                    return float(t) * 60

            interval_seconds = parse_time(interval_str)
            jitter_seconds = parse_time(jitter_str) if jitter_str else 0
            if interval_seconds <= 0:
                return None
            return {
                "mode": "interval",
                "interval_seconds": int(interval_seconds),
                "jitter_seconds": int(jitter_seconds)
            }
        logger.warning(f"无法解析定时表达式: {s}，任务将被禁用")
        return None

    # ---------- 工具注册（不检查黑名单，用户主动触发不受限制） ----------
    @register_tool(
        name="qzone_publish",
        description="发布一条说说到自己的QQ空间。配图：优先用你已经在聊天里看到过的图片、或[近期图片]清单里列出的图片 —— 前者的路径/URL 传给 images，后者的序号传给 image_indices。都不传即纯文字发布。",
        params={
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "说说内容"},
                "images": {
                    "type": "array", "items": {"type": "string"},
                    "description": "本地路径或URL列表（可选）",
                    "default": []
                },
                "image_indices": {
                    "type": "array", "items": {"type": "integer"},
                    "description": "[近期图片]清单序号（从1开始，可选）",
                    "default": []
                },
                "want_images": {
                    "type": "boolean",
                    "description": "只有在「想配图、但既没有图片路径、也没有[近期图片]清单」时才传 true：我会返回一份候选清单（本次不会发布），你再带 image_indices 调用一次。手里已有路径或清单时不要传（会白白多一次调用）。",
                    "default": False
                }
            },
            "required": ["text"]
        }
    )
    async def tool_publish(self, event: KiraMessageBatchEvent, text: str, images: list = None,
                           image_indices: list = None, want_images: bool = False):
        # 不检查黑名单，用户主动触发不受限制
        if not await self._check_master(event):
            return "抱歉，只有主人才能使用此功能。"
        await self._ensure_api()
        try:
            task_policy = self._scheduled_publish_policy(event)
            task_target = task_policy[0] if task_policy else None
            task_maximum = task_policy[1] if task_policy else None
            images = images or []
            image_indices = image_indices or []
            valid_sources = []
            for item in images:
                if not isinstance(item, str) or not item.strip():
                    continue
                # 只拦"公网 http(s) 之外/指向内网"的地址；本地路径不受影响。
                # （旧实现用 'example.com' 字符串硬编码过滤模型幻觉 URL，换成正则校验）
                if not is_safe_public_url(item):
                    logger.warning(f"已忽略不安全或指向内网的图片地址: {item[:80]}")
                    continue
                valid_sources.append(item)
            # 清单序号配图（定时/自动发布时应用图片去重，避免连续同一张图）
            if image_indices:
                resolved = await self._resolve_manifest_images(
                    event.sid, image_indices, apply_dedupe=task_policy is not None
                )
                if not resolved:
                    if task_target is None:
                        return (
                            f"未能从[近期图片]清单解析出图片（当前会话清单为空，或序号 {image_indices} 超出范围），说说未发布。"
                            "如想配图，可先传 want_images=true 取一份候选清单（本次不会发布），再带 image_indices 调用一次；"
                            "也可改用 images 参数传图片 URL 或本地路径（如 data/temp/xxx.jpg）。"
                            "如确认发纯文字，请不带这些参数重试。"
                        )
                    logger.info(
                        "定时发布清单选择不可用，按资源降级: target=%s indices=%s",
                        task_target,
                        image_indices,
                    )
                valid_sources.extend(resolved)
            elif images and not valid_sources:
                return "images 参数中的地址均无效，说说未发布。请传有效的图片 URL 或本地路径。"
            if images:
                # 显式传来的图片地址：只要是本插件登记过的（带 message_id），
                # 就顺手用 get_msg 续命一次，避免拿过期链接去发布而失败
                # （OneBot 历史图片的 rkey 约 1 小时就过期）。
                # 非登记来源原样返回，没有额外开销。
                valid_sources = await self._refresh_explicit_image_sources(
                    event.sid, valid_sources
                )
            valid_sources = self._dedupe_sources(valid_sources)
            # 显式请求清单：本次不发布，先把当前可用候选给她，再由她带 image_indices 调一次。
            # 清单为空时直接继续发布（不让她白跑一趟）。
            if want_images and not image_indices and not valid_sources:
                sid = getattr(event, "sid", "") or ""
                if time.time() - self._manifest_fresh_ts.get(sid, 0.0) < MANIFEST_FRESH_TTL:
                    # 清单已经在她的上下文里（例如定时发布任务那一轮）→ 这个参数是多余的，
                    # 直接忽略它照常发布，不让"多传一个参数"白白推迟一轮。
                    logger.info("本轮已注入[近期图片]清单，忽略 want_images 参数，直接按原流程发布")
                else:
                    entries = self._manifest_entries(
                        sid, apply_dedupe=task_policy is not None
                    )
                    if entries:
                        return self._manifest_reply(entries)
                    logger.info("want_images=true 但当前没有可用候选，按纯文字继续发布")
            if task_target is not None:
                if task_target > 0:
                    valid_sources = await self._fill_scheduled_publish_sources(
                        event.sid, valid_sources, task_target
                    )
                    if len(valid_sources) < task_target:
                        recent = await self._fetch_recent_images_for_event(
                            event, max_count=task_target
                        )
                        # 兜底补图同样要过去重：否则"清单挑不到→抓最近图"这条路
                        # 会把刚发过的那张图再配一次。
                        recent = [
                            url for url in recent
                            if not self._is_recently_published_image(url)
                        ]
                        valid_sources = self._dedupe_sources(
                            valid_sources + recent
                        )[:task_target]
                    if len(valid_sources) < task_target:
                        logger.info(
                            "定时发布工具层图片目标降级: target=%s usable=%s",
                            task_target,
                            len(valid_sources),
                        )
                else:
                    valid_sources = valid_sources[:task_maximum]
            # 吸附兑底：未指定图片且开启吸附模式时，自动抓最近一张图
            # （吸附图下载失败会降级为纯文字发布，保持"有时配有时不配"的随机感）
            allow_drop = False
            if task_policy is not None:
                # 定时任务：已通过 _fill_scheduled_publish_sources 尽力补足目标张数
                # （清单 → 近期图片 阶梯）；若图片仍全部获取失败，降级为纯文字发布，
                # 避免整个发布失败导致 LLM 反复重试空转。允许下载失败时再降级，
                # 不影响"尽可能按目标数量配图"的优先级。
                allow_drop = True
            elif (
                not valid_sources
                and self.auto_attach_recent_image
            ):
                valid_sources = await self._fetch_recent_images_for_event(event, max_count=1)
                allow_drop = bool(valid_sources)
            result = await self._publish(text, valid_sources, allow_image_drop=allow_drop)
            self._add_post_to_history(text)
            return result
        except Exception as e:
            return f"发布失败：{e}"

    @register_tool(
        name="qzone_view",
        description="查看QQ空间说说。不传 target_id 看自己的空间，传好友QQ号看好友动态；返回每条说说的 ID、时间、配图数与最新评论。需要了解某张配图内容时可再调 qzone_describe_image。",
        params={
            "type": "object",
            "properties": {
                "target_id": {"type": "string", "description": "目标QQ号（可选，默认自己）"},
                "num": {"type": "integer", "description": "查看条数，默认1", "default": 1}
            },
        }
    )
    async def tool_view(self, event: KiraMessageBatchEvent, target_id: str = None, num: int = 1):
        # 查看是只读操作，所有用户可用，不做权限检查
        if target_id is not None:
            block = self._target_block_reason(target_id)
            if block:
                return f"查看被拒绝：{block}"
        await self._ensure_api()
        try:
            if target_id is None:
                if self.my_uin is None:
                    return "无法获取自己的QQ号，请检查插件初始化。"
                target_id = str(self.my_uin)
            posts = await self._get_feeds(target_id, num)
            if not posts:
                return "没有找到说说。"
            lines = []
            for p in posts:
                time_str = self._format_time(p.create_time)
                line = f"【{p.name}】(ID:{p.tid}) [{time_str}]: {p.text}"
                if p.images:
                    img_count = len(p.images)
                    is_own = self.my_uin and p.uin == self.my_uin
                    if self.qzone_image_desc_enabled and (not is_own or self.qzone_image_desc_own):
                        line += f"\n配图x{img_count}（调用 qzone_describe_image(target_id='{p.uin}', tid='{p.tid}', index=第几张) 可查看图片内容）"
                    else:
                        line += f"\n配图x{img_count}"
                # 评论直接用详情接口拉取（h5 msgdetail_v6 为实测唯一稳定路径，
                # 不再尝试 PC/mobile 评论接口——它们在该环境返回空/参数错误，
                # 避免 view 链路先报错再回退的不优雅行为）
                # 详情与点赞列表彼此独立 → 并发拉取，避免每条说说串行等两次 HTTP
                # （纯等待优化：两者的取值、回退顺序与最终展示完全不变）
                full_post = None
                detail_post, like_resp = await asyncio.gather(
                    self._safe_detail_post(p), self._safe_like_list(p)
                )
                if detail_post is not None and detail_post.comments:
                    full_post = detail_post
                comments = (full_post or p).comments
                # 点赞信息：全部来自真实接口（get_like_list_app 的 like_uin_info/total_number/is_dolike）
                like_count = 0
                like_users: list[str] = []
                liked_uins: list[str] = []
                is_dolike = False
                if like_resp is not None and like_resp.ok:
                    like_data = like_resp.data or {}
                    like_users = [
                        str(u.get("nick") or u.get("fuin") or "")
                        for u in (like_data.get("like_uin_info") or [])
                        if isinstance(u, dict) and (u.get("nick") or u.get("fuin"))
                    ]
                    liked_uins = list(like_data.get("like_uins") or [])
                    like_count = int(
                        like_data.get("total_number") or len(like_users) or 0
                    )
                    is_dolike = bool(like_data.get("is_dolike"))
                if not like_users:
                    # get_like_list 失败/为空时回退 detail 内联 likeinfo（仍为接口数据）
                    like_count = (full_post or p).like_count
                    like_users = list((full_post or p).like_users)
                # 自己是否已赞：get_like_list_app 的 is_dolike 为接口真实字段（CSDN 教程确认），
                # 辅以 msglist isLiked / detail isliked。不用任何本地记录。
                msg_liked = bool(getattr(p, "is_liked", False))
                detail_liked = bool(getattr(full_post, "is_liked", False)) if full_post else False
                self_liked = is_dolike or msg_liked or detail_liked
                logger.info(
                    f"view 已赞状态: tid={p.tid} is_dolike={is_dolike} "
                    f"msglist_is_liked={getattr(p, 'is_liked', None)} "
                    f"detail_is_liked={detail_liked} self_liked={self_liked}"
                )
                # 接口确认：like_uin_info 不含当前登录者（"除我以外"），
                # 若接口确认自己已赞（is_dolike）且列表没有自己 → 补自己的真实昵称（仍为接口事实的组合）
                if (
                    self_liked
                    and liked_uins
                    and self.my_uin
                    and str(self.my_uin) not in liked_uins
                ):
                    # 显示自己的真实昵称并加「（我）」标记（如「General New（我）」）：
                    # 保留真实昵称防昵称诈骗，同时明确这是 bot 自己；昵称获取失败才回退「我」
                    nick = await self._get_my_nickname()
                    my_nick = f"{nick}（我）" if nick else "我"
                    like_users.append(my_nick)
                    if like_count <= len(liked_uins):
                        like_count += 1
                if like_count or like_users:
                    # 格式：已赞N人：周武 觉得很赞（前缀总数 + QQ 原生表达；
                    # 超过配置上限时「周武、C7 等人 觉得很赞」，不再重复数字）
                    n = like_count or len(like_users)
                    shown = like_users[:self.like_users_display_max]
                    if n == 1 and shown:
                        like_text = f"已赞1人：{shown[0]} 觉得很赞"
                    elif shown and len(shown) >= n:
                        like_text = f"已赞{n}人：{'、'.join(shown)} 觉得很赞"
                    elif shown:
                        like_text = f"已赞{n}人：{'、'.join(shown)} 等人 觉得很赞"
                    else:
                        like_text = f"已赞{n}人"
                    line += f"\n{like_text}"
                if comments:
                    comment_lines = []
                    replies_by_parent: dict[int, list[QzoneComment]] = {}
                    main_comments = []
                    for cmt in comments:
                        if cmt.parent_tid is None:
                            main_comments.append(cmt)
                        else:
                            replies_by_parent.setdefault(cmt.parent_tid, []).append(cmt)

                    shown_comments: set[int] = set()
                    for cmt in main_comments:
                        cmt_time_str = cmt.create_time_str or self._format_time(cmt.create_time)
                        comment_lines.append(self._format_comment_line(cmt, "主评论", "  ", cmt_time_str))
                        shown_comments.add(id(cmt))
                        for reply in replies_by_parent.get(cmt.tid, []):
                            reply_time_str = reply.create_time_str or self._format_time(reply.create_time)
                            comment_lines.append(self._format_comment_line(reply, "楼中回复", "    ", reply_time_str))
                            shown_comments.add(id(reply))

                    # 防御性展示没有匹配主评论的回复；不递归推断不存在的更深层级。
                    for cmt in comments:
                        if id(cmt) in shown_comments:
                            continue
                        cmt_time_str = cmt.create_time_str or self._format_time(cmt.create_time)
                        label = "主评论" if cmt.parent_tid is None else "楼中回复"
                        indent = "  " if cmt.parent_tid is None else "    "
                        comment_lines.append(self._format_comment_line(cmt, label, indent, cmt_time_str))
                        shown_comments.add(id(cmt))
                    if comment_lines:
                        line += "\n评论区：\n" + "\n".join(comment_lines[:30])
                lines.append(line)
            return "\n---\n".join(lines)
        except Exception as e:
            return f"查看失败：{e}"

    @register_tool(
        name="qzone_describe_image",
        description="查看说说中某张配图的实际内容。他人的说说：文字暗示了图片、或你打算评论前想了解内容时调用。自己的说说一般不必调用（确有必要时除外，如确认当时配图是否合适）。",
        params={
            "type": "object",
            "properties": {
                "target_id": {"type": "string", "description": "说说作者的QQ号"},
                "tid": {"type": "string", "description": "说说ID"},
                "index": {"type": "integer", "description": "第几张图（从1开始，默认1）", "default": 1}
            },
            "required": ["target_id", "tid"]
        }
    )
    async def tool_describe_image(self, event: KiraMessageBatchEvent, target_id: str, tid: str, index: int = 1):
        if not self.qzone_image_desc_enabled:
            return "空间图片识别功能未启用。"
        block = self._target_block_reason(target_id)
        if block:
            return f"识图被拒绝：{block}"
        # 识图是只读操作，所有用户可用，不做权限检查
        await self._ensure_api()
        # 自己的空间默认不识图：配图本来就是 bot 自己选的（可用配置开启）
        if not self.qzone_image_desc_own and self.my_uin and str(target_id) == str(self.my_uin):
            return "这是你自己发布的说说，一般不需要识图（配图本来就是你自己选的）。如确有需要，请让主人在插件配置中开启「允许对自己空间识图」。"
        try:
            post = QzonePost(uin=int(target_id), tid=tid)
            detail_resp = await self.api.get_detail(post)
            if not detail_resp.ok:
                return f"获取说说详情失败: {detail_resp.message}"
            parsed_posts = QzoneParser.parse_feeds([detail_resp.data])
            if not parsed_posts:
                return "解析说说详情失败"
            full_post = parsed_posts[0]
            if not full_post.images:
                return "这条说说没有配图。"
            if not (1 <= index <= len(full_post.images)):
                return f"图片序号超出范围，这条说说共 {len(full_post.images)} 张图。"
            url = full_post.images[index - 1]
            result = await fetch_bytes(url, timeout=self.image_download_timeout)
            if not result.ok:
                return f"图片下载失败，可能已过期（{result.reason}）。"
            desc = await self._describe_image_bytes(result.data, source_url=url)
            if not desc:
                return "图片识别失败（识图模型不可用或缓存未命中）。"
            return f"第{index}张图片内容（共{len(full_post.images)}张）：{desc}"
        except Exception as e:
            return f"识别失败：{e}"

    @register_tool(
        name="qzone_visitors",
        description="查看自己QQ空间最近访客与访客统计（最近访客明细、来源、隐身/黄钻状态、今日与最近30天访客数）。仅限自己空间。",
        params={
            "type": "object",
            "properties": {},
            "required": []
        }
    )
    async def tool_visitors(self, event: KiraMessageBatchEvent):
        """查询当前登录账号的 QQ 空间访客统计。"""
        if not await self._check_master(event):
            return "抱歉，只有主人才能使用此功能。"
        await self._ensure_api()
        try:
            resp = await self.api.get_visitor()
            if not resp.ok:
                return f"获取访客失败：{resp.message or resp.code}"
            return QzoneParser.parse_visitors(resp.raw, self.visitor_limit)
        except Exception as e:
            logger.exception("获取访客统计失败")
            return f"获取访客失败：{e}"

    @register_tool(
        name="qzone_like",
        description="给指定说说点赞或取消点赞（同一工具两职）。用户说“点赞/赞一下”用默认 action=like；说“取消赞/去掉赞”必须传 action=unlike。",
        params={
            "type": "object",
            "properties": {
                "target_id": {"type": "string", "description": "目标QQ号"},
                "tid": {"type": "string", "description": "说说ID"},
                "action": {"type": "string", "description": "like=点赞（默认），unlike=取消点赞", "enum": ["like", "unlike"]}
            },
            "required": ["target_id", "tid"]
        }
    )
    async def tool_like(self, event: KiraMessageBatchEvent, target_id: str, tid: str, action: str = "like"):
        # 不检查黑名单，用户主动触发不受限制
        if not await self._check_master(event):
            return "抱歉，只有主人才能使用此功能。"
        block = self._target_block_reason(target_id)
        if block:
            return f"点赞被拒绝：{block}"
        await self._ensure_api()
        await self._throttle_write()
        try:
            post = QzonePost(uin=int(target_id), tid=tid)
            # 先取详情：获得说说发布时间（点赞/取消赞参数需要）并做已赞检测（避免无意义请求）
            detail_resp = await self.api.get_detail(post)
            liked = None
            if detail_resp.ok:
                raw = detail_resp.data or {}
                liked_flag = raw.get("isliked", raw.get("isLiked", raw.get("liked")))
                liked = liked_flag in (1, True, "1")
                parsed_posts = QzoneParser.parse_feeds([raw])
                if parsed_posts:
                    post = parsed_posts[0]
                    if not post.uin:
                        post.uin = int(target_id)

            if action == "unlike":
                # 已赞检测（isliked）仅作参考，不作为取消的拦截依据：
                # h5 详情的 isliked 存在滞后（刚点赞成功详情仍可能显示未赞），
                # 拦截会导致取消操作被误挡；取消本身幂等无害，直接执行。
                resp = await self.api.unlike(post, abstime=post.create_time)
                if not resp.ok:
                    return f"取消点赞失败：{resp.message}"
                return "取消点赞成功"
            # 默认：点赞
            if liked is True:
                return "这条说说已经赞过了。"
            result = await self._like(post)
            return result
        except Exception as e:
            return f"点赞/取消点赞失败：{e}"

    @register_tool(
        name="qzone_comment",
        description="评论指定的说说。不传 content 则自动生成一条评论。",
        params={
            "type": "object",
            "properties": {
                "target_id": {"type": "string", "description": "说说作者的QQ号"},
                "tid": {"type": "string", "description": "说说ID"},
                "content": {"type": "string", "description": "评论内容（可选，不传自动生成）"}
            },
            "required": ["target_id", "tid"]
        }
    )
    async def tool_comment(self, event: KiraMessageBatchEvent, target_id: str, tid: str, content: str = ""):
        # 不检查黑名单，用户主动触发不受限制
        if not await self._check_master(event):
            return "抱歉，只有主人才能使用此功能。"
        block = self._target_block_reason(target_id)
        if block:
            return f"评论被拒绝：{block}"
        await self._ensure_api()
        await self._throttle_write()
        try:
            full_post = None
            if not content:
                post = QzonePost(uin=int(target_id), tid=tid)
                detail_resp = await self.api.get_detail(post)
                if detail_resp.ok:
                    parsed_posts = QzoneParser.parse_feeds([detail_resp.data])
                    if parsed_posts:
                        full_post = parsed_posts[0]
                        prompt = f"根据以下说说内容，生成一条简洁评论（0-15字）：\n{full_post.text}"
                        content = await self._call_llm(prompt, await self._get_persona_content(), use_backend_model=False)
                        if not content:
                            content = "赞一个！"
                else:
                    prompt = "为这条说说生成一条简洁评论（0-15字）"
                    content = await self._call_llm(prompt, await self._get_persona_content(), use_backend_model=False)
                    if not content:
                        content = "赞一个！"
            post = QzonePost(uin=int(target_id), tid=tid)
            result = await self._comment(post, content)
            # 评论后自动点赞（插件直接执行，无需 AI 再调一次工具）。
            # 幂等命中（已存在相同评论）时不重复点赞。
            if self.like_when_comment and "未重复提交" not in result:
                # 透明延迟：随机 0.5~1.5s 后再点赞，错开"评论+点赞"的连续请求特征
                await asyncio.sleep(
                    random.uniform(self.like_delay_min, self.like_delay_min + self.like_delay_jitter)
                )
                try:
                    like_post = full_post or post
                    if full_post is None:
                        detail_resp = await self.api.get_detail(post)
                        if detail_resp.ok:
                            raw = detail_resp.data or {}
                            liked_flag = raw.get("isliked", raw.get("isLiked", raw.get("liked")))
                            if liked_flag in (1, True, "1"):
                                return result + "（已赞过，跳过点赞）"
                            parsed_posts = QzoneParser.parse_feeds([raw])
                            if parsed_posts:
                                like_post = parsed_posts[0]
                    # 防御：详情解析缺失 uin 时回填作者 QQ，防止点赞 unikey 拼错
                    if not like_post.uin:
                        like_post.uin = int(target_id)
                    like_resp = await self.api.like(like_post, abstime=like_post.create_time)
                    if like_resp.ok:
                        result += "，已同时点赞"
                    else:
                        result += f"（自动点赞失败：{like_resp.message}）"
                except Exception as e:
                    result += f"（自动点赞失败：{e}）"
            return result
        except Exception as e:
            return f"评论失败：{e}"

    @register_tool(
        name="qzone_delete",
        description="删除自己的一条说说。",
        params={
            "type": "object",
            "properties": {
                "tid": {"type": "string", "description": "要删除的说说的ID"}
            },
            "required": ["tid"]
        }
    )
    async def tool_delete(self, event: KiraMessageBatchEvent, tid: str):
        # 不检查黑名单，用户主动触发不受限制
        if not await self._check_master(event):
            return "抱歉，只有主人才能使用此功能。"
        await self._ensure_api()
        try:
            result = await self._delete(tid)
            return result
        except Exception as e:
            return f"删除失败：{e}"

    @register_tool(
        name="qzone_delete_comment",
        description="删除一条评论（主评论或楼中回复均可）。支持删自己空间说说下的任意评论，或删自己发在别人说说下的评论/回复。",
        params={
            "type": "object",
            "properties": {
                "target_id": {"type": "string", "description": "说说作者的QQ号（删自己空间的评论就填自己）"},
                "tid": {"type": "string", "description": "说说ID"},
                "comment_id": {"type": "string", "description": "要删除的评论ID（从 qzone_view 获取）"},
                "comment_uin": {"type": "string", "description": "评论作者QQ号（可选，楼中回复建议传）"}
            },
            "required": ["target_id", "tid", "comment_id"]
        }
    )
    async def tool_delete_comment(self, event: KiraMessageBatchEvent, target_id: str, tid: str, comment_id: str, comment_uin: str = ""):
        # 不检查黑名单，用户主动触发不受限制
        if not await self._check_master(event):
            return "抱歉，只有主人才能使用此功能。"
        block = self._target_block_reason(target_id)
        if block:
            return f"删除评论被拒绝：{block}"
        await self._ensure_api()
        try:
            real_cid = str(comment_id)
            # 删除前反查评论：优先用详情接口（h5 msgdetail_v6 实测唯一稳定路径）拉取
            # 该说说的评论列表，按 ID/UIN 匹配；若详情返回的是短楼层号，直接用其删除
            # （h5 域 delcomment_ugc 接受短楼层号 commentId）。
            try:
                matched = None
                try:
                    detail_resp = await self.api.get_detail(QzonePost(uin=int(target_id), tid=str(tid)))
                    if detail_resp.ok:
                        parsed_posts = QzoneParser.parse_feeds([detail_resp.data])
                        if parsed_posts:
                            matched = self._match_comment(parsed_posts[0].comments, comment_id, comment_uin)
                except Exception as e:
                    logger.debug(f"删除评论反查-详情接口失败: {e}")
                if matched is not None and matched.comment_id:
                    real_cid = matched.comment_id
                    logger.info(
                        f"删除评论反查真实 ID: {comment_id} -> {real_cid} "
                        f"(post={tid} uin={target_id})"
                    )
                else:
                    logger.info(
                        f"删除评论未找到唯一匹配（可能为短楼层号），直接用原 ID 尝试: {comment_id} "
                        f"post={tid} uin={target_id}"
                    )
            except Exception as e:
                logger.debug(f"删除评论反查失败（继续用原 ID）: {e}")

            resp = await self.api.delete_comment(
                uin=str(target_id),
                tid=str(tid),
                comment_id=real_cid,
                comment_uin=str(comment_uin or ""),
            )
            if not resp.ok:
                return f"删除评论失败：{resp.message}"
            return "评论删除成功"
        except Exception as e:
            return f"删除评论失败：{e}"

    @staticmethod
    def _match_comment(comments: List[QzoneComment], comment_id: str, comment_uin: str = "") -> Optional[QzoneComment]:
        """按评论 ID（兼容真实 comment_id 与短楼层号 tid）和 UIN 匹配评论；0/多条返回 None。"""
        matches = [
            cmt for cmt in comments
            if str(cmt.comment_id) == str(comment_id)
            or str(cmt.tid) == str(comment_id)
        ]
        if comment_uin:
            matches = [cmt for cmt in matches if str(cmt.uin) == str(comment_uin)]
        return matches[0] if len(matches) == 1 else None

    @register_tool(
        name="qzone_reply_comment",
        description="回复某条评论。评论 ID 与作者 UIN 从 qzone_view 获取；同一说说内评论 ID 重复时必须同时传 comment_uin。",
        params={
            "type": "object",
            "properties": {
                "target_id": {"type": "string", "description": "说说作者的QQ号"},
                "tid": {"type": "string", "description": "说说ID"},
                "comment_id": {"type": "string", "description": "要回复的评论ID"},
                "comment_uin": {"type": "string", "description": "评论作者QQ号（同一说说内评论ID重复时必填）"},
                "content": {"type": "string", "description": "回复内容（可选，不传自动生成）"}
            },
            "required": ["target_id", "tid", "comment_id"]
        }
    )
    async def tool_reply_comment(self, event: KiraMessageBatchEvent, target_id: str, tid: str, comment_id: str, comment_uin: str = "", content: str = ""):
        # 不检查黑名单，用户主动触发不受限制
        if not await self._check_master(event):
            return "抱歉，只有主人才能使用此功能。"
        block = self._target_block_reason(target_id)
        if block:
            return f"回复被拒绝：{block}"
        await self._ensure_api()
        await self._throttle_write()
        try:
            post = QzonePost(uin=int(target_id), tid=tid)
            detail_resp = await self.api.get_detail(post)
            if not detail_resp.ok:
                return "获取说说详情失败，无法获取评论者信息"
            parsed_posts = QzoneParser.parse_feeds([detail_resp.data])
            if not parsed_posts:
                return "解析说说详情失败"
            full_post = parsed_posts[0]
            # view 展示的是真实评论 ID（comment_id），删除接口也按它匹配；
            # 这里若只比对 tid，遇到"展示长 ID、tid 是短楼层号/合成 id"就永远匹配不上。
            matches = [
                cmt for cmt in full_post.comments
                if str(cmt.comment_id) == str(comment_id) or str(cmt.tid) == str(comment_id)
            ]
            if comment_uin:
                matches = [cmt for cmt in matches if str(cmt.uin) == str(comment_uin)]
            if not matches:
                suffix = f"、UIN: {comment_uin}" if comment_uin else ""
                return f"未找到指定的评论 ID: {comment_id}{suffix}"
            if len(matches) > 1:
                options = "，".join(f"{cmt.nickname}(UIN:{cmt.uin})" for cmt in matches)
                return f"评论 ID {comment_id} 不唯一，请补充 comment_uin。可选目标：{options}"
            target_comment = matches[0]
            final_content = content
            if not final_content:
                _, prompt_content = self._parse_comment_content(target_comment.content)
                prompt = f"用户 {target_comment.nickname} 评论了你的说说：{prompt_content}，请生成一条简洁回复（0-15字）。"
                final_content = await self._call_llm(prompt, await self._get_persona_content(), use_backend_model=False)
                if not final_content:
                    return "生成回复内容为空"
            root_comment = self._find_root_comment(full_post.comments, target_comment)
            result = await self._reply_comment(
                post,
                target_comment,
                final_content,
                root_comment=root_comment,
            )
            return result
        except Exception as e:
            return f"回复失败：{e}"

