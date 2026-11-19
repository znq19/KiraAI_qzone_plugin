import aiohttp
import asyncio
import ipaddress
import logging
import html
import os
import re
import time
from dataclasses import dataclass
from typing import Union, List, Optional
from urllib.parse import urlparse

try:  # KiraAI 规范：插件日志并入框架统一管线（控制台 / 文件 / WebUI）
    from core.plugin import logger
except Exception:  # 单元测试桩等极简环境
    logger = logging.getLogger(__name__)

BytesOrStr = Union[str, bytes]

# 常见图片格式的文件头魔数
_IMAGE_MAGIC = (
    b"\xff\xd8\xff",          # JPEG
    b"\x89PNG\r\n\x1a\n",     # PNG
    b"GIF87a",                # GIF
    b"GIF89a",                # GIF
    b"RIFF",                  # WebP (RIFF....WEBP)
    b"BM",                    # BMP
    b"\x00\x00\x00",          # HEIC/MP4 系（粗判）
)

# ---------- 下载参数默认值 ----------
DEFAULT_IMAGE_TIMEOUT = 20          # 单次请求超时（秒）
DEFAULT_MAX_RETRIES = 2             # 网络抖动重试次数
DEFAULT_MAX_BYTES = 8 * 1024 * 1024  # 单图大小上限（8MB）
# 这些状态码重试同一 URL 没有意义（链接过期/不存在/无权限），直接快速失败
NO_RETRY_STATUSES = frozenset({400, 401, 403, 404, 405, 410, 451})
# 同一 URL 的失败日志静默窗口（秒）：避免过期图片每轮刷新都刷一条日志
FAIL_LOG_TTL = 600.0

_fail_log_ts: dict[str, float] = {}
_FAIL_LOG_MAX = 512


def _prune_fail_log(now: float) -> None:
    if len(_fail_log_ts) <= _FAIL_LOG_MAX:
        return
    for key, ts in list(_fail_log_ts.items()):
        if now - ts > FAIL_LOG_TTL:
            _fail_log_ts.pop(key, None)
    if len(_fail_log_ts) > _FAIL_LOG_MAX:
        for key, _ in sorted(_fail_log_ts.items(), key=lambda kv: kv[1])[: len(_fail_log_ts) - _FAIL_LOG_MAX]:
            _fail_log_ts.pop(key, None)


def reset_fail_log() -> None:
    """仅供自检使用：清空失败日志去重表。"""
    _fail_log_ts.clear()


def _log_fetch_failure(url: str, status: Optional[int], reason: str) -> None:
    """失败日志去重：同一 URL（按状态码分组）在 FAIL_LOG_TTL 内只打一条。

    链接过期（HTTP 400）属于**预期内**情况，降为 info 并说明会静默多久，
    不再每轮都刷 warning。
    """
    now = time.time()
    key = f"{status}:{url}"
    last = _fail_log_ts.get(key)
    first_time = last is None or (now - last) > FAIL_LOG_TTL
    if first_time:
        _fail_log_ts[key] = now
        _prune_fail_log(now)
        if status in NO_RETRY_STATUSES:
            logger.info(
                f"图片链接不可用（HTTP {status}，可能已过期），已跳过并在 {int(FAIL_LOG_TTL)}s 内静默: {url[:80]}"
            )
        else:
            logger.warning(
                f"图片下载失败（{reason}），已跳过并在 {int(FAIL_LOG_TTL)}s 内静默: {url[:80]}"
            )
    else:
        logger.debug(f"图片下载失败（重复，已静默）: {reason} {url[:80]}")


@dataclass
class FetchResult:
    """下载结果：把"为什么失败"显式带回上层，便于负缓存与降级决策。"""

    ok: bool
    data: Optional[bytes] = None
    status: Optional[int] = None
    reason: str = ""
    retryable: bool = False

    def __bool__(self) -> bool:
        return self.ok


def looks_like_image(data: bytes) -> bool:
    """校验下载到的内容是否真的是图片（防止把 HTML 错误页当图片上传）"""
    if not data or len(data) < 12:
        return False
    return any(data.startswith(m) for m in _IMAGE_MAGIC)


def clean_url(url: str) -> str:
    """清洗URL：去除多余空格、引号，解码HTML实体，修复常见编码问题"""
    url = url.strip().strip('"').strip("'")
    # 解码HTML实体（如 &amp; -> &）
    url = html.unescape(url)
    # 修复可能的错误编码（如 %3A 等，但通常不需要处理）
    # 移除 URL 中可能存在的多余空格（如 %20 已经是空格，不处理）
    # 如果 URL 中包含多余的特殊字符，可以尝试只保留有效部分
    # 这里简单处理：移除可能出现的不可见字符（如换行）
    url = re.sub(r'\s+', '', url)
    return url


