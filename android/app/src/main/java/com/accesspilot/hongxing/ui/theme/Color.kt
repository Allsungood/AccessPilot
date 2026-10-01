package com.accesspilot.hongxing.ui.theme

import androidx.compose.material3.darkColorScheme
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.Immutable
import androidx.compose.runtime.ReadOnlyComposable
import androidx.compose.runtime.staticCompositionLocalOf
import androidx.compose.runtime.Composable
import androidx.compose.ui.graphics.Color

/**
 * 红杏 Android · **全部**颜色都在这个文件里。
 *
 * 为什么要把颜色收干净:
 * 界面上任何一处 `Color(0xFF...)` 都是将来"想换个主色"时找不到的钉子。所以这里
 * 分成三层 —— 底层是具名色板, 中层是 Material3 的 ColorScheme(dark/light 两套),
 * 上层是 M3 没有、但界面需要的语义色(成功/警告/延迟档位/大圆钮的圆盘色), 通过
 * [LocalHongxingColors] 下发。业务代码只允许用后两层的名字, 不允许写色值。
 *
 * 主色为什么是暖珊瑚红:
 * 桌面端和托盘图标上那枚杏子就是这个颜色, 两端得看着像同一个产品。M3 模板默认的
 * 紫色是"没设计过"的典型标志, 一眼就能认出来, 所以从模板上就换掉了。
 */

// ---------------------------------------------------------------------------
// 底层色板: 只有这个文件的下半部分能引用它们
// ---------------------------------------------------------------------------

/** 品牌主色(浅色模式)。取饱和度偏低的珊瑚红, 大面积铺开不会刺眼。 */
private val Coral = Color(0xFFE2543C)

/** 主色的亮档: 深色模式的 primary, 也是浅色模式圆钮渐变的受光面。 */
private val CoralBright = Color(0xFFFF8E76)

/** 主色的浅档: 浅色模式下圆钮渐变的受光面。 */
private val CoralSoft = Color(0xFFF2705A)

/** 主色的深档: 渐变背光面 / 按压态。 */
private val CoralDeep = Color(0xFFD8432B)

/** 余烬色: 深色模式的主色容器, 像炭火而不是纯色块。 */
private val Ember = Color(0xFF7A2413)

/** 深色近黑。刻意带一点红, 和暖色主色同一个温度, 不用纯灰。 */
private val InkDark = Color(0xFF141110)

/**
 * 浅色底色。**必须和 `res/values/colors.xml` 里的 `hongxing_window_bg` 保持一致**
 * —— 冷启动时窗口底色先画出来, 界面再盖上去, 两者不一致就会闪一下。
 */
private val PaperLight = Color(0xFFFBF9F8)

// ---------------------------------------------------------------------------
// 中层: Material3 配色
// ---------------------------------------------------------------------------

val HongxingLightColors = lightColorScheme(
    primary = Coral,
    onPrimary = Color(0xFFFFFFFF),
    primaryContainer = Color(0xFFFFE2DA),
    onPrimaryContainer = Color(0xFF3D0A02),
    inversePrimary = CoralBright,
    secondary = Color(0xFF8A6156),
    onSecondary = Color(0xFFFFFFFF),
    secondaryContainer = Color(0xFFF6E3DC),
    onSecondaryContainer = Color(0xFF33201A),
    tertiary = Color(0xFF8A6A2F),
    onTertiary = Color(0xFFFFFFFF),
    tertiaryContainer = Color(0xFFF6E3C2),
    onTertiaryContainer = Color(0xFF2B1D00),
    background = PaperLight,
    onBackground = Color(0xFF1E1A19),
    surface = PaperLight,
    onSurface = Color(0xFF1E1A19),
    surfaceVariant = Color(0xFFF2EDEB),
    onSurfaceVariant = Color(0xFF6C625E),
    surfaceTint = Coral,
    inverseSurface = Color(0xFF332E2D),
    inverseOnSurface = Color(0xFFF7EFED),
    error = Color(0xFFC4362A),
    onError = Color(0xFFFFFFFF),
    errorContainer = Color(0xFFFFDAD4),
    onErrorContainer = Color(0xFF410E00),
    outline = Color(0xFFA79C98),
    outlineVariant = Color(0xFFDED6D3),
    scrim = Color(0xFF000000),
    surfaceBright = Color(0xFFFFFFFF),
    surfaceDim = Color(0xFFE4DEDC),
    surfaceContainerLowest = Color(0xFFFFFFFF),
    surfaceContainerLow = Color(0xFFF7F3F1),
    surfaceContainer = Color(0xFFF2ECEA),
    surfaceContainerHigh = Color(0xFFECE6E4),
    surfaceContainerHighest = Color(0xFFE6E0DE),
)

