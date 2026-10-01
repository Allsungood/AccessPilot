@file:OptIn(ExperimentalFoundationApi::class)

package com.accesspilot.hongxing.ui.preview

import androidx.annotation.StringRes
import androidx.compose.foundation.ExperimentalFoundationApi
import androidx.compose.foundation.clickable
import androidx.compose.foundation.combinedClickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.statusBarsPadding
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.graphicsLayer
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.unit.dp
import com.accesspilot.hongxing.R
import com.accesspilot.hongxing.core.EngineController
import com.accesspilot.hongxing.core.EnginePhase
import com.accesspilot.hongxing.core.EngineStatus
import com.accesspilot.hongxing.core.NodeInfo
import com.accesspilot.hongxing.ui.HongxingRoot
import com.accesspilot.hongxing.ui.component.MODE_RULE
import com.accesspilot.hongxing.ui.theme.HongxingTheme
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlin.random.Random

/**
 * 红杏 Android · 假引擎(只看界面时用)。
 *
 * 为什么必须有这个文件:
 * 真引擎要把 mihomo 编译进 so、起前台服务、拿到 VPN 授权之后才会动, 而界面改一行
 * 间距就得上真机跑一遍的话, 一天改不了几版。这个假实现把 [EngineController] 的
 * 六个命令全部用延时 + 定时器演出来 —— 界面拿到的状态流和真引擎**一模一样**,
 * 所以它既能给 Compose Preview 用, 也能直接装到手机上点着看。
 *
 * 它不是 mock: 它**真的**会经历"连接中 → 已连接 → 每秒涨流量"这个过程, 只差没有
 * 真的给你翻墙。这样界面上所有动效、时长、流量格式化的 bug 都能在这里被抓出来。
 *
 * 铁律: 这个文件只能在 `ui/` 里被引用, 绝不能被 `MainActivity` 或任何发布路径
 * 接上 —— 否则用户会看到一个"显示已连接但其实什么都没发生"的 App, 那是最糟的
 * 一种 bug(用户以为自己在受保护)。
 */