def is_safe_public_url(url: str) -> bool:
    """校验外链是否为公网 http(s) 地址（防止 AI 传内网/回环地址造成 SSRF）。

    非 URL（本地路径）一律返回 True，由调用方按本地路径处理。
    """
    text = str(url or "").strip()
    if not text.startswith(("http://", "https://")):
        return True
    try:
        host = urlparse(text).hostname or ""
    except Exception:
        return False
    if not host:
        return False
    if host.lower() in ("localhost", "localhost.localdomain"):
        return False
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return True  # 域名，交给 DNS
    return not (
        addr.is_private or addr.is_loopback or addr.is_link_local
        or addr.is_reserved or addr.is_multicast or addr.is_unspecified
    )


# ---------- 共享 HTTP 会话（连接复用，避免每次重试都新建 ClientSession） ----------
_shared_session: Optional[aiohttp.ClientSession] = None
_shared_session_loop: Optional[object] = None
_session_lock: Optional[asyncio.Lock] = None
_session_lock_loop: Optional[object] = None


async def _get_shared_session() -> aiohttp.ClientSession:
    """懒创建共享会话；事件循环变化（如插件热重载）时自动重建。"""
    global _shared_session, _shared_session_loop, _session_lock, _session_lock_loop
    loop = asyncio.get_running_loop()
    # 锁跟随"事件循环"而非"会话"：会话被关掉后不会顺手换锁，
    # 避免关闭瞬间的并发调用各拿一把锁、创建出两个会话。
    if _session_lock is None or _session_lock_loop is not loop:
        _session_lock = asyncio.Lock()
        _session_lock_loop = loop
    async with _session_lock:
        if _shared_session is None or _shared_session.closed or _shared_session_loop is not loop:
            _shared_session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=None),
                cookie_jar=aiohttp.DummyCookieJar(),
            )
            _shared_session_loop = loop
        return _shared_session


async def close_shared_session() -> None:
    """关闭共享会话（插件 terminate 时调用）。"""
    global _shared_session, _shared_session_loop
    session, _shared_session = _shared_session, None
    _shared_session_loop = None
    if session is not None and not session.closed:
        try:
            await session.close()
        except Exception as e:
            logger.warning(f"关闭共享 HTTP 会话时出错: {e}")


async def fetch_bytes(
    url: str,
    *,
    timeout: Optional[float] = None,
    max_retries: Optional[int] = None,
    max_bytes: Optional[int] = None,
    require_image: bool = True,
) -> FetchResult:
    """下载资源。

    与旧实现的关键差异：
    - 复用共享 aiohttp 会话（不再每次重试新建连接池）；
    - 400/403/404/410 等"重试也没用"的状态码零重试，立即返回；
    - 只有网络抖动与 5xx 才重试，且退避更短；
    - 有单文件大小上限，避免超大文件占满内存；
    - 失败原因通过 FetchResult 带回，便于上层做负缓存/降级；
    - 失败日志按 URL 去重，避免过期图片刷屏。
    """
    url = clean_url(url)
    if not url.startswith('http'):
        logger.warning(f"无效的 URL 格式: {url}")
        return FetchResult(False, reason="URL 格式无效")

    timeout = DEFAULT_IMAGE_TIMEOUT if timeout is None else timeout
    max_retries = DEFAULT_MAX_RETRIES if max_retries is None else max_retries
    max_bytes = DEFAULT_MAX_BYTES if max_bytes is None else max_bytes
    attempts = max(1, int(max_retries))

    base_headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    }
    last_reason = "未知错误"
    last_status: Optional[int] = None
    for attempt in range(attempts):
        # 前几次带 qzone Referer，最后一次不带（兼容外部 CDN）
        headers = dict(base_headers)
        if attempt < attempts - 1:
            headers['Referer'] = 'https://qzone.qq.com/'
        try:
            session = await _get_shared_session()
            async with session.get(
                url, headers=headers, timeout=aiohttp.ClientTimeout(total=timeout)
            ) as resp:
                if resp.status == 200:
                    chunks: list[bytes] = []
                    total = 0
                    async for chunk in resp.content.iter_chunked(64 * 1024):
                        total += len(chunk)
                        if max_bytes and total > max_bytes:
                            last_reason = f"文件超过大小上限 {max_bytes} 字节"
                            _log_fetch_failure(url, resp.status, last_reason)
                            return FetchResult(False, status=resp.status, reason=last_reason)
                        chunks.append(chunk)
                    data = b"".join(chunks)
                    if require_image and not looks_like_image(data):
                        last_reason = "下载内容不是图片（链接可能已过期返回错误页）"
                        _log_fetch_failure(url, resp.status, last_reason)
                        return FetchResult(False, status=resp.status, reason=last_reason)
                    logger.debug(f"图片下载成功: {url} ({len(data)} bytes)")
                    return FetchResult(True, data=data, status=resp.status, reason="ok")
                last_status = resp.status
                last_reason = f"HTTP {resp.status}"
                if resp.status in NO_RETRY_STATUSES or 400 <= resp.status < 500:
                    # 链接过期/不存在：重试同一 URL 无意义（NapCat #190/#265），
                    # 快速失败交由上层 get_msg 续命或降级，避免 3 次 × 2s 白等。
                    _log_fetch_failure(url, resp.status, last_reason)
                    return FetchResult(False, status=resp.status, reason=last_reason)
                # 其余（5xx 等）：记录并允许重试
        except asyncio.TimeoutError:
            last_reason = f"下载超时（{timeout}s）"
        except aiohttp.ClientError as e:
            last_reason = f"网络异常: {e}"
        except Exception as e:
            last_reason = f"下载异常: {e}"
        if attempt < attempts - 1:
            await asyncio.sleep(0.8 * (attempt + 1))
    _log_fetch_failure(url, last_status, last_reason)
    return FetchResult(False, status=last_status, reason=last_reason, retryable=True)


