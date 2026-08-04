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
        self.auto_publish_image_prob = cfg.get("auto_publish_image_prob", 0.5)

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
            }
            path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        except Exception as e:
            logger.warning(f"保存插件状态失败: {e}")

    # ---------- Persona 获取（每次实时读取，跟随 WebUI 切换） ----------
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
    def _parse_interval_seconds(s) -> Optional[int]:
        """解析 '6h' / '30m' / '7200' 等间隔；空或 0 返回 None（禁用）"""
        if s is None:
            return None
        s = str(s).strip().lower()
        if not s or s in ("0", "0s", "0m", "0h"):
            return None
        m = re.match(r"^(\d+(?:\.\d+)?)([hms]?)$", s)
        if not m:
            logger.warning(f"无法解析 Cookie 刷新间隔: {s}，使用默认 6h")
            return 6 * 3600
        val = float(m.group(1))
        unit = m.group(2)
        if unit == "h":
            val *= 3600
        elif unit == "s":
            pass
        else:  # 默认按分钟
            val *= 60
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

    async def _send_task_instruction(self, instruction_text: str, with_place: bool = True) -> bool:
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
                extra={"qzone_task": True},
            ),
            timestamp=t,
        )
        event.session = Session(
            adapter_name=adapter_name,
            session_type=session_type,
            session_id=target_id,
        )
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

            if self.task_group_ids or self.task_private_ids:
                instruction = (
                    "【定时任务】请根据最近聊天发布一条说说，自然一点，不要提及这是定时任务。"
                    "配图根据内容随机决定配 1 张或多张："
                    "用 images 参数传聊天记录里见过的图片 URL 或本地路径，"
                    "或用 image_indices 参数引用[近期图片]清单中的序号。"
                )
                if await self._send_task_instruction(instruction):
                    self.last_auto_publish_time = datetime.now()
                return

            await self._legacy_auto_publish()
            self.last_auto_publish_time = datetime.now()
        except Exception as e:
            logger.error(f"自动发布任务失败: {e}")

    async def _legacy_auto_publish(self):
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

        # 提前获取候选图及描述，让 LLM 知情选图
        image_urls: list[str] = []
        if source_id and self.auto_publish_image_prob > 0 and random.random() < self.auto_publish_image_prob:
            candidates = await self._fetch_recent_images(source_type, source_id, max_count=3)
            if candidates:
                desc_lines = []
                for i, url in enumerate(candidates, 1):
                    desc = await self._describe_image_url(url)
                    if desc:
                        desc_lines.append(f"{i}. {desc}")
                    else:
                        desc_lines.append(f"{i}. （内容未知）")
                prompt += (
                    "\n\n以下是最近聊天中出现的图片及内容描述：\n" + "\n".join(desc_lines) +
                    "\n如果你认为说说适合配图，请在正文后另起一行输出 IMG:序号（如 IMG:1），否则不要输出 IMG 行。"
                )
                text_with_choice = await self._call_llm(prompt, system_prompt, use_backend_model=True)
                text, chosen = self._split_img_choice(text_with_choice)
                if chosen is not None and 1 <= chosen <= len(candidates):
                    image_urls = [candidates[chosen - 1]]
                    logger.info(f"后台发布选用第 {chosen} 张图")
            else:
                logger.info("未找到图片，将只发布文字")
                text_with_choice = await self._call_llm(prompt, system_prompt, use_backend_model=True)
                text, _ = self._split_img_choice(text_with_choice)
        else:
            text = await self._call_llm(prompt, system_prompt, use_backend_model=True)

        if not text:
            logger.warning("LLM生成内容为空，跳过自动发布")
            return

        await self._publish(text, image_urls, allow_image_drop=True)
        self._add_post_to_history(text)
        logger.info(f"自动发布说说成功: {text} (图片数: {len(image_urls)})")

    @staticmethod
    def _split_img_choice(text: str) -> tuple[str, Optional[int]]:
        """解析 LLM 输出末尾的 IMG:序号 行"""
        if not text:
            return "", None
        m = re.search(r"^\s*IMG\s*[:：]\s*(\d+)\s*$", text, re.M)
        if not m:
            return text.strip(), None
        chosen = int(m.group(1))
        cleaned = re.sub(r"^\s*IMG\s*[:：]\s*\d+\s*$", "", text, flags=re.M).strip()
        return cleaned, chosen

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
                await self.api.comment(post, comment_text)
                logger.info(f"自动评论成功: {post.tid} -> {comment_text}")
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
                instruction = "【回复任务】请回复你最近说说下的新评论，开头必须qzone_reply_comment(target_id, tid, comment_id, content)，target_id为自己的QQ号，自然一点，严禁内容重复和复读。检测comment_id来不回复自己。优先没有回复过的用户和新回复，否则不回复。"
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
                    if comment.tid in self.replied_comments:
                        continue
                    prompt = f"用户 {comment.nickname} 评论了你的说说：{comment.content}，请生成一条友好回复（10-30字）。"
                    reply_text = await self._call_llm(prompt, await self._get_persona_content(), use_backend_model=True)
                    if not reply_text:
                        continue
                    if f"@{comment.nickname}" not in reply_text:
                        reply_text = f"回复 @{comment.nickname}：{reply_text}"
                    await self.api.reply(full_post, comment, reply_text)
                    logger.info(f"自动回复成功: {comment.tid} -> {reply_text}")
                    self.replied_comments.add(comment.tid)
                    self._save_state()
                    new_replies += 1
                    if new_replies >= self.max_replies_per_cycle:
                        break
                if new_replies >= self.max_replies_per_cycle:
                    break
            logger.info(f"自动回复任务完成，共回复 {new_replies} 条新评论")
        except Exception as e:
            logger.error(f"自动回复任务失败: {e}")

    # ---------- 历史与图片获取辅助 ----------
    async def _fetch_chat_history(self, source_type: str, source_id: str, count: int = 10) -> List[str]:
        """获取群/私聊最近消息文本"""
        if source_type == "group":
            action, params = "get_group_msg_history", {"group_id": int(source_id), "count": count}
        else:
            action, params = "get_friend_msg_history", {"user_id": int(source_id), "count": count}
        result = await self._call_onebot_action(action, params)
        if not result or result.get("status") != "ok":
            logger.error(f"获取{'群' if source_type == 'group' else '私聊'}历史失败: {result}")
            return []
        messages = result.get("data", {}).get("messages", [])
        if not messages:
            return []
        summaries = []
        for msg in messages[-count:]:
            sender = msg.get("sender", {}).get("nickname", "未知")
            content = self._extract_text_simple(msg.get("message", []))
            summaries.append(f"{sender}: {content}")
        return summaries

    async def _fetch_recent_images(self, source_type: str, source_id: str, max_count: int = 1) -> List[str]:
        """从群/私聊历史获取最近图片 URL（新图优先）"""
        if source_type == "group":
            action, params = "get_group_msg_history", {"group_id": int(source_id), "count": 20}
        else:
            action, params = "get_friend_msg_history", {"user_id": int(source_id), "count": 20}
        result = await self._call_onebot_action(action, params)
        if not result or result.get("status") != "ok":
            logger.error(f"oneBot返回错误: {result}")
            return []
        messages = result.get("data", {}).get("messages", [])
        if not messages:
            return []
        urls = []
        for msg in reversed(messages):
            for seg in msg.get("message", []):
                if seg.get("type") == "image":
                    url = seg.get("data", {}).get("url", "")
                    if url:
                        url = html.unescape(url.strip().strip('"').strip("'"))
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
        """解析单张图片的描述：elem.caption -> md5 缓存 -> 后台描述"""
        if entry.get("desc"):
            return entry["desc"]
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
            entries = self._manifest_entries(sid)
            if not entries:
                return
            lines = []
            for i, entry in enumerate(entries, 1):
                desc = await self._resolve_entry_desc(entry)
                if not desc:
                    continue
                time_str = datetime.fromtimestamp(entry["time"]).strftime("%m-%d %H:%M")
                sender = entry.get("sender") or "未知"
                lines.append(f"{i}. [{time_str} {sender}] {desc}")
            if not lines:
                return
            text = (
                "[近期图片] 本群/会话最近出现的图片及内容描述，"
                "调用 qzone_publish 发说说时可用 image_indices 参数引用序号配图：\n"
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
        """按清单序号解析图片为本地路径（供发布使用），过期 URL 自动续命一次"""
        paths = []
        entries = self._manifest_entries(sid)
        for idx in indices:
            try:
                i = int(idx)
            except (TypeError, ValueError):
                continue
            if not (1 <= i <= len(entries)):
                continue
            entry = entries[i - 1]
            elem: Image = entry["elem"]
            for attempt in range(2):
                try:
                    path = await elem.to_path()
                    if path:
                        # 校验内容真的是图片（缓存文件可能是过期时下载的错误页）
                        with open(path, 'rb') as f:
                            head = f.read(16)
                        if looks_like_image(head):
                            paths.append(str(path))
                            break
                        logger.warning(f"清单图片 {i} 缓存内容不是图片: {path}")
                    raise ValueError("to_path 为空或内容非图片")
                except Exception as e:
                    if attempt == 0 and await self._refresh_image_url(entry):
                        continue  # 续命成功，重试一次
                    logger.warning(f"清单图片 {i} 获取失败: {e}")
                    break
        return paths

    # ---------- 核心 API 封装 ----------
    async def _publish(self, text: str, image_urls: list, allow_image_drop: bool = False) -> str:
        await self._ensure_api()
        post = QzonePost(text=text, images=image_urls)
        resp = await self.api.publish(post, allow_image_drop=allow_image_drop)
        if not resp.ok:
            raise RuntimeError(f"发布失败: {resp.message}")
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

    async def _comment(self, post: QzonePost, content: str) -> str:
        await self._ensure_api()
        resp = await self.api.comment(post, content)
        if not resp.ok:
            raise RuntimeError(f"评论失败: {resp.message}")
        return "评论成功"

    async def _delete(self, tid: str) -> str:
        await self._ensure_api()
        resp = await self.api.delete(tid)
        if not resp.ok:
            raise RuntimeError(f"删除失败: {resp.message}")
        return f"说说 {tid} 删除成功"

    async def _reply_comment(self, post: QzonePost, comment: QzoneComment, content: str = "") -> str:
        await self._ensure_api()
        if not content:
            prompt = f"用户 {comment.nickname} 评论了你的说说：{comment.content}，请生成一条友好回复（10-30字）。"
            content = await self._call_llm(prompt, await self._get_persona_content(), use_backend_model=False)
            if not content:
                raise RuntimeError("生成回复内容为空")
        if f"@{comment.nickname}" not in content:
            content = f"回复 @{comment.nickname}：{content}"
        await self.api.reply(post, comment, content)
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
                    return (
                        f"未能从[近期图片]清单解析出图片（当前会话清单为空，或序号 {image_indices} 超出范围），说说未发布。"
                        "如确认发纯文字，请不带 image_indices 重试；"
                        "如想配图，可改用 images 参数传图片 URL 或本地路径（如 data/temp/xxx.jpg）。"
                    )
                valid_sources.extend(resolved)
            elif images and not valid_sources:
                return "images 参数中的地址均无效，说说未发布。请传有效的图片 URL 或本地路径。"
            # 吸附兑底：未指定图片且开启吸附模式时，自动抓最近一张图
            # （吸附图下载失败会降级为纯文字发布，保持“有时配有时不配”的随机感）
            allow_drop = False
            if not valid_sources and self.auto_attach_recent_image:
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
                    for i, cmt in enumerate(p.comments[:5]):
                        cmt_time_str = cmt.create_time_str if hasattr(cmt, 'create_time_str') and cmt.create_time_str else self._format_time(cmt.create_time)
                        comment_lines.append(f"  └ {cmt.nickname} (ID:{cmt.tid}) [{cmt_time_str}]: {cmt.content}")
                    if comment_lines:
                        line += "\n评论区：\n" + "\n".join(comment_lines)
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
        description="回复指定评论（可自动生成内容）。评论ID可以从 qzone_view 的输出中获取（格式：└ 昵称 (ID:xxx) [时间]: 内容）。",
        params={
            "type": "object",
            "properties": {
                "target_id": {"type": "string", "description": "说说作者的QQ号"},
                "tid": {"type": "string", "description": "说说ID"},
                "comment_id": {"type": "string", "description": "要回复的评论ID"},
                "content": {"type": "string", "description": "回复内容（可选）"}
            },
            "required": ["target_id", "tid", "comment_id"]
        }
    )
    async def tool_reply_comment(self, event: KiraMessageBatchEvent, target_id: str, tid: str, comment_id: str, content: str = ""):
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
            target_comment = None
            for cmt in full_post.comments:
                if str(cmt.tid) == str(comment_id):
                    target_comment = cmt
                    break
            if not target_comment:
                return f"未找到指定的评论 ID: {comment_id}"
            final_content = content
            if not final_content:
                prompt = f"用户 {target_comment.nickname} 评论了你的说说：{target_comment.content}，请生成一条友好回复（10-30字）。"
                final_content = await self._call_llm(prompt, await self._get_persona_content(), use_backend_model=False)
                if not final_content:
                    return "生成回复内容为空"
            result = await self._reply_comment(post, target_comment, final_content)
            return result
        except Exception as e:
            return f"回复失败：{e}"

