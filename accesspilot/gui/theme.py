"""红杏主窗口的视觉层: 配色、字体、ttk 样式, 以及把状态翻成人话的小工具.

为什么颜色和字体要单独一个模块
==============================
主窗口控件多, 一旦色值/字号散落在各处, "把未连接状态改成灰的"这种小需求
就得翻遍 app.py, 而且很容易改漏一处, 出现两种蓝。这里把所有色值和字号收成
常量并集中配置 ttk 样式, app.py 只引用名字, 不写 #RRGGBB。

为什么显式切到 ttk 的 clam 主题
--------------------------------
Windows 上 ttk 默认走 vista 主题, 它把大部分配色交给系统: 背景色、选中色、
表头色配了也不生效。结果是"浅色卡片里嵌一块深色表头"这种不协调, 而且不同
机器上样子还不一样。clam 是纯 Tk 自绘的, 配色可控、跨机器一致, 所以显式切过去。

为什么字体要运行时挑, 而不是写死
--------------------------------
中文变方块是 Tk 在 Windows 上最刺眼的故障: 一旦指定的字体在系统里不存在,
Tk 会退回到一个不含中文字形的字体, 满屏方框。所以这里**先查系统字体列表
再决定**, 挑不到任何中文字体时退回 Tk 默认字体家族(由 Tk 自己挑, 至少有
中文字形), 绝不硬写一个可能不存在的名字。
"""
from __future__ import annotations

import time
import tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk

# --------------------------------------------------------------------------- #
# 配色
# --------------------------------------------------------------------------- #

#: 窗口底色。偏冷的浅灰, 让白色卡片能浮起来, 又不像纯白那样刺眼。
BG = "#eef1f7"
#: 卡片/输入区底色
CARD = "#ffffff"
BORDER = "#d8dee9"
TEXT = "#1c2230"
#: 次要说明文字。用浅一档的灰, 让"解释"不会喧宾夺主。
MUTED = "#6b7484"
#: 品牌蓝。"亮蓝 = 已连接"是整个界面的唯一强视觉约定。
ACCENT = "#2f6bff"
ACCENT_ACTIVE = "#1f55e0"
#: 未连接时的圆钮: 灰而不是红 —— 没开代理不是错误, 不该让用户以为出了问题。
OFF_FACE = "#c6cdda"
OFF_TEXT = "#4b5563"
OK = "#12a150"
WARN = "#c2740a"
DANGER = "#d92d20"
DANGER_BG = "#fdecea"
#: 当前节点那一行的底色
ROW_CURRENT = "#e8f0ff"

# --------------------------------------------------------------------------- #
# 字体
# --------------------------------------------------------------------------- #

#: 按优先级排列的中文字体候选。Microsoft YaHei UI 是 Win10 默认界面字体,
#: 字形和字重都合适; 其余是不同 Windows 版本/精简版系统上的常见替代。
CJK_CANDIDATES = (
    "Microsoft YaHei UI",
    "Microsoft YaHei",
    "Noto Sans SC",
    "Source Han Sans SC",
    "DengXian",
    "SimHei",
    "SimSun",
)
DEFAULT_FAMILY = "Microsoft YaHei UI"

_FAMILY = DEFAULT_FAMILY


def pick_family(root: tk.Misc) -> str:
    """挑一个系统里真实存在的中文字体家族。"""
    try:
        available = set(tkfont.families(root))
    except Exception:  # noqa: PERF203
        return DEFAULT_FAMILY
    for name in CJK_CANDIDATES:
        if name in available:
            return name
    try:
        # 一个候选都没有: 交给 Tk 自己挑默认家族, 它至少保证有中文字形,
        # 比硬用一个不存在的名字(→ 满屏方框)好得多。
        return str(tkfont.nametofont("TkDefaultFont").actual("family"))
    except Exception:  # noqa: PERF203
        return DEFAULT_FAMILY


def family() -> str:
    """当前使用的字体家族(必须在 setup() 之后取)。"""
    return _FAMILY


