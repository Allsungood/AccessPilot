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
#
# 色板规则(改配色请守着它, 否则又会散出一堆魔法色值):
#   * **一个主色**: 暖橙红(柿子色)。它取自托盘图标的杏子叶 —— icon.py 里那片
#     叶子的渐变是 (255,150,96) → (232,62,63), 主色落在这条色带偏深的一端,
#     这样标题栏图标、托盘图标、窗口里的按钮是同一套色。
#   * **一套暖中性灰**: 底色/卡片/描边/文字/次要文字。用暖灰不用冷灰, 否则
#     暖主色会显得像"贴上去的"。
#   * **状态色**: 绿=可用, 黄=慢, 红=不可用/错误。红必须和主色分得开: 主色偏
#     橙(色相约 16°), 错误红是正红(色相 0°)而且更深, 放一起不会看混。

#: 窗口底色。暖浅灰, 让白卡片浮起来, 又不像纯白那样刺眼。
BG = "#F5F2EF"
#: 卡片底色
CARD = "#FFFFFF"
BORDER = "#E4DDD6"
TEXT = "#241F1C"
#: 次要说明文字。浅一档, 让"解释"不喧宾夺主。
MUTED = "#7C736B"
#: 主色。"亮起来 = 已连接"是整个界面唯一的强视觉约定。
ACCENT = "#D9552A"
ACCENT_ACTIVE = "#BC451C"
#: 主色的浅色调: hover 描边、选中行底色
ACCENT_SOFT = "#FBE9E1"
#: 未连接时的圆钮: 灰而不是红 —— 没开代理不是错误, 不该让用户以为出了问题。
OFF_FACE = "#CFC7C0"
OFF_TEXT = "#5A544F"
OK = "#12A150"
WARN = "#C2740A"
DANGER = "#C02424"
DANGER_BG = "#FCECEC"
#: 当前节点那一行的底色
ROW_CURRENT = "#FCEFE9"
#: 表头底色
HEAD_BG = "#F8F4F1"
#: 未选中页签的底色
TAB_BG = "#EDE7E2"
#: 次要按钮的 hover / 按下底色。中性色按钮只做"深浅变化", 不用主色 ——
#: 否则一屏里会同时出现好几个被高亮的按钮, 反而看不出哪个是主要的。
HOVER_BG = "#FAF6F2"
PRESS_BG = "#EFE8E2"
DISABLED_BG = "#F4F1EE"
DISABLED_FG = "#B3AAA3"
#: 滚动条
SCROLL_BG = "#D9D0C9"
SCROLL_TROUGH = "#F7F3F0"
SCROLL_ACTIVE = "#C3B8B0"

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
    """给经典 Tk 控件(Canvas 文字等)用的字体元组。

    字号用"点"而不是像素: 点会跟着 `tk scaling` 走, 声不声明 DPI 感知都对 ——
    这也是为什么 DPI 适配里**唯一不用改**的就是字号。
    """
    return (_FAMILY, size, weight)


# --------------------------------------------------------------------------- #
# DPI
# --------------------------------------------------------------------------- #
#
# 为什么必须声明 DPI 感知
# ----------------------
# 不声明的话, 在 150% 缩放的屏幕上 Windows 会把整个窗口**位图拉伸** 1.5 倍:
# 字全糊。实测同一段 10pt 文字, 不感知时 linespace 19px(被拉伸), 感知后
# 27px 原生渲染 —— 字号不用动, 但**所有像素量都得乘缩放因子**, 否则窗口按
# 物理像素算就只有原来的 2/3 大, 内容会被大片裁掉。
#
# 缩放因子运行时读(绝不硬编码 1.5): 换台机器 / 改缩放比例都得对。

#: 每英寸多少像素时的缩放因子为 1(Windows 的 100% 基准)。
BASE_DPI = 96.0

_SCALE = 1.0
_DPI_SOURCE = "unaware/1.0"


