import asyncio
from http.cookies import SimpleCookie

import logging

from .model import QzoneContext

logger = logging.getLogger(__name__)


class QzoneSession:
    """QQ 登录上下文"""

    DOMAIN = "user.qzone.qq.com"

    def __init__(self, config):
        self.cfg = config
        self._ctx: QzoneContext | None = None
        self._lock = asyncio.Lock()

    @staticmethod
    def build_ctx(cookies_str: str) -> QzoneContext:
        """从 Cookie 字符串构建上下文（不发起网络请求）"""
        if not cookies_str:
            raise RuntimeError("未提供 Cookie，请在插件配置中填写 cookies_str 或启用自动刷新")

        c = {k: v.value for k, v in SimpleCookie(cookies_str).items()}
        uin_str = c.get("uin") or c.get("p_uin") or ""
        if not uin_str.startswith("o"):
            raise RuntimeError("Cookie 中缺少合法 uin")
        uin = int(uin_str[1:])

        return QzoneContext(
            uin=uin,
            skey=c.get("skey", ""),
            p_skey=c.get("p_skey", ""),
            cookies=c,
        )

    async def get_ctx(self) -> QzoneContext:
        async with self._lock:
            if not self._ctx:
                logger.info("正在登录 QQ 空间")
                self._ctx = self.build_ctx(self.cfg.cookies_str)
                logger.info(f"登录成功，uin={self._ctx.uin}")
            return self._ctx

    async def update_cookies(self, cookies_str: str) -> QzoneContext:
        """原地更新 Cookie（复用现有 HTTP 连接，不重建 session）"""
        async with self._lock:
            self._ctx = self.build_ctx(cookies_str)
            self.cfg.cookies_str = cookies_str
            logger.info(f"Cookie 已原地更新，uin={self._ctx.uin}")
            return self._ctx

    async def login(self) -> QzoneContext:
        """兼容旧调用：用当前配置中的 Cookie 重建上下文"""
        async with self._lock:
            self._ctx = self.build_ctx(self.cfg.cookies_str)
            logger.info(f"登录成功，uin={self._ctx.uin}")
            return self._ctx