class FakeEngineController(
    initial: EngineStatus = FakeEngineScenarios.connected(),
) : EngineController {

    // Main.immediate: 真引擎的状态也是主线程改的(前台服务回调), 这里保持一致,
    // 免得将来把假引擎接进界面时冒出一堆并发时序差异。
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main.immediate)

    private val _status = MutableStateFlow(initial)
    override val status: StateFlow<EngineStatus> = _status.asStateFlow()

    /** 连接/断开/切换节点这类"过程"任务, 同一时刻只允许有一个。 */
    private var transition: Job? = null

    /** 已连接时的每秒心跳: 涨时长、涨流量。 */
    private var ticker: Job? = null

    // 固定种子: 每次启动的假数据一样, 截图对比才有意义;
    // 但同一个实例里连续调用会往前走, 所以"测速"看起来是真的重测了。
    private val rng = Random(20260101)

    override suspend fun start(): Result<Unit> {
        // 幂等: 已经在跑就直接成功, 不能起第二条隧道
        if (_status.value.isRunning) return Result.success(Unit)

        if (!_status.value.vpnPermission) {
            val message = FAKE_ERR_NO_PERMISSION
            _status.update { it.copy(phase = EnginePhase.Error, error = message) }
            return Result.failure(IllegalStateException(message))
        }

        transition?.cancel()
        _status.update {
            it.copy(
                phase = EnginePhase.Connecting,
                error = "",
                message = FAKE_MSG_TUNNEL,
                uptimeSeconds = 0,
                uploadBytes = 0,
                downloadBytes = 0,
            )
        }
        transition = scope.launch {
            delay(900)
            _status.update { it.copy(message = FAKE_MSG_PROBE) }
            delay(800)
            _status.update { it.copy(phase = EnginePhase.Connected, message = "") }
            startTicker()
        }
        return Result.success(Unit)
    }

    override suspend fun stop(): Result<Unit> {
        if (_status.value.phase == EnginePhase.Idle) return Result.success(Unit)

        transition?.cancel()
        ticker?.cancel()
        _status.update { it.copy(phase = EnginePhase.Disconnecting, message = FAKE_MSG_TEARDOWN) }
        transition = scope.launch {
            delay(600)
            _status.update {
                it.copy(
                    phase = EnginePhase.Idle,
                    message = "",
                    uptimeSeconds = 0,
                    uploadBytes = 0,
                    downloadBytes = 0,
                )
            }
        }
        return Result.success(Unit)
    }

    override suspend fun toggle(): Result<Unit> =
        if (_status.value.isRunning) stop() else start()

    override suspend fun selectNode(name: String): Result<Unit> {
        val wasRunning = _status.value.isRunning
        _status.update { current ->
            current.copy(
                node = name,
                nodes = current.nodes.map { it.copy(current = it.name == name) },
            )
        }
        // 换出口 = 内核重载配置, 真机上会短暂断流。这里演出来, 是为了让界面
        // "切换时圆钮会转一下"这条逻辑在假引擎下也能被看到。
        if (wasRunning) {
            transition?.cancel()
            _status.update { it.copy(phase = EnginePhase.Connecting, message = switchingMessage(name)) }
            transition = scope.launch {
                delay(700)
                _status.update { it.copy(phase = EnginePhase.Connected, message = "") }
            }
        }
        return Result.success(Unit)
    }

    override suspend fun refreshNodes(): Result<Unit> {
        _status.update { it.copy(message = FAKE_MSG_REFRESH) }
        delay(900)
        val nodes = FakeEngineScenarios.withFreshLatency(rng)
        val currentNode = _status.value.node.ifBlank { nodes.firstOrNull()?.name.orEmpty() }
        _status.update {
            it.copy(
                nodes = nodes.map { node -> node.copy(current = node.name == currentNode) },
                nodeCount = nodes.size,
                node = currentNode,
                message = "",
            )
        }
        return Result.success(Unit)
    }

    override suspend fun testLatency(): Result<Unit> {
        _status.update { it.copy(message = FAKE_MSG_LATENCY) }
        delay(1100)
        _status.update { current ->
            current.copy(
                nodes = current.nodes.mapIndexed { index, node ->
                    node.copy(latencyMs = freshLatency(index, rng))
                },
                message = "",
            )
        }
        return Result.success(Unit)
    }

    override fun setMode(mode: String): Result<Unit> {
        _status.update { it.copy(mode = mode) }
        return Result.success(Unit)
    }

    override fun onVpnPermissionResult(granted: Boolean) {
        _status.update {
            it.copy(
                vpnPermission = granted,
                error = if (granted && it.phase == EnginePhase.Error) "" else it.error,
            )
        }
    }

    /**
     * 开发用: 直接跳到某个状态, 不走"连接中"的过程。
     *
     * 截图验收要的是四张确定状态的图, 而不是等它自己演 —— 所以这个入口是必需的。
     * 跳到已连接时会顺手把心跳接上, 所以截图里的时长和流量是活的。
     */
    fun devSet(next: EngineStatus) {
        transition?.cancel()
        ticker?.cancel()
        _status.value = next
        if (next.phase == EnginePhase.Connected) startTicker()
    }

    private fun startTicker() {
        ticker?.cancel()
        ticker = scope.launch {
            while (isActive) {
                delay(1000)
                _status.update { current ->
                    if (current.phase != EnginePhase.Connected) {
                        current
                    } else {
                        current.copy(
                            uptimeSeconds = current.uptimeSeconds + 1,
                            uploadBytes = current.uploadBytes + rng.nextInt(2_000, 90_000).toLong(),
                            downloadBytes = current.downloadBytes + rng.nextInt(20_000, 900_000).toLong(),
                        )
                    }
                }
            }
        }
    }
}

/**
 * 预置场景。截图/自检时按这个顺序走一遍, 界面上的四种状态就都覆盖到了。
 *
 * 节点数据照现实造, 有三点刻意为之:
 *  * 延迟分成绿/黄/红/未知四档, 一屏就能看出配色对不对;
 *  * 有两个节点是死的不通, 空状态之外的"不可用"行也得被看到;
 *  * **香港节点不给 AI 标记**, 而美日新有 —— 现实里 OpenAI 对香港落地直接 403,
 *    假数据要是"哪里都快哪里都能用 AI", 就会把界面上的错误结论验成"正确"。
 */
object FakeEngineScenarios {

