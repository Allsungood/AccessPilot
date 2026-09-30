"""红杏图标: 纯 Python 手写 .ico / .png, 不依赖 Pillow.

为什么要自己写 ICO
------------------
项目坚持**运行时零第三方依赖**: 目标机器常常连 PyPI 都打不开, 为了一个
图标去装 Pillow(几十 MB, 还要编译)会把"双击就能用"变成"先配环境"。
而真正需要 .ico 文件的地方只有两处 —— 托盘图标和 PyInstaller 的 exe 资源
图标。ICO 本质就是几个 DIB 头拼在一起, `struct` 足够, 不值得引依赖。

格式要点(都是会画出花屏的坑)
----------------------------
* ICO 里的 DIB 是"两倍高度": `biHeight = 2 * 像素高度`。上半是 XOR(颜色)
  位图, 下半是 AND(掩码)位图; 只写一半, Windows 会把掩码当像素读, 花屏。
* 32bpp 也必须带 AND 掩码: Vista 之后按 alpha 通道渲染, 但旧组件(以及
  某些第三方资源读取器)只看掩码, 缺了会把透明区域画成黑块。
* 像素行自下而上(BMP 传统); AND 掩码每行要按 4 字节对齐, 否则 16x16 这种
  尺寸在读取方那里会整体错位 2 字节。
* 256x256 标准允许存 PNG, 但那要求读取方走 Vista+ 的分支; 直接写未压缩
  DIB 兼容性最好 —— 托盘和资源管理器都不在乎这几百 KB。

图形为什么长这样
----------------
红杏 = 暖红色的果子 + 一枚叶子。托盘只有 16x16, 所以造型必须"远看是一个
红点": 果子的直径占了画布八成, 叶子只做顶部一点点缀。未连接状态整枚图标
转灰 —— 这是蓝灯式的语言, 用户扫一眼托盘就知道现在是开着还是关着。

配色与 gui/theme.py 的主色保持一致(暖红 + dashboard 里那支绿色)。这里写成
常量而不是 `import theme`, 是为了 icon.py 能单独使用: 打包脚本只想要一个
图标文件时, 不该被迫拉起整个 GUI 包。
"""
from __future__ import annotations

import math
import os
import struct
import sys
import zlib
from pathlib import Path

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #

#: 一档尺寸对应 Explorer/托盘的一种显示场景: 16 托盘, 32 任务栏/Alt+Tab,
#: 48 资源管理器"中图标", 256 大图标与 exe 属性页。
SIZES: tuple[int, ...] = (16, 32, 48, 256)

#: 已连接 / 未连接两枚图标。文件名固定, PyInstaller 的 spec 直接引用它们。
ICO_NAME = "hongxing.ico"
ICO_NAME_OFF = "hongxing_off.ico"

_ASSET_SUBDIR = ("accesspilot", "gui", "assets")

# 几何(归一化坐标, 原点左上, y 向下)
_FRUIT_C = (0.500, 0.545)
_FRUIT_R = 0.400
_LEAF_C = (0.660, 0.198)
_LEAF_A = 0.190
_LEAF_B = 0.088
_LEAF_DEG = -28.0  # 逆时针, 让叶尖朝右上
_STEM_A = (0.478, 0.315)
_STEM_B = (0.556, 0.140)
_STEM_R = 0.042
_LIGHT = (0.340, 0.300)  # 高光方向(左上打光)

# 调色板: (近光点色, 中间色, 远边缘色, 叶子色, 叶脉/茎色, 高光强度)
_PALETTE_ON = (
    (255, 150, 96),
    (232, 62, 63),
    (162, 20, 46),
    (63, 185, 80),
    (36, 118, 58),
    0.55,
)
_PALETTE_OFF = (
    (170, 176, 188),
    (118, 124, 138),
    (74, 80, 94),
    (126, 132, 146),
    (98, 104, 118),
    0.22,
)


