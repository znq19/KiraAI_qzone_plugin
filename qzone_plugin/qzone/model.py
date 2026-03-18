# model.py
import datetime as _dt
import re
from datetime import datetime
from typing import Any, Optional, List

import pydantic
from pydantic import BaseModel


def extract_and_replace_nickname(input_string):
    """提取并替换昵称（处理QQ空间消息中的格式）"""
    pattern = r"\{[^{}]*\}"
    def replace_func(match):
        content = match.group(0)
        pairs = content[1:-1].split(",")
        nick_value = ""
        for pair in pairs:
            if ":" not in pair:
                continue
            key, value = pair.split(":", 1)
            if key.strip() == "nick":
                nick_value = value.strip()
                break
        return f"{nick_value} " if nick_value else ""
    return re.sub(pattern, replace_func, input_string)


def remove_em_tags(text):
    """移除 [em]...[/em] 标签"""
    return re.sub(r"\[em\].*?\[/em\]", "", text)


class QzoneContext:
    """统一封装 Qzone 请求所需的所有动态参数"""

    def __init__(self, uin: int, skey: str, p_skey: str):
        self.uin = uin
        self.skey = skey
        self.p_skey = p_skey

    @property
    def gtk2(self) -> str:
        """动态计算 gtk2"""
        hash_val = 5381
        for ch in self.p_skey:
            hash_val += (hash_val << 5) + ord(ch)
        return str(hash_val & 0x7FFFFFFF)

    def cookies(self) -> dict[str, str]:
        return {
            "uin": f"o{self.uin}",
            "skey": self.skey,
            "p_skey": self.p_skey,
        }

    def headers(self) -> dict[str, str]:
        return {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36",
            "referer": f"https://user.qzone.qq.com/{self.uin}",
            "origin": "https://user.qzone.qq.com",
            "Host": "user.qzone.qq.com",
            "Connection": "keep-alive",
        }


class ApiResponse:
    """
    统一接口响应结果
    """

    def __init__(self, ok: bool, code: int, message: str | None, data: dict[str, Any], raw: dict[str, Any]):
        self.ok = ok
        self.code = code
        self.message = message
        self.data = data
        self.raw = raw

    @classmethod
    def from_raw(
        cls,
        raw: dict[str, Any],
        *,
        code_key: str = "code",
        msg_key: str | tuple[str, ...] = ("message", "msg"),
        data_key: str | None = None,
        success_code: int = 0,
    ) -> "ApiResponse":
        # 解析 code
        code = raw.get(code_key, -1)

        # 解析 message
        message = None
        if isinstance(msg_key, tuple):
            for k in msg_key:
                if raw.get(k):
                    message = raw.get(k)
                    break
        else:
            message = raw.get(msg_key) or raw.get("data", {}).get(msg_key) or str(code)

        # 成功
        if code == success_code:
            if data_key is None:
                data = dict(raw)
                # 移除内部元数据
                data.pop("__qzone_internal__", None)
            else:
                data = raw.get(data_key, {})
            return cls(
                ok=True,
                code=code,
                message=None,
                data=data,
                raw=raw,
            )

        # 失败
        return cls(
            ok=False,
            code=code,
            message=message,
            data={},
            raw=raw,
        )

    def __bool__(self) -> bool:
        return self.ok

    def __repr__(self) -> str:
        if self.ok:
            return f"<ApiResponse ok code={self.code}>"
        return f"<ApiResponse fail code={self.code} message={self.message!r}>"

    def unwrap(self) -> dict[str, Any]:
        if not self.ok:
            raise RuntimeError(f"{self.code}: {self.message}")
        return self.data or {}

    def get(self, key: str, default: Any = None) -> Any:
        if not self.ok or not self.data:
            return default
        return self.data.get(key, default)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "code": self.code,
            "message": self.message,
            "data": self.data,
            "raw": self.raw,
        }