def font(size: int = 10, weight: str = "normal") -> tuple[str, int, str]:
    """给经典 Tk 控件(Canvas 文字等)用的字体元组。"""
    return (_FAMILY, size, weight)


# --------------------------------------------------------------------------- #
# ttk 样式
# --------------------------------------------------------------------------- #


def setup(root: tk.Misc) -> ttk.Style:
    """把整套 ttk 样式装到 root 上, 返回 Style。必须先于建控件调用。"""
    global _FAMILY
    _FAMILY = pick_family(root)

    style = ttk.Style(root)
    try:
        # clam 才吃我们的配色; vista/xpnative 会把大部分选项忽略掉。
        style.theme_use("clam")
    except Exception:  # noqa: PERF203
        pass

    body = font(10)
    small = font(9)
    bold = font(10, "bold")

    style.configure(".", background=BG, foreground=TEXT, font=body)
    style.configure("App.TFrame", background=BG)
    style.configure("Card.TFrame", background=CARD)
    style.configure("Card.TLabel", background=CARD, foreground=TEXT)
    style.configure("TLabel", background=BG, foreground=TEXT, font=body)
    style.configure("Brand.TLabel", background=BG, foreground=ACCENT,
                    font=font(17, "bold"))
    style.configure("Title.TLabel", background=CARD, foreground=TEXT,
                    font=font(11, "bold"))
    style.configure("Hint.TLabel", background=BG, foreground=MUTED, font=small)
    style.configure("CardHint.TLabel", background=CARD, foreground=MUTED, font=small)
    style.configure("Value.TLabel", background=CARD, foreground=TEXT, font=bold)
    style.configure("Key.TLabel", background=CARD, foreground=MUTED, font=small)
    style.configure("Ok.TLabel", background=CARD, foreground=OK, font=bold)
    style.configure("Warn.TLabel", background=CARD, foreground=WARN, font=bold)
    style.configure("Bad.TLabel", background=CARD, foreground=DANGER, font=bold)
    style.configure("Status.TLabel", background=BG, foreground=MUTED, font=small)
    style.configure("Error.TFrame", background=DANGER_BG)
    style.configure("Error.TLabel", background=DANGER_BG, foreground=DANGER,
                    font=font(10, "bold"))

    # 按钮: 次要动作用描边, 主要动作(连接/选最优)用实心蓝, 一眼能分清主次。
    style.configure("TButton", background=CARD, foreground=TEXT, font=body,
                    bordercolor=BORDER, lightcolor=CARD, darkcolor=CARD,
                    focuscolor=ACCENT, relief="flat", padding=(10, 6))
    style.map("TButton",
              background=[("pressed", "#e6eaf3"), ("active", "#f2f5fb"),
                          ("disabled", "#f2f3f6")],
              foreground=[("disabled", "#a3a9b5")])
    style.configure("Accent.TButton", background=ACCENT, foreground="#ffffff",
                    bordercolor=ACCENT, lightcolor=ACCENT, darkcolor=ACCENT,
                    font=bold, relief="flat", padding=(12, 7))
    style.map("Accent.TButton",
              background=[("pressed", ACCENT_ACTIVE), ("active", ACCENT_ACTIVE),
                          ("disabled", "#a8bdf5")],
              foreground=[("disabled", "#f0f4ff")])
    # 错误条上的"知道了": 做得矮一点。错误条是**额外**插进来的一行, 它每高
    # 一像素都从左侧状态列里扣, 扣多了底部的"设置"就会被切掉半行。
    style.configure("Small.TButton", background=CARD, foreground=TEXT,
                    font=small, bordercolor=BORDER, lightcolor=CARD,
                    darkcolor=CARD, relief="flat", padding=(8, 1))
    style.map("Small.TButton",
              background=[("pressed", "#e6eaf3"), ("active", "#f2f5fb")])

    # 单选/勾选: 底色必须跟卡片一致, 否则会出现一块突兀的灰底。
    # padding 上下只留 2px: 这六行加起来能省出十几像素, 直接决定"设置"卡片
    # 会不会被挤出 620 高的窗口。
    for name in ("TCheckbutton", "TRadiobutton"):
        style.configure(name, background=CARD, foreground=TEXT, font=body,
                        focuscolor=ACCENT, padding=(0, 2))
        style.map(name,
                  background=[("active", CARD)],
                  foreground=[("disabled", "#a3a9b5")])

    style.configure("TLabelframe", background=CARD, bordercolor=BORDER,
                    lightcolor=CARD, darkcolor=BORDER,
                    relief="solid", borderwidth=1)
    style.configure("TLabelframe.Label", background=CARD, foreground=MUTED,
                    font=small)

    # 表格: 行高按字号调, 默认行高在中文下会挤成一团。
    style.configure("Treeview", background=CARD, fieldbackground=CARD,
                    foreground=TEXT, font=body, rowheight=24,
                    bordercolor=BORDER, lightcolor=CARD, darkcolor=CARD,
                    borderwidth=1, relief="flat")
    style.map("Treeview",
              background=[("selected", ACCENT)],
              foreground=[("selected", "#ffffff")])
    style.configure("Treeview.Heading", background="#f5f7fb", foreground=MUTED,
                    font=small, relief="flat", padding=(6, 5))
    style.map("Treeview.Heading", background=[("active", "#eaeef7")])

    # 滚动条: clam 默认是深灰的, 跟浅色表格放一起像一条黑边, 必须显式改浅。
    for orient in ("Vertical", "Horizontal"):
        style.configure(f"{orient}.TScrollbar", background="#c9cfda",
                        troughcolor="#f2f4f9", bordercolor="#f2f4f9",
                        lightcolor="#c9cfda", darkcolor="#c9cfda",
                        arrowcolor=MUTED, relief="flat", arrowsize=13)
        style.map(f"{orient}.TScrollbar",
                  background=[("pressed", "#9aa5b8"), ("active", "#b3bccc")])

    style.configure("TNotebook", background=BG, bordercolor=BORDER,
                    tabmargins=(4, 6, 4, 0), borderwidth=1)
    style.configure("TNotebook.Tab", background="#e3e8f2", foreground=MUTED,
                    font=body, padding=(16, 7), borderwidth=0)
    style.map("TNotebook.Tab",
              background=[("selected", CARD), ("active", "#eef1f8")],
              foreground=[("selected", ACCENT)],
              expand=[("selected", (0, 0, 0, 0))])

    style.configure("TSeparator", background=BORDER)
    return style