val HongxingDarkColors = darkColorScheme(
    primary = CoralBright,
    onPrimary = Color(0xFF5A1206),
    primaryContainer = Ember,
    onPrimaryContainer = Color(0xFFFFDAD1),
    inversePrimary = Coral,
    secondary = Color(0xFFE5BFB4),
    onSecondary = Color(0xFF4A2C24),
    secondaryContainer = Color(0xFF5C3F38),
    onSecondaryContainer = Color(0xFFFFDAD1),
    tertiary = Color(0xFFE5C48A),
    onTertiary = Color(0xFF442F00),
    tertiaryContainer = Color(0xFF624400),
    onTertiaryContainer = Color(0xFFFFDFA0),
    background = InkDark,
    onBackground = Color(0xFFEDE4E1),
    surface = InkDark,
    onSurface = Color(0xFFEDE4E1),
    surfaceVariant = Color(0xFF2B2625),
    onSurfaceVariant = Color(0xFFD3C4C0),
    surfaceTint = CoralBright,
    inverseSurface = Color(0xFFEDE4E1),
    inverseOnSurface = Color(0xFF322A28),
    error = Color(0xFFFF9A8E),
    onError = Color(0xFF690005),
    errorContainer = Color(0xFF8C1D18),
    onErrorContainer = Color(0xFFFFDAD4),
    outline = Color(0xFF8C807D),
    outlineVariant = Color(0xFF443C3A),
    scrim = Color(0xFF000000),
    surfaceBright = Color(0xFF3A3432),
    surfaceDim = InkDark,
    surfaceContainerLowest = Color(0xFF0E0B0B),
    surfaceContainerLow = Color(0xFF1C1817),
    surfaceContainer = Color(0xFF201C1B),
    surfaceContainerHigh = Color(0xFF2B2625),
    surfaceContainerHighest = Color(0xFF363030),
)

// ---------------------------------------------------------------------------
// 上层: M3 没有的语义色
// ---------------------------------------------------------------------------

/**
 * Material3 的 ColorScheme 里没有"成功""警告""延迟档位"这些概念, 但它们
 * 恰恰是这个 App 最需要"用颜色说话"的地方, 所以单开一组。
 *
 * 大圆钮的圆盘色也在这里: 圆钮有四种状态(未连接/连接中/已连接/出错), 每种
 * 状态的圆盘都有受光面+背光面两个色, 写死在 Canvas 里就没法跟着主题走。
 */
@Immutable
data class HongxingExtraColors(
    /** 成功 / 通畅 */
    val success: Color,
    /** 警告 / 偏慢 */
    val warning: Color,
    /** 危险 / 出错(比 M3 的 error 更饱和, 用在描边和指示点上) */
    val danger: Color,
    val latencyGood: Color,
    val latencyFair: Color,
    val latencyPoor: Color,
    val latencyUnknown: Color,
    /** 大圆钮(未连接/出错)圆盘的受光面 */
    val discTop: Color,
    /** 大圆钮(未连接/出错)圆盘的背光面 */
    val discBottom: Color,
    /** 大圆钮未连接时的描边色 —— 几乎是背景色的加深, "没有边框线"的观感 */
    val discRing: Color,
    /** 未连接/出错时圆钮上的文字色 */
    val discContent: Color,
    /** 已连接/连接中圆钮渐变的受光面 */
    val accentTop: Color,
    /** 已连接/连接中圆钮渐变的背光面 */
    val accentBottom: Color,
)

val HongxingLightExtra = HongxingExtraColors(
    success = Color(0xFF1F9D6B),
    warning = Color(0xFFC07A00),
    danger = Color(0xFFD03A2E),
    latencyGood = Color(0xFF1F9D6B),
    latencyFair = Color(0xFFC07A00),
    latencyPoor = Color(0xFFD03A2E),
    latencyUnknown = Color(0xFF9A8F8B),
    discTop = Color(0xFFFFFFFF),
    discBottom = Color(0xFFEFE9E6),
    discRing = Color(0xFFE2D9D5),
    discContent = Color(0xFF57504D),
    accentTop = CoralSoft,
    accentBottom = CoralDeep,
)

val HongxingDarkExtra = HongxingExtraColors(
    success = Color(0xFF4FCB92),
    warning = Color(0xFFF0C05A),
    danger = Color(0xFFFF8A80),
    latencyGood = Color(0xFF4FCB92),
    latencyFair = Color(0xFFF0C05A),
    latencyPoor = Color(0xFFFF8A80),
    latencyUnknown = Color(0xFF9C908C),
    discTop = Color(0xFF272222),
    discBottom = Color(0xFF1A1616),
    discRing = Color(0xFF3C3533),
    discContent = Color(0xFFC6BAB6),
    accentTop = Color(0xFFFFA48F),
    accentBottom = Coral,
)

/** 语义色的下发通道, 由 [HongxingTheme] 提供, 业务代码用 [HongxingColors] 读。 */
val LocalHongxingColors = staticCompositionLocalOf { HongxingLightExtra }

/**
 * 读语义色: `HongxingColors.extra.latencyGood`。
 *
 * 做成对象而不是顶层变量, 是为了在调用处一眼看出"这是红杏自己的扩展色",
 * 和 `MaterialTheme.colorScheme.xxx` 区分开。
 */
object HongxingColors {
    val extra: HongxingExtraColors
        @Composable
        @ReadOnlyComposable
        get() = LocalHongxingColors.current
}
