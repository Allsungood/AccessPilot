@file:OptIn(ExperimentalMaterial3Api::class)

package com.accesspilot.hongxing.ui.component

import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxHeight
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.rounded.Check
import androidx.compose.material.icons.rounded.CloudOff
import androidx.compose.material.icons.rounded.NetworkCheck
import androidx.compose.material.icons.rounded.Refresh
import androidx.compose.material.icons.rounded.SmartToy
import androidx.compose.material3.BottomSheetDefaults
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.Icon
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.ModalBottomSheet
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.rememberModalBottomSheetState
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import com.accesspilot.hongxing.R
import com.accesspilot.hongxing.core.NodeInfo
import com.accesspilot.hongxing.ui.format.LatencyLevel
import com.accesspilot.hongxing.ui.format.latencyLevel
import com.accesspilot.hongxing.ui.theme.HongxingColors

/**
 * 红杏 Android · 节点列表(底部抽屉)。
 *
 * 为什么做成抽屉而不是第二个页面:
 * 换节点是"顺手调一下"的动作 —— 用户是连上之后发现某个站打不开, 才想换一个。
 * 做成页面就意味着"离开主界面 → 选 → 返回", 而主界面那个大圆钮的状态是用户
 * 唯一想盯着的东西, 不该被推走。抽屉盖住下半屏, 圆钮还在上面看得见。
 *
 * 性能: 免费机场的节点池动辄几千个, 所以列表必须是 [LazyColumn] 且带稳定 key
 * (`NodeInfo.name` 在 mihomo 配置里就是唯一的, 同名节点配置根本加载不了)。
 * 有了 key, 测速后整表刷新时 Compose 只重组延迟变了的那几行, 不会闪。
 */
@Composable
fun NodeSheet(
    nodes: List<NodeInfo>,
    nodeCount: Int,
    current: String,
    listBusy: Boolean,
    engineBusy: Boolean,
    message: String,
    onSelect: (String) -> Unit,
    onRefresh: () -> Unit,
    onTestLatency: () -> Unit,
    onDismiss: () -> Unit,
) {
    val sheetState = rememberModalBottomSheetState(skipPartiallyExpanded = true)
    ModalBottomSheet(
        onDismissRequest = onDismiss,
        sheetState = sheetState,
        containerColor = MaterialTheme.colorScheme.surfaceContainerLow,
        dragHandle = { BottomSheetDefaults.DragHandle() },
    ) {
        NodeSheetContent(
            nodes = nodes,
            nodeCount = nodeCount,
            current = current,
            listBusy = listBusy,
            engineBusy = engineBusy,
            message = message,
            onSelect = onSelect,
            onRefresh = onRefresh,
            onTestLatency = onTestLatency,
        )
    }
}

/**
 * 抽屉的内容。
 *
 * 和 [NodeSheet] 拆开是为了让内容本身不依赖 ModalBottomSheet —— 抽屉的宿主
 * 只能在真实窗口里显示, 而内容是纯的, 将来要放到测试或第二种宿主里都行。
 */
