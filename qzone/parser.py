# parser.py
import datetime
import json
import re
import logging
from typing import Any, Optional

import bs4
import json5

from .constants import (
    QZONE_CODE_UNKNOWN,
    QZONE_MSG_EMPTY_RESPONSE,
    QZONE_MSG_INVALID_RESPONSE,
    QZONE_MSG_JSON_PARSE_ERROR,
    QZONE_MSG_NON_OBJECT_RESPONSE,
)
from .model import Comment, Post

try:  # KiraAI 规范：插件日志并入框架统一管线（控制台 / 文件 / WebUI）
    from core.plugin import logger
except Exception:  # 单元测试桩等极简环境
    logger = logging.getLogger(__name__)


def _safe_cell(text: str, max_len: int = 30) -> str:
    """
    安全的表格单元格：
    - 无换行
    - 无 |
    - 不为空
    - 长度受限
    """
    if not text:
        return "-"
    text = str(text)
    text = text.replace("\n", " ").replace("|", "｜").strip()
    if len(text) > max_len:
        text = text[:max_len] + "…"
    return text or "-"


def _as_int(value, default: int = 0) -> int:
    """把接口里五花八门的字段安全转成 int（None/""/"abc"/"1.0" 都不抛异常）。"""
    if value is None or isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        try:
            return int(float(str(value).strip()))
        except (TypeError, ValueError):
            return default


def _as_list(value) -> list:
    """接口返回 null 时 .get(key, []) 仍会拿到 None，这里统一成 list。"""
    if isinstance(value, list):
        return value
    if value is None:
        return []
    return [value]


def _as_str(value, default: str = "") -> str:
    if value is None:
        return default
    return value if isinstance(value, str) else str(value)