# --------------------------------------------------------------------------- #
# 展示格式: 把 control 层的原始值翻成人话
# --------------------------------------------------------------------------- #

#: 常见出口国家/地区的中文名。用户看到 "US" 未必反应得过来, 看到"美国"秒懂。
_COUNTRIES: dict[str, str] = {
    "US": "美国", "JP": "日本", "SG": "新加坡", "HK": "中国香港",
    "TW": "中国台湾", "KR": "韩国", "GB": "英国", "UK": "英国",
    "DE": "德国", "FR": "法国", "NL": "荷兰", "CA": "加拿大",
    "AU": "澳大利亚", "IN": "印度", "RU": "俄罗斯", "TR": "土耳其",
    "BR": "巴西", "VN": "越南", "TH": "泰国", "MY": "马来西亚",
    "ID": "印尼", "PH": "菲律宾", "IT": "意大利", "ES": "西班牙",
    "SE": "瑞典", "CH": "瑞士", "PL": "波兰", "UA": "乌克兰",
    "AE": "阿联酋", "IL": "以色列", "MX": "墨西哥", "AR": "阿根廷",
    "ZA": "南非", "FI": "芬兰", "NO": "挪威", "DK": "丹麦",
    "IE": "爱尔兰", "CZ": "捷克", "AT": "奥地利", "RO": "罗马尼亚",
    "CL": "智利", "EG": "埃及", "NG": "尼日利亚", "PK": "巴基斯坦",
    "BD": "孟加拉", "KZ": "哈萨克斯坦", "CN": "中国大陆", "MO": "中国澳门",
}


def country_label(code: str) -> str:
    """出口国家代码 → "美国（US）"。未知代码原样返回, 不猜。"""
    code = (code or "").strip().upper()
    if not code:
        return ""
    name = _COUNTRIES.get(code)
    return f"{name}（{code}）" if name else code


