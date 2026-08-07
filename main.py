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
from .qzone.utils import download_file, looks_like_image
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

MAX_HISTORY = 10
MAX_REPLIED_CACHE = 1000
IMAGE_REGISTRY_CAP = 20

# 登录失效后强制刷新的最小间隔（防失效风暴）
FORCE_REFRESH_MIN_INTERVAL = 3
# 常规刷新节流
REFRESH_THROTTLE = 300


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

        self.scheduler = AsyncIOScheduler()

        # 定时配置
        self.auto_publish_schedule = cfg.get("auto_publish_schedule", "")
        self.auto_comment_schedule = cfg.get("auto_comment_schedule", "")
        self.auto_reply_schedule = cfg.get("auto_reply_schedule", "")
        self.auto_reply_enabled = cfg.get("auto_reply_enabled", False)
        self.like_when_comment = cfg.get("like_when_comment", False)

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
        self.cookie_refresh_interval = self._parse_interval_seconds(cfg.get("cookie_refresh_interval", "2h"))
        # 用即刷节流：调用空间功能时，若距上次刷新超过该间隔则顺手刷新（秒），0/None 关闭
        self.cookie_refresh_on_use = self._parse_interval_seconds(cfg.get("cookie_refresh_on_use", "10m"))

        # 图片识图相关配置
        self.image_manifest_enabled = cfg.get("image_manifest_enabled", True)
        self.image_manifest_count = max(1, int(cfg.get("image_manifest_count", 5) or 5))
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

        # Cookie 刷新控制
        self._cookie_refresh_lock = asyncio.Lock()
        self._last_cookie_refresh = 0.0
        self._cookie_refresh_task: Optional[asyncio.Task] = None

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
        # 正在后台描述中的图片（按元素 id 去重）
        self._describing: set[int] = set()
        # 空间图片 url -> md5 映射（避免重复下载）
        self._url_md5: dict[str, str] = {}
        # 主动发布成功使用过的图片指纹及时间，仅在发布成功后写入。
        self._published_image_history: list[dict] = []

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
                {"identity": item.get("identity") or item.get("source", ""), "time": item.get("time", 0)}
                for item in data.get("published_image_history", [])[-IMAGE_REGISTRY_CAP:]
                if item.get("identity") or item.get("source")
            ]
            logger.info(f"已加载持久化状态：历史说说 {len(self.my_posts_history)} 条，已回复评论 {len(self.replied_comments)} 条")
        except Exception as e:
            logger.warning(f"加载插件状态失败: {e}")

    def _save_state(self):
        try:
            path = self._state_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "replied_comments": list(self.replied_comments)[-MAX_REPLIED_CACHE:],
                "my_posts_history": self.my_posts_history[-MAX_HISTORY:],
                "published_image_history": self._published_image_history[-IMAGE_REGISTRY_CAP:],
            }
            path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        except Exception as e:
            logger.warning(f"保存插件状态失败: {e}")

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
        for index in range(1, len(self._manifest_entries(sid)) + 1):
            resolved = await self._resolve_manifest_images(sid, [index])
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
        """
        if not self.auto_refresh:
            return False
        async with self._cookie_refresh_lock:
            now = time.time()
            min_interval = FORCE_REFRESH_MIN_INTERVAL if force else REFRESH_THROTTLE
            if (now - self._last_cookie_refresh) < min_interval:
                if force and self._last_cookie_refresh > 0:
                    # 刚刷新过：不再骚扰 OneBot，让调用方带现有 Cookie 直接重试。
                    # QZone 的 -100 很多是抽风型（非真过期），延迟重试比反复刷新更有效。
                    logger.debug("距上次刷新过近，跳过实际刷新，直接重试")
                    return True
                return False
            new_cookie = await self._get_cookie_from_onebot()
            if not new_cookie:
                logger.warning("从 OneBot 获取 Cookie 失败，保留现有会话")
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
                logger.error(f"应用新 Cookie 失败: {e}")
                return False

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
            # 用即刷（节流）：距上次刷新超过间隔就顺手刷新，保持 Cookie 新鲜
            if (
                self.auto_refresh
                and self.cookie_refresh_on_use is not None
                and (time.time() - self._last_cookie_refresh) > self.cookie_refresh_on_use
            ):
                try:
                    await self._refresh_cookie(force=False)
                except Exception as e:
                    logger.debug(f"用即刷失败（不影响本次调用）: {e}")
            return
        if self.auto_refresh:
            try:
                if await self._refresh_cookie(force=False):
                    return
            except Exception as e:
                logger.error(f"刷新 Cookie 异常: {e}")
        if self.api is None:
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
            logger.info(f"QQ空间 API 初始化成功，当前账号: {self.my_uin}")
            self._init_failed = False
        except Exception as e:
            logger.error(f"初始化失败: {e}")
            self._init_failed = True
            raise

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

    def _ensure_ada(self):
        """获取 QQ 适配器实例（兼容新版 KiraAI）"""
        if self._ada_obj:
            return
        ada_name = self.qq_ada
        if ada_name:
            ada = self.ctx.adapter_mgr.get_adapter(ada_name)
            if ada:
                self._ada_obj = ada
                return
            logger.warning(f"未找到配置的适配器: {ada_name}，将自动查找")
        try:
            if hasattr(self.ctx.adapter_mgr, 'get_adapters'):
                adapters = self.ctx.adapter_mgr.get_adapters()
                for name, ada in adapters.items():
                    if hasattr(ada, 'info') and ada.info.platform == "QQ":
                        self._ada_obj = ada
                        logger.info(f"自动找到 QQ 适配器: {name}")
                        return
            if hasattr(self.ctx.adapter_mgr, '_adapters'):
                for name, ada in self.ctx.adapter_mgr._adapters.items():
                    if hasattr(ada, 'info') and ada.info.platform == "QQ":
                        self._ada_obj = ada
                        logger.info(f"自动找到 QQ 适配器: {name}")
                        return
            logger.error("未找到平台为 QQ 的适配器，无法调用 OneBot 接口")
        except Exception as e:
            logger.error(f"查找 QQ 适配器时出错: {e}")

    async def _call_onebot_action(self, action: str, params: dict):
        self._ensure_ada()
        if not self._ada_obj:
            raise RuntimeError("无法获取 QQ 适配器")
        ob_client = self._ada_obj.get_client()
        res = await ob_client.send_action(action, params)
        return res

    async def _get_cookie_from_onebot(self) -> Optional[str]:
        try:
            data = await self._call_onebot_action("get_cookies", {"domain": "user.qzone.qq.com"})
            if data.get("status") != "ok":
                logger.error(f"oneBot 返回错误: {data}")
                return None
            cookie_str = data.get("data", {}).get("cookies")
            if not cookie_str:
                logger.error("返回数据中未找到 cookies 字段")
                return None
            logger.info("成功从 oneBot 获取 Cookie")
            return cookie_str
        except Exception as e:
            logger.error(f"从 oneBot 获取 Cookie 失败: {e}")
            return None

    # ---------- 插件生命周期 ----------
    async def initialize(self):
        self._load_state()

        if self.auto_refresh:
            try:
                await self._refresh_cookie(force=True)
            except Exception as e:
                logger.warning(f"启动时刷新 Cookie 失败: {e}")

        if self.session is None:
            if not self.cookies_str:
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

            if self.api:
                await self.api.close()
            self.api = None
            self.session = None

            try:
                for job in self.scheduler.get_jobs():
                    job.remove()
                self.scheduler.shutdown(wait=False)
            except Exception:
                pass
            self.scheduler = AsyncIOScheduler()
            self._jobs_added = False
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

        self._ensure_ada()
        if not self._ada_obj:
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
            candidates_with_desc = []
            for url in candidates:
                desc = await self._describe_image_url(url)
                candidates_with_desc.append(
                    (url, candidate_label(desc))
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

    @staticmethod
    def _split_img_choice(text: str) -> tuple[str, Optional[int]]:
        cleaned, choices = QzonePlugin._split_img_choices(text)
        return cleaned, (choices[0] if choices else None)

    async def _auto_comment_job(self):
        if self._is_in_blackout():
            logger.info("当前时间处于黑名单内，跳过自动评论")
            return
        try:
            await self._ensure_api()
            if self.task_group_ids or self.task_private_ids:
                instruction = "【评论任务】请对最近的好友（不包括自己）说说进行评论，自然一点。严禁内容重复和复读。注意，检查用户昵称来不要评论自己发布的QQ说说，优先没有评论过的内容，该内容时间戳与当前系统时间戳不得超过7天，否则不评论。"
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
            if not posts:
                return
            selected = random.sample(posts, min(self.max_comments_per_cycle, len(posts)))
            for post in selected:
                prompt = f"根据以下说说内容，生成一条简短评论（10-20字）：\n{post.text}"
                # 可选：识图后评论
                if self.auto_comment_image_desc and post.images and self.qzone_image_desc_enabled:
                    desc = await self._describe_image_url(post.images[0])
                    if desc:
                        prompt += f"\n该说说配图内容：{desc}"
                comment_text = await self._call_llm(prompt, await self._get_persona_content(), use_backend_model=True)
                if not comment_text:
                    continue
                try:
                    await self._comment(post, comment_text)
                    logger.info(f"自动评论成功并已确认落地: {post.tid} -> {comment_text}")
                except Exception as e:
                    logger.warning(f"自动评论失败或未落地: {post.tid} -> {e}")
                    continue
                if self.like_when_comment:
                    like_resp = await self.api.like(post, abstime=post.create_time)
                    if like_resp.ok:
                        logger.info(f"自动点赞成功: {post.tid}")
                    else:
                        logger.warning(f"自动点赞失败: {post.tid} -> {like_resp.message}")
                await asyncio.sleep(2)
        except Exception as e:
            logger.error(f"自动评论任务失败: {e}")

    async def _auto_reply_job(self):
        if self._is_in_blackout():
            logger.info("当前时间处于黑名单内，跳过自动回复")
            return
        try:
            await self._ensure_api()
            if self.task_group_ids or self.task_private_ids:
                instruction = "【回复任务】请回复你最近说说下的新评论，使用qzone_reply_comment和评论自身的ID、UIN准确回复，target_id为自己的QQ号。自然一点，严禁内容重复和复读。根据评论作者UIN不回复自己，优先没有回复过的用户和新回复，否则不回复。"
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
                    prompt = f"用户 {comment.nickname} 评论了你的说说：{prompt_content}，请生成一条友好回复（10-30字）。"
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
                    if new_replies >= self.max_replies_per_cycle:
                        break
                if new_replies >= self.max_replies_per_cycle:
                    break
            logger.info(f"自动回复任务完成，共回复 {new_replies} 条新评论")
        except Exception as e:
            logger.error(f"自动回复任务失败: {e}")

    async def _register_current_event_images(self, event: KiraMessageBatchEvent):
        if not self.image_manifest_enabled or self.auto_attach_recent_image:
            return
        sid = getattr(event, "sid", "")
        if not sid:
            return
        registry = self._image_registry.setdefault(sid, [])
        for message in getattr(event, "messages", None) or []:
            sender = ""
            if message.sender:
                sender = message.sender.nickname or str(message.sender.user_id or "未知")
            for elem in getattr(message, "chain", []) or []:
                if not isinstance(elem, Image):
                    continue
                if any(e.get("elem") is elem for e in registry):
                    continue
                registry.append({"elem": elem, "sender": sender,
                                 "time": int(getattr(message, "timestamp", 0) or time.time()),
                                 "desc": getattr(elem, "caption", None),
                                 "msg_id": getattr(message, "message_id", None)})
        if len(registry) > IMAGE_REGISTRY_CAP:
            del registry[: len(registry) - IMAGE_REGISTRY_CAP]

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
                        registry.append({"source": "url", "url": url, "sender": sender,
                                         "time": timestamp, "desc": None, "msg_id": msg_id})
            if len(registry) > IMAGE_REGISTRY_CAP:
                del registry[: len(registry) - IMAGE_REGISTRY_CAP]
        return messages[-count:]

    async def _fetch_chat_history(self, source_type: str, source_id: str, count: int = 10) -> List[str]:
        messages = await self._fetch_history_messages(source_type, source_id, max(count, 20))
        return [
            f"{msg.get('sender', {}).get('nickname', '未知')}: {self._extract_text_simple(msg.get('message', []))}"
            for msg in messages[-count:]
        ]

    async def _fetch_recent_images(self, source_type: str, source_id: str, max_count: int = 1) -> List[str]:
        """从历史登记候选并返回最新图片 URL（兼容吸附模式）。"""
        messages = await self._fetch_history_messages(source_type, source_id, 20)
        urls = []
        for msg in reversed(messages):
            for seg in msg.get("message") or []:
                if seg.get("type") == "image":
                    url = html.unescape(str((seg.get("data") or {}).get("url") or "").strip().strip('"').strip("'"))
                    if url:
                        urls.append(url)
                        if len(urls) >= max_count:
                            return urls
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
            logger.error("无法从事件中获取会话ID")
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
            self._url_md5[source_url] = md5
        cached = await self._cache_get_desc(md5)
        if cached:
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
        """描述 URL 图片（先查 url->md5 映射避免重复下载）"""
        if not url:
            return ""
        known_md5 = self._url_md5.get(url)
        if known_md5:
            cached = await self._cache_get_desc(known_md5)
            if cached:
                return cached
        data = await download_file(url)
        if not data:
            return ""
        return await self._describe_image_bytes(data, source_url=url)

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
            for img in images:
                registry.append({
                    "elem": img,
                    "sender": sender,
                    "time": int(event.message.timestamp or time.time()),
                    "desc": None,
                    "msg_id": getattr(event.message, "message_id", None),
                })
            if len(registry) > IMAGE_REGISTRY_CAP:
                del registry[: len(registry) - IMAGE_REGISTRY_CAP]
        except Exception as e:
            logger.debug(f"收集图片失败: {e}")

    def _manifest_entries(self, sid: str) -> list[dict]:
        """取该会话最近 N 张图片（保持时间顺序，供注入与序号解析共用）"""
        registry = self._image_registry.get(sid) or []
        return registry[-self.image_manifest_count:]

    async def _resolve_entry_desc(self, entry: dict) -> str:
        """解析图片描述，兼容实时 Image 与历史 URL 候选。"""
        if entry.get("desc"):
            return entry["desc"]
        if entry.get("source") == "url":
            desc = await self._describe_image_url(entry.get("url", ""))
            if desc:
                entry["desc"] = desc
            return desc
        elem: Image = entry["elem"]
        if elem.caption:
            entry["desc"] = elem.caption
            return elem.caption
        try:
            md5 = await elem.hash_image()
        except Exception:
            md5 = None
        if md5:
            cached = await self._cache_get_desc(md5)
            if cached:
                entry["desc"] = cached
                return cached
        # 未命中：启动后台描述，本轮先跳过
        self._describe_element_background(entry)
        return ""

    def _describe_element_background(self, entry: dict):
        elem: Image = entry["elem"]
        key = id(elem)
        if key in self._describing:
            return
        self._describing.add(key)

        async def _run():
            try:
                path = await elem.to_path()
                data = Path(path).read_bytes()
                desc = await self._describe_image_bytes(data)
                if desc:
                    entry["desc"] = desc
            except Exception as e:
                logger.debug(f"后台描述图片失败: {e}")
            finally:
                self._describing.discard(key)

        asyncio.create_task(_run())

    @on.llm_request()
    async def _inject_image_manifest(self, event, req: LLMRequest, tag_set, *_):
        """向本轮请求注入近期图片清单（persist=False，不落记忆）"""
        # 吸附模式开启时彻底回到旧行为，不注入清单
        if not self.image_manifest_enabled or self.auto_attach_recent_image:
            return
        try:
            sid = getattr(event, "sid", "")
            if not sid:
                return
            await self._register_current_event_images(event)
            entries = self._manifest_entries(sid)
            if not entries:
                return
            lines = []
            for i, entry in enumerate(entries, 1):
                desc = await self._resolve_entry_desc(entry)
                display_desc = candidate_label(desc)
                time_str = datetime.fromtimestamp(entry["time"]).strftime("%m-%d %H:%M")
                sender = entry.get("sender") or "未知"
                lines.append(f"{i}. [{time_str} {sender}] {display_desc}")
            if not lines:
                return
            text = (
                "[近期图片] 本群/会话最近出现的图片及内容描述，调用 qzone_publish 发说说时可用 image_indices 参数引用序号配图：\n"
                + "\n".join(lines)
            )
            req.user_prompt.insert(0, Prompt(
                text, name="qzone_images", source="qzone_plugin", persist=False
            ))
        except Exception as e:
            logger.debug(f"注入图片清单失败: {e}")

    async def _refresh_image_url(self, entry: dict) -> bool:
        """图片 URL 过期时，用 get_msg 按 message_id 换取新签名 URL（rkey 续命）"""
        msg_id = entry.get("msg_id")
        if not msg_id:
            return False
        try:
            res = await self._call_onebot_action("get_msg", {"message_id": int(msg_id)})
            if not res or res.get("status") != "ok":
                return False
            for seg in res.get("data", {}).get("message", []):
                if seg.get("type") == "image":
                    url = seg.get("data", {}).get("url", "")
                    if url:
                        url = html.unescape(url.strip().strip('"').strip("'"))
                        elem: Image = entry["elem"]
                        elem.image = url
                        elem.file = url
                        elem.image_type = "url"
                        elem._temp_path = None  # 作废可能已污染的缓存文件
                        logger.info(f"已通过 get_msg 刷新过期图片 URL (msg_id={msg_id})")
                        return True
        except Exception as e:
            logger.debug(f"刷新图片 URL 失败 (msg_id={msg_id}): {e}")
        return False

    async def _resolve_manifest_images(self, sid: str, indices: list) -> List[str]:
        """按清单序号解析图片；历史 URL 直接传入，实时 Image 转成本地路径。"""
        paths = []
        entries = self._manifest_entries(sid)
        try:
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
                if entry.get("source") == "url":
                    url = entry.get("url", "")
                    if url:
                        paths.append(url)
                    continue
                elem: Image = entry["elem"]
                for attempt in range(2):
                    try:
                        path = await elem.to_path()
                        if path:
                            with open(path, "rb") as f:
                                head = f.read(16)
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
        finally:
            pass

    # ---------- 核心 API 封装 ----------
    def _image_identity(self, source: str) -> str:
        return self._url_md5.get(source, source)

    def _is_recently_published_image(self, source: str) -> bool:
        identity = self._image_identity(source)
        interval = self.auto_publish_image_dedupe_interval
        if not interval:
            return False
        now = time.time()
        return any(
            item.get("identity") == identity and now - float(item.get("time", 0)) < interval
            for item in self._published_image_history
        )

    def _record_published_images(self, sources: list[str]):

        now = int(time.time())
        for source in sources:
            self._published_image_history.append({"identity": self._image_identity(source), "time": now})
        self._published_image_history = self._published_image_history[-IMAGE_REGISTRY_CAP:]
        self._save_state()

    async def _publish(self, text: str, image_urls: list, allow_image_drop: bool = False) -> str:
        await self._ensure_api()
        post = QzonePost(text=text, images=image_urls)
        resp = await self.api.publish(post, allow_image_drop=allow_image_drop)
        if not resp.ok:
            raise RuntimeError(f"发布失败: {resp.message}")
        if image_urls and not (allow_image_drop and resp.message):
            self._record_published_images([str(x) for x in image_urls])
        tid = resp.data.get("tid")
        result = f"说说发布成功！TID: {tid}"
        if resp.message:
            result += f"（注意：{resp.message}）"
        return result

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

    async def _get_detail_post(self, post: QzonePost) -> QzonePost:
        detail_resp = await self.api.get_detail(post)
        if not detail_resp.ok:
            raise RuntimeError(f"获取说说详情失败: {detail_resp.message}")
        parsed_posts = QzoneParser.parse_feeds([detail_resp.data])
        if not parsed_posts:
            raise RuntimeError("说说详情解析失败")
        return parsed_posts[0]

    async def _comment(self, post: QzonePost, content: str) -> str:
        """提交评论并回读确认；只有确认新增评论后才能报告成功或继续点赞。"""
        await self._ensure_api()
        if not self.my_uin:
            raise RuntimeError("无法确认当前登录 QQ，已取消评论")

        before_post = await self._get_detail_post(post)
        before_count = self._count_own_comment(before_post, content)

        resp = await self.api.comment(post, content)
        if not resp.ok:
            raise RuntimeError(f"评论接口失败: {resp.message}")

        last_error = ""
        for delay in (0, 1, 2, 4):
            if delay:
                await asyncio.sleep(delay)
            try:
                current_post = await self._get_detail_post(post)
                current_count = self._count_own_comment(current_post, content)
                if current_count > before_count:
                    logger.info(
                        "QZone评论落地确认: post=%s own_uin=%s before=%s after=%s",
                        post.tid,
                        self.my_uin,
                        before_count,
                        current_count,
                    )
                    return "评论成功（已确认落地）"
                last_error = f"匹配评论数未增加（{before_count}->{current_count}）"
            except Exception as e:
                last_error = str(e)

        raise RuntimeError(
            "评论接口返回成功，但详情回读未发现新增评论；"
            f"不会执行自动点赞。{last_error}"
        )

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
            prompt = f"用户 {comment.nickname} 评论了你的说说：{prompt_content}，请生成一条友好回复（10-30字）。"
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
        return (
            f"{indent}└ [{label} ID:{comment.tid} UIN:{comment.uin}] "
            f"{comment.nickname}{relation} [{time_str}]: {content}"
        )

    def _add_post_to_history(self, text: str):
        self.my_posts_history.append(text)
        if len(self.my_posts_history) > MAX_HISTORY:
            self.my_posts_history.pop(0)
        self._save_state()

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
        description="发布一条说说到自己的QQ空间。配图方式：1) images 参数传聊天中出现过的图片路径（如 data/temp/xxx.jpg，你能在聊天记录里看到这些图片的内容描述和路径）；2) image_indices 参数引用[近期图片]清单中的序号。优先使用你真正了解内容的方式配图；都不传时默认纯文字发布。",
        params={
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "说说内容"},
                "images": {
                    "type": "array", "items": {"type": "string"},
                    "description": "图片本地路径或URL列表（可选）。可传聊天记录里看到的图片 file_path",
                    "default": []
                },
                "image_indices": {
                    "type": "array", "items": {"type": "integer"},
                    "description": "[近期图片]清单中的图片序号（从1开始，可选）",
                    "default": []
                }
            },
            "required": ["text"]
        }
    )
    async def tool_publish(self, event: KiraMessageBatchEvent, text: str, images: list = None, image_indices: list = None):
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
                if not isinstance(item, str):
                    continue
                if 'example.com' in item:
                    continue
                valid_sources.append(item)
            # 清单序号配图
            if image_indices:
                resolved = await self._resolve_manifest_images(event.sid, image_indices)
                if not resolved:
                    if task_target is None:
                        return (
                            f"未能从[近期图片]清单解析出图片（当前会话清单为空，或序号 {image_indices} 超出范围），说说未发布。"
                            "如确认发纯文字，请不带 image_indices 重试；"
                            "如想配图，可改用 images 参数传图片 URL 或本地路径（如 data/temp/xxx.jpg）。"
                        )
                    logger.info(
                        "定时发布清单选择不可用，按资源降级: target=%s indices=%s",
                        task_target,
                        image_indices,
                    )
                valid_sources.extend(resolved)
            elif images and not valid_sources:
                return "images 参数中的地址均无效，说说未发布。请传有效的图片 URL 或本地路径。"
            valid_sources = self._dedupe_sources(valid_sources)
            if task_target is not None:
                if task_target > 0:
                    valid_sources = await self._fill_scheduled_publish_sources(
                        event.sid, valid_sources, task_target
                    )
                    if len(valid_sources) < task_target:
                        recent = await self._fetch_recent_images_for_event(
                            event, max_count=task_target
                        )
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
            # （吸附图下载失败会降级为纯文字发布，保持“有时配有时不配”的随机感）
            allow_drop = False
            if (
                task_policy is None
                and not valid_sources
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
        description="查看QQ空间说说。如果不提供target_id，默认查看自己的空间；要查看好友动态，请提供好友QQ号。返回的每条说说包含ID、发布时间、配图数量和最新评论。如果说说有配图且你需要了解图片内容（比如对方文字暗示了图片、或你打算认真评论），可调用 qzone_describe_image。",
        params={
            "type": "object",
            "properties": {
                "target_id": {"type": "string", "description": "目标QQ号（可选）"},
                "num": {"type": "integer", "description": "查看条数，默认1", "default": 1}
            },
        }
    )
    async def tool_view(self, event: KiraMessageBatchEvent, target_id: str = None, num: int = 1):
        # 查看是只读操作，所有用户可用，不做权限检查
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
                if p.comments:
                    comment_lines = []
                    replies_by_parent: dict[int, list[QzoneComment]] = {}
                    main_comments = []
                    for cmt in p.comments:
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
                    for cmt in p.comments:
                        if id(cmt) in shown_comments:
                            continue
                        cmt_time_str = cmt.create_time_str or self._format_time(cmt.create_time)
                        label = "主评论" if cmt.parent_tid is None else "楼中回复"
                        indent = "  " if cmt.parent_tid is None else "    "
                        comment_lines.append(self._format_comment_line(cmt, label, indent, cmt_time_str))
                        shown_comments.add(id(cmt))
                    if comment_lines:
                        line += "\n评论区：\n" + "\n".join(comment_lines[:20])
                lines.append(line)
            return "\n---\n".join(lines)
        except Exception as e:
            return f"查看失败：{e}"

    @register_tool(
        name="qzone_describe_image",
        description="查看说说中某张配图的实际内容。他人的说说：当文字暗示图片很重要或你打算评论前想了解图片内容时调用。自己的说说：一般不要调用（配图本来就是你选的），仅当确有必要时再用，如想确认当时配的图是否合适或回复评论前需回顾图片内容。",
        params={
            "type": "object",
            "properties": {
                "target_id": {"type": "string", "description": "说说作者的QQ号"},
                "tid": {"type": "string", "description": "说说ID"},
                "index": {"type": "integer", "description": "第几张图片（从1开始），默认1", "default": 1}
            },
            "required": ["target_id", "tid"]
        }
    )
    async def tool_describe_image(self, event: KiraMessageBatchEvent, target_id: str, tid: str, index: int = 1):
        if not self.qzone_image_desc_enabled:
            return "空间图片识别功能未启用。"
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
            data = await download_file(url)
            if not data:
                return "图片下载失败，可能已过期。"
            desc = await self._describe_image_bytes(data, source_url=url)
            if not desc:
                return "图片识别失败（识图模型不可用或缓存未命中）。"
            return f"第{index}张图片内容（共{len(full_post.images)}张）：{desc}"
        except Exception as e:
            return f"识别失败：{e}"

    @register_tool(
        name="qzone_visitors",
        description="查看自己QQ空间最近访客和访客统计。返回最近访客明细、来源、隐身/黄钻状态，以及今日和最近30天访客数。仅支持查看当前登录账号自己的空间。",
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
        description="给指定的说说点赞",
        params={
            "type": "object",
            "properties": {
                "target_id": {"type": "string", "description": "目标QQ号"},
                "tid": {"type": "string", "description": "说说ID"}
            },
            "required": ["target_id", "tid"]
        }
    )
    async def tool_like(self, event: KiraMessageBatchEvent, target_id: str, tid: str):
        # 不检查黑名单，用户主动触发不受限制
        if not await self._check_master(event):
            return "抱歉，只有主人才能使用此功能。"
        await self._ensure_api()
        try:
            post = QzonePost(uin=int(target_id), tid=tid)
            # 先取详情：获得说说发布时间（点赞必需参数）并做已赞检测
            detail_resp = await self.api.get_detail(post)
            if detail_resp.ok:
                raw = detail_resp.data or {}
                liked_flag = raw.get("isliked", raw.get("isLiked", raw.get("liked")))
                if liked_flag in (1, True, "1"):
                    return "这条说说已经赞过了。"
                parsed_posts = QzoneParser.parse_feeds([raw])
                if parsed_posts:
                    post = parsed_posts[0]
                    if not post.uin:
                        post.uin = int(target_id)
            result = await self._like(post)
            return result
        except Exception as e:
            return f"点赞失败：{e}"

    @register_tool(
        name="qzone_comment",
        description="评论指定的说说，如果不提供内容则AI自动生成。",
        params={
            "type": "object",
            "properties": {
                "target_id": {"type": "string", "description": "目标QQ号"},
                "tid": {"type": "string", "description": "说说ID"},
                "content": {"type": "string", "description": "评论内容（可选）"}
            },
            "required": ["target_id", "tid"]
        }
    )
    async def tool_comment(self, event: KiraMessageBatchEvent, target_id: str, tid: str, content: str = ""):
        # 不检查黑名单，用户主动触发不受限制
        if not await self._check_master(event):
            return "抱歉，只有主人才能使用此功能。"
        await self._ensure_api()
        try:
            full_post = None
            if not content:
                post = QzonePost(uin=int(target_id), tid=tid)
                detail_resp = await self.api.get_detail(post)
                if detail_resp.ok:
                    parsed_posts = QzoneParser.parse_feeds([detail_resp.data])
                    if parsed_posts:
                        full_post = parsed_posts[0]
                        prompt = f"根据以下说说内容，生成一条简短评论（10-20字）：\n{full_post.text}"
                        content = await self._call_llm(prompt, await self._get_persona_content(), use_backend_model=False)
                        if not content:
                            content = "赞一个！"
                else:
                    prompt = "为这条说说生成一条简短评论（10-20字）"
                    content = await self._call_llm(prompt, await self._get_persona_content(), use_backend_model=False)
                    if not content:
                        content = "赞一个！"
            post = QzonePost(uin=int(target_id), tid=tid)
            result = await self._comment(post, content)
            # 评论后自动点赞（插件直接执行，无需 AI 再调一次工具）
            if self.like_when_comment:
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
        description="删除自己的一条说说",
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
        name="qzone_reply_comment",
        description="回复指定评论。先从 qzone_view 获取评论 ID 和 UIN；当同一说说内 ID 重复时必须同时传 comment_uin，避免回复错人。",
        params={
            "type": "object",
            "properties": {
                "target_id": {"type": "string", "description": "说说作者的QQ号"},
                "tid": {"type": "string", "description": "说说ID"},
                "comment_id": {"type": "string", "description": "要回复的评论ID"},
                "comment_uin": {"type": "string", "description": "评论作者QQ号；同一说说内评论ID重复时必填"},
                "content": {"type": "string", "description": "回复内容（可选）"}
            },
            "required": ["target_id", "tid", "comment_id"]
        }
    )
    async def tool_reply_comment(self, event: KiraMessageBatchEvent, target_id: str, tid: str, comment_id: str, comment_uin: str = "", content: str = ""):
        # 不检查黑名单，用户主动触发不受限制
        if not await self._check_master(event):
            return "抱歉，只有主人才能使用此功能。"
        await self._ensure_api()
        try:
            post = QzonePost(uin=int(target_id), tid=tid)
            detail_resp = await self.api.get_detail(post)
            if not detail_resp.ok:
                return "获取说说详情失败，无法获取评论者信息"
            parsed_posts = QzoneParser.parse_feeds([detail_resp.data])
            if not parsed_posts:
                return "解析说说详情失败"
            full_post = parsed_posts[0]
            matches = [cmt for cmt in full_post.comments if str(cmt.tid) == str(comment_id)]
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
                prompt = f"用户 {target_comment.nickname} 评论了你的说说：{prompt_content}，请生成一条友好回复（10-30字）。"
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