def enable_dpi_awareness() -> bool:
    """声明本进程 DPI 感知。**必须在创建 Tk 根窗口之前调用**。

    三级降级, 全失败也不抛: 最坏情况就是回到"被系统拉伸"的旧行为, 界面照常
    能用 —— 清晰度是加分项, 不能拿"能不能打开"去换。

        SetProcessDpiAwarenessContext(PER_MONITOR_AWARE_V2)   Win10 1703+
        shcore.SetProcessDpiAwareness(2)                       Win8.1+
        user32.SetProcessDPIAware()                            Vista+
    """
    global _DPI_SOURCE
    try:
        import ctypes

        # -4 = DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
        if ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            _DPI_SOURCE = "per-monitor-v2"
            return True
    except Exception:  # noqa: PERF203
        pass
    try:
        import ctypes

        ctypes.windll.shcore.SetProcessDpiAwareness(2)   # PROCESS_PER_MONITOR_DPI_AWARE
        _DPI_SOURCE = "shcore-per-monitor"
        return True
    except Exception:  # noqa: PERF203
        pass
    try:
        import ctypes

        ctypes.windll.user32.SetProcessDPIAware()
        _DPI_SOURCE = "system-aware(legacy)"
        return True
    except Exception:  # noqa: PERF203
        return False


def read_scale(root: tk.Misc) -> float:
    """读这台机器上的缩放因子(dpi/96)。必须先有 Tk 根窗口。"""
    try:
        import ctypes

        dpi = int(ctypes.windll.user32.GetDpiForWindow(root.winfo_id()) or 0)
        if dpi > 0:
            return dpi / BASE_DPI
    except Exception:  # noqa: PERF203
        pass
    try:
        # 退回 Tk 自己的换算: 一英寸等于多少像素
        return float(root.winfo_fpixels("1i")) / BASE_DPI
    except Exception:  # noqa: PERF203
        return 1.0


def set_scale(value: float) -> None:
    global _SCALE
    _SCALE = max(1.0, float(value or 1.0))


def scale() -> float:
    return _SCALE


def dpi_source() -> str:
    """走到了哪条 API(排查用)。"""
    return _DPI_SOURCE


def px(value: float) -> int:
    """把设计稿上的像素换算成当前缩放下该用的像素。

    界面的所有尺寸都写成"96 DPI 下的样子", 由这里统一换算 —— 这样代码里
    不会出现 1.5 这种魔法数字, 换台 125% / 200% 的机器也是对的。
    """
    return max(1, int(round(float(value) * _SCALE)))


def pxs(*values: float) -> tuple[int, ...]:
    """一次换算一组(给 padding 这种元组用)。"""
    return tuple(px(v) for v in values)


# --------------------------------------------------------------------------- #
# ttk 样式
# --------------------------------------------------------------------------- #