# --------------------------------------------------------------------------- #
# 路径
# --------------------------------------------------------------------------- #


def assets_dir() -> Path:
    """资源目录: 源码运行时是 `accesspilot/gui/assets/`。

    PyInstaller onefile 会把数据解到临时目录并设置 `sys._MEIPASS`, 所以冻结
    之后要优先看那里 —— 否则打包出来的 exe 会去 exe 旁边找 assets 而找不到。
    """
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        frozen = Path(meipass).joinpath(*_ASSET_SUBDIR)
        if frozen.is_dir():
            return frozen
    return Path(__file__).resolve().parent / "assets"


def _cache_dir() -> Path:
    """只读安装时的退路目录(装在 Program Files 里时资源目录不可写)。"""
    home = os.environ.get("ACCESSPILOT_HOME")
    if home:
        base = Path(home)
    else:
        local = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        base = (Path(local) if local else Path.home()) / "AccessPilot"
    return base / "cache"


def ico_path(*, connected: bool = True) -> Path:
    """图标**期望**所在的位置(不保证存在, 用 ensure_ico() 保证)。"""
    return assets_dir() / (ICO_NAME if connected else ICO_NAME_OFF)


# --------------------------------------------------------------------------- #
# 绘图
# --------------------------------------------------------------------------- #


def _clamp01(x: float) -> float:
    return 0.0 if x < 0.0 else (1.0 if x > 1.0 else x)


def _mix(a: tuple[int, int, int], b: tuple[int, int, int], t: float) -> tuple[int, int, int]:
    return (
        int(a[0] + (b[0] - a[0]) * t + 0.5),
        int(a[1] + (b[1] - a[1]) * t + 0.5),
        int(a[2] + (b[2] - a[2]) * t + 0.5),
    )


def _coverage(distance: float, size: int) -> float:
    """有符号距离 -> 覆盖率。1px 宽的过渡带就是抗锯齿。

    直接按距离算覆盖率, 而不是超采样再缩小: 圆和椭圆的距离场是解析的,
    省掉 4 倍像素的循环, 生成图标从"等一下"变成"瞬间"。
    """
    return _clamp01(0.5 - distance * size)


def _sd_leaf(u: float, v: float) -> float:
    """叶子(旋转椭圆)的近似有符号距离, 单位与 u/v 相同。"""
    rad = math.radians(_LEAF_DEG)
    cos_a, sin_a = math.cos(rad), math.sin(rad)
    dx, dy = u - _LEAF_C[0], v - _LEAF_C[1]
    x = dx * cos_a + dy * sin_a
    y = -dx * sin_a + dy * cos_a
    k = math.hypot(x / _LEAF_A, y / _LEAF_B)
    if k == 0.0:
        return -min(_LEAF_A, _LEAF_B)
    return (k - 1.0) * min(_LEAF_A, _LEAF_B) / k


def _sd_stem(u: float, v: float) -> float:
    """果柄: 胶囊体(线段 + 半径)的距离。"""
    ax, ay = _STEM_A
    bx, by = _STEM_B
    pax, pay = u - ax, v - ay
    bax, bay = bx - ax, by - ay
    denom = bax * bax + bay * bay
    h = 0.0 if denom == 0.0 else _clamp01((pax * bax + pay * bay) / denom)
    return math.hypot(pax - bax * h, pay - bay * h) - _STEM_R


