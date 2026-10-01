package com.accesspilot.hongxing.ui.component

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.rounded.ArrowDownward
import androidx.compose.material.icons.rounded.ArrowUpward
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import com.accesspilot.hongxing.R
import com.accesspilot.hongxing.ui.format.formatBytes

/**
 * 红杏 Android · 状态卡。
 *
 * 圆钮回答"通不通", 这张卡回答"从哪儿通、怎么通的、跑了多少"。四行, 一行不多 ——
 * 再多就成了调试面板, 普通用户看一眼就关掉, 反而什么都记不住。
 *
 * 刻意**不画分割线**: 只有四行, 靠 14dp 行距和"标签灰、数值黑"的对比就够分了。
 * 线一多, 界面立刻变吵。
 *
 * 流量那一行带上下行箭头。方向用图标表示而不是文字, 是因为这一行右边要塞两个
 * 数字, 中文"上行/下行"四个字会把布局挤爆; 图标省下来的宽度正好给数值。
 */
@Composable
fun StatusCard(
    node: String,
    mode: String,
    nodeCount: Int,
    uploadBytes: Long,
    downloadBytes: Long,
    modifier: Modifier = Modifier,
) {
    HongxingCard(modifier = modifier) {
        StatusRow(label = stringResource(R.string.status_label_node)) {
            Text(
                text = if (node.isBlank()) {
                    stringResource(R.string.status_value_node_none)
                } else {
                    node
                },
                style = MaterialTheme.typography.bodyLarge,
                color = MaterialTheme.colorScheme.onSurface,
                maxLines = 1,
                overflow = TextOverflow.Ellipsis,
                textAlign = TextAlign.End,
            )
        }
        Spacer(modifier = Modifier.height(14.dp))
        StatusRow(label = stringResource(R.string.status_label_mode)) {
            Text(
                text = modeDisplayName(mode),
                style = MaterialTheme.typography.bodyLarge,
                color = MaterialTheme.colorScheme.primary,
                maxLines = 1,
                overflow = TextOverflow.Ellipsis,
                textAlign = TextAlign.End,
            )
        }
        Spacer(modifier = Modifier.height(14.dp))
        StatusRow(label = stringResource(R.string.status_label_node_count)) {
            Text(
                text = stringResource(R.string.status_value_node_count, nodeCount),
                style = MaterialTheme.typography.bodyLarge,
                color = MaterialTheme.colorScheme.onSurface,
                maxLines = 1,
            )
        }
        Spacer(modifier = Modifier.height(14.dp))
        StatusRow(label = stringResource(R.string.status_label_traffic)) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                TrafficValue(
                    icon = Icons.Rounded.ArrowUpward,
                    contentDescription = stringResource(R.string.traffic_upload_label),
                    value = formatBytes(uploadBytes),
                )
                Spacer(modifier = Modifier.width(14.dp))
                TrafficValue(
                    icon = Icons.Rounded.ArrowDownward,
                    contentDescription = stringResource(R.string.traffic_download_label),
                    value = formatBytes(downloadBytes),
                )
            }
        }
    }
}

@Composable
private fun TrafficValue(
    icon: ImageVector,
    contentDescription: String,
    value: String,
) {
    Row(verticalAlignment = Alignment.CenterVertically) {
        Icon(
            imageVector = icon,
            contentDescription = contentDescription,
            tint = MaterialTheme.colorScheme.onSurfaceVariant,
            modifier = Modifier.size(13.dp),
        )
        Spacer(modifier = Modifier.width(4.dp))
        Text(
            text = value,
            style = MaterialTheme.typography.bodyLarge,
            color = MaterialTheme.colorScheme.onSurface,
            maxLines = 1,
        )
    }
}

/**
 * 一行"标签 — 值"。
 *
 * 用 0.42 / 0.58 的权重而不是 SpaceBetween: 节点名可能很长(机场的节点名能写到
 * 三十个字), SpaceBetween 会让长名字直接把左边标签挤成两行, 而这个布局里标签
 * 必须始终是一行。数值侧右对齐 + 省略号, 长名字最多损失自己, 不会伤到别人。
 */
@Composable
private fun StatusRow(
    label: String,
    value: @Composable () -> Unit,
) {
    Row(
        modifier = Modifier.fillMaxWidth(),
        horizontalArrangement = Arrangement.Start,
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Text(
            text = label,
            style = MaterialTheme.typography.bodyMedium,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
            maxLines = 1,
            modifier = Modifier.weight(0.42f),
        )
        Box(
            modifier = Modifier.weight(0.58f),
            contentAlignment = Alignment.CenterEnd,
        ) {
            value()
        }
    }
}
