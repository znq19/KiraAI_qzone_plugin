import aiohttp
import asyncio
import logging
import html
import os
import re
from typing import Union, List, Optional

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


async def download_file(url: str, timeout: int = 60, max_retries: int = 3) -> Optional[bytes]:
    """下载文件（图片），支持超时和重试，自动解码HTML实体。
    QQ 多媒体 CDN 对 Referer 敏感，失败时会额外尝试不带 Referer。
    """
    # 清洗URL
    url = clean_url(url)
    if not url.startswith('http'):
        logger.warning(f"无效的 URL 格式: {url}")
        return None

    base_headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    }
    for attempt in range(max_retries):
        # 前几次带 qzone Referer，最后一次不带（兼容外部 CDN）
        headers = dict(base_headers)
        if attempt < max_retries - 1:
            headers['Referer'] = 'https://qzone.qq.com/'
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
                    if resp.status == 200:
                        data = await resp.read()
                        logger.info(f"图片下载成功: {url} ({len(data)} bytes)")
                        return data
                    else:
                        logger.warning(f"下载失败 (HTTP {resp.status}): {url}")
        except asyncio.TimeoutError:
            logger.warning(f"下载超时 (尝试 {attempt+1}/{max_retries}): {url}")
        except Exception as e:
            logger.warning(f"下载异常 (尝试 {attempt+1}/{max_retries}): {e}")
        await asyncio.sleep(2)  # 重试前等待
    logger.error(f"图片下载最终失败: {url}")
    return None


async def normalize_images(images: List[BytesOrStr] | None, errors: Optional[list] = None) -> List[bytes]:
    """
    将 str/bytes 混合列表统一转成 bytes 列表：
    - str（本地路径）-> 读取文件 bytes
    - str（URL）-> 下载后转 bytes，并校验图片魔数
    - bytes -> 原样保留
    - None -> 空列表
    errors 传入列表时会收集每项失败原因（不抛异常，静默跳过该项）。
    """
    if images is None:
        return []

    def _fail(reason: str):
        logger.warning(reason)
        if errors is not None:
            errors.append(reason)

    cleaned: List[bytes] = []
    for item in images:
        if isinstance(item, bytes):
            cleaned.append(item)
        elif isinstance(item, str):
            # 本地路径（如聊天图片缓存 data/temp/xxx.jpg）直接读取
            local = item.strip().strip('"').strip("'")
            if not local.startswith(('http://', 'https://')):
                if os.path.exists(local):
                    try:
                        with open(local, 'rb') as f:
                            data = f.read()
                        if not looks_like_image(data):
                            _fail(f"本地文件内容不是图片（可能是过期链接下载的错误页）: {local}")
                            continue
                        cleaned.append(data)
                    except Exception as e:
                        _fail(f"读取本地图片失败: {local}: {e}")
                else:
                    _fail(f"图片路径不存在: {local}")
                continue
            file = await download_file(item)
            if file is None:
                _fail(f"图片下载失败（可能链接已过期）: {item[:80]}")
                continue
            if not looks_like_image(file):
                _fail(f"下载内容不是图片（链接可能已过期返回错误页）: {item[:80]}")
                continue
            cleaned.append(file)
        else:
            raise TypeError(f"image 必须是 str 或 bytes，收到 {type(item)}")
    return cleaned