def _render_rgba(size: int, *, connected: bool) -> bytearray:
    """画出一张 size x size 的 RGBA 位图(自上而下, 非预乘)。

    合成顺序: 叶子 -> 果柄 -> 果子 -> 高光。叶子压在果子下面, 果子圆边
    自然把它"切"掉一截, 看起来才是长在果子后面的。
    """
    near, mid, far, leaf_c, stem_c, gloss = _PALETTE_ON if connected else _PALETTE_OFF
    out = bytearray(size * size * 4)
    inv = 1.0 / size
    # 只扫可能落在图形上的矩形, 四角直接留空(256x256 时省掉约三成像素)
    x0 = int(0.06 * size)
    x1 = min(size, int(0.96 * size) + 2)
    y0 = int(0.05 * size)
    y1 = min(size, int(0.99 * size) + 2)

    for py in range(y0, y1):
        v = (py + 0.5) * inv
        row = py * size * 4
        for px in range(x0, x1):
            u = (px + 0.5) * inv
            r = g = b = 0.0
            a = 0.0

            # --- 叶子 ---
            ca = _coverage(_sd_leaf(u, v), size)
            if ca > 0.0:
                r, g, b, a = float(leaf_c[0]), float(leaf_c[1]), float(leaf_c[2]), ca

            # --- 果柄 ---
            cs = _coverage(_sd_stem(u, v), size)
            if cs > 0.0:
                na = cs + a * (1.0 - cs)
                if na > 0.0:
                    keep = a * (1.0 - cs) / na
                    r = stem_c[0] * cs / na + r * keep
                    g = stem_c[1] * cs / na + g * keep
                    b = stem_c[2] * cs / na + b * keep
                a = na

            # --- 果子(径向渐变) ---
            df = math.hypot(u - _FRUIT_C[0], v - _FRUIT_C[1]) - _FRUIT_R
            cf = _coverage(df, size)
            if cf > 0.0:
                t = _clamp01(math.hypot(u - _LIGHT[0], v - _LIGHT[1]) / (_FRUIT_R * 1.42))
                if t < 0.5:
                    fr, fg, fb = _mix(near, mid, t * 2.0)
                else:
                    fr, fg, fb = _mix(mid, far, (t - 0.5) * 2.0)
                na = cf + a * (1.0 - cf)
                if na > 0.0:
                    keep = a * (1.0 - cf) / na
                    r = fr * cf / na + r * keep
                    g = fg * cf / na + g * keep
                    b = fb * cf / na + b * keep
                a = na

                # --- 高光(只画在果子内部, 否则会在轮廓外留一圈白雾) ---
                dg = math.hypot((u - 0.375) / 0.115, (v - 0.345) / 0.085)
                if dg < 1.0:
                    ga = gloss * (1.0 - dg) ** 2 * cf
                    r = r * (1.0 - ga) + 255.0 * ga
                    g = g * (1.0 - ga) + 255.0 * ga
                    b = b * (1.0 - ga) + 255.0 * ga

            if a <= 0.0:
                continue
            o = row + px * 4
            out[o] = int(_clamp01(r / 255.0) * 255.0 + 0.5)
            out[o + 1] = int(_clamp01(g / 255.0) * 255.0 + 0.5)
            out[o + 2] = int(_clamp01(b / 255.0) * 255.0 + 0.5)
            out[o + 3] = int(_clamp01(a) * 255.0 + 0.5)
    return out


# --------------------------------------------------------------------------- #
# ICO 编码
# --------------------------------------------------------------------------- #


