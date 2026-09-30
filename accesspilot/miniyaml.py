"""最小 YAML 子集解析/生成器 (零依赖).

为什么不用 PyYAML:
  * 本工具需要在"没有 pip / 没有外网 PyPI"的机器上开箱可用;
  * 我们只处理代理配置这类结构简单、由缩进构成的 YAML。

支持: 嵌套映射、序列(含映射序列)、行内 [] 与 {}、引号字符串、注释、
布尔/null/数字标量。不支持: 锚点、多行块标量、复杂键、文档分隔流。
"""
from __future__ import annotations

import re
from typing import Any

__all__ = ["load", "dump", "YamlError"]


class YamlError(ValueError):
    pass


# --------------------------------------------------------------------------- #
# 标量
# --------------------------------------------------------------------------- #

_NUM_RE = re.compile(r"^[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?$")


#: 双引号标量里的简单转义(YAML 1.2 的 c-ns-esc-char)。
_SIMPLE_UNESCAPE = {
    "0": "\0", "a": "\a", "b": "\b", "t": "\t", "n": "\n",
    "v": "\v", "f": "\f", "r": "\r", "e": "\x1b", " ": " ",
    '"': '"', "/": "/", "\\": "\\",
    "N": "\x85", "_": "\xa0", "L": "\u2028", "P": "\u2029",
}

_HEX = set("0123456789abcdefABCDEF")


def _unescape_double(body: str) -> str:
    """按 YAML 双引号规则反转义, **单趟扫描**.

    不能像以前那样链式 replace —— 顺序本身就会出错:
    `\\\\n`(反斜杠 + 字面 n)会被先行的 `.replace("\\\\n", "\\n")` 吃成
    "反斜杠 + 换行", 语义直接反了。
    """
    out: list[str] = []
    i = 0
    n = len(body)
    while i < n:
        ch = body[i]
        if ch != "\\" or i + 1 >= n:
            out.append(ch)
            i += 1
            continue
        e = body[i + 1]
        if e in ("x", "u", "U"):
            width = {"x": 2, "u": 4, "U": 8}[e]
            digits = body[i + 2 : i + 2 + width]
            if len(digits) == width and all(c in _HEX for c in digits):
                out.append(chr(int(digits, 16)))
                i += 2 + width
                continue
            out.append(ch)  # 残缺的转义: 原样保留, 不吞字符
            i += 1
            continue
        rep = _SIMPLE_UNESCAPE.get(e)
        if rep is None:
            out.append(ch)  # 未知转义同样保留
            i += 1
            continue
        out.append(rep)
        i += 2
    return "".join(out)


def _unquote(s: str) -> str:
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
        body = s[1:-1]
        if s[0] == "'":
            return body.replace("''", "'")
        return _unescape_double(body)
    return s


def _scalar(tok: str) -> Any:
    t = tok.strip()
    if t == "" or t in ("~", "null", "Null", "NULL"):
        return None
    low = t.lower()
    if low in ("true", "yes", "on"):
        return True
    if low in ("false", "no", "off"):
        return False
    if t[0] in ("'", '"'):
        return _unquote(t)
    if t.startswith("[") and t.endswith("]"):
        inner = t[1:-1].strip()
        if not inner:
            return []
        return [_scalar(x) for x in _split_flow(inner)]
    if t.startswith("{") and t.endswith("}"):
        inner = t[1:-1].strip()
        if not inner:
            return {}
        out: dict[str, Any] = {}
        for part in _split_flow(inner):
            k, sep, v = _split_key(part)
            if not sep:
                raise YamlError(f"行内映射缺少 ':' -> {part!r}")
            out[str(_scalar(k))] = _scalar(v)
        return out
    if _NUM_RE.match(t):
        try:
            return int(t)
        except ValueError:
            return float(t)
    return t


def _split_flow(text: str) -> list[str]:
    """按逗号切分行内集合, 尊重引号与嵌套括号."""
    parts: list[str] = []
    depth = 0
    q: str | None = None
    cur = ""
    i = 0
    while i < len(text):
        ch = text[i]
        if q:
            cur += ch
            if ch == "\\" and q == '"' and i + 1 < len(text):
                cur += text[i + 1]
                i += 2
                continue
            if ch == q:
                q = None
        elif ch in ("'", '"'):
            q = ch
            cur += ch
        elif ch in "[{":
            depth += 1
            cur += ch
        elif ch in "]}":
            depth -= 1
            cur += ch
        elif ch == "," and depth == 0:
            parts.append(cur.strip())
            cur = ""
        else:
            cur += ch
        i += 1
    if cur.strip():
        parts.append(cur.strip())
    return parts