class QzoneParser:
    """QQ 空间响应解析器"""

    @staticmethod
    def _error_payload(message: str) -> dict[str, Any]:
        return {"code": QZONE_CODE_UNKNOWN, "message": message, "data": {}}

    @staticmethod
    def parse_response(text: str, *, debug: bool = False) -> dict[str, Any]:
        """
        解析 JSON / JSONP / 非标准 JSON
        """

        if debug:
            logger.debug(f"响应数据: {text}")

        if not text or not text.strip():
            logger.warning("响应内容为空")
            return QzoneParser._error_payload(QZONE_MSG_EMPTY_RESPONSE)

        # 尝试匹配 JSONP 回调
        if m := re.search(
            r"callback\s*\(\s*([^{]*(\{.*\})[^)]*)\s*\)",
            text,
            re.I | re.S,
        ):
            json_str = m.group(2)
        else:
            start = text.find("{")
            end = text.rfind("}")
            if start == -1 or end == -1 or end < start:
                logger.warning("响应内容缺少 JSON 片段")
                return QzoneParser._error_payload(QZONE_MSG_INVALID_RESPONSE)
            json_str = text[start : end + 1]

        json_str = json_str.replace("undefined", "null").strip()

        try:
            data = json5.loads(json_str)
        except (ValueError, json.JSONDecodeError) as e:
            logger.error(f"JSON 解析错误: {e}")
            return QzoneParser._error_payload(QZONE_MSG_JSON_PARSE_ERROR)

        if not isinstance(data, dict):
            logger.error("JSON 解析结果不是 dict")
            return QzoneParser._error_payload(QZONE_MSG_NON_OBJECT_RESPONSE)

        if debug:
            logger.debug(f"解析后数据: {data}")

        return data

    @staticmethod
    def parse_upload_result(payload: dict[str, Any]) -> tuple[str, str]:
        """解析图片上传结果。

        字段缺失/格式变化时抛出**带原因**的异常（上层按"上传失败"处理并给出提示），
        而不是 KeyError/IndexError 直接把整条发布链路打挂。
        """
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            raise ValueError(f"上传返回缺少 data 字段: {str(payload)[:200]}")
        url = _as_str(data.get("url"))
        if "&bo=" not in url:
            raise ValueError(f"上传返回 url 格式异常: {url[:120]}")
        picbo = url.split("&bo=", 1)[1]
        try:
            richval = ",{},{},{},{},{},{},,{},{}".format(
                data["albumid"],
                data["lloc"],
                data["sloc"],
                data["type"],
                data["height"],
                data["width"],
                data["height"],
                data["width"],
            )
        except KeyError as e:
            raise ValueError(f"上传返回缺少字段 {e}: {str(data)[:200]}")
        return picbo, richval

    @staticmethod
    def parse_visitors(data: dict[str, Any], max_items: int = 20) -> str:
        data = data.get("data") or {}
        items = data.get("items")

        if not isinstance(items, list) or not items:
            return "### 最近来访明细\n\n暂无访客记录"
        max_items = max(1, int(max_items or 20))
        items = items[:max_items]

        src_map: dict[int, str] = {
            0: "访问空间",
            13: "查看动态",
            32: "手机QQ",
            41: "国际版QQ/TIM",
        }

        lines: list[str] = []

        lines.append("\n### 最近来访明细\n")
        lines.append("| 时间 | 访客 | 来源 | 状态 | 带来了 |")
        lines.append("| --- | --- | --- | --- | --- |")

        for v in items:
            if not isinstance(v, dict):
                continue

            ts = v.get("time")
            ts_int = ts if isinstance(ts, int) else 0
            dt = datetime.datetime.fromtimestamp(ts_int).strftime("%m-%d %H:%M")

            name = v.get("name")
            visitor = _safe_cell(name if isinstance(name, str) else "匿名", 16)

            src_val = v.get("src")
            src_key = src_val if isinstance(src_val, int) else -1
            src = _safe_cell(src_map.get(src_key, f"未知({src_key})"), 12)

            status_parts: list[str] = []
            yellow = v.get("yellow")
            if isinstance(yellow, int) and yellow > 0:
                status_parts.append(f"LV{yellow}")
            if v.get("is_hide_visit"):
                status_parts.append("隐身")
            status = _safe_cell(" / ".join(status_parts), 12)

            remark = "-"

            shuos = v.get("shuoshuoes")
            if isinstance(shuos, list):
                for s in shuos:
                    if isinstance(s, dict):
                        title = s.get("name")
                        if isinstance(title, str) and title.strip():
                            remark = _safe_cell(f"说说:{title}", 30)
                            break

            uins = v.get("uins")
            if remark == "-" and isinstance(uins, list):
                names = []
                for u in uins:
                    if isinstance(u, dict):
                        n = u.get("name")
                        if isinstance(n, str) and n.strip():
                            names.append(n)
                if names:
                    remark = _safe_cell("、".join(names), 30)

            lines.append(
                f"| {_safe_cell(dt, 16)} | {visitor} | {src} | {status} | {remark} |"
            )

        today = _as_int(data.get("todaycount"), 0)
        total = _as_int(data.get("totalcount"), 0)
        lines.append(f"今日访客共 {today} 人， 最近30天访客共 {total} 人")

        return "\n".join(lines)

    @staticmethod
    def parse_feeds(msglist: list[dict]) -> list[Post]:
        """解析说说列表，确保 tid 为字符串。

        容错原则：**一条脏数据只丢那一条**，不再让整页说说一起消失
        （接口里 pic 为 null、created_time 为 None/异常字符串、uin 非数字都真实出现过，
        旧实现会把整页吞成空列表，表现为"查看说说没有找到说说"）。
        """
        posts: list[Post] = []
        if not isinstance(msglist, list):
            logger.error(f"说说列表类型异常: {type(msglist)}")
            return posts
        for msg in msglist:
            try:
                post = QzoneParser._parse_single_post(msg)
            except Exception as e:
                tid = msg.get("tid") if isinstance(msg, dict) else None
                logger.warning(f"跳过一条无法解析的说说 (tid={tid}): {e}")
                continue
            if post is not None:
                posts.append(post)
        return posts

    @staticmethod
    def _parse_single_post(msg: dict) -> Optional[Post]:
        """解析单条说说（全部字段安全转换；异常由 parse_feeds 按条兜住）。"""
        if not isinstance(msg, dict):
            return None
        logger.debug(msg)
        # 提取图片信息（pic 可能是 null，必须走 _as_list）
        image_urls = []
        for img_data in _as_list(msg.get("pic")):
            if not isinstance(img_data, dict):
                continue
            for key in ("url2", "url3", "url1", "smallurl"):
                if raw := img_data.get(key):
                    image_urls.append(raw)
                    break
        # 读取视频封面（按图片处理）+ 提取视频播放地址
        video_urls = []
        for video in _as_list(msg.get("video")):
            if not isinstance(video, dict):
                continue
            video_image_url = video.get("url1") or video.get("pic_url")
            if video_image_url:
                image_urls.append(video_image_url)
            url = video.get("url3")
            if url:
                video_urls.append(url)
        # 提取转发内容
        rt_con_raw = msg.get("rt_con")
        rt_con = rt_con_raw.get("content", "") if isinstance(rt_con_raw, dict) else ""
        # 提取评论
        comments = Comment.build_list(_as_list(msg.get("commentlist")))
        # 提取点赞信息（部分接口在 likeinfo 内联返回，未内联时由 view 调 get_like_list 补充）
        likeinfo = msg.get("likeinfo") if isinstance(msg.get("likeinfo"), dict) else {}
        like_uin_info = _as_list(likeinfo.get("like_uin_info"))
        like_users = [
            str(u.get("nick") or u.get("fuin") or "")
            for u in like_uin_info
            if isinstance(u, dict) and (u.get("nick") or u.get("fuin"))
        ]
        like_count = _as_int(
            likeinfo.get("total_num") or likeinfo.get("total_number") or len(like_users), 0
        )
        # 点赞 key：取列表接口（get_like_list_app）必需，优先用说说返回的真实 key，
        # 避免手拼 unikey 在 appid 非 311 时说说不适用导致点赞列表不全
        like_key = str(msg.get("curlikekey") or msg.get("orglikekey") or "")
        # 当前登录者是否已赞（兼容多种字段名；仅作 view 补偿的辅助，主来源为本地点赞记录）
        liked_flag = (
            msg.get("isliked", msg.get("isLiked", msg.get("liked", msg.get("is_liked"))))
        )
        is_liked = liked_flag in (1, True, "1")
        # 构造Post对象，tid强制转为字符串
        tid = _as_str(msg.get("tid"), "")
        if tid == "":
            tid = "0"  # 默认值
        return Post(
            tid=tid,
            uin=_as_int(msg.get("uin"), 0),
            name=_as_str(msg.get("name"), ""),
            gin=0,
            text=_as_str(msg.get("content"), "").strip(),
            images=image_urls,
            videos=video_urls,
            anon=False,
            status="approved",
            create_time=_as_int(msg.get("created_time"), 0),
            rt_con=_as_str(rt_con, ""),
            comments=comments,
            like_count=like_count,
            like_users=like_users,
            like_key=like_key,
            is_liked=is_liked,
            extra_text=msg.get("source_name"),
        )

    @staticmethod
    def parse_recent_feeds(data: dict) -> list[Post]:
        """解析最近说说列表"""
        nested = data.get("data") if isinstance(data, dict) else None
        feeds = nested.get("data") if isinstance(nested, dict) else None
        if not isinstance(feeds, list) or not feeds:
            logger.debug("最近说说结构异常或为空，跳过")
            return []
        try:
            posts = []
            for feed in feeds:
                if not feed:
                    continue
                try:
                    # 过滤广告类内容（appid=311）
                    appid = str(feed.get("appid", ""))
                    if appid != "311":
                        continue
                    uin = feed.get("uin", "")
                    tid = feed.get("key", "")
                    if not uin or not tid:
                        logger.error(f"无效的说说数据: target_qq={uin}, tid={tid}")
                        continue
                    create_time = feed.get("abstime", "")
                    # abstime 可能为空或字符串，统一转 int
                    try:
                        create_time = int(create_time)
                    except (TypeError, ValueError):
                        create_time = 0
                    nickname = feed.get("nickname", "")
                    html_content = feed.get("html", "")
                    if not html_content:
                        logger.error(f"说说内容为空: UIN={uin}, TID={tid}")
                        continue

                    soup = bs4.BeautifulSoup(html_content, "html.parser")

                    # 提取文字内容
                    text_div = soup.find("div", class_="f-info")
                    text = text_div.get_text(strip=True) if text_div else ""
                    # 提取转发内容
                    rt_con = ""
                    txt_box = soup.select_one("div.txt-box")
                    if txt_box:
                        # 获取除昵称外的纯文本内容
                        rt_con = txt_box.get_text(strip=True)
                        # 分割掉昵称部分（从第一个冒号开始取内容）
                        if "：" in rt_con:
                            rt_con = rt_con.split("：", 1)[1].strip()
                    # 提取图片URL
                    image_urls = []
                    # 查找所有图片容器
                    if img_box := soup.find("div", class_="img-box"):
                        for img in img_box.find_all("img"):
                            src = img.get("src")
                            if src and not str(src).startswith(
                                "http://qzonestyle.gtimg.cn"
                            ):  # 过滤表情图标
                                image_urls.append(src)
                    # 视频缩略图（临时处理）
                    img_tag = soup.select_one("div.video-img img")
                    if img_tag and "src" in img_tag.attrs:
                        image_urls.append(img_tag["src"])
                    # 获取视频url
                    videos = []
                    video_div = soup.select_one("div.img-box.f-video-wrap.play")
                    if video_div and "url3" in video_div.attrs:
                        videos.append(video_div["url3"])
                    # 获取评论内容
                    comments: list[Comment] = []
                    # 查找所有评论项（包括主评论和回复）
                    comment_items = soup.select("li.comments-item.bor3")
                    if comment_items:
                        for item in comment_items:
                            data_uin = str(item.get("data-uin", ""))
                            comment_tid = str(item.get("data-tid", ""))
                            nickname = str(item.get("data-nick", ""))

                            content_div = item.select_one("div.comments-content")
                            if content_div:
                                # 移除操作按钮（回复/删除）
                                for op in content_div.select("div.comments-op"):
                                    op.decompose()
                                # 获取纯文本内容
                                content = content_div.get_text(" ", strip=True).split(
                                    ":", 1
                                )[-1]
                            else:
                                content = ""

                            comment_time_span = item.select_one("span.state")
                            comment_time = (
                                comment_time_span.get_text(strip=True)
                                if comment_time_span
                                else ""
                            )

                            # 检查是否是回复
                            parent_tid = None
                            parent_div = item.find_parent("div", class_="mod-comments-sub")
                            if parent_div:
                                parent_li = parent_div.find_parent(
                                    "li", class_="comments-item"
                                )
                                if parent_li:
                                    parent_tid = str(parent_li.get("data-tid"))

                            comments.append(
                                Comment(
                                    uin=int(data_uin) if data_uin.isdigit() else 0,
                                    nickname=nickname,
                                    content=content,
                                    create_time=0,
                                    create_time_str=comment_time,
                                    tid=int(comment_tid) if comment_tid.isdigit() else 0,
                                    parent_tid=int(parent_tid)
                                    if parent_tid and parent_tid.isdigit()
                                    else None,
                                )
                            )
                    post = Post(
                        tid=_as_str(tid),
                        uin=_as_int(uin, 0),
                        name=_as_str(nickname),
                        text=text,
                        images=list(set(image_urls)),
                        videos=videos,
                        create_time=create_time,
                        rt_con=rt_con,
                        comments=comments,
                    )
                    posts.append(post)
                except Exception as e:
                    logger.warning(f"跳过一条无法解析的最新说说: {e}")

            logger.info(f"成功解析 {len(posts)} 条最新说说")
            return posts
        except Exception as e:
            logger.error(f"解析说说错误：{e}")
            return []
