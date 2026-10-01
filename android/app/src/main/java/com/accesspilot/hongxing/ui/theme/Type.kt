package com.accesspilot.hongxing.ui.theme

import androidx.compose.material3.Typography
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.sp

/**
 * 红杏 Android · 字体层级。
 *
 * 只做三件事, 都是为了"干净":
 *  1. 标题统一压到 SemiBold —— M3 默认的 Normal 太软, 大圆钮里的"已连接"
 *     会显得没力气。
 *  2. 中文标题收紧一点字距。中文字形四周留白比拉丁字母大, 大字号下 M3 默认
 *     字距看着发散。
 *  3. 正文字号不变, 但把行高抬一点(18/21/24sp), 中文段落比英文更需要行距。
 *
 * 不引入任何自定义字体: 系统字体在中文下就是最好的, 塞一个第三方字体只会让
 * APK 变大、还得自己处理字重缺失。
 */
private val M3 = Typography()

val HongxingTypography = M3.copy(
    headlineLarge = M3.headlineLarge.copy(fontWeight = FontWeight.SemiBold, letterSpacing = (-0.4f).sp),
    headlineMedium = M3.headlineMedium.copy(fontWeight = FontWeight.SemiBold, letterSpacing = (-0.2f).sp),
    headlineSmall = M3.headlineSmall.copy(fontWeight = FontWeight.SemiBold),
    titleLarge = M3.titleLarge.copy(fontWeight = FontWeight.SemiBold),
    titleMedium = M3.titleMedium.copy(fontWeight = FontWeight.Medium),
    titleSmall = M3.titleSmall.copy(fontWeight = FontWeight.Medium),
    bodyLarge = M3.bodyLarge.copy(lineHeight = 24.sp),
    bodyMedium = M3.bodyMedium.copy(lineHeight = 21.sp),
    bodySmall = M3.bodySmall.copy(lineHeight = 18.sp),
    labelLarge = M3.labelLarge.copy(fontWeight = FontWeight.Medium),
    labelMedium = M3.labelMedium.copy(fontWeight = FontWeight.Medium),
)