def _strip_comment(line: str) -> str:
    out: list[str] = []
    q: str | None = None
    i = 0
    while i < len(line):
        ch = line[i]
        if q:
            out.append(ch)
            if ch == "\\" and q == '"' and i + 1 < len(line):
                out.append(line[i + 1])
                i += 2
                continue
            if ch == q:
                q = None
            i += 1
            continue
        if ch in ("'", '"'):
            q = ch
            out.append(ch)
            i += 1
            continue
        if ch == "#" and (not out or out[-1] in (" ", "\t")):
            break
        out.append(ch)
        i += 1
    return "".join(out).rstrip()


def _split_key(text: str) -> tuple[str, bool, str]:
    """切分 `key: value`, 返回 (key, 是否找到分隔符, value)."""
    q: str | None = None
    depth = 0
    i = 0
    while i < len(text):
        ch = text[i]
        if q:
            if ch == "\\" and q == '"':
                i += 2
                continue
            if ch == q:
                q = None
        elif ch in ("'", '"'):
            q = ch
        elif ch in "[{":
            depth += 1
        elif ch in "]}":
            depth -= 1
        elif ch == ":" and depth == 0:
            if i + 1 >= len(text) or text[i + 1] in (" ", "\t"):
                return text[:i].strip(), True, text[i + 1 :].strip()
        i += 1
    return text.strip(), False, ""


# --------------------------------------------------------------------------- #
# 解析
# --------------------------------------------------------------------------- #


def _tokenize(text: str) -> list[tuple[int, str]]:
    rows: list[tuple[int, str]] = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw.strip() == "" or raw.lstrip().startswith("#"):
            continue
        if "\t" in raw[: len(raw) - len(raw.lstrip())]:
            raw = raw.replace("\t", "  ")
        content = _strip_comment(raw)
        if not content.strip():
            continue
        indent = len(content) - len(content.lstrip(" "))
        if content.strip() == "---":
            continue
        rows.append((indent, content.strip()))
    return rows


def _parse(lines: list[tuple[int, str]], i: int, indent: int) -> tuple[Any, int]:
    if i >= len(lines):
        return None, i
    if lines[i][1].startswith("- ") or lines[i][1] == "-":
        seq: list[Any] = []
        while i < len(lines) and lines[i][0] == indent and (
            lines[i][1].startswith("- ") or lines[i][1] == "-"
        ):
            row_indent, content = lines[i]
            rest = content[1:].strip()
            if rest == "":
                i += 1
                if i < len(lines) and lines[i][0] > row_indent:
                    val, i = _parse(lines, i, lines[i][0])
                else:
                    val = None
                seq.append(val)
                continue
            key, found, _ = _split_key(rest)
            if found:
                # 映射序列项: 后续更深缩进的行属于同一个映射
                sub: list[tuple[int, str]] = [(row_indent + 2, rest)]
                i += 1
                while i < len(lines) and lines[i][0] > row_indent:
                    sub.append(lines[i])
                    i += 1
                val, _ = _parse(sub, 0, row_indent + 2)
                seq.append(val)
            else:
                seq.append(_scalar(rest))
                i += 1
        return seq, i

    mapping: dict[str, Any] = {}
    while i < len(lines) and lines[i][0] == indent and not (
        lines[i][1].startswith("- ") or lines[i][1] == "-"
    ):
        _, content = lines[i]
        key, found, value = _split_key(content)
        if not found:
            # 顶层裸标量(罕见), 直接返回
            if not mapping:
                return _scalar(content), i + 1
            raise YamlError(f"无法解析的行: {content!r}")
        key = str(_scalar(key))
        if value == "":
            i += 1
            if i < len(lines) and lines[i][0] > indent:
                value_obj, i = _parse(lines, i, lines[i][0])
            else:
                value_obj = None
        else:
            value_obj = _scalar(value)
            i += 1
        mapping[key] = value_obj
    return mapping, i


def load(text: str) -> Any:
    """解析 YAML 文本为 Python 对象."""
    lines = _tokenize(text)
    if not lines:
        return None
    obj, _ = _parse(lines, 0, lines[0][0])
    return obj


# --------------------------------------------------------------------------- #
# 生成
# --------------------------------------------------------------------------- #

_PLAIN_OK = re.compile(r"^[A-Za-z0-9\u4e00-\u9fff][A-Za-z0-9\u4e00-\u9fff ._/@+()\[\]-]*$")