async def download_file(url: str, timeout: int = DEFAULT_IMAGE_TIMEOUT,
                        max_retries: int = DEFAULT_MAX_RETRIES,
                        max_bytes: int = DEFAULT_MAX_BYTES) -> Optional[bytes]:
    """下载文件（图片）。返回 bytes 或 None，失败原因见日志/FetchResult。

    保留旧签名以兼容既有调用；需要失败原因时直接用 fetch_bytes()。
    """
    result = await fetch_bytes(
        url, timeout=timeout, max_retries=max_retries, max_bytes=max_bytes
    )
    return result.data if result.ok else None


def _read_local_bytes(path: str) -> bytes:
    with open(path, 'rb') as f:
        return f.read()


async def normalize_images(images: List[BytesOrStr] | None, errors: Optional[list] = None,
                           pairs: Optional[list] = None) -> List[bytes]:
    """
    将 str/bytes 混合列表统一转成 bytes 列表：
    - str（本地路径）-> 异步读取文件 bytes
    - str（URL）-> 下载后转 bytes，并校验图片魔数
    - bytes -> 原样保留
    - None -> 空列表
    errors 传入列表时会收集每项失败原因（不抛异常，静默跳过该项）。
    pairs  传入列表时会收集 (原始输入, 实际 bytes)，供上层建立"来源 -> 内容指纹"映射。
    """
    if images is None:
        return []

    def _fail(reason: str):
        logger.warning(reason)
        if errors is not None:
            errors.append(reason)

    def _ok(source, data: bytes):
        if pairs is not None:
            pairs.append((source, data))

    cleaned: List[bytes] = []
    for item in images:
        if isinstance(item, bytes):
            cleaned.append(item)
            _ok(item, item)
        elif isinstance(item, str):
            # 本地路径（如聊天图片缓存 data/temp/xxx.jpg）直接读取
            local = item.strip().strip('"').strip("'")
            if not local.startswith(('http://', 'https://')):
                if os.path.exists(local):
                    try:
                        # 读盘是同步阻塞调用，放到线程池避免卡住事件循环
                        data = await asyncio.to_thread(_read_local_bytes, local)
                        if not looks_like_image(data):
                            _fail(f"本地文件内容不是图片（可能是过期链接下载的错误页）: {local}")
                            continue
                        cleaned.append(data)
                        _ok(item, data)
                    except Exception as e:
                        _fail(f"读取本地图片失败: {local}: {e}")
                else:
                    _fail(f"图片路径不存在: {local}")
                continue
            result = await fetch_bytes(item)
            if not result.ok:
                _fail(f"图片下载失败（{result.reason}）: {item[:80]}")
                continue
            cleaned.append(result.data)
            _ok(item, result.data)
        else:
            raise TypeError(f"image 必须是 str 或 bytes，收到 {type(item)}")
    return cleaned
