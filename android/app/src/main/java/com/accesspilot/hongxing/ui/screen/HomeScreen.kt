package com.accesspilot.hongxing.ui.screen

import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.rounded.Dns
import androidx.compose.material.icons.rounded.ExpandMore
import androidx.compose.material.icons.rounded.Shield
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import com.accesspilot.hongxing.R
import com.accesspilot.hongxing.core.EngineController
import com.accesspilot.hongxing.core.EnginePhase
import com.accesspilot.hongxing.ui.component.ErrorBanner
import com.accesspilot.hongxing.ui.component.HongxingCard
import com.accesspilot.hongxing.ui.component.ModePicker
import com.accesspilot.hongxing.ui.component.NodeSheet
import com.accesspilot.hongxing.ui.component.PowerButton
import com.accesspilot.hongxing.ui.component.StatusCard
import com.accesspilot.hongxing.ui.component.rememberCopyToClipboard
import com.accesspilot.hongxing.ui.format.formatClock
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch

/**
 * 红杏 Android · 主界面(一屏)。
 *
 * 从上到下就是用户的心智顺序:
 * **能不能用(大圆钮) → 现在什么情况(状态卡) → 怎么分流(模式) → 走哪个节点(列表)**。
 * 再多就不加了 —— 这一屏的目标是"打开就知道通没通", 不是一个控制台。
 *
 * 三条设计上的硬规矩:
 *
 * 1. **所有状态只有一个来源**: 这里只读 `controller.status`。界面上任何一个字都不是
 *    自己拼出来的判断, 否则迟早会出现"界面说已连接、实际隧道没起来"。
 * 2. **动作的失败不能静默**。`EngineController` 的六个命令都返回 `Result`, 而
 *    `status.error` 是引擎写的; 界面另外留了一份 [localError] 兜底 —— 万一某个
 *    实现忘了往 status 里写 error, 用户看到的仍然是一句话而不是"点了没反应"。
 * 3. **圆钮永远只有一个动作**: 点一下 = `toggle()`。不给它加长按、双击之类的隐藏
 *    操作, 这个按钮的存在意义就是"没有第二种可能"。
 */