    /**
     * 冷启动的数据源要**声明在 `nodes` 前面**。
     *
     * Kotlin 的 object 是按声明顺序初始化的, 而 `nodes` 的初始化会调用
     * [buildNodes] —— 如果它读到的 [NODE_NAMES] 还声明在后面, 那一刻它还是
     * null, 结果就是 App 一启动就 NPE。这个坑只有把数据提到前面才躲得掉。
     */
    private val NODE_NAMES = listOf(
        "香港 01 · IPLC 专线", "香港 02 · BGP 中转", "香港 03 · 流媒体解锁",
        "日本 东京 01", "日本 东京 02 · 直连", "日本 大阪 01",
        "新加坡 01", "新加坡 02 · 直连",
        "台湾 台北 01", "韩国 首尔 01",
        "美国 洛杉矶 01", "美国 圣何塞 02", "美国 纽约 03", "美国 西雅图 04",
        "英国 伦敦 01", "德国 法兰克福 01", "法国 巴黎 01", "荷兰 阿姆斯特丹 01",
        "加拿大 多伦多 01", "澳大利亚 悉尼 01", "土耳其 伊斯坦布尔 01", "阿联酋 迪拜 01",
        "印度 孟买 01", "马来西亚 吉隆坡 01", "泰国 曼谷 01", "越南 胡志明市 01",
        "菲律宾 马尼拉 01", "俄罗斯 莫斯科 01", "波兰 华沙 01", "西班牙 马德里 01",
        "意大利 米兰 01", "巴西 圣保罗 01", "南非 约翰内斯堡 01", "阿根廷 布宜诺斯艾利斯 01",
    )

    /** OpenAI 会 403 掉的落地。香港延迟最低但用不了 ChatGPT, 这个坑必须显式演出来。 */
    private val AI_FRIENDLY = listOf(
        "美国", "日本", "新加坡", "台湾", "韩国", "英国", "德国", "加拿大", "澳大利亚",
    )

    private fun buildNodes(): List<NodeInfo> {
        val seed = Random(7)
        return NODE_NAMES.mapIndexed { index, name ->
            NodeInfo(
                name = name,
                latencyMs = freshLatency(index, seed),
                alive = index % 13 != 7,
                current = index == 0,
                aiCapable = AI_FRIENDLY.any { name.contains(it) } && index % 3 != 2,
            )
        }
    }

    /** 34 个节点: 够触发滚动, 又不会让手点不到底。 */
    val nodes: List<NodeInfo> = buildNodes()

    fun idle(): EngineStatus = base(nodes.first().name)

    fun connecting(): EngineStatus = base(nodes.first().name).copy(
        phase = EnginePhase.Connecting,
        message = FAKE_MSG_TUNNEL,
    )

    fun connected(): EngineStatus = base(nodes.first().name).copy(
        phase = EnginePhase.Connected,
        uptimeSeconds = 754,
        uploadBytes = 3_842_000,
        downloadBytes = 128_400_000,
    )

    fun error(): EngineStatus = base(nodes.first().name).copy(
        phase = EnginePhase.Error,
        error = FAKE_ERR_KERNEL,
    )

    fun noPermission(): EngineStatus = EngineStatus(
        phase = EnginePhase.Idle,
        vpnPermission = false,
        node = nodes.first().name,
        nodeCount = nodes.size,
        nodes = nodes.mapIndexed { index, node -> node.copy(current = index == 0) },
        mode = MODE_RULE,
    )

    fun emptyNodes(): EngineStatus = EngineStatus(
        phase = EnginePhase.Idle,
        vpnPermission = true,
        node = "",
        nodeCount = 0,
        nodes = emptyList(),
        mode = MODE_RULE,
    )

    /** 开发切换器里的按钮顺序 = 从"最正常"到"最异常"。 */
    val all: List<DevScenario> = listOf(
        DevScenario(R.string.dev_state_connected) { connected() },
        DevScenario(R.string.dev_state_connecting) { connecting() },
        DevScenario(R.string.dev_state_idle) { idle() },
        DevScenario(R.string.dev_state_error) { error() },
        DevScenario(R.string.dev_state_no_permission) { noPermission() },
        DevScenario(R.string.dev_state_no_nodes) { emptyNodes() },
    )

    /** 重测延迟: 保留节点名和存活性, 只换延迟数字。 */
    fun withFreshLatency(random: Random): List<NodeInfo> =
        nodes.mapIndexed { index, node -> node.copy(latencyMs = freshLatency(index, random)) }

    private fun base(currentNodeName: String): EngineStatus = EngineStatus(
        phase = EnginePhase.Idle,
        vpnPermission = true,
        node = currentNodeName,
        nodeCount = nodes.size,
        nodes = nodes.map { it.copy(current = it.name == currentNodeName) },
        mode = MODE_RULE,
    )
}

/**
 * 延迟造数: 四档都要有, 否则界面上"绿/黄/红/灰"四种配色只有一种能被看到。
 * `-1` 是契约里"还没测到"的表示法(`NodeInfo.latencyMs` 默认值)。
 */
private fun freshLatency(index: Int, random: Random): Int = when {
    index % 11 == 3 -> -1
    index % 5 == 0 -> random.nextInt(400, 900)
    index % 3 == 0 -> random.nextInt(150, 399)
    else -> random.nextInt(35, 149)
}

/** 一个可切换的预置状态。 */
data class DevScenario(
    @StringRes val labelRes: Int,
    val build: () -> EngineStatus,
)

