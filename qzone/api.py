import base64
import time
import logging
from typing import Any

from .client import QzoneHttpClient
from .model import ApiResponse, Post, Comment
from .parser import QzoneParser
from .session import QzoneSession
from .utils import normalize_images

logger = logging.getLogger(__name__)

_MOBILE_UA = "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36"


def normalize_tid(tid) -> str:
    """规范化说说 tid：
    - 剥离 unikey 形式（http://user.qzone.qq.com/123/mood/<tid>）
    - 剥离 .311 / .1 等 appid 后缀
    - feeds3 复合 key 取最后一段有效 hex
    """
    s = str(tid or "").strip()
    if not s:
        return s
    if "/mood/" in s:
        s = s.split("/mood/", 1)[1]
    # 去掉 .311 / .1 后缀
    if "." in s:
        head, _, tail = s.rpartition(".")
        if tail.isdigit() and head:
            s = head
    return s


class QzoneAPI(QzoneHttpClient):
    """QQ 空间 HTTP API 封装"""

    BASE_URL = "https://user.qzone.qq.com"
    UPLOAD_IMAGE_URL = "https://up.qzone.qq.com/cgi-bin/upload/cgi_upload_image"
    EMOTION_URL = "https://user.qzone.qq.com/proxy/domain/taotao.qzone.qq.com/cgi-bin/emotion_cgi_publish_v6"
    DOLIKE_URL = "https://user.qzone.qq.com/proxy/domain/w.qzone.qq.com/cgi-bin/likes/internal_dolike_app"
    DOLIKE_UNLIKE_URL = "https://user.qzone.qq.com/proxy/domain/w.qzone.qq.com/cgi-bin/likes/internal_unlike_app"
    LIKE_LIST_URL = "https://user.qzone.qq.com/proxy/domain/users.qzone.qq.com/cgi-bin/likes/get_like_list_app"
    PERSONAL_CARD_URL = "https://user.qzone.qq.com/proxy/domain/r.qzone.qq.com/cgi-bin/user/cgi_personal_card"
    LIST_URL = "https://user.qzone.qq.com/proxy/domain/taotao.qq.com/cgi-bin/emotion_cgi_msglist_v6"
    COMMENT_URL = "https://user.qzone.qq.com/proxy/domain/taotao.qzone.qq.com/cgi-bin/emotion_cgi_re_feeds"
    COMMENT_H5_URL = "https://h5.qzone.qq.com/proxy/domain/taotao.qzone.qq.com/cgi-bin/emotion_cgi_re_feeds"
    ZONE_LIST_URL = "https://user.qzone.qq.com/proxy/domain/ic2.qzone.qq.com/cgi-bin/feeds/feeds3_html_more"
    VISITOR_URL = "https://h5.qzone.qq.com/proxy/domain/g.qzone.qq.com/cgi-bin/friendshow/cgi_get_visitor_more"
    REPLY_URL = "https://h5.qzone.qq.com/proxy/domain/taotao.qzone.qq.com/cgi-bin/emotion_cgi_re_feeds"
    DELETE_URL = "https://h5.qzone.qq.com/proxy/domain/taotao.qzone.qq.com/cgi-bin/emotion_cgi_delete_v6"
    DELETE_COMMENT_H5_URL = "https://h5.qzone.qq.com/proxy/domain/taotao.qzone.qq.com/cgi-bin/emotion_cgi_delcomment_ugc"
    DETAIL_URL = "https://h5.qzone.qq.com/proxy/domain/taotao.qq.com/cgi-bin/emotion_cgi_msgdetail_v6"
    DETAIL_PC_URL = "https://user.qzone.qq.com/proxy/domain/taotao.qzone.qq.com/cgi-bin/emotion_cgi_getdetailv6"
    DETAIL_MOBILE_URL = "https://mobile.qzone.qq.com/detail"

    def __init__(self, session: QzoneSession, config):
        super().__init__(session, config)

    async def _upload_image(self, image: bytes) -> ApiResponse:
        """上传单张图片 (本接口较为脆弱)"""
        ctx = await self.session.get_ctx()
        raw = await self.request(
            "POST",
            self.UPLOAD_IMAGE_URL,
            params={"g_tk": ctx.gtk2},
            data={
                "filename": "filename",
                "uploadtype": "1",
                "albumtype": "7",
                "exttype": "0",
                "refer": "shuoshuo",
                "skey": ctx.skey,
                "uin": ctx.uin,
                "p_uin": ctx.uin,
                "zzpaneluin": ctx.uin,
                "zzpanelkey": "",
                "p_skey": ctx.p_skey,
                "output_type": "json",
                "charset": "utf-8",
                "output_charset": "utf-8",
                "upload_hd": "1",
                "hd_width": "2048",
                "hd_height": "10000",
                "hd_quality": "96",
                "base64": "1",
                "picfile": base64.b64encode(image).decode(),
            },
            headers={
                "referer": f"{self.BASE_URL}/{ctx.uin}",
                "origin": self.BASE_URL,
            },
            timeout=60,
        )
        resp = ApiResponse.from_raw(raw, code_key="ret", msg_key="msg")
        if not resp.ok:
            # 记录原始响应便于诊断（截断防爆日志）
            logger.warning(f"图片上传失败原始响应: {str(raw)[:500]}")
        return resp

    async def get_visitor(self) -> ApiResponse:
        """获取最近访客和统计，不清除访客提示状态。"""
        ctx = await self.session.get_ctx()
        raw = await self.request(
            "GET",
            self.VISITOR_URL,
            params={
                "uin": ctx.uin,
                "mask": 7,
                "g_tk": ctx.gtk2,
                "format": "json",
                "page": 1,
                "fupdate": 1,
                "clear": 0,
            },
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36",
                "Referer": f"{self.BASE_URL}/{ctx.uin}",
                "Accept": "application/json, text/javascript, */*; q=0.01",
                "X-Requested-With": "XMLHttpRequest",
            },
            empty_retry_limit=1,
        )
        resp = ApiResponse.from_raw(raw)
        if resp.ok:
            logger.info("QZone访客接口成功")
        else:
            logger.warning(
                "QZone访客接口失败: code=%s message=%s raw=%s",
                resp.code,
                resp.message,
                str(raw)[:500],
            )
        return resp

    async def publish(self, post: Post, allow_image_drop: bool = False) -> ApiResponse:
        """发表说说, 返回tid

        allow_image_drop=True 时（吸附兑底/后台自动等场景），
        配图全部获取失败会降级为纯文字发布而不是整个失败。
        """
        ctx = await self.session.get_ctx()
        data: dict[str, Any] = {
            "syn_tweet_verson": "1",
            "paramstr": "1",
            "who": "1",
            "con": post.text,
            "feedversion": "1",
            "ver": "1",
            "ugc_right": "1",
            "to_sign": "0",
            "hostuin": ctx.uin,
            "code_version": "1",
            "format": "json",
            "qzreferrer": f"{self.BASE_URL}/{ctx.uin}",
        }
        download_errors: list[str] = []
        if post.images:
            logger.debug(f"正在上传图片: {post.images}")
            pic_bos, richvals = [], []
            imgs: list[bytes] = await normalize_images(post.images, errors=download_errors)
            if not imgs:
                if allow_image_drop:
                    logger.warning(
                        f"配图全部获取失败，降级为纯文字发布: {'; '.join(download_errors)}"
                    )
                    download_errors = ["配图获取失败（链接可能已过期），已降级为纯文字"]
                else:
                    raise RuntimeError(
                        f"所有图片均获取失败（共 {len(post.images)} 张）: {'; '.join(download_errors) or '原因未知'}"
                    )
            elif download_errors:
                logger.warning(f"部分图片获取失败: {'; '.join(download_errors)}")
            for img in imgs:
                resp = await self._upload_image(img)
                if not resp.ok:
                    raise RuntimeError(f"上传图片失败: {resp.message}")
                picbo, richval = QzoneParser.parse_upload_result(resp.data)
                pic_bos.append(picbo)
                richvals.append(richval)
            if pic_bos:
                data.update(
                    pic_bo=",".join(pic_bos),
                    richtype="1",
                    richval="\t".join(richvals),
                )

        raw = await self.request(
            "POST",
            self.EMOTION_URL,
            params={"g_tk": ctx.gtk2, "uin": ctx.uin},
            data=data,
        )
        resp = ApiResponse.from_raw(raw)
        # 部分图片失败时把原因捎带给调用方（不影响发布结果）
        if resp.ok and download_errors:
            resp.message = "; ".join(download_errors)
        return resp

    async def like(
        self,
        post: Post,
        abstime: int | None = None,
        appid: int = 311,
        typeid: int = 0,
    ) -> ApiResponse:
        """
        点赞指定说说。

        参数采用精易论坛 2024 实测成功格式（网页端真实抓包）：
        - unikey/curkey 带 `.1` 后缀：http://user.qzone.qq.com/{作者}/mood/{tid}.1
        - from=-100, face=0
        - 旧格式（from=1、unikey 无 .1、带 appid/typeid/fid）在现代 QQ 空间
          返回 ret=0 但实际不生效（假成功），不再使用。
        """
        ctx = await self.session.get_ctx()
        tid = normalize_tid(post.tid)
        unikey = f"http://user.qzone.qq.com/{post.uin}/mood/{tid}.1"
        qzreferrer = f"https://user.qzone.qq.com/{post.uin}"

        try:
            raw = await self.request(
                "POST",
                self.DOLIKE_URL,
                params={"g_tk": ctx.gtk2},
                data={
                    "qzreferrer": qzreferrer,
                    "opuin": post.uin,
                    "unikey": unikey,
                    "curkey": unikey,
                    "from": -100,
                    "fupdate": 1,
                    "face": 0,
                    "format": "json",
                },
            )
            if raw.get("ret") == 0 or raw.get("code") == 0:
                raw["code"] = 0
                logger.info(f"QZone点赞成功: route=dolike post={tid}")
                return ApiResponse.from_raw(raw)
            logger.warning(
                f"点赞失败(dolike): code={raw.get('code')} ret={raw.get('ret')} "
                f"msg={raw.get('message') or raw.get('msg')}"
            )
            return ApiResponse(
                ok=False,
                code=-1,
                message=f"点赞失败: code={raw.get('code')} msg={raw.get('message') or raw.get('msg')}",
                data={},
                raw=raw,
            )
        except Exception as e:
            logger.warning(f"点赞异常(dolike): {e}")
            return ApiResponse(
                ok=False, code=-1, message=str(e),
                data={}, raw={},
            )
        return ApiResponse(
            ok=False,
            code=-1,
            message="; ".join(errors) or "点赞失败",
            data={},
            raw={},
        )

    async def unlike(
        self,
        post: Post,
        abstime: int | None = None,
        appid: int = 311,
        typeid: int = 0,
    ) -> ApiResponse:
        """
        取消点赞指定说说。

        唯一路径：internal_unlike_app（精易论坛实测：把 internal_dolike_app 的 DOlike
        改成 unlike 即取消赞）+ 精易 2024 实测成功参数：
        - unikey/curkey 带 `.1` 后缀
        - from=-100, face=0
        旧格式（active 参数切换 / from=1 / unikey 无 .1）均假成功，不再使用。
        """
        ctx = await self.session.get_ctx()
        tid = normalize_tid(post.tid)
        unikey = f"http://user.qzone.qq.com/{post.uin}/mood/{tid}.1"
        qzreferrer = f"https://user.qzone.qq.com/{post.uin}"

        try:
            raw = await self.request(
                "POST",
                self.DOLIKE_UNLIKE_URL,
                params={"g_tk": ctx.gtk2},
                data={
                    "qzreferrer": qzreferrer,
                    "opuin": post.uin,
                    "unikey": unikey,
                    "curkey": unikey,
                    "from": -100,
                    "fupdate": 1,
                    "face": 0,
                    "format": "json",
                },
            )
            if raw.get("ret") == 0 or raw.get("code") == 0:
                raw["code"] = 0
                logger.info(
                    f"QZone取消点赞成功: route=unlike_app post={tid}"
                )
                return ApiResponse.from_raw(raw)
            logger.warning(
                f"取消点赞失败(unlike_app): code={raw.get('code')} ret={raw.get('ret')} "
                f"msg={raw.get('message') or raw.get('msg')}"
            )
            return ApiResponse(
                ok=False,
                code=-1,
                message=f"取消点赞失败: code={raw.get('code')} msg={raw.get('message') or raw.get('msg')}",
                data={},
                raw=raw,
            )
        except Exception as e:
            logger.warning(f"取消点赞异常(unlike_app): {e}")
            return ApiResponse(
                ok=False, code=-1, message=str(e),
                data={}, raw={},
            )

    async def get_like_list(self, post: Post, query_count: int = 20) -> ApiResponse:
        """获取说说点赞列表（点赞人 + 总数），模拟空间页「xx等人觉得很赞」。

        点赞人是独立接口 get_like_list_app 返回（说说列表/详情接口不含点赞人明细）：
        - unikey 需带 `.1` 后缀（与点赞操作的 unikey 不同，见爬虫实测）
        - 返回 data.like_uin_info（[{fuin, nick, ...}]）+ data.total_number
        """
        ctx = await self.session.get_ctx()
        tid = normalize_tid(post.tid)
        # 优先用说说的真实 like key（curlikekey/orglikekey，取自详情/列表接口），
        # 手拼 unikey 仅作回退：`http://user.qzone.qq.com/{作者}/mood/{tid}.1`
        # （.1 后缀与点赞操作的 unikey 不同，为点赞列表弹窗专用格式）
        like_key = getattr(post, "like_key", "") or ""
        if like_key:
            unikey = like_key
        else:
            unikey = f"http://user.qzone.qq.com/{post.uin}/mood/{tid}.1"
        try:
            raw = await self.request(
                "GET",
                self.LIKE_LIST_URL,
                params={
                    "uin": ctx.uin,
                    "unikey": unikey,
                    "begin_uin": 0,
                    "query_count": query_count,
                    "if_first_page": 1,
                    "g_tk": ctx.gtk2,
                    "format": "json",
                },
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36",
                    "Referer": f"https://user.qzone.qq.com/{post.uin}",
                },
                empty_retry_limit=1,
            )
            data = raw.get("data") if isinstance(raw.get("data"), dict) else {}
            like_uin_info = data.get("like_uin_info") if isinstance(data.get("like_uin_info"), list) else []
            total = data.get("total_number") or len(like_uin_info) or 0
            # is_dolike：当前用户是否已赞（接口真实字段，CSDN 爬取教程确认）
            is_dolike = data.get("is_dolike") in (1, True, "1")
            like_uins = [
                str(u.get("fuin") or u.get("uin") or "")
                for u in like_uin_info
                if isinstance(u, dict) and (u.get("fuin") or u.get("uin"))
            ]
            uin_str = ",".join(like_uins) or "-"
            logger.info(
                f"QZone点赞列表: post={tid} total={total} shown={len(like_uin_info)} "
                f"is_dolike={is_dolike} uins=[{uin_str}] key={'real' if like_key else 'handmade'}"
            )
            return ApiResponse(
                ok=True, code=0, message=None,
                data={
                    "like_uin_info": like_uin_info,
                    "like_uins": like_uins,
                    "total_number": total,
                    "is_dolike": is_dolike,
                },
                raw=raw,
            )
        except Exception as e:
            logger.debug(f"获取点赞列表失败: post={tid} err={e}")
            return ApiResponse(
                ok=False, code=-1, message=str(e),
                data={}, raw={},
            )

    async def get_user_info(self, uin: str) -> ApiResponse:
        """获取用户基本资料（昵称等），用于展示自己昵称（防「我」昵称诈骗）。"""
        ctx = await self.session.get_ctx()
        try:
            raw = await self.request(
                "GET",
                self.PERSONAL_CARD_URL,
                params={"uin": uin, "g_tk": ctx.gtk2},
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36",
                    "Referer": f"https://user.qzone.qq.com/{uin}",
                },
                empty_retry_limit=1,
            )
            data = raw.get("data") if isinstance(raw.get("data"), dict) else {}
            nickname = str(
                data.get("nickname")
                or data.get("nick")
                or data.get("name")
                or ""
            )
            logger.info(f"QZone用户资料: uin={uin} nickname={nickname}")
            return ApiResponse(
                ok=True, code=0, message=None,
                data={"nickname": nickname, "uin": uin},
                raw=raw,
            )
        except Exception as e:
            logger.debug(f"获取用户资料失败: uin={uin} err={e}")
            return ApiResponse(
                ok=False, code=-1, message=str(e),
                data={}, raw={},
            )

    async def comment(self, post: Post, content: str) -> ApiResponse:
        """评论指定说说，优先使用 user JSON 路径，失败后回退 H5 表单路径。"""
        ctx = await self.session.get_ctx()
        qzreferrer = f"https://user.qzone.qq.com/{post.uin}/main"

        raw_user = await self.request(
            "POST",
            self.COMMENT_URL,
            params={"g_tk": ctx.gtk2},
            data={
                "hostUin": post.uin,
                "topicId": f"{post.uin}_{post.tid}",
                "content": content,
                "format": "json",
                "qzreferrer": qzreferrer,
            },
        )
        # QZone 部分接口成功时返回 ret=0 而非 code=0，统一兼容，
        # 避免 user 路径实际成功却误判失败 → 走 H5 再发一条 → 重复评论
        user_resp = ApiResponse.from_raw(raw_user)
        if user_resp.ok or raw_user.get("ret") == 0:
            if not user_resp.ok:
                user_resp = ApiResponse(
                    ok=True, code=0, message=None,
                    data=dict(raw_user), raw=raw_user,
                )
            logger.info(
                "QZone评论接口成功: route=user post=%s code=%s",
                post.tid,
                user_resp.code,
            )
            return user_resp

        logger.warning(
            "QZone评论 user 路径失败，回退 H5: post=%s code=%s message=%s raw=%s",
            post.tid,
            user_resp.code,
            user_resp.message,
            str(raw_user)[:500],
        )
        raw_h5 = await self.request(
            "POST",
            self.COMMENT_H5_URL,
            params={"g_tk": ctx.gtk2},
            data={
                "topicId": f"{post.uin}_{post.tid}__1",
                "uin": ctx.uin,
                "hostUin": post.uin,
                "feedsType": 100,
                "inCharset": "utf-8",
                "outCharset": "utf-8",
                "plat": "qzone",
                "source": "ic",
                "isSignIn": "",
                "platformid": 50,
                "format": "fs",
                "ref": "feeds",
                "content": content,
                "richval": "",
                "richtype": "",
                "private": "0",
                "paramstr": "1",
                "qzreferrer": qzreferrer,
            },
            headers={
                "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
                "Referer": qzreferrer,
                "Origin": "https://user.qzone.qq.com",
            },
        )
        h5_resp = ApiResponse.from_raw(raw_h5)
        if h5_resp.ok or raw_h5.get("ret") == 0:
            if not h5_resp.ok:
                h5_resp = ApiResponse(
                    ok=True, code=0, message=None,
                    data=dict(raw_h5), raw=raw_h5,
                )
            logger.info(
                "QZone评论接口成功: route=h5 post=%s code=%s",
                post.tid,
                h5_resp.code,
            )
            return h5_resp

        logger.warning(
            "QZone评论两条路径均失败: post=%s user=%s/%s h5=%s/%s raw=%s",
            post.tid,
            user_resp.code,
            user_resp.message,
            h5_resp.code,
            h5_resp.message,
            str(raw_h5)[:500],
        )
        return ApiResponse(
            ok=False,
            code=h5_resp.code,
            message=(
                f"user: {user_resp.message or user_resp.code}; "
                f"h5: {h5_resp.message or h5_resp.code}"
            ),
            data={},
            raw={"user": raw_user, "h5": raw_h5},
        )

    async def reply(
        self,
        post: Post,
        comment: Comment,
        content: str,
        root_comment: Comment | None = None,
    ) -> ApiResponse:
        """回复评论；API 锚定主评论，楼中回复目标写入 QQ 原生关系标记。"""
        ctx = await self.session.get_ctx()
        root = root_comment or comment
        if comment.parent_tid is not None and root.tid != comment.parent_tid:
            raise ValueError(
                f"楼中回复所属主评论不匹配: target={comment.tid} "
                f"parent={comment.parent_tid} root={root.tid}"
            )

        reply_content = content
        if comment.parent_tid is not None:
            reply_content = f"@{{uin:{comment.uin},nick:{comment.nickname},who:1,auto:1}}{content}"

        logger.info(
            "QZone回复参数: post=%s target_comment=%s root_comment=%s "
            "root_uin=%s target_uin=%s native_target=%s",
            post.tid,
            comment.tid,
            root.tid,
            root.uin,
            comment.uin,
            comment.parent_tid is not None,
        )
        raw = await self.request(
            "POST",
            self.REPLY_URL,
            params={
                "g_tk": ctx.gtk2,
            },
            data={
                "topicId": f"{post.uin}_{post.tid}__1",
                "uin": ctx.uin,
                "hostUin": post.uin,
                "feedsType": 100,
                "inCharset": "utf-8",
                "outCharset": "utf-8",
                "plat": "qzone",
                "source": "ic",
                "platformid": 52,
                "format": "fs",
                "ref": "feeds",
                "content": reply_content,
                "commentId": root.tid,
                "commentUin": root.uin,
                "richval": "",
                "richtype": "",
                "private": "0",
                "paramstr": "2",
                "qzreferrer": f"https://user.qzone.qq.com/{ctx.uin}/main",
            },
            headers={
                "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
                "Sec-Fetch-Dest": "empty",
                "Sec-Fetch-Mode": "cors",
                "Sec-Fetch-Site": "same-site",
                "TE": "trailers",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36",
                "Referer": "https://user.qzone.qq.com/",
                "Origin": "https://user.qzone.qq.com",
            },
        )
        resp = ApiResponse.from_raw(raw)
        if not resp.ok:
            logger.warning(
                "QZone回复失败原始响应: code=%s message=%s raw=%s",
                resp.code,
                resp.message,
                str(raw)[:1000],
            )
        return resp

    async def delete(self, tid: str) -> ApiResponse:
        """删除指定说说"""
        ctx = await self.session.get_ctx()
        raw = await self.request(
            "POST",
            self.DELETE_URL,
            params={"g_tk": ctx.gtk2},
            data={
                "uin": ctx.uin,
                "topicId": f"{ctx.uin}_{tid}__1",
                "feedsType": 0,
                "feedsFlag": 0,
                "feedsKey": tid,
                "feedsAppid": 311,
                "feedsTime": int(time.time()),
                "fupdate": 1,
                "ref": "feeds",
                "qzreferrer": (
                    "https://user.qzone.qq.com/"
                    f"proxy/domain/ic2.qzone.qq.com/cgi-bin/feeds/"
                    f"feeds_html_module?g_iframeUser=1&i_uin={ctx.uin}&i_login_uin={ctx.uin}"
                    "&mode=4&previewV8=1&style=35&version=8"
                    "&needDelOpr=true"
                ),
            },
        )
        return ApiResponse.from_raw(raw)

    async def delete_comment(
        self,
        uin: str,
        tid: str,
        comment_id: str,
        comment_uin: str = "",
    ) -> ApiResponse:
        """删除指定评论（主评论或楼中回复）。

        唯一路径：h5 代理域 emotion_cgi_delcomment_ugc（实测成功）。
        uin/tid: 说说的作者 QQ 与说说 ID（topicId = uin_tid）
        comment_id: 评论 ID（短楼层号 1/2/3 即可，网页端同款，无需真实长 ID）
        comment_uin: 评论作者 QQ（仅用于 tool 层反查定位，本接口请求不携带）
        支持场景：删自己空间的评论、删自己发在别人说说下的评论/回复。
        """
        ctx = await self.session.get_ctx()
        topic_id = f"{uin}_{tid}"
        qzreferrer = f"https://user.qzone.qq.com/{ctx.uin}/main"
        errors: list[str] = []

        def _ok(raw: dict) -> bool:
            """成功判定兼容顶层与嵌套 data 里的 ret/code。

            注意：不能用 `val or -1` 这类写法——code=0 时 0 是 falsy，
            会被 `or` 吞掉变成 -1，导致"实际成功（code=0）被判失败"。
            必须用 None 判断。
            """
            for container in (raw, raw.get("data")):
                if not isinstance(container, dict):
                    continue
                for key in ("ret", "code"):
                    val = container.get(key)
                    if val is None:
                        continue
                    try:
                        if int(val) == 0:
                            return True
                    except (TypeError, ValueError):
                        continue
            return False

        def _msg(raw: dict) -> str:
            for container in (raw, raw.get("data")):
                if not isinstance(container, dict):
                    continue
                for key in ("msg", "message"):
                    if container.get(key):
                        return str(container[key])
            return ""

        # 唯一路径：h5 代理域 emotion_cgi_delcomment_ugc（实测成功，见更新记录 v1.4.4）。
        # user.qzone.qq.com 域 / sns / mobile 在 NapCat 环境全部不可用（-3/空响应），已移除；
        # 若将来换环境需要恢复，参考更新记录 v1.4.4 的历史实现。
        # v1 为实测版参数（topicId+commentId+format=fs，网页端同款，短楼层号可直接删）；
        # v0 仅作同域参数兜底。
        variants = (
            {
                "uin": ctx.uin,
                "hostUin": uin,
                "topicId": topic_id,
                "commentId": comment_id,
                "inCharset": "utf-8",
                "outCharset": "utf-8",
                "ref": "",
                "hostuin": ctx.uin,
                "code_version": "1",
                "format": "fs",
                "qzreferrer": f"https://user.qzone.qq.com/{ctx.uin}",
            },
            {
                "hostuin": ctx.uin,
                "uin": uin,
                "tid": tid,
                "comment_id": comment_id,
                "format": "json",
                "qzreferrer": qzreferrer,
            },
        )
        for idx, ugc_data in enumerate(variants):
            try:
                raw_ugc = await self.request(
                    "POST",
                    self.DELETE_COMMENT_H5_URL,
                    params={"g_tk": ctx.gtk2},
                    data=ugc_data,
                    headers={
                        "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
                        "Referer": "https://h5.qzone.qq.com/",
                        "Origin": "https://h5.qzone.qq.com",
                        "Accept": "*/*",
                    },
                    empty_retry_limit=1,
                )
                if _ok(raw_ugc):
                    logger.info(
                        "QZone删除评论成功: route=h5 variant=%s post=%s comment=%s",
                        idx, tid, comment_id,
                    )
                    return ApiResponse(
                        ok=True, code=0, message=None,
                        data=dict(raw_ugc), raw=raw_ugc,
                    )
                errors.append(
                    f"h5[v{idx}]: code={raw_ugc.get('code')} ret={raw_ugc.get('ret')} msg={_msg(raw_ugc)}"
                )
            except Exception as e:
                errors.append(f"h5[v{idx}]: {e}")

        logger.warning(f"删除评论失败（h5 域两次参数变体均失败）: {' | '.join(errors)}")
        return ApiResponse(
            ok=False,
            code=-1,
            message="; ".join(errors) or "删除评论失败",
            data={},
            raw={},
        )

    async def get_feeds(
        self,
        target_id: str,
        *,
        pos: int = 0,
        num: int = 1,
    ) -> ApiResponse:
        """
        获取指定QQ号的好友说说列表
        """
        ctx = await self.session.get_ctx()
        raw = await self.request(
            "GET",
            self.LIST_URL,
            params={
                "g_tk": ctx.gtk2,
                "uin": target_id,
                "ftype": 0,
                "sort": 0,
                "pos": pos,
                "num": num,
                "replynum": 100,
                "callback": "_preloadCallback",
                "code_version": 1,
                "format": "json",
                "need_comment": 1,
                "need_private_comment": 1,
            },
        )
        return ApiResponse.from_raw(raw)

    async def get_detail(self, post: Post) -> ApiResponse:
        """
        获取单条说说详情（含完整评论、转发、图片、视频等）。

        路由顺序（按实机可用性）：
        1. h5 msgdetail_v6（当前环境实测可用，评论 tid 为帖内短楼层号）
        2. PC emotion_cgi_getdetailv6（评论含真实 commentid，供删除/回复定位；多数环境不可用）
        3. mobile detail（多数环境不可用）
        PC/mobile 仅作快速兜底：空响应只试 1 次，不累进重试，避免白等。
        """
        ctx = await self.session.get_ctx()
        errors: list[str] = []

        # 方法1: h5 msgdetail_v6（当前环境唯一实测可行的详情接口）
        try:
            raw1 = await self.request(
                "GET",
                self.DETAIL_URL,
                params={
                    "uin": post.uin,
                    "tid": post.tid,
                    "format": "jsonp",
                    "g_tk": ctx.gtk2,
                },
            )
            if raw1.get("code") == 0 or raw1.get("msglist") or raw1.get("data"):
                logger.info(f"QZone详情成功: route=h5 post={post.tid}")
                return ApiResponse.from_raw(raw1)
            errors.append(f"h5: code={raw1.get('code')} msg={raw1.get('msg') or raw1.get('message')}")
        except Exception as e:
            errors.append(f"h5: {e}")

        # 方法2: PC getdetailv6（快速兜底，空响应只试 1 次）
        try:
            raw2 = await self.request(
                "POST",
                self.DETAIL_PC_URL,
                params={"g_tk": ctx.gtk2},
                data={
                    "uin": post.uin,
                    "tid": post.tid,
                    "format": "json",
                    "hostuin": ctx.uin,
                    "qzreferrer": f"https://user.qzone.qq.com/{ctx.uin}/main",
                },
                headers={
                    "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
                    "Referer": f"https://user.qzone.qq.com/{post.uin}",
                    "Origin": "https://user.qzone.qq.com",
                },
                empty_retry_limit=1,
            )
            if raw2.get("code") == 0 or raw2.get("ret") == 0 or raw2.get("msglist") or raw2.get("data"):
                logger.info(f"QZone详情成功: route=pc post={post.tid}")
                return ApiResponse.from_raw(raw2)
            errors.append(f"pc: code={raw2.get('code')} msg={raw2.get('msg') or raw2.get('message')}")
        except Exception as e:
            errors.append(f"pc: {e}")

        # 方法3: mobile detail（快速兜底，空响应只试 1 次）
        try:
            raw3 = await self.request(
                "GET",
                self.DETAIL_MOBILE_URL,
                params={
                    "g_tk": ctx.gtk2,
                    "uin": post.uin,
                    "cellid": post.tid,
                    "format": "json",
                },
                headers={
                    "User-Agent": _MOBILE_UA,
                    "Referer": "https://mobile.qzone.qq.com",
                    "Accept": "application/json, text/plain, */*",
                    "X-Requested-With": "XMLHttpRequest",
                },
                empty_retry_limit=1,
            )
            if raw3.get("code") == 0 or raw3.get("data"):
                logger.info(f"QZone详情成功: route=mobile post={post.tid}")
                return ApiResponse.from_raw(raw3)
            errors.append(f"mobile: code={raw3.get('code')} msg={raw3.get('msg') or raw3.get('message')}")
        except Exception as e:
            errors.append(f"mobile: {e}")

        logger.warning(f"QZone详情全部失败: {' | '.join(errors)}")
        return ApiResponse(
            ok=False,
            code=-1,
            message="; ".join(errors) or "获取详情失败",
            data={},
            raw={},
        )

    async def get_recent_feeds(self, page: int = 1) -> ApiResponse:
        """
        获取自己的好友说说列表，返回已读与未读的说说列表
        """
        ctx = await self.session.get_ctx()
        raw = await self.request(
            "GET",
            self.ZONE_LIST_URL,
            params={
                "uin": ctx.uin,
                "scope": 0,
                "view": 1,
                "filter": "all",
                "flag": 1,
                "applist": "all",
                "pagenum": page,
                "aisortEndTime": 0,
                "aisortOffset": 0,
                "aisortBeginTime": 0,
                "begintime": 0,
                "format": "json",
                "g_tk": ctx.gtk2,
                "useutf8": 1,
                "outputhtmlfeed": 1,
            },
        )
        return ApiResponse.from_raw(raw)