@Composable
fun HomeScreen(
    controller: EngineController,
    modifier: Modifier = Modifier,
    onPermissionNeeded: (() -> Unit)? = null,
) {
    // collectAsStateWithLifecycle 而不是 collectAsState: 切到后台时自动停止收集,
    // 不然一个每秒都在推流量数字的 StateFlow 会在后台一直唤醒界面重组, 白耗电。
    val status by controller.status.collectAsStateWithLifecycle()
    val scope = rememberCoroutineScope()
    val copyToClipboard = rememberCopyToClipboard()

    var localError by remember { mutableStateOf("") }
    var dismissedError by remember { mutableStateOf("") }
    var copied by remember { mutableStateOf(false) }
    var showNodes by remember { mutableStateOf(false) }
    var listBusy by remember { mutableStateOf(false) }

    val genericFailText = stringResource(R.string.action_failed)

    /** 把一次操作的结果落到"有没有错误要显示"上。成功就顺手清掉旧错误。 */
    fun noteResult(result: Result<Unit>) {
        localError = if (result.isSuccess) {
            ""
        } else {
            result.exceptionOrNull()?.message?.takeIf { it.isNotBlank() } ?: genericFailText
        }
    }

    fun launchAction(block: suspend () -> Result<Unit>) {
        scope.launch { noteResult(block()) }
    }

    // 错误原文: 引擎写的优先, 引擎没写就用界面兜底的
    val errorText = status.error.ifBlank { localError }
    val errorVisible = errorText.isNotBlank() && errorText != dismissedError

    // 复制成功的自提示。安卓 13+ 系统也会弹一个, 我们这条是给低版本兜底的,
    // 所以它只需要短暂出现, 不需要用户确认。
    LaunchedEffect(copied) {
        if (copied) {
            delay(1800)
            copied = false
        }
    }

    val uptimeText = if (status.uptimeSeconds >= 3600) {
        stringResource(
            R.string.uptime_hours_minutes,
            (status.uptimeSeconds / 3600).toInt(),
            ((status.uptimeSeconds % 3600) / 60).toInt(),
        )
    } else {
        formatClock(status.uptimeSeconds)
    }

    val buttonTitle: String
    val buttonCaption: String
    when (status.phase) {
        EnginePhase.Idle -> {
            buttonTitle = stringResource(R.string.power_title_idle)
            buttonCaption = stringResource(R.string.power_caption_idle)
        }

        EnginePhase.Connecting -> {
            buttonTitle = stringResource(R.string.power_title_connecting)
            // 用 if 而不是 message.ifBlank { stringResource(...) }: 后者是把
            // 可组合调用塞进一个 lambda 里, 容易踩到可组合上下文的问题
            buttonCaption = if (status.message.isNotBlank()) {
                status.message
            } else {
                stringResource(R.string.power_caption_connecting)
            }
        }

        EnginePhase.Disconnecting -> {
            buttonTitle = stringResource(R.string.power_title_disconnecting)
            buttonCaption = if (status.message.isNotBlank()) {
                status.message
            } else {
                stringResource(R.string.power_caption_disconnecting)
            }
        }

        EnginePhase.Connected -> {
            buttonTitle = stringResource(R.string.power_title_connected)
            buttonCaption = stringResource(R.string.power_caption_uptime, uptimeText)
        }

        EnginePhase.Error -> {
            buttonTitle = stringResource(R.string.power_title_error)
            // 圆钮里只放"点一下重试", 错误原文交给下面那张红卡片整段显示。
            //
            // 为什么不让圆钮直接显示 error: 圆盘里只有一百多 dp 宽, 而内核报错
            // 动不动三四十个字, 截成"内核启动失败: 端口 7890 已被占用。换…"
            // 正好把最关键的后半句("怎么办")吃掉 —— 用户看得见, 却用不上。
            // (长按这行小字依然能复制到完整原文。)
            buttonCaption = stringResource(R.string.power_caption_error)
        }
    }

    // 显式写出 () -> Unit: 不然最后一句 `copied = true` 会让 lambda 被推成
    // () -> Boolean, 而它是不能放到 () -> Unit 的位置上的
    val copyError: () -> Unit = {
        copyToClipboard(errorText)
        copied = true
    }

    Scaffold(
        modifier = modifier.fillMaxSize(),
        containerColor = MaterialTheme.colorScheme.background,
    ) { innerPadding ->
        // 用 Box 包一层是为了让底部抽屉有一个明确的层叠容器, 而不是和主内容
        // 平铺在 Scaffold 的 content 里各测各的。
        Box(modifier = Modifier.fillMaxSize()) {
            Column(
                modifier = Modifier
                    .fillMaxSize()
                    .padding(innerPadding)
                    // 小屏(比如 5 寸 16:9)上这一屏是放不下的, 所以整屏可滚。
                    // 大圆钮因此不能吃 weight(可滚动的 Column 给不了有限高度,
                    // 用 weight 会直接崩), 见下面 fillMaxWidth(0.76f)。
                    .verticalScroll(rememberScrollState())
                    .padding(horizontal = 20.dp),
                horizontalAlignment = Alignment.CenterHorizontally,
            ) {
                Spacer(modifier = Modifier.height(10.dp))
                Text(
                    text = stringResource(R.string.app_name),
                    style = MaterialTheme.typography.titleLarge,
                    color = MaterialTheme.colorScheme.primary,
                )
                Spacer(modifier = Modifier.height(2.dp))
                Text(
                    text = stringResource(R.string.app_tagline),
                    style = MaterialTheme.typography.labelMedium,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )

                Spacer(modifier = Modifier.height(22.dp))
                PowerButton(
                    phase = status.phase,
                    title = buttonTitle,
                    caption = buttonCaption,
                    busy = status.isBusy,
                    onClick = {
                        if (!status.vpnPermission && onPermissionNeeded != null) {
                            // 没授权就先要权限, 不直接 start: 直接 start 必然失败,
                            // 用户只会看到一条红字, 却不知道该点哪里。
                            // (真正的权限申请必须挂在 Activity 上, 所以由外部注入。)
                            onPermissionNeeded()
                        } else {
                            launchAction { controller.toggle() }
                        }
                    },
                    onCaptionLongPress = if (errorText.isNotBlank()) copyError else null,
                    // 占屏宽的 76%: 再大就顶到边, 再小就不像"主角"了
                    modifier = Modifier.fillMaxWidth(0.76f),
                )

                Spacer(modifier = Modifier.height(26.dp))

                if (!status.vpnPermission) {
                    VpnPermissionHint()
                    Spacer(modifier = Modifier.height(16.dp))
                }

                // 只要 error 非空就必须显示出来(契约要求, 不能静默)。
                // 出错态也一样显示 —— 圆钮放不下的原文, 这里整段给出来。
                if (errorVisible) {
                    ErrorBanner(
                        text = errorText,
                        copied = copied,
                        onCopy = copyError,
                        onDismiss = { dismissedError = errorText },
                    )
                    Spacer(modifier = Modifier.height(16.dp))
                }

                StatusCard(
                    node = status.node,
                    mode = status.mode,
                    nodeCount = status.nodeCount,
                    uploadBytes = status.uploadBytes,
                    downloadBytes = status.downloadBytes,
                )

                Spacer(modifier = Modifier.height(16.dp))

                ModePicker(
                    current = status.mode,
                    // 建隧道的过程中不让改分流模式: 内核正在读配置, 这时候改会
                    // 让"界面显示的模式"和"内核实际用的模式"对不上。
                    enabled = !status.isBusy,
                    onSelect = { mode -> noteResult(controller.setMode(mode)) },
                )

                Spacer(modifier = Modifier.height(16.dp))

                NodeEntry(
                    nodeCount = status.nodeCount,
                    onClick = { showNodes = true },
                )

                Spacer(modifier = Modifier.height(28.dp))
            }

            if (showNodes) {
                NodeSheet(
                    nodes = status.nodes,
                    nodeCount = status.nodeCount,
                    current = status.node,
                    listBusy = listBusy,
                    engineBusy = status.isBusy,
                    message = status.message,
                    onSelect = { name -> launchAction { controller.selectNode(name) } },
                    onRefresh = {
                        if (!listBusy) {
                            listBusy = true
                            scope.launch {
                                noteResult(controller.refreshNodes())
                                listBusy = false
                            }
                        }
                    },
                    onTestLatency = {
                        if (!listBusy) {
                            listBusy = true
                            scope.launch {
                                noteResult(controller.testLatency())
                                listBusy = false
                            }
                        }
                    },
                    // 选完不关抽屉: 用户往往是"试一个、不行、再试一个",
                    // 关掉再打开一次是纯浪费。
                    onDismiss = { showNodes = false },
                )
            }
        }
    }
}