class Comment(BaseModel):
    """QQ 空间单条评论（含主评论与楼中楼）"""

    uin: int
    nickname: str
    content: str
    create_time: int
    create_time_str: str = ""
    tid: int = 0
    parent_tid: Optional[int] = None  # 为 None 表示主评论
    source_name: str = ""
    source_url: str = ""

    @property
    def dt(self) -> _dt.datetime:
        return _dt.datetime.fromtimestamp(self.create_time)

    @property
    def plain_content(self) -> str:
        return remove_em_tags(self.content)

    @staticmethod
    def from_raw(raw: dict, parent_tid: Optional[int] = None) -> "Comment":
        """从原始字典构造 Comment"""
        return Comment(
            uin=int(raw.get("uin") or 0),
            nickname=raw.get("name") or "",
            content=raw.get("content") or "",
            create_time=int(raw.get("create_time") or 0),
            create_time_str=raw.get("createTime2") or "",
            tid=int(raw.get("tid") or 0),
            parent_tid=parent_tid,
            source_name=raw.get("source_name") or "",
            source_url=raw.get("source_url") or "",
        )

    @staticmethod
    def build_list(comment_list: List[dict]) -> List["Comment"]:
        """将 commentlist 整段 flatten 成 List[Comment]"""
        res: List["Comment"] = []
        for main in comment_list:
            main_tid = int(main.get("tid") or 0)
            res.append(Comment.from_raw(main, parent_tid=None))
            for sub in main.get("list_3") or []:
                res.append(Comment.from_raw(sub, parent_tid=main_tid))
        return res

    def __str__(self) -> str:
        flag = "└─↩" if self.parent_tid else "●"
        return f"{flag} {self.nickname}({self.uin}): {self.plain_content}"

    def pretty(self, indent: int = 0) -> str:
        prefix = "  " * indent
        return f"{prefix}{self.nickname}: {self.plain_content}"


class Post(pydantic.BaseModel):
    """稿件/说说"""

    id: Optional[int] = None
    """稿件ID"""
    tid: Optional[str] = None
    """QQ给定的说说ID"""
    uin: int = 0
    """用户ID"""
    name: str = ""
    """用户昵称"""
    gin: int = 0
    """群聊ID"""
    text: str = ""
    """文本内容"""
    images: List[str] = pydantic.Field(default_factory=list)
    """图片URL列表"""
    videos: List[str] = pydantic.Field(default_factory=list)
    """视频URL列表"""
    anon: bool = False
    """是否匿名"""
    status: str = "approved"
    """状态：pending, approved, rejected"""
    create_time: int = pydantic.Field(
        default_factory=lambda: int(datetime.now().timestamp())
    )
    """创建时间戳"""
    rt_con: str = ""
    """转发内容"""
    comments: List[Comment] = pydantic.Field(default_factory=list)
    """评论列表"""
    extra_text: Optional[str] = None
    """额外文本"""

    class Config:
        json_encoders = {Comment: lambda c: c.model_dump()}

    @property
    def show_name(self) -> str:
        if self.anon:
            return "匿名者"
        return extract_and_replace_nickname(self.name)

    def to_str(self) -> str:
        """把稿件信息整理成易读文本"""
        is_pending = self.status == "pending"
        lines = [
            f"### 【{self.id}】{self.name}{'投稿' if is_pending else '发布'}于{datetime.fromtimestamp(self.create_time).strftime('%Y-%m-%d %H:%M')}"
        ]
        if self.text:
            lines.append(f"\n\n{remove_em_tags(self.text)}\n\n")
        if self.rt_con:
            lines.append(f"\n\n[转发]：{remove_em_tags(self.rt_con)}\n\n")
        if self.images:
            images_str = "\n".join(f"  ![图片]({img})" for img in self.images)
            lines.append(images_str)
        if self.videos:
            videos_str = "\n".join(f"  [视频]({vid})" for vid in self.videos)
            lines.append(videos_str)
        if self.comments:
            lines.append("\n\n【评论区】\n")
            for comment in self.comments:
                lines.append(
                    f"- **{remove_em_tags(comment.nickname)}**: {remove_em_tags(extract_and_replace_nickname(comment.content))}"
                )
        if is_pending:
            name = "匿名者" if self.anon else f"{self.name}({self.uin})"
            lines.append(f"\n\n备注：稿件#{self.id}待审核, 投稿来自{name}")

        return "\n".join(lines)

    def update(self, **kwargs):
        """更新 Post 对象的属性"""
        for key, value in kwargs.items():
            if hasattr(self, key):
                setattr(self, key, value)
            else:
                raise AttributeError(f"Post 对象没有属性 {key}")