def _needs_quote(s: str) -> bool:
    if s == "":
        return True
    if s != s.strip():
        return True
    if s.lower() in ("true", "false", "null", "yes", "no", "on", "off", "~"):
        return True
    if _NUM_RE.match(s):
        return True
    if s[0] in "-?:,[]{}#&*!|>'\"%@`":
        return True
    if ": " in s or " #" in s or s.endswith(":"):
        return True
    if "\n" in s or "\t" in s:
        return True
    if _CTRL_RE.search(s):
        return True
    if not _PLAIN_OK.match(s):
        return True
    return False


#: YAML 标量里**禁止出现**的字符。
#:
#: 真实事故(2026-09): 某个免费节点的 `sni` 字段里带了 `ð\x9f\x87` ——
#: 那是 UTF-8 的 emoji 字节被按 Latin-1 解码的产物, 落在 C1 区
#: (U+0080~U+009F)。旧版转义只处理 `\` `"` 和换行, 于是原样写进 YAML,
#: 内核直接报 `yaml: control characters are not allowed`, **整份 6000 个
#: 节点的配置**因此加载失败; free auto 也在校验那一步中断, 连"抓完节点
#: 立刻测速 + 清理"都没跑到, 配置档被留在了 2.6 MB 的中间状态。
#:
#: 范围依据 Go yaml.v3 的 c-printable: C0(除 \t \n \r) + DEL + C1。
_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")

#: 双引号标量支持的简短转义(其余控制字符走 \xNN / \uNNNN)。
_ESCAPES = {
    "\0": "\\0", "\a": "\\a", "\b": "\\b", "\t": "\\t", "\n": "\\n",
    "\v": "\\v", "\f": "\\f", "\r": "\\r", "\x1b": "\\e",
    '"': '\\"', "\\": "\\\\",
}


def _escape_double(s: str) -> str:
    """把字符串转成 YAML 双引号标量安全的形态(控制字符一律转义)."""
    out: list[str] = []
    for ch in s:
        esc = _ESCAPES.get(ch)
        if esc is not None:
            out.append(esc)
            continue
        o = ord(ch)
        if o < 0x20 or 0x7F <= o <= 0x9F:
            out.append(f"\\x{o:02x}")
        elif o in (0xFFFE, 0xFFFF):
            out.append(f"\\u{o:04x}")
        else:
            out.append(ch)
    return "".join(out)


def _emit_scalar(v: Any) -> str:
    if v is None:
        return ""
    if v is True:
        return "true"
    if v is False:
        return "false"
    if isinstance(v, (int, float)):
        return repr(v)
    s = str(v)
    if _needs_quote(s):
        return f'"{_escape_double(s)}"'
    return s


def has_control_chars(s: str) -> bool:
    """字符串里有没有 YAML 禁止的控制字符."""
    return bool(_CTRL_RE.search(s))


def strip_control_chars(s: str) -> str:
    """把 YAML 禁止的控制字符去掉(数据层清洗, 与 _escape_double 互补).

    转义能保证配置**能加载**; 清洗能保证字段值**有意义** ——
    例如一个被 Latin-1 污染的 SNI, 转义后语法合法但握手指令仍然是垃圾。
    """
    return _CTRL_RE.sub("", s)


def _emit(obj: Any, indent: int, out: list[str]) -> None:
    pad = " " * indent
    if isinstance(obj, dict):
        for k, v in obj.items():
            key = _emit_scalar(k)
            if isinstance(v, (dict, list)) and v:
                out.append(f"{pad}{key}:")
                _emit(v, indent + 2, out)
            elif isinstance(v, (dict, list)):
                out.append(f"{pad}{key}: {'{}' if isinstance(v, dict) else '[]'}")
            else:
                out.append(f"{pad}{key}: {_emit_scalar(v)}")
    elif isinstance(obj, list):
        for item in obj:
            if isinstance(item, dict) and item:
                first = True
                for k, v in item.items():
                    key = _emit_scalar(k)
                    prefix = f"{pad}- " if first else f"{pad}  "
                    first = False
                    if isinstance(v, (dict, list)) and v:
                        out.append(f"{prefix}{key}:")
                        _emit(v, indent + 4, out)
                    elif isinstance(v, (dict, list)):
                        out.append(
                            f"{prefix}{key}: {'{}' if isinstance(v, dict) else '[]'}"
                        )
                    else:
                        out.append(f"{prefix}{key}: {_emit_scalar(v)}")
            elif isinstance(item, list):
                out.append(f"{pad}-")
                _emit(item, indent + 2, out)
            else:
                out.append(f"{pad}- {_emit_scalar(item)}")
    else:
        out.append(f"{pad}{_emit_scalar(obj)}")


def dump(obj: Any) -> str:
    """把 Python 对象序列化为 YAML 文本."""
    out: list[str] = []
    _emit(obj, 0, out)
    return "\n".join(out) + "\n"
