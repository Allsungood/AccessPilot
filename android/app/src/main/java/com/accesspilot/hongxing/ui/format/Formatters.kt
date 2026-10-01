package com.accesspilot.hongxing.ui.format

import java.util.Locale

/**
 * 红杏 Android · 数值 → 人话。
 *
 * 全是**纯函数**, 不碰 Compose: 这样它们能被单元测试直接覆盖, 而这一层恰恰是
 * 最容易出错的地方(流量单位算错一位、时长显示成 00:00 一直不动)。
 *
 * 单位用 Locale.US 固定小数点 —— 中文用户看 "1.5 MB" 是正常的, 但某些地区的
 * Locale 会把它格式化成 "1,5 MB", 那就是 bug 而不是本地化了。
 */

/**
 * 已连接时长 → `12:34` / `1:05:03`。
 *
 * 为什么超过一小时不写成 "65:03": 分钟位一旦超过 59 就需要用户自己做除法,
 * 而"用了多久"是这一屏最重要的信息之一, 不能让人算。
 */
fun formatClock(totalSeconds: Long): String {
    val s = totalSeconds.coerceAtLeast(0L)
    val hours = s / 3600
    val minutes = (s % 3600) / 60
    val seconds = s % 60
    return if (hours > 0) {
        String.format(Locale.US, "%d:%02d:%02d", hours, minutes, seconds)
    } else {
        String.format(Locale.US, "%02d:%02d", minutes, seconds)
    }
}

/**
 * 字节数 → `512 B` / `12.4 KB` / `3.7 MB` / `1.2 GB`。
 *
 * 保留一位小数的阈值定在 10: `9.4 MB` 有意义, `1234.5 MB` 没意义 —— 十位以上
 * 再带小数就是噪音, 用户的注意力在那时候只关心量级。
 */
fun formatBytes(bytes: Long): String {
    val b = bytes.coerceAtLeast(0L)
    if (b < 1024L) return "$b B"
    val kb = b / 1024.0
    if (kb < 1024.0) return trimNumber(kb) + " KB"
    val mb = kb / 1024.0
    if (mb < 1024.0) return trimNumber(mb) + " MB"
    return trimNumber(mb / 1024.0) + " GB"
}

private fun trimNumber(value: Double): String =
    if (value < 10.0) String.format(Locale.US, "%.1f", value)
    else String.format(Locale.US, "%.0f", value)

/**
 * 延迟档位。界面只认这四档, 不认具体毫秒 —— 颜色规则集中在一处, 否则
 * "多少算慢"会在圆钮、列表、排序里各写一遍, 迟早不一致。
 *
 * 阈值来自实际体验: 150ms 以内基本无感, 400ms 以上视频就开始转圈了。
 */
enum class LatencyLevel { Good, Fair, Poor, Unknown }

fun latencyLevel(latencyMs: Int): LatencyLevel = when {
    latencyMs < 0 -> LatencyLevel.Unknown
    latencyMs < 150 -> LatencyLevel.Good
    latencyMs < 400 -> LatencyLevel.Fair
    else -> LatencyLevel.Poor
}
