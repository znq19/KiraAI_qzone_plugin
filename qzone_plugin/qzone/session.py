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

    async def get_ctx(self) -> QzoneContext:
        async with self._lock:
            if not self._ctx:
                self._ctx = await self.login()
            return self._ctx

    async def login(self) -> QzoneContext:
        logger.info("正在登录 QQ 空间")
        cookies_str = self.cfg.cookies_str
        if not cookies_str:
            raise RuntimeError("未提供 Cookie，请在插件配置中填写 cookies_str")

        c = {k: v.value for k, v in SimpleCookie(cookies_str).items()}
        uin_str = c.get("uin", "")
        if not uin_str.startswith("o"):
            raise RuntimeError("Cookie 中缺少合法 uin")
        uin = int(uin_str[1:])

        self._ctx = QzoneContext(
            uin=uin,
            skey=c.get("skey", ""),
            p_skey=c.get("p_skey", ""),
        )

        logger.info(f"登录成功，uin={uin}")
        return self._ctx