def latency_text(ms: int) -> str:
    """延迟文案。负数/-1 表示"没测到", 不能显示成 0ms 骗用户。"""
    if ms is None or ms <= 0:
        return "—"
    if ms >= 3000:
        return "超时"
    return f"{ms} ms"


def latency_style(ms: int) -> str:
    """延迟对应的 ttk 样式名(绿/黄/红/灰)。"""
    if ms is None or ms <= 0:
        return "Key.TLabel"
    if ms < 200:
        return "Ok.TLabel"
    if ms < 600:
        return "Warn.TLabel"
    return "Bad.TLabel"


def check_latency_text(ms: int, ok: bool) -> str:
    """平台自检表里的"延迟"。

    结果明明写着"可用", 延迟却显示"超时", 用户只会怀疑整张表都是错的
    (ChatGPT/YouTube 这类站点走的是网页探测, 拿不到毫秒数是正常的)。
    所以: 可用但没测到延迟就显示 "—", 只有真的失败才说"超时"。
    """
    if ok and (ms is None or ms <= 0):
        return "—"
    return latency_text(ms)


def format_ago(ts: float, *, now: float | None = None) -> str:
    """时间戳 → "3 分钟前验证"。0 表示从未验证过。"""
    if not ts:
        return "尚未验证"
    delta = max(0.0, (now if now is not None else time.time()) - float(ts))
    if delta < 90:
        return "刚刚验证"
    if delta < 3600:
        return f"{int(delta // 60)} 分钟前验证"
    if delta < 86400:
        return f"{int(delta // 3600)} 小时前验证"
    return f"{int(delta // 86400)} 天前验证"


def format_uptime(seconds: float) -> str:
    """已连接时长。用户问"连了多久"时能直接看到。"""
    if seconds <= 0:
        return ""
    if seconds < 3600:
        return f"{int(seconds // 60)} 分钟"
    if seconds < 86400:
        return f"{seconds / 3600:.1f} 小时"
    return f"{seconds / 86400:.1f} 天"


def version_text(raw: str) -> str:
    """内核版本号 → 显示用文本。

    内核对版本号的写法不统一(有的带 v, 有的不带), 直接拼就会显示成
    "vv1.19.31"。这里统一成一个 v, 别让用户看到重影。
    """
    raw = (raw or "").strip()
    if not raw:
        return ""
    return raw if raw[:1] in ("v", "V") else f"v{raw}"


def shorten(text: str, limit: int) -> str:
    """按字符数截断并加省略号。

    为什么要在 Python 里截: 左侧状态卡是固定高度的, 让 Tk 自己换行会把
    整列撑高, 底部的"设置"就被挤出窗口 —— 宁可显示 "很长的节点名字…",
    也不能让用户看不到开关。完整名字在节点列表里。
    """
    text = str(text or "")
    return text if len(text) <= limit else text[: max(1, limit - 1)] + "…"


def display_width(text: str) -> int:
    """文本占多少"列": 中日韩全角字符算 2, 其余算 1。

    节点池里的名字中英混排("sg 新加坡 | SGP #5"), 按字符数截断会出现
    "看起来没超, 实际超了一倍宽"的情况, 把左侧卡片顶宽。
    """
    return sum(2 if ord(ch) > 0x2E7F else 1 for ch in str(text or ""))


def shorten_display(text: str, columns: int) -> str:
    """按显示宽度截断并加省略号。"""
    text = str(text or "")
    if display_width(text) <= columns:
        return text
    out: list[str] = []
    used = 0
    for ch in text:
        w = 2 if ord(ch) > 0x2E7F else 1
        if used + w > columns - 1:
            break
        out.append(ch)
        used += w
    return "".join(out) + "…"


def switch_face(connected: bool) -> tuple[str, str, str]:
    """大圆钮的 (填充色, 文字色, 按钮文字)。"""
    if connected:
        return ACCENT, "#ffffff", "已连接"
    return OFF_FACE, OFF_TEXT, "未连接"
