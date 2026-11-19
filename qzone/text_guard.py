"""模型输出文本护栏（v1.4.13）。

问题
----
部分模型（实测 deepseek-v4.1-flash）会把框架的**回复格式**或**工具参数的伪 XML**
写进 `qzone_publish(text=...)`，例如：

    正文</text>
    <image_indices" string="false">[1, 2, 3]

结果这些标签被原样发到了说说正文里（用户可见的"漏字"）。

设计原则
--------
1. **只认协议标签**：`msg` / `text` / `image_indices` / `images` / `want_images` /
   `qzone_publish`。其它尖括号（`<3`、`1 < 2 > 0`、代码片段）一律不碰。
2. **先看信号再动手**：必须出现闭合标签、`<msg>`、工具参数标签、行首 `<text>`
   或带 `string=` 属性的 `<text>` 等**明确信号**才进入清理；普通文本连正则都不跑。
3. **能救则救**：误写在正文里的 `image_indices` / `images` 会被解析出来；调用方
   没有显式传参时补上，避免"标签被删了、图也没了"的功能损失。
4. **幂等**：清理过的文本再清理一次，结果不变。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

# 只处理这些"框架/本插件协议标签"，其余尖括号内容原样保留。
_TAG_NAMES = ("msg", "text", "image_indices", "images", "want_images", "qzone_publish")
_TAG_ALT = "|".join(_TAG_NAMES)

# 成对标签：内容保留（把 <text>正文</text> 变成 正文）。
_MSG_PAIR = re.compile(r"<\s*msg\b[^<>]*>(.*?)<\s*/\s*msg\s*>", re.I | re.S)
_TEXT_PAIR = re.compile(r"<\s*text\b[^<>]*>(.*?)<\s*/\s*text\s*>", re.I | re.S)

# 自闭合、闭合、开标签、行尾未闭合的开标签。
_SELF_CLOSE = re.compile(rf"<\s*(?:{_TAG_ALT})\b[^<>]*?/\s*>", re.I)
# 闭合标签也容忍模型乱塞的属性（如 </text string="false">），
# 否则这类畸形闭合标签会原样漏进正文。
_CLOSE = re.compile(rf"<\s*/\s*(?:{_TAG_ALT})\b[^<>]*>", re.I)
_OPEN = re.compile(rf"<\s*(?:{_TAG_ALT})\b[^<>]*>", re.I)
_OPEN_DANGLING = re.compile(rf"<\s*(?:{_TAG_ALT})\b[^<>\n]*$", re.I | re.M)
# 行尾被截断的闭合标签（如模型输出到 "</text" 就停了）
_CLOSE_DANGLING = re.compile(rf"<\s*/\s*(?:{_TAG_ALT})\b[^<>\n]*$", re.I | re.M)
_ANY_TAG = re.compile(rf"<\s*/?\s*(?:{_TAG_ALT})\b", re.I)

# "这像是协议泄漏"的信号。刻意保守：宁可漏清一次，也不误伤正常文案。
_SIGNAL = re.compile(
    r"""<\s*/\s*(?:msg|text)\b                        # 闭合标签（含属性/截断）
      | <\s*msg\b                                     # <msg> / <msg/>
      | <\s*(?:image_indices|images|want_images|qzone_publish)\b   # 工具参数标签
      | ^[ \t]*<\s*text\b                             # 行首 <text>
      | <\s*text\b[^<>]*\bstring\s*=                  # 带类型属性的 <text string="false">
    """,
    re.I | re.X | re.M,
)

# 参数值形态（用于"未闭合标签"的尾部收尾，只吃掉长得像值的部分）。
_INDEX_VALUE = r"\[[^\[\]\n]*\]|[\d][\d\s,，]*"
_IMAGES_VALUE = (
    r"\[[^\[\]\n]*\]"
    r"|(?:https?://|data/|/)[^\s<>\"']+"
    r"(?:[\s,，]+(?:https?://|data/|/)[^\s<>\"']+)*"
)
_BOOL_VALUE = r"true|false|yes|no|on|off|1|0"
_VALUE_PATTERNS = {
    "image_indices": _INDEX_VALUE,
    "images": _IMAGES_VALUE,
    "want_images": _BOOL_VALUE,
}

# 线性预检：没有对应闭合标签时直接跳过"成对匹配"。
# 否则 N 个孤立开标签会让非贪婪匹配各自扫到结尾（O(N²)），
# 3000 个 <image_indices> 实测可拖到 800ms+。
_HAS_CLOSE = {
    name: re.compile(rf"<\s*/\s*{name}\b[^<>]*>", re.I)
    for name in ("msg", "text", *_VALUE_PATTERNS)
}
_CLOSED_RES = {
    name: re.compile(rf"<\s*{name}\b[^<>]*>(.*?)<\s*/\s*{name}\s*>", re.I | re.S)
    for name in _VALUE_PATTERNS
}
# 未闭合的尾巴：先看"标签同一行就是一个值"，再看"整段只剩一个值"。
_TAIL_LINE_RES = {
    name: re.compile(
        rf"<\s*{name}\b[^<>]*>[ \t]*({_VALUE_PATTERNS[name]})[ \t]*(?=\n|$)", re.I
    )
    for name in _VALUE_PATTERNS
}
_TAIL_RES = {
    name: re.compile(rf"<\s*{name}\b[^<>]*>([\s\S]*)$", re.I)
    for name in _VALUE_PATTERNS
}

_URL_PREFIXES = ("http://", "https://", "data/", "/", "data\\")


@dataclass
class GuardedText:
    """清理结果：正文 + 从正文里捡回来的参数。"""

    text: str
    changed: bool = False
    image_indices: list[int] = field(default_factory=list)
    images: list[str] = field(default_factory=list)
    want_images: bool | None = None
    reasons: list[str] = field(default_factory=list)


def sanitize_text(value: Any, *, recover_params: bool = True) -> GuardedText:
    """清理模型输出里的协议标签，并尽量把误写的参数捡回来。

    对普通文本（不含协议信号）是**完全 no-op**，只做首尾空白整理。
    """
    original = _coerce_text(value)
    reasons: list[str] = []
    indices: list[int] = []
    images: list[str] = []
    want_images: bool | None = None

    text = original.replace("\ufeff", "").replace("\u200b", "")

    # 1) 整个参数就是一个 JSON 对象（模型把工具调用 JSON 塞进了 text）
    obj = _unwrap_json_object(text)
    if obj is not None:
        inner = obj.get("text")
        if not isinstance(inner, str):
            inner = obj.get("content")
        if isinstance(inner, str):
            text = inner
            reasons.append("json")
            if recover_params:
                indices.extend(_indices_from_any(obj.get("image_indices")))
                images.extend(_images_from_any(obj.get("images")))
                flag = _bool_from_any(obj.get("want_images"))
                if flag is not None:
                    want_images = flag

    # 2) 没有协议信号 → 原样返回（只整理空白）
    if not _SIGNAL.search(text):
        # 没有协议信号：只做首尾整理，绝不改内部空白（避免误伤排版）。
        cleaned = _trim(text)
        return GuardedText(
            text=cleaned,
            # 普通文本只做了首尾空白整理，不算"清理协议标签"——
            # changed 只表示真的动过协议内容（JSON 解包等），避免误导日志。
            changed=bool(reasons),
            image_indices=_unique_indices(indices),
            images=_unique(images),
            want_images=want_images,
            reasons=reasons,
        )

    # 3) 先把误写的参数块摘出来（无论是否 recover，标签本身都要摘掉）
    for name in ("image_indices", "images", "want_images"):
        text, values = _extract_param(text, name)
        if not values:
            continue
        reasons.append(name)
        if not recover_params:
            continue
        for value in values:
            if name == "image_indices":
                indices.extend(_parse_indices(value))
            elif name == "images":
                images.extend(_parse_images(value))
            elif want_images is None:
                flag = _parse_bool(value)
                if flag is not None:
                    want_images = flag

    # 4) 成对标签就地解包（<msg><text>正文</text></msg> → 正文），嵌套也吃干净
    unwrapped = False
    if _HAS_CLOSE["msg"].search(text) or _HAS_CLOSE["text"].search(text):
        for _ in range(3):
            before = text
            text = _MSG_PAIR.sub(lambda m: m.group(1), text)
            text = _TEXT_PAIR.sub(lambda m: m.group(1), text)
            if text == before:
                break
            unwrapped = True
    if unwrapped:
        reasons.append("unwrap")

    # 5) 清掉残留的孤立标签（逐行处理：只由标签构成的行整行丢弃，
    #    避免清完标签后留下"空行"；原本就有的空行不受影响）
    text = _strip_tags_by_line(text)

    cleaned = _tidy(text)
    return GuardedText(
        text=cleaned,
        changed=(cleaned != original),
        image_indices=_unique_indices(indices),
        images=_unique(images),
        want_images=want_images,
        reasons=reasons,
    )


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------
def _coerce_text(value: Any) -> str:
    """把模型可能传出的任意形态（None/list/dict/数字）安全转成字符串。"""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, list):
        return "\n".join(_coerce_text(item) for item in value if item is not None)
    if isinstance(value, dict):
        for key in ("text", "content", "body", "message"):
            if isinstance(value.get(key), str):
                return value[key]
        try:
            return json.dumps(value, ensure_ascii=False)
        except (TypeError, ValueError):
            return str(value)
    return str(value)


def _unwrap_json_object(text: str) -> dict | None:
    """整段是 {"text": "...", "image_indices": [...]} 形态时解包。"""
    stripped = text.strip()
    if not (stripped.startswith("{") and stripped.endswith("}")):
        return None
    obj = None
    try:
        obj = json.loads(stripped)
    except (TypeError, ValueError):
        try:
            import json5  # 插件已有依赖，兼容模型写的单引号/无引号键
            obj = json5.loads(stripped)
        except Exception:
            return None
    if not isinstance(obj, dict):
        return None
    if isinstance(obj.get("text"), str) or isinstance(obj.get("content"), str):
        return obj
    return None


def _extract_param(text: str, name: str) -> tuple[str, list[str]]:
    """摘掉 <name ...>value</name> 或未闭合的 <name ...>value，返回值与剩余文本。"""
    values: list[str] = []

    def take_closed(match: re.Match) -> str:
        values.append(match.group(1))
        return " "

    if _HAS_CLOSE[name].search(text):
        text = _CLOSED_RES[name].sub(take_closed, text)

    def take_tail_line(match: re.Match) -> str:
        values.append(match.group(1))
        return " "

    # 标签与值在同一行（最常见的截断形态）：只吃这一行的值，后面的正文保留。
    text = _TAIL_LINE_RES[name].sub(take_tail_line, text)

    def take_tail(match: re.Match) -> str:
        body = match.group(1)
        if _value_only(name, body):
            values.append(body)
            return " "
        return match.group(0)

    text = _TAIL_RES[name].sub(take_tail, text)
    return text, values


def _value_only(name: str, body: str) -> bool:
    """剩余整段是否"只是一个参数值"（防止把后面的正文一起吞掉）。"""
    body = body.strip()
    if not body:
        return False
    if name == "image_indices":
        return bool(re.fullmatch(r"\[[^\[\]]*\]|[\d\s,，]+", body))
    if name == "want_images":
        return bool(re.fullmatch(_BOOL_VALUE, body, re.I))
    # images：把候选拆开后必须每一项都像图片地址（或 JSON 数组）
    if body.startswith("["):
        return True
    parts = [part for part in re.split(r"[\s,，]+", body) if part]
    return bool(parts) and all(part.startswith(_URL_PREFIXES) for part in parts)


def _parse_indices(value: Any) -> list[int]:
    raw = _coerce_text(value)
    if not re.fullmatch(r"\s*(\[[^\[\]]*\]|[\d\s,，]+)\s*", raw):
        return []
    return _unique_indices(int(num) for num in re.findall(r"\d+", raw))


def _indices_from_any(value: Any) -> list[int]:
    if isinstance(value, (list, tuple)):
        out: list[int] = []
        for item in value:
            try:
                out.append(int(item))
            except (TypeError, ValueError):
                continue
        return _unique_indices(out)
    return _parse_indices(value)


def _parse_images(value: Any) -> list[str]:
    raw = _coerce_text(value).strip()
    if not raw:
        return []
    if raw.startswith("["):
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            try:
                import json5
                data = json5.loads(raw)
            except Exception:
                data = None
        if isinstance(data, list):
            return _unique(str(item) for item in data if isinstance(item, str))
        return []
    return _unique(part for part in re.split(r"[\s,，]+", raw) if part.startswith(_URL_PREFIXES))


def _images_from_any(value: Any) -> list[str]:
    if isinstance(value, (list, tuple)):
        return _unique(str(item) for item in value if isinstance(item, str))
    return _parse_images(value)


def _parse_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    text = str(value or "").strip().lower()
    if text in ("true", "1", "yes", "on"):
        return True
    if text in ("false", "0", "no", "off"):
        return False
    return None


def _bool_from_any(value: Any) -> bool | None:
    return _parse_bool(value) if value is not None else None


def _unique(values) -> list:
    out = []
    for value in values:
        if value and value not in out:
            out.append(value)
    return out


def _unique_indices(values) -> list[int]:
    out: list[int] = []
    for value in values:
        if 1 <= value <= 999 and value not in out:
            out.append(value)
    return out


def _strip_tags_by_line(text: str) -> str:
    """逐行剥离协议标签；整行只剩标签时直接丢掉该行。"""
    out: list[str] = []
    for line in text.split("\n"):
        if not _ANY_TAG.search(line):
            out.append(line)
            continue
        cleaned = _SELF_CLOSE.sub(" ", line)
        cleaned = _CLOSE.sub(" ", cleaned)
        cleaned = _CLOSE_DANGLING.sub(" ", cleaned)
        cleaned = _OPEN.sub(" ", cleaned)
        cleaned = _OPEN_DANGLING.sub(" ", cleaned)
        if cleaned.strip():
            out.append(cleaned)
    return "\n".join(out)


def _trim(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n").strip()


def _tidy(text: str) -> str:
    """整理空白：不动单换行，只清行尾空格、重复空格与三连空行。"""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"^[ \t]+", "", text, flags=re.M)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    # 只在本函数（协议清理路径）里合并空行：清掉标签行后不留空洞。
    text = re.sub(r"\n[ \t]*\n+", "\n", text)
    return text.strip()