def setup(root: tk.Misc) -> ttk.Style:
    """把整套 ttk 样式装到 root 上, 返回 Style。必须先于建控件调用。"""
    global _FAMILY
    _FAMILY = pick_family(root)
    # 缩放因子在这里定下来: 之后 px() 全靠它, 所以 setup() 必须早于任何控件。
    set_scale(read_scale(root))

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

    # 按钮: 次要动作是"安静的描边", 主要动作(连接/选最优)是实心主色 ——
    # 一屏里只该有一个实心主色按钮, 多了就没有主次了。
    style.configure("TButton", background=CARD, foreground=TEXT, font=body,
                    bordercolor=BORDER, lightcolor=CARD, darkcolor=CARD,
                    focuscolor=ACCENT, relief="flat", padding=(px(12), px(7)))
    style.map("TButton",
              background=[("pressed", PRESS_BG), ("active", HOVER_BG),
                          ("disabled", DISABLED_BG)],
              foreground=[("disabled", DISABLED_FG)])
    style.configure("Accent.TButton", background=ACCENT, foreground="#FFFFFF",
                    bordercolor=ACCENT, lightcolor=ACCENT, darkcolor=ACCENT,
                    font=bold, relief="flat", padding=(px(14), px(8)))
    style.map("Accent.TButton",
              background=[("pressed", ACCENT_ACTIVE), ("active", lighten(ACCENT, 0.10)),
                          ("disabled", "#E5B7A6")],
              foreground=[("disabled", "#FFF6F2")])
    # 错误条上的"知道了": 做得矮一点。错误条是**额外**插进来的一行, 它每高
    # 一像素都从左侧状态列里扣, 扣多了底部的"设置"就会被切掉半行。
    style.configure("Small.TButton", background=CARD, foreground=TEXT,
                    font=small, bordercolor=BORDER, lightcolor=CARD,
                    darkcolor=CARD, relief="flat", padding=(px(9), px(1)))
    style.map("Small.TButton",
              background=[("pressed", PRESS_BG), ("active", HOVER_BG)])

    # 单选/勾选: 底色必须跟卡片一致, 否则会出现一块突兀的灰底。
    # padding 上下只留 4px: 这六行加起来能省出十几像素, 直接决定"设置"卡片
    # 会不会被挤出 620 高的窗口。
    for name in ("TCheckbutton", "TRadiobutton"):
        style.configure(name, background=CARD, foreground=TEXT, font=body,
                        focuscolor=ACCENT, padding=(0, px(4)))
        style.map(name,
                  background=[("active", CARD), ("pressed", CARD)],
                  foreground=[("disabled", DISABLED_FG)])

    # 卡片: 用极浅的描边 + 白底跟窗口底色拉开层次。不做重边框 ——
    # 蓝灯那种"干净"靠的是底色差, 不是线。再加一圈同色内描边当内边距。
    style.configure("TLabelframe", background=CARD, bordercolor=BORDER,
                    lightcolor=CARD, darkcolor=BORDER,
                    relief="solid", borderwidth=1)
    style.configure("TLabelframe.Label", background=CARD, foreground=MUTED,
                    font=small)

    # 表格: 行高按字号调, 默认行高在中文下会挤成一团。
    # DPI 下这里**必须**跟着缩放: 字是点单位会自己变大, 行高是像素不会 ——
    # 不乘的话 150% 屏上 27px 高的字会被塞进 24px 的行里切掉下半截。
    style.configure("Treeview", background=CARD, fieldbackground=CARD,
                    foreground=TEXT, font=body, rowheight=px(30),
                    bordercolor=BORDER, lightcolor=CARD, darkcolor=CARD,
                    borderwidth=1, relief="flat")
    style.map("Treeview",
              background=[("selected", ACCENT)],
              foreground=[("selected", "#FFFFFF")])
    style.configure("Treeview.Heading", background=HEAD_BG, foreground=MUTED,
                    font=small, relief="flat", padding=(px(8), px(6)))
    style.map("Treeview.Heading", background=[("active", ACCENT_SOFT)])

    # 滚动条: clam 默认是深灰的, 跟浅色表格放一起像一条黑边, 必须显式改浅。
    for orient in ("Vertical", "Horizontal"):
        style.configure(f"{orient}.TScrollbar", background=SCROLL_BG,
                        troughcolor=SCROLL_TROUGH, bordercolor=SCROLL_TROUGH,
                        lightcolor=SCROLL_BG, darkcolor=SCROLL_BG,
                        arrowcolor=MUTED, relief="flat", arrowsize=px(15))
        style.map(f"{orient}.TScrollbar",
                  background=[("pressed", SCROLL_ACTIVE),
                              ("active", lighten(SCROLL_BG, 0.12))])

    style.configure("TNotebook", background=BG, bordercolor=BORDER,
                    tabmargins=(px(4), px(8), px(4), 0), borderwidth=1)
    style.configure("TNotebook.Tab", background=TAB_BG, foreground=MUTED,
                    font=body, padding=(px(20), px(9)), borderwidth=0)
    style.map("TNotebook.Tab",
              background=[("selected", CARD), ("active", ACCENT_SOFT)],
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

    "可用"和"超时"同时出现在一行里, 用户只会怀疑整张表都是错的 —— 而这两列
    其实来自不同的探测路径: 可用性看状态码, 延迟看计时, 一个有一个没有很正常
    (ChatGPT/YouTube 这类网页探测经常拿不到毫秒数, 或者慢到 3 秒以上但确实通了)。
    所以规则是: **可用就只报真实毫秒数**, 拿不到就 "—"; 只有真的失败, 才允许
    出现"超时"这种带判断的措辞。
    """
    if ok:
        return f"{int(ms)} ms" if ms and ms > 0 else "—"
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


def clean_node_name(text: str) -> str:
    """把节点名里**系统字体画不出来**的字符换成能读的等价物。

    免费节点池的名字里混着国旗 emoji(🇭🇰 其实是两个"区域指示符"码位)。微软雅黑
    没有这些字形, Tk 只能画成 "?" 或空心方块 —— 用户看到的就是"乱码", 会以为
    客户端出问题了。这里做三件事:
      * 国旗对 → 两字母国家码(🇭🇰 → HK), 语义一点没丢;
      * 其它星平面字符(表情) → 去掉, 反正是装饰;
      * 杂项符号(♻ ✅ 这类) → 去掉, 同样是因为多半没字形。
    只用于**显示**, 内部匹配仍然用原始名字。
    """
    s = str(text or "")
    out: list[str] = []
    i = 0
    while i < len(s):
        cp = ord(s[i])
        if (0x1F1E6 <= cp <= 0x1F1FF and i + 1 < len(s)
                and 0x1F1E6 <= ord(s[i + 1]) <= 0x1F1FF):
            code = (chr(cp - 0x1F1E6 + ord("A"))
                    + chr(ord(s[i + 1]) - 0x1F1E6 + ord("A")))
            i += 2
            # 免费池里大量节点本来就叫 "🇺🇸US_237|..."、"🇮🇩ID_6|..." ——
            # 国旗后面**已经跟着国家码**了。无脑替换会得到 "USUS_237"、
            # "IDID_6", 比原来的乱码还难读(真实踩过)。所以: 紧跟其后的文本
            # 已经以这个国家码开头时就把国旗**丢掉**, 否则才补上它。
            following = s[i:i + 2].upper()
            if following == code:
                continue
            out.append(code)
            continue
        if cp > 0xFFFF or 0x2600 <= cp <= 0x27BF or 0x2B00 <= cp <= 0x2BFF:
            i += 1
            continue
        # 变体选择符(U+FE0E/U+FE0F)是给前一个字符选字形的, 单独留下会变成
        # 一个看不见但占位的杂字符 —— 实测 "☁️ WARP" 去掉了 ☁ 之后还剩一个
        # FE0F, 显示成 "️ WARP"(前面多一个空档)。一并清掉。
        if 0xFE00 <= cp <= 0xFE0F:
            i += 1
            continue
        out.append(s[i])
        i += 1
    return " ".join("".join(out).split())


def latency_tag(ms: int) -> str:
    """节点表里一行延迟对应的 tag 名(绿/黄/红/无)。"""
    if ms is None or ms <= 0:
        return ""
    if ms < 200:
        return "fast"
    if ms < 600:
        return "mid"
    return "slow"


def switch_face(connected: bool) -> tuple[str, str, str]:
    """大圆钮的 (填充色, 文字色, 按钮文字)。"""
    if connected:
        return ACCENT, "#ffffff", "已连接"
    return OFF_FACE, OFF_TEXT, "未连接"


def _mix(color: str, other: str, amount: float) -> str:
    """把 color 朝 other 混 amount(0~1)。用于 hover / pressed 的深浅变化。"""
    try:
        c = color.lstrip("#")
        o = other.lstrip("#")
        parts = []
        for i in (0, 2, 4):
            a = int(c[i:i + 2], 16)
            b = int(o[i:i + 2], 16)
            parts.append(max(0, min(255, int(round(a + (b - a) * amount)))))
        return "#%02x%02x%02x" % tuple(parts)
    except Exception:  # noqa: PERF203
        return color


def lighten(color: str, amount: float = 0.16) -> str:
    return _mix(color, "#ffffff", amount)


def darken(color: str, amount: float = 0.14) -> str:
    return _mix(color, "#000000", amount)
