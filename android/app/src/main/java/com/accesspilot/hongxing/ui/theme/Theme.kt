package com.accesspilot.hongxing.ui.theme

import android.app.Activity
import android.content.Context
import android.content.ContextWrapper
import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Shapes
import androidx.compose.runtime.Composable
import androidx.compose.runtime.CompositionLocalProvider
import androidx.compose.runtime.SideEffect
import androidx.compose.ui.platform.LocalView
import androidx.compose.ui.unit.dp
import androidx.core.view.WindowCompat

/**
 * 红杏 Android · 主题。
 *
 * 三个决定:
 *
 * 1. **不跟随系统取色(dynamicColor)**。安卓 12+ 会拿壁纸的颜色去染整个 App,
 *    听起来很美, 但结果是每台手机上的红杏长得都不一样 —— 一个品牌色都立不住,
 *    而且用户壁纸偏绿时那个大圆钮会变成绿色, 状态颜色就全乱了。所以固定用
 *    [HongxingLightColors] / [HongxingDarkColors]。
 *
 * 2. **深色模式跟随系统**, 不是自己加个开关。系统在日落时切深色, 用户不会希望
 *    还得再来我们这儿切一次。
 *
 * 3. 顺带把状态栏/导航栏图标的明暗设对。界面是 edge-to-edge 的(内容顶到状态栏
 *    底下), 深色主题下不把图标改白, 时间电量就是黑压压一片看不见。
 *
 * 圆角整体偏大(14~28dp)。参考的蓝灯那种观感里, 圆角是主要的"软"来源 ——
 * 既然几乎不画边框线, 形状本身就得让人看着舒服。
 */
private val HongxingShapes = Shapes(
    extraSmall = RoundedCornerShape(10.dp),
    small = RoundedCornerShape(14.dp),
    medium = RoundedCornerShape(18.dp),
    large = RoundedCornerShape(22.dp),
    extraLarge = RoundedCornerShape(28.dp),
)

@Composable
fun HongxingTheme(
    darkTheme: Boolean = isSystemInDarkTheme(),
    content: @Composable () -> Unit,
) {
    val colorScheme = if (darkTheme) HongxingDarkColors else HongxingLightColors
    val extraColors = if (darkTheme) HongxingDarkExtra else HongxingLightExtra

    val view = LocalView.current
    if (!view.isInEditMode) {
        SideEffect {
            val window = view.context.findActivity()?.window ?: return@SideEffect
            WindowCompat.getInsetsController(window, view).apply {
                isAppearanceLightStatusBars = !darkTheme
                isAppearanceLightNavigationBars = !darkTheme
            }
        }
    }

    CompositionLocalProvider(LocalHongxingColors provides extraColors) {
        MaterialTheme(
            colorScheme = colorScheme,
            typography = HongxingTypography,
            shapes = HongxingShapes,
            content = content,
        )
    }
}

/**
 * 从 Compose 的 Context 里往上找到 Activity。
 *
 * 为什么要往上找: `LocalView.current.context` 在 Compose 里经常被 ContextWrapper
 * 包了好几层(主题包装、Hilt、字体缩放…), 直接 `as Activity` 会随机崩在某些机型上。
 */
private tailrec fun Context.findActivity(): Activity? = when (this) {
    is Activity -> this
    is ContextWrapper -> baseContext.findActivity()
    else -> null
}
