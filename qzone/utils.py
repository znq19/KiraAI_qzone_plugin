import aiohttp
import asyncio
import logging
import html
import re
from typing import Union, List, Optional

logger = logging.getLogger(__name__)

BytesOrStr = Union[str, bytes]


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
    """下载文件（图片），支持超时和重试，自动解码HTML实体"""
    # 清洗URL
    url = clean_url(url)
    if not url.startswith('http'):
        logger.warning(f"无效的 URL 格式: {url}")
        return None

    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Referer': 'https://qzone.qq.com/',
    }
    for attempt in range(max_retries):
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


async def normalize_images(images: List[BytesOrStr] | None) -> List[bytes]:
    """
    将 str/bytes 混合列表统一转成 bytes 列表：
    - str -> 下载后转 bytes（下载失败则忽略）
    - bytes -> 原样保留
    - None -> 空列表
    """
    if images is None:
        return []

    cleaned: List[bytes] = []
    for item in images:
        if isinstance(item, bytes):
            cleaned.append(item)
        elif isinstance(item, str):
            file = await download_file(item)
            if file is not None:
                cleaned.append(file)
        else:
            raise TypeError(f"image 必须是 str 或 bytes，收到 {type(item)}")
    return cleaned