/**
 * 首次启动的 VPN 授权引导。
 *
 * 为什么要专门写一段话: 安卓的 VPN 授权弹窗里写着"允许此应用接管你的网络流量",
 * 这句话对普通用户是恐怖的。第一次点开关的人如果在这里犹豫, 那这个 App 就失败了
 * —— 所以要在弹窗之前先解释清楚"它是什么、我们拿它做什么、不做什么"。
 *
 * 只在 `vpnPermission == false` 时出现, 授权后永久消失, 不留痕迹。
 */
@Composable
private fun VpnPermissionHint(modifier: Modifier = Modifier) {
    val contentColor = MaterialTheme.colorScheme.onSecondaryContainer
    HongxingCard(
        modifier = modifier,
        color = MaterialTheme.colorScheme.secondaryContainer,
    ) {
        Row(verticalAlignment = Alignment.Top) {
            Icon(
                imageVector = Icons.Rounded.Shield,
                contentDescription = null,
                tint = contentColor,
                modifier = Modifier.size(20.dp),
            )
            Spacer(modifier = Modifier.width(12.dp))
            Column {
                Text(
                    text = stringResource(R.string.vpn_hint_title),
                    style = MaterialTheme.typography.titleSmall,
                    color = contentColor,
                )
                Spacer(modifier = Modifier.height(4.dp))
                Text(
                    text = stringResource(R.string.vpn_hint_body),
                    style = MaterialTheme.typography.bodySmall,
                    color = contentColor.copy(alpha = 0.85f),
                )
            }
        }
    }
}

/** 节点列表入口。做成整行可点, 而不是一个小按钮 —— 大目标总比小目标好点。 */
@Composable
private fun NodeEntry(
    nodeCount: Int,
    onClick: () -> Unit,
    modifier: Modifier = Modifier,
) {
    Surface(
        modifier = modifier
            .fillMaxWidth()
            .clip(MaterialTheme.shapes.extraLarge)
            .clickable(onClick = onClick),
        shape = MaterialTheme.shapes.extraLarge,
        color = MaterialTheme.colorScheme.surfaceContainerLow,
    ) {
        Row(
            modifier = Modifier.padding(horizontal = 20.dp, vertical = 16.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Icon(
                imageVector = Icons.Rounded.Dns,
                contentDescription = null,
                tint = MaterialTheme.colorScheme.onSurfaceVariant,
                modifier = Modifier.size(20.dp),
            )
            Spacer(modifier = Modifier.width(14.dp))
            Column(modifier = Modifier.weight(1f)) {
                Text(
                    text = stringResource(R.string.nodes_entry_title),
                    style = MaterialTheme.typography.titleSmall,
                    color = MaterialTheme.colorScheme.onSurface,
                )
                Spacer(modifier = Modifier.height(2.dp))
                Text(
                    text = stringResource(R.string.nodes_entry_subtitle, nodeCount),
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
            Icon(
                imageVector = Icons.Rounded.ExpandMore,
                contentDescription = null,
                tint = MaterialTheme.colorScheme.onSurfaceVariant,
                modifier = Modifier.size(20.dp),
            )
        }
    }
}