def _dib(rgba: bytearray, size: int) -> bytes:
    """把 RGBA 像素封成 ICO 里那一段 DIB: BITMAPINFOHEADER + XOR + AND。"""
    header = struct.pack(
        "<IiiHHIIiiII",
        40,  # biSize
        size,  # biWidth
        size * 2,  # biHeight: 两倍 —— XOR 与 AND 叠在一起
        1,  # biPlanes
        32,  # biBitCount
        0,  # biCompression = BI_RGB
        0,  # biSizeImage(BI_RGB 可填 0)
        0,
        0,
        0,
        0,
    )

    xor = bytearray(size * size * 4)
    for y in range(size):
        src = (size - 1 - y) * size * 4  # BMP 自下而上
        dst = y * size * 4
        for x in range(size):
            s = src + x * 4
            d = dst + x * 4
            # BGRA
            xor[d] = rgba[s + 2]
            xor[d + 1] = rgba[s + 1]
            xor[d + 2] = rgba[s]
            xor[d + 3] = rgba[s + 3]

    # AND 掩码: 1 = 透明。alpha 半透明处按"看不见"处理, 与大多数工具一致。
    stride = ((size + 31) // 32) * 4
    mask = bytearray(stride * size)
    for y in range(size):
        src = (size - 1 - y) * size * 4
        base = y * stride
        for x in range(size):
            if rgba[src + x * 4 + 3] < 128:
                mask[base + (x >> 3)] |= 0x80 >> (x & 7)

    return header + bytes(xor) + bytes(mask)


def ico_bytes(*, connected: bool = True, sizes: tuple[int, ...] = SIZES) -> bytes:
    """生成完整 .ico 字节流: ICONDIR + 每条 ICONDIRENTRY + 各自的 DIB。"""
    images = [_dib(_render_rgba(s, connected=connected), s) for s in sizes]
    out = bytearray(struct.pack("<HHH", 0, 1, len(images)))
    offset = 6 + 16 * len(images)
    for size, image in zip(sizes, images):
        # 宽高各 1 字节, 256 要写 0(格式如此); 位深 32 让 Windows 用 alpha 渲染
        out += struct.pack(
            "<BBBBHHII",
            size % 256,
            size % 256,
            0,
            0,
            1,
            32,
            len(image),
            offset,
        )
        offset += len(image)
    for image in images:
        out += image
    return bytes(out)


def png_bytes(size: int = 256, *, connected: bool = True) -> bytes:
    """同一张图的 PNG 编码。

    给不需要文件、只要能塞进 bytes 的场合用(例如以后做 web 控制台的
    favicon, 或者往剪贴板/日志里丢一张预览图)。纯 zlib, 没有第三方依赖。
    """
    if size < 1:
        raise ValueError("size 必须为正数")
    rgba = _render_rgba(size, connected=connected)
    raw = bytearray()
    stride = size * 4
    for y in range(size):
        raw.append(0)  # filter type 0 (None)
        raw += rgba[y * stride : (y + 1) * stride]

    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + tag
            + payload
            + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
        + chunk(b"IEND", b"")
    )


# --------------------------------------------------------------------------- #
# 对外接口
# --------------------------------------------------------------------------- #


def _is_usable(path: Path) -> bool:
    """文件是不是一份"四个尺寸都在"的 ICO。

    只看文件存在是不够的: 上一次生成到一半被杀掉会留下一个 0 字节文件,
    之后每次启动都会拿到一个坏图标(而且是静默的)。
    """
    try:
        data = path.read_bytes()
    except OSError:
        return False
    if len(data) < 6 or data[0:4] != b"\x00\x00\x01\x00":
        return False
    count = struct.unpack_from("<H", data, 4)[0]
    if count != len(SIZES):
        return False
    for i in range(count):
        off = 6 + 16 * i
        if len(data) < off + 16:
            return False
        width, _, _, _, _, bits, length, image_off = struct.unpack_from("<BBBBHHII", data, off)
        if width not in {s % 256 for s in SIZES} or bits != 32:
            return False
        if image_off + length > len(data):
            return False
    return True


def ensure_ico(*, connected: bool = True, force: bool = False) -> Path:
    """保证图标存在并返回路径; 已存在且完好就直接返回(幂等)。

    写资源目录失败时退到用户缓存目录而不是抛异常 —— 应用可能被装在
    Program Files 这类只读位置, 一个图标不值得让整个客户端起不来。
    """
    target = ico_path(connected=connected)
    if not force and _is_usable(target):
        return target
    data = ico_bytes(connected=connected)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return target
    except OSError:
        fallback = _cache_dir() / target.name
        fallback.parent.mkdir(parents=True, exist_ok=True)
        fallback.write_bytes(data)
        return fallback


__all__ = [
    "ICO_NAME",
    "ICO_NAME_OFF",
    "SIZES",
    "assets_dir",
    "ensure_ico",
    "ico_bytes",
    "ico_path",
    "png_bytes",
]
