import asyncio
import aiohttp
import logging

from .constants import (
    HTTP_STATUS_UNAUTHORIZED,
    HTTP_STATUS_FORBIDDEN,
    QZONE_CODE_LOGIN_EXPIRED,
    QZONE_CODE_UNKNOWN,
    QZONE_INTERNAL_HTTP_STATUS_KEY,
    QZONE_INTERNAL_META_KEY,
    QZONE_MSG_PERMISSION_DENIED,
)
from .parser import QzoneParser
from .session import QzoneSession

logger = logging.getLogger(__name__)


class QzoneHttpClient:
    def __init__(self, session: QzoneSession, config):
        self.cfg = config
        self.session = session
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=self.cfg.timeout)
        )

    async def close(self):
        await self._session.close()

    async def request(
        self,
        method: str,
        url: str,
        *,
        params: dict | None = None,
        data: dict | None = None,
        headers: dict | None = None,
        timeout: int | None = None,
        retry: int = 0,
    ) -> dict:
        ctx = await self.session.get_ctx()
        async with self._session.request(
            method,
            url,
            params=params,
            data=data,
            headers=headers or ctx.headers(),
            cookies=ctx.cookies(),
            timeout=timeout,
        ) as resp:
            text = await resp.text()

        parsed = QzoneParser.parse_response(text)
        meta = parsed.get(QZONE_INTERNAL_META_KEY)
        if not isinstance(meta, dict):
            meta = {}
            parsed[QZONE_INTERNAL_META_KEY] = meta
        meta[QZONE_INTERNAL_HTTP_STATUS_KEY] = resp.status

        if resp.status == HTTP_STATUS_UNAUTHORIZED or parsed.get(
            "code"
        ) == QZONE_CODE_LOGIN_EXPIRED:
            if retry >= 2:
                raise RuntimeError("登录失效，重试失败")

            logger.warning("登录失效，重新登录中")
            await self.session.login()
            return await self.request(
                method,
                url,
                params=params,
                data=data,
                headers=headers,
                retry=retry + 1,
            )

        if resp.status == HTTP_STATUS_FORBIDDEN and parsed.get("code") in (
            QZONE_CODE_UNKNOWN,
            None,
        ):
            parsed["code"] = resp.status
            parsed["message"] = QZONE_MSG_PERMISSION_DENIED

        return parsed