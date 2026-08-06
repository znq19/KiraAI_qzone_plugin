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
    LIKE_V6_URL = "https://user.qzone.qq.com/proxy/domain/taotao.qzone.qq.com/cgi-bin/like_cgi_likev6"
    MOBILE_LIKE_URL = "https://mobile.qzone.qq.com/like"
    LIST_URL = "https://user.qzone.qq.com/proxy/domain/taotao.qq.com/cgi-bin/emotion_cgi_msglist_v6"
    COMMENT_URL = "https://user.qzone.qq.com/proxy/domain/taotao.qzone.qq.com/cgi-bin/emotion_cgi_re_feeds"
    COMMENT_H5_URL = "https://h5.qzone.qq.com/proxy/domain/taotao.qzone.qq.com/cgi-bin/emotion_cgi_re_feeds"
    ZONE_LIST_URL = "https://user.qzone.qq.com/proxy/domain/ic2.qzone.qq.com/cgi-bin/feeds/feeds3_html_more"
    VISITOR_URL = "https://h5.qzone.qq.com/proxy/domain/g.qzone.qq.com/cgi-bin/friendshow/cgi_get_visitor_more"
    REPLY_URL = "https://h5.qzone.qq.com/proxy/domain/taotao.qzone.qq.com/cgi-bin/emotion_cgi_re_feeds"
    DELETE_URL = "https://h5.qzone.qq.com/proxy/domain/taotao.qzone.qq.com/cgi-bin/emotion_cgi_delete_v6"
    DETAIL_URL = "https://h5.qzone.qq.com/proxy/domain/taotao.qq.com/cgi-bin/emotion_cgi_msgdetail_v6"

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
        点赞指定说说（三级降级，参考 onebot-qzone 实机验证实现）：
        1. internal_dolike_app（w.qzone.qq.com）
        2. like_cgi_likev6（taotao.qzone.qq.com）
        3. mobile.qzone.qq.com/like
        abstime 应为说说的发布时间（不是当前时间），缺失时回退当前时间。
        """
        ctx = await self.session.get_ctx()
        tid = normalize_tid(post.tid)
        abstime = int(abstime or getattr(post, "create_time", 0) or time.time())
        if appid == 311:
            unikey = f"http://user.qzone.qq.com/{post.uin}/mood/{tid}"
        else:
            unikey = f"http://user.qzone.qq.com/{post.uin}/app/{tid}"
        qzreferrer = f"{self.BASE_URL}/{ctx.uin}/main"
        errors: list[str] = []

        # 方法1: internal_dolike_app
        try:
            raw1 = await self.request(
                "POST",
                self.DOLIKE_URL,
                params={"g_tk": ctx.gtk2},
                data={
                    "qzreferrer": qzreferrer,
                    "opuin": ctx.uin,
                    "unikey": unikey,
                    "curkey": unikey,
                    "appid": appid,
                    "typeid": typeid,
                    "fid": tid,
                    "from": 1,
                    "active": 0,
                    "fupdate": 1,
                    "abstime": abstime,
                    "format": "json",
                },
            )
            if raw1.get("ret") == 0 or raw1.get("code") == 0:
                raw1["code"] = 0
                return ApiResponse.from_raw(raw1)
            errors.append(f"dolike: code={raw1.get('code')} msg={raw1.get('message') or raw1.get('msg')}")
        except Exception as e:
            errors.append(f"dolike: {e}")

        # 方法2: like_cgi_likev6
        try:
            raw2 = await self.request(
                "POST",
                self.LIKE_V6_URL,
                params={"g_tk": ctx.gtk2},
                data={
                    "opuin": ctx.uin,
                    "ouin": post.uin,
                    "fid": tid,
                    "abstime": abstime,
                    "appid": appid,
                    "typeid": typeid,
                    "key": "",
                    "format": "json",
                    "qzreferrer": qzreferrer,
                },
            )
            if raw2.get("ret") == 0 or raw2.get("code") == 0:
                raw2["code"] = 0
                return ApiResponse.from_raw(raw2)
            errors.append(f"likev6: code={raw2.get('code')} msg={raw2.get('message') or raw2.get('msg')}")
        except Exception as e:
            errors.append(f"likev6: {e}")

        # 方法3: mobile like（兜底）
        try:
            raw3 = await self.request(
                "POST",
                self.MOBILE_LIKE_URL,
                params={"g_tk": ctx.gtk2},
                data={
                    "unikey": unikey,
                    "curkey": unikey,
                    "appid": appid,
                    "typeid": typeid,
                    "active": 0,
                    "fupdate": 1,
                },
                headers={
                    "User-Agent": _MOBILE_UA,
                    "Referer": "https://mobile.qzone.qq.com",
                    "Accept": "application/json, text/plain, */*",
                    "X-Requested-With": "XMLHttpRequest",
                },
            )
            if raw3.get("ret") == 0 or raw3.get("code") == 0:
                raw3["code"] = 0
                return ApiResponse.from_raw(raw3)
            errors.append(f"mobile: code={raw3.get('code')} msg={raw3.get('message') or raw3.get('msg')}")
        except Exception as e:
            errors.append(f"mobile: {e}")

        logger.warning(f"点赞三级降级全部失败: {' | '.join(errors)}")
        return ApiResponse(
            ok=False,
            code=-1,
            message="; ".join(errors) or "点赞失败",
            data={},
            raw={},
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
        user_resp = ApiResponse.from_raw(raw_user)
        if user_resp.ok:
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
        if h5_resp.ok:
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
        获取单条说说详情（含完整评论、转发、图片、视频等）
        """
        ctx = await self.session.get_ctx()
        raw = await self.request(
            "GET",
            self.DETAIL_URL,
            params={
                "uin": post.uin,
                "tid": post.tid,
                "format": "jsonp",
                "g_tk": ctx.gtk2,
            },
        )
        return ApiResponse.from_raw(raw)

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