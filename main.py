import asyncio
import logging
import socket
import random
from pathlib import Path
from typing import Optional, List, Dict, Any
import concurrent.futures
import json
import re
import html
import httpx
from datetime import datetime, timedelta

from core.plugin import BasePlugin, register_tool
from core.chat.message_utils import KiraMessageBatchEvent
from core.chat.message_elements import Image, Reply, Text
from core.chat import MessageChain

from .qzone.api import QzoneAPI
from .qzone.session import QzoneSession
from .qzone.utils import download_file
from .qzone.parser import QzoneParser
from .qzone.model import Post as QzonePost, Comment as QzoneComment, ApiResponse

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from core.provider import LLMRequest, LLMResponse

logger = logging.getLogger(__name__)

# 修复 true/false 未定义问题
true = True
false = False

executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)

MAX_HISTORY = 10


def is_online(host="8.8.8.8", port=53, timeout=3):
    try:
        socket.setdefaulttimeout(timeout)
        socket.socket(socket.AF_INET, socket.SOCK_STREAM).connect((host, port))
        return True
    except Exception:
        return False


def _get_gtk_from_cookie(cookie_str: str) -> int:
    """从 Cookie 中计算 g_tk 值（备用）"""
    match = re.search(r'p_skey=([^;]+)', cookie_str)
    if not match:
        return 0
    p_skey = match.group(1)
    hash_val = 5381
    for c in p_skey:
        hash_val += (hash_val << 5) + ord(c)
    return hash_val & 0x7fffffff


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

        # 新定时配置（单字符串）
        self.auto_publish_schedule = cfg.get("auto_publish_schedule", "")
        self.auto_comment_schedule = cfg.get("auto_comment_schedule", "")
        self.auto_reply_schedule = cfg.get("auto_reply_schedule", "")
        self.auto_reply_enabled = cfg.get("auto_reply_enabled", False)
        self.like_when_comment = cfg.get("like_when_comment", True)

        # 旧定时配置（向后兼容）
        self.auto_publish_cron = cfg.get("auto_publish_cron", "")
        self.auto_comment_cron = cfg.get("auto_comment_cron", "")
        self.auto_reply_cron = cfg.get("auto_reply_cron", "")

        # 解析新配置为触发器字典，供 _setup_scheduled_jobs 使用
        self.auto_publish_trigger_dict = self._parse_schedule(self.auto_publish_schedule) if self.auto_publish_schedule else None
        self.auto_comment_trigger_dict = self._parse_schedule(self.auto_comment_schedule) if self.auto_comment_schedule else None
        self.auto_reply_trigger_dict = self._parse_schedule(self.auto_reply_schedule) if self.auto_reply_schedule else None

        self.max_comments_per_cycle = cfg.get("max_comments_per_cycle", 3)
        self.max_replies_per_cycle = cfg.get("max_replies_per_cycle", 5)

        self.replied_comments = set()

        self.persona_content = self._load_persona()
        self.my_posts_history: List[str] = []
        self.last_auto_publish_time: Optional[datetime] = None
        self._jobs_added = False

        # 保活任务标志
        self._keep_alive_task = None

        # QQ适配器对象
        self._ada_obj = None

    def _ensure_ada(self):
        """确保正确获取了QQ适配器对象"""
        if self._ada_obj:
            return
        ada_name = None
        ada = None
        if self.qq_ada:
            ada_name = self.qq_ada
            ada = self.ctx.adapter_mgr.get_adapter(ada_name)
        if not ada:
            ada_infos = self.ctx.adapter_mgr.get_adapter_infos()
            for info in ada_infos:
                if info.platform == "QQ":
                    ada_name = info.name
            if not ada_name:
                logger.error("未找到适配器平台为 QQ 的适配器，无法调用 OneBot 接口")
                return None
            ada = self.ctx.adapter_mgr.get_adapter(ada_name)
            if not ada:
                logger.error(f"未找到 {ada_name} 适配器，无法调用 OneBot 接口")
                return None
        self._ada_obj = ada
    
    async def _call_onebot_action(self, action: str, params: dict):
        self._ensure_ada()
        ada = self._ada_obj
        ob_client = ada.get_client()
        res = await ob_client.send_action(action, params)
        return res

    def _load_persona(self) -> str:
        persona = self.ctx.persona_mgr.get_persona()
        return persona

    def _add_post_to_history(self, text: str):
        self.my_posts_history.append(text)
        if len(self.my_posts_history) > MAX_HISTORY:
            self.my_posts_history.pop(0)

    @staticmethod
    def _format_time(ts) -> str:
        """将时间戳或时间字符串格式化为可读形式"""
        if isinstance(ts, (int, float)) and ts > 0:
            dt = datetime.fromtimestamp(ts)
            return dt.strftime("%Y-%m-%d %H:%M")
        elif isinstance(ts, str):
            return ts
        else:
            return "未知时间"

    def _parse_schedule(self, s: str) -> Optional[dict]:
        """解析调度字符串，返回包含 mode 和参数的字典，或 None（禁用）
        格式示例：
            - cron: "0 8 * * *"
            - interval: "2h" 或 "2h/30m" 或 "20m" 或 "20m/5m"
        """
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

    async def _call_llm(self, prompt: str, system_prompt: Optional[str] = None) -> str:
        try:
            client = self.ctx.get_default_llm_client()
            if not client:
                logger.error("无法获取默认 LLM 客户端")
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

    async def _reinit_session(self):
        """重新初始化 session 和 api（Cookie 更新后调用）"""
        try:
            config = type("Config", (), {
                "cookies_str": self.cookies_str,
                "timeout": self.timeout
            })()
            self.session = QzoneSession(config)
            self.api = QzoneAPI(self.session, config)
            ctx = await self.session.get_ctx()
            self.my_uin = ctx.uin
            logger.info(f"Cookie 更新后重新初始化成功，当前账号: {self.my_uin}")
        except Exception as e:
            logger.error(f"重新初始化失败: {e}")

    # ---------- 新增：通过 LLOneBot 获取 Cookie ----------
    async def _get_cookie_from_llonebot(self) -> Optional[str]:
        """向 LLOneBot 发送请求，获取指定域名的 Cookie 字符串"""
        data = await self._call_onebot_action("get_cookies", {"domain": "user.qzone.qq.com"})
        if data.get("status") != "ok":
            logger.error(f"LLOneBot 返回错误: {data}")
            return None
        cookie_str = data.get("data", {}).get("cookies")
        if not cookie_str:
            logger.error("返回数据中未找到 cookies 字段")
            return None
        logger.info("成功从 LLOneBot 获取 Cookie")
        return cookie_str

    async def initialize(self):
        # 如果启用自动刷新，先尝试从 LLOneBot 获取最新 Cookie
        if self.auto_refresh:
            new_cookie = await self._get_cookie_from_llonebot()
            if new_cookie:
                self.cookies_str = new_cookie
                logger.info("已从 LLOneBot 获取最新 Cookie")
            else:
                logger.warning("从 LLOneBot 获取 Cookie 失败，将使用配置中的旧 Cookie")

        if not self.cookies_str:
            logger.error("未提供Cookie，插件无法工作")
            return

        try:
            config = type("Config", (), {
                "cookies_str": self.cookies_str,
                "timeout": self.timeout
            })()
            self.session = QzoneSession(config)
            self.api = QzoneAPI(self.session, config)
            ctx = await self.session.get_ctx()
            self.my_uin = ctx.uin
            logger.info(f"QQ空间插件初始化完成，当前账号: {self.my_uin}")

            await self._setup_scheduled_jobs()

            # 启动保活任务（如果启用自动刷新）
            if self.auto_refresh:
                self._keep_alive_task = asyncio.create_task(self._keep_alive_loop())

        except Exception as e:
            logger.error(f"初始化API失败: {e}")

    async def _test_cookie(self) -> bool:
        if not self.cookies_str:
            return False
        try:
            config = type("Config", (), {
                "cookies_str": self.cookies_str,
                "timeout": self.timeout
            })()
            session = QzoneSession(config)
            api = QzoneAPI(session, config)
            test = await api.get_visitor()
            return test.ok
        except Exception:
            return False

    async def terminate(self):
        try:
            if self.api:
                await self.api.close()
            executor.shutdown(wait=False)
            self.scheduler.shutdown()
            if self._keep_alive_task:
                self._keep_alive_task.cancel()
        except Exception as e:
            logger.error(f"停止QQ空间插件时出错：{e}")

    async def _auto_refresh_cookie(self) -> str:
        """自动刷新 Cookie 的方法，被外部调用"""
        logger.info("尝试自动获取Cookie...")
        new_cookie = await self._get_cookie_from_llonebot()
        if new_cookie:
            return new_cookie
        else:
            return ""

    # ---------- 保活任务（使用访客接口） ----------
    async def _keep_alive_loop(self):
        """每5分钟发送一次登录保持请求（使用访客接口），若失败则重新获取 Cookie"""
        while True:
            try:
                if not self.cookies_str or not self.my_uin:
                    await asyncio.sleep(60)
                    continue

                # 使用访客接口，不需要 g_tk
                url = "https://h5.qzone.qq.com/proxy/domain/g.qzone.qq.com/cgi-bin/friendshow/cgi_get_visitor_more"
                headers = {
                    "Cookie": self.cookies_str,
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                }
                async with httpx.AsyncClient() as client:
                    resp = await client.get(url, headers=headers, timeout=10)
                    if resp.status_code == 200:
                        logger.debug("登录保持成功")
                    else:
                        logger.warning(f"登录保持失败，状态码 {resp.status_code}，尝试重新获取 Cookie")
                        new_cookie = await self._get_cookie_from_llonebot()
                        if new_cookie:
                            self.cookies_str = new_cookie
                            await self._reinit_session()
                        else:
                            logger.error("重新获取 Cookie 失败")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"保活任务异常: {e}")
            await asyncio.sleep(1800)  # 30分钟

    # ---------- 以下为原有功能，完全不变 ----------
    async def _setup_scheduled_jobs(self):
        if self._jobs_added:
            logger.warning("定时任务已添加，跳过")
            return

        def add_job(job_func, trigger_dict, cron_fallback, job_id):
            if trigger_dict:
                if trigger_dict["mode"] == "cron":
                    trigger = CronTrigger.from_crontab(trigger_dict["expr"])
                else:
                    trigger = IntervalTrigger(
                        seconds=trigger_dict["interval_seconds"],
                        jitter=trigger_dict["jitter_seconds"]
                    )
                self.scheduler.add_job(job_func, trigger, id=job_id, replace_existing=True)
                logger.info(f"定时任务 {job_id} 已调度: {trigger_dict}")
            elif cron_fallback:
                try:
                    trigger = CronTrigger.from_crontab(cron_fallback)
                    self.scheduler.add_job(job_func, trigger, id=job_id, replace_existing=True)
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

    # ---------- 通用：向目标发送指令 ----------
    async def _send_task_instruction(self, instruction_text: str) -> bool:
        targets = []
        for gid in self.task_group_ids:
            targets.append(("group", gid))
        for uid in self.task_private_ids:
            targets.append(("private", uid))

        if not targets:
            return False

        target_type, target_id = random.choice(targets)
        sid = f"qq:{'gm' if target_type == 'group' else 'dm'}:{target_id}"
        msg_chain = MessageChain([Text(instruction_text)])
        await self.ctx.publish_notice(sid, msg_chain)
        logger.info(f"已向 {target_type} {target_id} 发送指令: {instruction_text[:30]}...")
        return True

    # ---------- 自动发布说说 ----------
    async def _auto_publish_job(self):
        try:
            if self.last_auto_publish_time and (datetime.now() - self.last_auto_publish_time).total_seconds() < 60:
                logger.warning("距离上次自动发布不足60秒，跳过本次自动发布")
                return

            if self.task_group_ids or self.task_private_ids:
                instruction = "【定时任务】请根据最近聊天发布一条说说，自然一点，不要提及这是定时任务。"
                if await self._send_task_instruction(instruction):
                    self.last_auto_publish_time = datetime.now()
                return

            await self._legacy_auto_publish()
            self.last_auto_publish_time = datetime.now()

        except Exception as e:
            logger.error(f"自动发布任务失败: {e}")

    # ---------- 自动评论 ----------
    async def _auto_comment_job(self):
        try:
            if self.task_group_ids or self.task_private_ids:
                instruction = "【评论任务】请对最近的好友说说进行评论，自然一点。严禁内容重复和复读。优先没有评论过的内容，该内容时间戳与当前系统时间戳不得超过7天，否则不评论。"
                await self._send_task_instruction(instruction)
                return

            await self._legacy_auto_comment()

        except Exception as e:
            logger.error(f"自动评论任务失败: {e}")

    # ---------- 自动回复 ----------
    async def _auto_reply_job(self):
        try:
            if not self.my_uin:
                logger.error("无法获取当前账号的QQ号")
                return

            if self.task_group_ids or self.task_private_ids:
                instruction = "【回复任务】请回复你最近说说下的新评论，开头必须qzone_reply_comment(target_id, tid, comment_id, content)，自然一点，严禁内容重复和复读。检测target_id来不回复自己。优先没有回复过的用户和新回复，否则不回复。"
                await self._send_task_instruction(instruction)
                return

            await self._legacy_auto_reply()

        except Exception as e:
            logger.error(f"自动回复任务失败: {e}")

    # ---------- 后台模式（原有逻辑）----------
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
                if source_type == "group":
                    messages = await self._fetch_group_history(source_id, count=10)
                else:
                    messages = await self._fetch_private_history(source_id, count=10)
                if messages:
                    context_messages = messages
                    logger.info(f"从 {source_type} {source_id} 获取到 {len(messages)} 条消息作为上下文")
            except Exception as e:
                logger.error(f"获取历史失败: {e}")

        system_prompt = self.persona_content
        if self.my_posts_history:
            history_str = "\n".join([f"- {post}" for post in self.my_posts_history[-5:]])
            system_prompt += f"\n\n你最近发布的说说是：\n{history_str}"

        if context_messages:
            history_text = "\n".join(context_messages)
            prompt = f"根据以下最近对话，生成一条QQ空间说说（20-50字），要符合你的人设：\n{history_text}"
        else:
            prompt = "请生成一条QQ空间说说，内容可以是心情、日常、段子，20-50字，要符合你的人设。"

        text = await self._call_llm(prompt, system_prompt)
        if not text:
            logger.warning("LLM生成内容为空，跳过自动发布")
            return

        image_urls = []
        if self.auto_publish_image_prob > 0 and random.random() < self.auto_publish_image_prob:
            if source_id:
                if source_type == "group":
                    img_urls = await self._fetch_recent_images_by_group(source_id, max_count=1)
                else:
                    img_urls = await self._fetch_recent_images_by_private(source_id, max_count=1)
                if img_urls:
                    image_urls = img_urls
                    logger.info("自动发布带图")

        await self._publish(text, image_urls)
        self._add_post_to_history(text)
        logger.info(f"自动发布说说成功: {text} (图片数: {len(image_urls)})")

    async def _legacy_auto_comment(self):
        try:
            posts = await self._get_feeds(target_id=None, num=20)
            if not posts:
                return
            selected = random.sample(posts, min(self.max_comments_per_cycle, len(posts)))
            for post in selected:
                prompt = f"根据以下说说内容，生成一条简短评论（10-20字）：\n{post.text}"
                comment_text = await self._call_llm(prompt, self.persona_content)
                if not comment_text:
                    continue
                await self.api.comment(post, comment_text)
                logger.info(f"自动评论成功: {post.tid} -> {comment_text}")
                if self.like_when_comment:
                    await self.api.like(post)
                    logger.info(f"自动点赞成功: {post.tid}")
                await asyncio.sleep(2)
        except Exception as e:
            logger.error(f"自动评论任务失败: {e}")

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
                    reply_text = await self._call_llm(prompt, self.persona_content)
                    if not reply_text:
                        continue
                    if f"@{comment.nickname}" not in reply_text:
                        reply_text = f"回复 @{comment.nickname}：{reply_text}"
                    await self.api.reply(full_post, comment, reply_text)
                    logger.info(f"自动回复成功: {comment.tid} -> {reply_text}")
                    self.replied_comments.add(comment.tid)
                    new_replies += 1
                    if new_replies >= self.max_replies_per_cycle:
                        break
                if new_replies >= self.max_replies_per_cycle:
                    break
            logger.info(f"自动回复任务完成，共回复 {new_replies} 条新评论")
        except Exception as e:
            logger.error(f"自动回复任务失败: {e}")

    # ---------- 获取私聊历史消息摘要 ----------
    async def _fetch_private_history(self, user_id: str, count: int = 10) -> List[str]:

        result = await self._call_onebot_action("get_friend_msg_history", {"user_id": int(user_id), "count": count})
        if not result or result.get("status") != "ok":
            logger.error(f"获取私聊历史失败: {result}")
            return []

        if result.get("status") != "ok":
            logger.error(f"llonebot返回错误: {result}")
            return []

        messages = result.get("data", {}).get("messages", [])
        if not messages:
            return []

        summaries = []
        for msg in messages[-count:]:
            sender = msg.get("sender", {}).get("nickname", "对方")
            content = self._extract_text_simple(msg.get("message", []))
            summaries.append(f"{sender}: {content}")
        return summaries

    async def _fetch_recent_images_by_private(self, user_id: str, max_count: int = 1) -> List[str]:
        result = await self._call_onebot_action("get_friend_msg_history", {"user_id": int(user_id), "count": 20})

        if result.get("status") != "ok":
            logger.error(f"llonebot返回错误: {result}")
            return []

        messages = result.get("data", {}).get("messages", [])
        if not messages:
            return []

        urls = []
        for msg in reversed(messages):
            msg_segments = msg.get("message", [])
            for seg in msg_segments:
                if seg.get("type") == "image":
                    url = seg.get("data", {}).get("url", "")
                    if url:
                        url = url.strip().strip('"').strip("'")
                        url = html.unescape(url)
                        urls.append(url)
                        if len(urls) >= max_count:
                            break
            if len(urls) >= max_count:
                break
        return urls

    async def _fetch_group_history(self, group_id: str, count: int = 10) -> List[str]:
        result = await self._call_onebot_action("get_group_msg_history", {"group_id": int(group_id), "count": count})
        if not result or result.get("status") != "ok":
            logger.error(f"获取群历史失败: {result}")
            return []

        if result.get("status") != "ok":
            logger.error(f"llonebot返回错误: {result}")
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

    async def _fetch_recent_images_by_group(self, group_id: str, max_count: int = 1) -> List[str]:
        result = await self._call_onebot_action("get_group_msg_history", {"group_id": int(group_id), "count": 20})
        if result.get("status") != "ok":
            logger.error(f"llonebot返回错误: {result}")
            return []

        messages = result.get("data", {}).get("messages", [])
        if not messages:
            return []

        urls = []
        for msg in reversed(messages):
            msg_segments = msg.get("message", [])
            for seg in msg_segments:
                if seg.get("type") == "image":
                    url = seg.get("data", {}).get("url", "")
                    if url:
                        url = url.strip().strip('"').strip("'")
                        url = html.unescape(url)
                        urls.append(url)
                        if len(urls) >= max_count:
                            break
            if len(urls) >= max_count:
                break
        return urls

    def _extract_text_simple(self, message_list: List[dict]) -> str:
        texts = []
        for seg in message_list:
            if seg.get("type") == "text":
                texts.append(seg.get("data", {}).get("text", ""))
        return " ".join(texts)

    async def _fetch_recent_images(self, event: KiraMessageBatchEvent, max_count: int = 1) -> List[str]:
        session_type = None
        session_id = None

        try:
            if hasattr(event, 'get_group_id') and callable(event.get_group_id):
                gid = event.get_group_id()
                if gid:
                    session_type = "group"
                    session_id = str(gid)
                    logger.debug(f"通过 get_group_id 获取到群ID: {session_id}")
        except Exception:
            pass

        if not session_id and hasattr(event, 'group_id'):
            try:
                gid = event.group_id
                if gid:
                    session_type = "group"
                    session_id = str(gid)
                    logger.debug(f"通过 group_id 属性获取到群ID: {session_id}")
            except Exception:
                pass

        if not session_id and hasattr(event, 'message_obj') and hasattr(event.message_obj, 'group'):
            try:
                gid = event.message_obj.group.group_id
                if gid:
                    session_type = "group"
                    session_id = str(gid)
                    logger.debug(f"通过 message_obj.group.group_id 获取到群ID: {session_id}")
            except Exception:
                pass

        if not session_id:
            try:
                if hasattr(event, 'get_user_id') and callable(event.get_user_id):
                    uid = event.get_user_id()
                    if uid:
                        session_type = "private"
                        session_id = str(uid)
                        logger.debug(f"通过 get_user_id 获取到用户ID: {session_id}")
            except Exception:
                pass

        if not session_id and hasattr(event, 'user_id'):
            try:
                uid = event.user_id
                if uid:
                    session_type = "private"
                    session_id = str(uid)
                    logger.debug(f"通过 user_id 属性获取到用户ID: {session_id}")
            except Exception:
                pass

        if not session_id and hasattr(event, 'message_obj') and hasattr(event.message_obj, 'sender'):
            try:
                uid = event.message_obj.sender.user_id
                if uid:
                    session_type = "private"
                    session_id = str(uid)
                    logger.debug(f"通过 message_obj.sender.user_id 获取到用户ID: {session_id}")
            except Exception:
                pass

        if not session_id and hasattr(event, 'messages') and event.messages:
            first_msg = event.messages[0]
            if hasattr(first_msg, 'group') and first_msg.group:
                session_type = "group"
                session_id = first_msg.group.group_id
                logger.debug(f"从第一条消息的 group 获取到群ID: {session_id}")
            elif hasattr(first_msg, 'sender') and first_msg.sender:
                session_type = "private"
                session_id = first_msg.sender.user_id
                logger.debug(f"从第一条消息的 sender 获取到用户ID: {session_id}")

        if not session_id:
            logger.error("无法从事件中获取会话ID")
            return []

        if session_type == "group":
            api = "get_group_msg_history"
            params = {"group_id": int(session_id), "count": 20}
        else:
            api = "get_friend_msg_history"
            params = {"user_id": int(session_id), "count": 20}
        
        result = await self._call_onebot_action(api, params)

        if result.get("status") != "ok":
            logger.error(f"llonebot返回错误: {result}")
            return []

        messages = result.get("data", {}).get("messages", [])
        if not messages:
            return []

        urls = []
        for msg in reversed(messages):
            msg_segments = msg.get("message", [])
            for seg in msg_segments:
                if seg.get("type") == "image":
                    url = seg.get("data", {}).get("url", "")
                    if url:
                        url = url.strip().strip('"').strip("'")
                        url = html.unescape(url)
                        urls.append(url)
                        if len(urls) >= max_count:
                            break
            if len(urls) >= max_count:
                break
        return urls

    async def _publish(self, text: str, image_urls: list[str]) -> str:
        post = QzonePost(text=text, images=image_urls)
        resp = await self.api.publish(post)
        if not resp.ok:
            raise RuntimeError(f"发布失败: {resp.message}")
        tid = resp.data.get("tid")
        return f"说说发布成功！TID: {tid}"

    async def _get_feeds(self, target_id: Optional[str] = None, num: int = 1) -> list[QzonePost]:
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
        resp = await self.api.like(post)
        if not resp.ok:
            raise RuntimeError(f"点赞失败: {resp.message}")
        return "点赞成功"

    async def _comment(self, post: QzonePost, content: str) -> str:
        resp = await self.api.comment(post, content)
        if not resp.ok:
            raise RuntimeError(f"评论失败: {resp.message}")
        return "评论成功"

    async def _delete(self, tid: str) -> str:
        resp = await self.api.delete(tid)
        if not resp.ok:
            raise RuntimeError(f"删除失败: {resp.message}")
        return f"说说 {tid} 删除成功"

    async def _reply_comment(self, post: QzonePost, comment: QzoneComment, content: str = "") -> str:
        if not content:
            prompt = f"用户 {comment.nickname} 评论了你的说说：{comment.content}，请生成一条友好回复（10-30字）。"
            content = await self._call_llm(prompt, self.persona_content)
            if not content:
                raise RuntimeError("生成回复内容为空")
        if f"@{comment.nickname}" not in content:
            content = f"回复 @{comment.nickname}：{content}"
        await self.api.reply(post, comment, content)
        return f"回复成功: {content}"

    # ---------- 权限检查辅助函数 ----------
    async def _check_master(self, event: KiraMessageBatchEvent) -> bool:
        if not self.master_ids:
            return True
        user_id = None
        try:
            if hasattr(event, 'get_user_id') and callable(event.get_user_id):
                user_id = event.get_user_id()
            elif hasattr(event, 'user_id'):
                user_id = event.user_id
            elif hasattr(event, 'sender') and hasattr(event.sender, 'user_id'):
                user_id = event.sender.user_id
        except Exception:
            pass
        if user_id is None:
            logger.debug("无法获取用户ID，允许执行（系统内部调用）")
            return True
        if str(user_id) in self.master_ids:
            return True
        logger.warning(f"用户 {user_id} 尝试使用QQ空间工具，但不在主人列表中")
        return False

    # ---------- 工具注册 ----------
    @register_tool(
        name="qzone_publish",
        description="发布一条说说到自己的QQ空间。如果用户指定了图片（例如通过引用），你应该提供对应的图片URL；如果用户只说“配图”而不指定具体图片，你可以不提供URL，插件会自动选择最近的一张图片。",
        params={
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "说说内容"},
                "image_urls": {"type": "array", "items": {"type": "string"}, "description": "图片URL列表（可选，如果用户指定了图片，请填入URL；否则留空，插件会自动选一张）", "default": []}
            },
            "required": ["text"]
        }
    )
    async def tool_publish(self, event: KiraMessageBatchEvent, text: str, image_urls: list[str] = []):
        if not await self._check_master(event):
            return "抱歉，只有主人才能使用此功能。"

        try:
            valid_urls = []
            for url in image_urls:
                if url.startswith('http') and 'example.com' not in url and 'image_url_from' not in url and 'previous_message' not in url:
                    valid_urls.append(url)
                else:
                    logger.warning(f"AI提供了无效的图片URL: {url}，将忽略")

            if not valid_urls:
                valid_urls = await self._fetch_recent_images(event, max_count=1)
                if valid_urls:
                    logger.info(f"自动获取到 1 张最近的图片")
                else:
                    logger.info("未找到图片，将只发布文字")
            else:
                logger.info(f"使用AI提供的 {len(valid_urls)} 张图片")

            result = await self._publish(text, valid_urls)
            self._add_post_to_history(text)
            return result
        except Exception as e:
            return f"发布失败：{e}"

    @register_tool(
        name="qzone_view",
        description="查看QQ空间说说。如果不提供target_id，默认查看自己的空间；要查看好友动态，请提供好友QQ号。返回的每条说说会包含ID、发布时间和最新评论，每条评论会显示评论者昵称、评论ID、时间和内容。",
        params={
            "type": "object",
            "properties": {
                "target_id": {"type": "string", "description": "目标QQ号（可选，不填则查看自己的空间）"},
                "num": {"type": "integer", "description": "查看条数，默认1", "default": 1}
            },
        }
    )
    async def tool_view(self, event: KiraMessageBatchEvent, target_id: str = None, num: int = 1):
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
                    line += f"\n图片: {p.images}"
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
        if not await self._check_master(event):
            return "抱歉，只有主人才能使用此功能。"

        post = QzonePost(uin=int(target_id), tid=tid)
        try:
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
                "content": {"type": "string", "description": "评论内容（可选，不填则AI自动生成）"}
            },
            "required": ["target_id", "tid"]
        }
    )
    async def tool_comment(self, event: KiraMessageBatchEvent, target_id: str, tid: str, content: str = ""):
        if not await self._check_master(event):
            return "抱歉，只有主人才能使用此功能。"

        try:
            if not content:
                post = QzonePost(uin=int(target_id), tid=tid)
                detail_resp = await self.api.get_detail(post)
                if detail_resp.ok:
                    parsed_posts = QzoneParser.parse_feeds([detail_resp.data])
                    if parsed_posts:
                        full_post = parsed_posts[0]
                        prompt = f"根据以下说说内容，生成一条简短评论（10-20字）：\n{full_post.text}"
                        content = await self._call_llm(prompt, self.persona_content)
                        if not content:
                            content = "赞一个！"
                else:
                    prompt = f"为这条说说生成一条简短评论（10-20字）"
                    content = await self._call_llm(prompt, self.persona_content)
                    if not content:
                        content = "赞一个！"

            post = QzonePost(uin=int(target_id), tid=tid)
            result = await self._comment(post, content)
            return result
        except Exception as e:
            logger.error(f"评论失败，错误详情: {e}")
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
        if not await self._check_master(event):
            return "抱歉，只有主人才能使用此功能。"

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
                "content": {"type": "string", "description": "回复内容（可选，不填则AI自动生成）"}
            },
            "required": ["target_id", "tid", "comment_id"]
        }
    )
    async def tool_reply_comment(self, event: KiraMessageBatchEvent, target_id: str, tid: str, comment_id: str, content: str = ""):
        if not await self._check_master(event):
            return "抱歉，只有主人才能使用此功能。"

        try:
            post = QzonePost(uin=int(target_id), tid=tid)
            detail_resp = await self.api.get_detail(post)
            if not detail_resp.ok:
                return f"获取说说详情失败，无法获取评论者信息"
            parsed_posts = QzoneParser.parse_feeds([detail_resp.data])
            if not parsed_posts:
                return f"解析说说详情失败"
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
                final_content = await self._call_llm(prompt, self.persona_content)
                if not final_content:
                    return "生成回复内容为空"

            result = await self._reply_comment(post, target_comment, final_content)
            return result
        except Exception as e:
            logger.error(f"回复失败，错误详情: {e}")
            return f"回复失败：{e}"