@Composable
fun NodeSheetContent(
    nodes: List<NodeInfo>,
    nodeCount: Int,
    current: String,
    listBusy: Boolean,
    engineBusy: Boolean,
    message: String,
    onSelect: (String) -> Unit,
    onRefresh: () -> Unit,
    onTestLatency: () -> Unit,
    modifier: Modifier = Modifier,
) {
    Column(
        modifier = modifier
            .fillMaxWidth()
            // 定高而不是让内容自己撑: 三个节点时抽屉只有一条缝, 三千个时又要滚到
            // 天上去。固定 88% 屏高, 内容多少都长得一样, 用户知道往哪儿看。
            .fillMaxHeight(0.88f),
    ) {
        Row(
            modifier = Modifier
                .fillMaxWidth()
                .padding(start = 20.dp, end = 14.dp, top = 2.dp, bottom = 12.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Column(modifier = Modifier.weight(1f)) {
                Text(
                    text = stringResource(R.string.nodes_sheet_title),
                    style = MaterialTheme.typography.titleLarge,
                    color = MaterialTheme.colorScheme.onSurface,
                )
                Spacer(modifier = Modifier.height(3.dp))
                Text(
                    text = if (current.isBlank()) {
                        stringResource(R.string.nodes_sheet_subtitle, nodeCount)
                    } else {
                        stringResource(R.string.nodes_sheet_subtitle_current, nodeCount, current)
                    },
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                    maxLines = 1,
                    overflow = TextOverflow.Ellipsis,
                )
            }
            Spacer(modifier = Modifier.width(8.dp))
            SheetAction(
                icon = Icons.Rounded.Refresh,
                label = stringResource(R.string.nodes_action_refresh),
                enabled = !listBusy,
                onClick = onRefresh,
            )
            Spacer(modifier = Modifier.width(8.dp))
            SheetAction(
                icon = Icons.Rounded.NetworkCheck,
                label = stringResource(R.string.nodes_action_test),
                enabled = !listBusy && !engineBusy,
                onClick = onTestLatency,
            )
        }

        if (listBusy) {
            LinearProgressIndicator(
                modifier = Modifier
                    .fillMaxWidth()
                    .padding(horizontal = 20.dp),
                color = MaterialTheme.colorScheme.primary,
                trackColor = Color.Transparent,
            )
            Spacer(modifier = Modifier.height(8.dp))
        }

        ThinDivider()

        if (nodes.isEmpty()) {
            EmptyNodes(
                busy = listBusy,
                message = message,
                modifier = Modifier.weight(1f),
            )
        } else {
            LazyColumn(modifier = Modifier.weight(1f)) {
                items(
                    items = nodes,
                    key = { node -> node.name },
                    contentType = { "node" },
                ) { node ->
                    NodeRow(
                        node = node,
                        enabled = !listBusy,
                        onClick = { onSelect(node.name) },
                    )
                    ThinDivider(modifier = Modifier.padding(start = 20.dp))
                }
                // 底部留白, 免得最后一行贴着导航条
                item { Spacer(modifier = Modifier.height(28.dp)) }
            }
        }
    }
}

@Composable
private fun SheetAction(
    icon: ImageVector,
    label: String,
    enabled: Boolean,
    onClick: () -> Unit,
) {
    val scheme = MaterialTheme.colorScheme
    val contentColor = if (enabled) scheme.onSurface else scheme.onSurfaceVariant.copy(alpha = 0.45f)
    Surface(
        modifier = Modifier
            .clip(MaterialTheme.shapes.small)
            .clickable(enabled = enabled, onClick = onClick),
        shape = MaterialTheme.shapes.small,
        color = scheme.surfaceContainerHigh,
    ) {
        Row(
            modifier = Modifier.padding(horizontal = 12.dp, vertical = 8.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Icon(
                imageVector = icon,
                contentDescription = null,
                tint = contentColor,
                modifier = Modifier.size(15.dp),
            )
            Spacer(modifier = Modifier.width(5.dp))
            Text(
                text = label,
                style = MaterialTheme.typography.labelLarge,
                color = contentColor,
                maxLines = 1,
            )
        }
    }
}

@Composable
private fun NodeRow(
    node: NodeInfo,
    enabled: Boolean,
    onClick: () -> Unit,
) {
    val scheme = MaterialTheme.colorScheme
    Row(
        modifier = Modifier
            .fillMaxWidth()
            .clickable(enabled = enabled, onClick = onClick)
            .padding(horizontal = 20.dp, vertical = 13.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Column(modifier = Modifier.weight(1f)) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Text(
                    text = node.name,
                    style = MaterialTheme.typography.titleSmall,
                    color = if (node.alive) scheme.onSurface else scheme.onSurfaceVariant,
                    maxLines = 1,
                    overflow = TextOverflow.Ellipsis,
                    // fill = false: 名字短的时候小徽标就紧跟名字, 不会跑到最右边
                    // 去, 造成"这个徽标属于谁"的歧义
                    modifier = Modifier.weight(1f, fill = false),
                )
                if (node.aiCapable) {
                    Spacer(modifier = Modifier.width(6.dp))
                    AiBadge()
                }
            }
            if (node.current) {
                Spacer(modifier = Modifier.height(3.dp))
                Text(
                    text = stringResource(R.string.nodes_current),
                    style = MaterialTheme.typography.labelSmall,
                    color = scheme.primary,
                )
            }
        }
        Spacer(modifier = Modifier.width(12.dp))
        if (node.alive) {
            LatencyChip(latencyMs = node.latencyMs)
        } else {
            Text(
                text = stringResource(R.string.nodes_unavailable),
                style = MaterialTheme.typography.labelMedium,
                color = HongxingColors.extra.latencyUnknown,
            )
        }
        if (node.current) {
            Spacer(modifier = Modifier.width(4.dp))
            Icon(
                imageVector = Icons.Rounded.Check,
                contentDescription = null,
                tint = scheme.primary,
                modifier = Modifier.size(17.dp),
            )
        }
    }
}

/**
 * 延迟胶囊: 一个色点 + 数字。
 *
 * 颜色分级只有一处定义([latencyLevel]), 绿色 <150ms / 黄色 <400ms / 红色更高 /
 * 灰色未知。用户不需要知道阈值, 扫一眼颜色就知道哪些能选。
 */
@Composable
private fun LatencyChip(latencyMs: Int) {
    val extra = HongxingColors.extra
    val level = latencyLevel(latencyMs)
    val color = when (level) {
        LatencyLevel.Good -> extra.latencyGood
        LatencyLevel.Fair -> extra.latencyFair
        LatencyLevel.Poor -> extra.latencyPoor
        LatencyLevel.Unknown -> extra.latencyUnknown
    }
    val text = if (level == LatencyLevel.Unknown) {
        stringResource(R.string.nodes_latency_unknown)
    } else {
        stringResource(R.string.nodes_latency_ms, latencyMs)
    }
    Row(
        modifier = Modifier
            .clip(RoundedCornerShape(percent = 50))
            .background(color.copy(alpha = 0.13f))
            .padding(horizontal = 10.dp, vertical = 5.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Box(
            modifier = Modifier
                .size(6.dp)
                .clip(CircleShape)
                .background(color),
        )
        Spacer(modifier = Modifier.width(6.dp))
        Text(
            text = text,
            style = MaterialTheme.typography.labelMedium,
            color = color,
            maxLines = 1,
        )
    }
}

/**
 * ChatGPT 可用的小徽标。
 *
 * 为什么要单独标: 实测过"延迟最低的节点反而打不开 ChatGPT" —— 出口落在香港时
 * OpenAI 直接 403, 而 X/Discord 全绿。所以"能上 X"绝不能当成"能上 ChatGPT",
 * 两个信息必须分开, 而且这个标记得显眼到用户换节点时会看它。
 */
@Composable
private fun AiBadge() {
    Surface(
        shape = RoundedCornerShape(percent = 50),
        color = MaterialTheme.colorScheme.tertiaryContainer,
    ) {
        Row(
            modifier = Modifier.padding(horizontal = 7.dp, vertical = 2.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Icon(
                imageVector = Icons.Rounded.SmartToy,
                // 徽标上只有 "AI" 两个字, 读屏时得说清楚它到底能不能干嘛
                contentDescription = stringResource(R.string.nodes_badge_ai_desc),
                tint = MaterialTheme.colorScheme.onTertiaryContainer,
                modifier = Modifier.size(11.dp),
            )
            Spacer(modifier = Modifier.width(3.dp))
            Text(
                text = stringResource(R.string.nodes_badge_ai),
                style = MaterialTheme.typography.labelSmall,
                color = MaterialTheme.colorScheme.onTertiaryContainer,
                maxLines = 1,
            )
        }
    }
}

/**
 * 空状态。
 *
 * 不能是一片白: 第一次装好的用户看到空白列表, 第一反应是"这 App 坏了"。
 * 所以要明说"还没有节点" + 下一步该点哪里, 并且把引擎给的 message 显示出来
 * (比如"正在拉取节点…"), 让用户知道它在干活。
 */
@Composable
private fun EmptyNodes(
    busy: Boolean,
    message: String,
    modifier: Modifier = Modifier,
) {
    Column(
        modifier = modifier
            .fillMaxWidth()
            .padding(horizontal = 36.dp),
        verticalArrangement = Arrangement.Center,
        horizontalAlignment = Alignment.CenterHorizontally,
    ) {
        Icon(
            imageVector = Icons.Rounded.CloudOff,
            contentDescription = null,
            tint = MaterialTheme.colorScheme.onSurfaceVariant.copy(alpha = 0.55f),
            modifier = Modifier.size(52.dp),
        )
        Spacer(modifier = Modifier.height(18.dp))
        Text(
            text = if (busy) {
                stringResource(R.string.nodes_empty_refreshing)
            } else {
                stringResource(R.string.nodes_empty_title)
            },
            style = MaterialTheme.typography.titleMedium,
            color = MaterialTheme.colorScheme.onSurface,
        )
        Spacer(modifier = Modifier.height(8.dp))
        Text(
            text = if (message.isBlank()) {
                stringResource(R.string.nodes_empty_body)
            } else {
                message
            },
            style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
            textAlign = TextAlign.Center,
        )
    }
}