/**
 * 开发用状态切换器: 右上角一个 DEV 小胶囊, 点开就是一排状态。
 *
 * 为什么做成机上切换而不是 Compose Preview:
 * 这台机器上没有 `ui-tooling-preview` 依赖(加依赖要动 build.gradle.kts, 不归界面
 * 这边管), 而且**配色和动效只有装到真机上看才算数** —— Preview 渲染不了字体
 * 回退、深色模式下的对比度、圆钮的呼吸动效。所以直接给一个能在手机上按的开关,
 * 验收截图靠它切换四种状态。
 *
 * 正式接线时不要调用它, 折叠状态下它只是一个 5dp 的小胶囊, 但仍然是多余的。
 *
 * 操作: 点一下展开/收起; **长按淡到 12%** —— 截图验收时它不该出现在画面里,
 * 但又不能真的收起来(不然没法切下一个状态)。再长按一次恢复。
 */
@Composable
fun DevStateSwitcher(
    controller: FakeEngineController,
    modifier: Modifier = Modifier,
) {
    var expanded by remember { mutableStateOf(false) }
    var dimmed by remember { mutableStateOf(false) }
    Column(
        modifier = modifier,
        horizontalAlignment = Alignment.End,
        verticalArrangement = Arrangement.spacedBy(6.dp),
    ) {
        Surface(
            modifier = Modifier
                .graphicsLayer { alpha = if (dimmed) 0.12f else 1f }
                .combinedClickable(
                    onClick = { expanded = !expanded },
                    onLongClick = { dimmed = !dimmed },
                ),
            shape = RoundedCornerShape(percent = 50),
            color = MaterialTheme.colorScheme.inverseSurface.copy(alpha = 0.86f),
        ) {
            Text(
                text = stringResource(R.string.dev_switcher_title),
                style = MaterialTheme.typography.labelSmall,
                color = MaterialTheme.colorScheme.inverseOnSurface,
                modifier = Modifier.padding(horizontal = 10.dp, vertical = 5.dp),
            )
        }
        if (expanded) {
            FakeEngineScenarios.all.forEach { scenario ->
                Surface(
                    modifier = Modifier.clickable {
                        controller.devSet(scenario.build())
                        expanded = false
                    },
                    shape = RoundedCornerShape(percent = 50),
                    color = MaterialTheme.colorScheme.surfaceContainerHighest,
                ) {
                    Text(
                        text = stringResource(scenario.labelRes),
                        style = MaterialTheme.typography.labelMedium,
                        color = MaterialTheme.colorScheme.onSurface,
                        modifier = Modifier.padding(horizontal = 12.dp, vertical = 6.dp),
                    )
                }
            }
        }
    }
}

/**
 * 装上手机看界面用的完整壳子。MainActivity 临时换成:
 * ```
 * setContent { FakeEnginePreviewHost() }
 * ```
 * 就有假引擎 + 状态切换器, 四张验收截图都能出。
 */
@Composable
fun FakeEnginePreviewHost(modifier: Modifier = Modifier) {
    val controller = remember { FakeEngineController(FakeEngineScenarios.connected()) }
    HongxingTheme {
        Box(modifier = modifier.fillMaxSize()) {
            HongxingRoot(controller = controller)
            DevStateSwitcher(
                controller = controller,
                modifier = Modifier
                    .align(Alignment.TopEnd)
                    .statusBarsPadding()
                    .padding(12.dp),
            )
        }
    }
}

/*
 * 假引擎自己"说"的话。
 *
 * 它们字面上是硬编码中文, 但**不是界面文案**: 契约里 `status.message` 和
 * `status.error` 是引擎给的数据(真实现里来自 mihomo 日志和 REST API), 界面只
 * 负责渲染。界面文案全部在 strings.xml —— 两者不要混为一谈。
 */
private const val FAKE_MSG_TUNNEL = "正在建立隧道…"
private const val FAKE_MSG_PROBE = "正在校验出口节点…"
private const val FAKE_MSG_TEARDOWN = "正在收尾，让流量回到直连…"
private const val FAKE_MSG_REFRESH = "正在刷新节点列表…"
private const val FAKE_MSG_LATENCY = "正在逐个节点实测延迟…"
private const val FAKE_ERR_NO_PERMISSION = "系统还没有授予 VPN 权限，先在弹窗里点「确定」"
private const val FAKE_ERR_KERNEL =
    "内核启动失败：端口 7890 已被占用。换个端口，或者先关掉占用它的程序再试一次。"

private fun switchingMessage(name: String): String = "正在切换到 $name…"
