package com.accesspilot.hongxing.ui.component

import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ColumnScope
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.unit.dp

/**
 * 红杏 Android · 卡片与分割线的统一做法。
 *
 * 为什么全用一个 [HongxingCard] 而不是各写各的 Surface:
 * 蓝灯那种观感的关键不是单个组件好看, 而是**所有块长得一样** —— 同样的圆角、
 * 同样的内边距、同样的"比背景亮一点点"的底色。分头写迟早会飘。
 *
 * 卡片刻意**没有边框、没有阴影**。层次靠 `surfaceContainerLow` 和背景之间那
 * 一点点明度差撑起来: 有边框就会有很多线, 线多了就吵。
 */
@Composable
fun HongxingCard(
    modifier: Modifier = Modifier,
    color: Color = MaterialTheme.colorScheme.surfaceContainerLow,
    content: @Composable ColumnScope.() -> Unit,
) {
    Surface(
        modifier = modifier.fillMaxWidth(),
        shape = MaterialTheme.shapes.extraLarge,
        color = color,
    ) {
        Column(modifier = Modifier.padding(horizontal = 20.dp, vertical = 18.dp), content = content)
    }
}

/** 分区小标题。用 onSurfaceVariant 而不是 onSurface —— 它是说明, 不是内容。 */
@Composable
fun SectionLabel(text: String, modifier: Modifier = Modifier) {
    Text(
        text = text,
        modifier = modifier,
        style = MaterialTheme.typography.labelLarge,
        color = MaterialTheme.colorScheme.onSurfaceVariant,
    )
}

/**
 * 发丝分割线。
 *
 * 只在**列表**里用(节点列表需要横向扫描, 没有线会串行), 状态卡里一条都不用 ——
 * 那里行数少, 靠行距就能分清。
 */
@Composable
fun ThinDivider(modifier: Modifier = Modifier) {
    HorizontalDivider(
        modifier = modifier,
        thickness = 1.dp,
        color = MaterialTheme.colorScheme.outlineVariant.copy(alpha = 0.55f),
    )
}
