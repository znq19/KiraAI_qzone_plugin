import asyncio
import re
from typing import Awaitable, Callable, Optional

import aiohttp
import logging

from .constants import (
    HTTP_STATUS_UNAUTHORIZED,
    HTTP_STATUS_FORBIDDEN,
    QZONE_CODE_LOGIN_EXPIRED,
    QZONE_CODE_UNKNOWN,
    QZONE_INTERNAL_HTTP_STATUS_KEY,
    QZONE_INTERNAL_META_KEY,
    QZONE_MSG_EMPTY_RESPONSE,
    QZONE_MSG_PERMISSION_DENIED,
)
from .parser import QzoneParser
from .session import QzoneSession

logger = logging.getLogger(__name__)

# 业务层登录失效特征码（参考 onebot-qzone 实机探针结果）
AUTH_FAILURE_CODES = {-3000, -100, -10001, -10006}
_AUTH_MSG_RE = re.compile(r"need\s*login|请先登录|需要登录|未登录|登录后|重新登录|登录失败", re.I)


class QzoneHttpClient:
    def __init__(self, session: QzoneSession, config):
        self.cfg = config
        self.session = session
        # DummyCookieJar：防止 aiohttp 自动保存响应 Set-Cookie 导致旧 Cookie 残留
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=self.cfg.timeout),
            cookie_jar=aiohttp.DummyCookieJar(),
        )
        # 登录失效回调：由插件主类注入，返回 True 表示已刷新凭证可重试
        self.on_auth_expired: Optional[Callable[[], Awaitable[bool]]] = None

    async def close(self):
        try:
            await self._session.close()
        except Exception as e:
            logger.warning(f"关闭 HTTP 会话时出错: {e}")

    @staticmethod
    def _is_auth_failure(status: int, parsed: dict) -> bool:
        if status == HTTP_STATUS_UNAUTHORIZED:
            return True
        # 部分接口（如图片上传）把错误码放在 ret 或嵌套的 data.ret 里
        candidates = [parsed.get("code"), parsed.get("ret")]
        data = parsed.get("data")
        if isinstance(data, dict):
            candidates.extend([data.get("code"), data.get("ret")])
        for code in candidates:
            if code == QZONE_CODE_LOGIN_EXPIRED or code in AUTH_FAILURE_CODES:
                return True
        try:
            if int(parsed.get("subcode") or 0) == -4001:
                return True
        except (TypeError, ValueError):
            pass
        messages = [str(parsed.get(k) or "") for k in ("message", "msg", "tips")]
        if isinstance(data, dict):
            messages.extend(str(data.get(k) or "") for k in ("message", "msg", "tips"))
        return bool(_AUTH_MSG_RE.search(" ".join(messages)))

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
        empty_retry: int = 0,
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

        # 服务端偶发空响应（QZone 常见抽风），独立重试额度，最多 4 次
        # 递增退避：1s/2s/3s/4s（刚刷新完 Cookie 后服务端有短暂热身窗口）
        if parsed.get("message") == QZONE_MSG_EMPTY_RESPONSE and empty_retry < 4:
            wait = empty_retry + 1
            logger.warning(f"响应内容为空，{wait}秒后重试({empty_retry + 1}/4): {url}")
            await asyncio.sleep(wait)
            return await self.request(
                method, url, params=params, data=data,
                headers=headers, retry=retry, empty_retry=empty_retry + 1,
            )

        if self._is_auth_failure(resp.status, parsed):
            if retry >= 4:
                raise RuntimeError("登录失效，Cookie 刷新后重试仍失败")

            # 记录触发判定的响应特征，便于区分真过期与误判/抽风
            logger.warning(
                f"检测到登录失效，尝试刷新 Cookie "
                f"(status={resp.status}, 响应片段: {text[:200]!r})"
            )
            refreshed = False
            if self.on_auth_expired is not None:
                try:
                    refreshed = bool(await self.on_auth_expired())
                except Exception as e:
                    logger.error(f"刷新 Cookie 回调异常: {e}")
            if not refreshed:
                # 手动 Cookie 模式下的兼容重试：部分 -3000 为瞬时错误
                if retry == 0:
                    try:
                        await self.session.login()
                        return await self.request(
                            method, url, params=params, data=data,
                            headers=headers, retry=retry + 2,
                            empty_retry=empty_retry,
                        )
                    except Exception:
                        pass
                raise RuntimeError("登录失效且无法从 OneBot 刷新 Cookie")
            # 刷新成功后稍等再重试：新凭证在服务端有短暂生效窗口
            await asyncio.sleep(1.5)
            return await self.request(
                method,
                url,
                params=params,
                data=data,
                headers=headers,
                retry=retry + 1,
                empty_retry=empty_retry,
            )

        if resp.status == HTTP_STATUS_FORBIDDEN and parsed.get("code") in (
            QZONE_CODE_UNKNOWN,
            None,
        ):
            parsed["code"] = resp.status
            parsed["message"] = QZONE_MSG_PERMISSION_DENIED

        return parsed
