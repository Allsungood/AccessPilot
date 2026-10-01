package com.accesspilot.hongxing.core

import kotlinx.coroutines.flow.StateFlow

/**
 * 红杏 Android · 界面与引擎之间的**唯一**契约。
 *
 * 沿用桌面端 `accesspilot/control.py` 的做法: 界面代码只认这一层, 不直接碰
 * VpnService / mihomo 进程 / 配置文件。这样两边可以并行开发, 而且任何一个
 * 出问题都能被单独定位 —— 桌面端就是靠这条规矩, 才能让 GUI 在引擎反复重构
 * 的过程中一次都没跟着坏。
 *
 * 三条硬约定(改这个文件必须遵守):
 *  1. **状态只有一个来源**: 界面永远读 [EngineController.status], 不自己
 *     拼状态。否则"界面说已连接、实际隧道没起来"这类不一致迟早出现。
 *  2. **可预期的失败不抛异常**: 返回值里带 error, 界面只需要把它渲染出来。
 *     真正意外的情况(编程错误)才让它炸。
 *  3. **start/stop 必须幂等**: 用户会连点那个大开关。重复 start 不能起两个
 *     隧道, 重复 stop 不能报错。
 */

/** 引擎阶段。界面就靠这一个字段决定大圆钮显示什么。 */
enum class EnginePhase {
    /** 内核没在跑, 流量走直连 */
    Idle,

    /** 正在建立隧道(VpnService 授权 -> 起内核 -> 等 API 就绪) */
    Connecting,

    /** 隧道已建立, 流量被接管 */
    Connected,

    /** 正在停止(先停内核再断隧道, 顺序反了会漏流量) */
    Disconnecting,

    /** 出错了, 详情在 [EngineStatus.error] */
    Error,
}

/**
 * 一个节点。
 *
 * [aiCapable] 单独列出来是有原因的: 实测过"延迟最低的节点反而打不开 ChatGPT"
 * —— 出口落在香港时 OpenAI 直接 403, 而 X/Discord 全绿。所以"能上 X"绝不能
 * 当成"能上 ChatGPT", 界面上要分开标。
 */
data class NodeInfo(
    val name: String,
    val latencyMs: Int = -1,
    val alive: Boolean = false,
    val current: Boolean = false,
    val aiCapable: Boolean = false,
)

/**
 * 界面渲染一屏所需的全部状态。
 *
 * 刻意做成一个不可变 data class 而不是散落的 LiveData: Compose 里
 * `collectAsState()` 拿到新实例就重组, 不用关心哪个字段变了。
 */
data class EngineStatus(
    val phase: EnginePhase = EnginePhase.Idle,

    /** 系统是否已经授予 VPN 权限。没授予时点开关要先去要权限, 不能直接 start。 */
    val vpnPermission: Boolean = false,

    /** 当前出口节点名。空 = 还没选。 */
    val node: String = "",

    /** 节点池大小(界面显示"共 N 个节点")。 */
    val nodeCount: Int = 0,

    /** 节点列表。只有界面主动请求刷新时才去拉, 不在轮询里拉 —— 桌面端
     *  在这上面踩过坑: 免费池六千个节点时, 每次轮询都拉全量会把界面拖死。 */
    val nodes: List<NodeInfo> = emptyList(),

    /** 分流模式: rule(智能分流) / global(全局) / direct(直连)。 */
    val mode: String = "rule",

    /** 本次连接累计的上行/下行字节数, 来自内核 REST API。 */
    val uploadBytes: Long = 0,
    val downloadBytes: Long = 0,

    /** 本次连接已持续秒数。 */
    val uptimeSeconds: Long = 0,

    /** 给用户看的一句话状态(比如"正在逐个平台实测…")。 */
    val message: String = "",

    /** 非空就是出错了。界面必须显示出来, 不能静默。 */
    val error: String = "",
) {
    val isBusy: Boolean
        get() = phase == EnginePhase.Connecting || phase == EnginePhase.Disconnecting

    val isRunning: Boolean
        get() = phase == EnginePhase.Connected || phase == EnginePhase.Connecting
}

/**
 * 引擎控制器。UI 只依赖这个接口, 不依赖它的实现 —— 这样界面能在没有真内核的
 * 情况下用假实现跑起来(做预览、跑 UI 测试), 桌面端的 GUI 就是这么验收的。
 */
interface EngineController {

    /** 唯一的状态来源。界面 `collectAsState()` 它。 */
    val status: StateFlow<EngineStatus>

    /** 连接。幂等: 已经连着时直接返回成功, 不会起第二个隧道。 */
    suspend fun start(): Result<Unit>

    /** 断开。幂等。**必须先停内核再断隧道**, 反了会让在途流量走直连。 */
    suspend fun stop(): Result<Unit>

    /** 大开关: 连着就断, 没连就连。 */
    suspend fun toggle(): Result<Unit>

    /** 手动指定出口节点。 */
    suspend fun selectNode(name: String): Result<Unit>

    /** 刷新节点列表(重新读配置档 + 问内核要延迟)。 */
    suspend fun refreshNodes(): Result<Unit>

    /** 让内核把所有节点重测一遍延迟。 */
    suspend fun testLatency(): Result<Unit>

    /** 切换分流模式。 */
    fun setMode(mode: String): Result<Unit>

    /** 记下 VPN 授权结果(VpnService 的 onActivityResult 回调里调)。 */
    fun onVpnPermissionResult(granted: Boolean)
}
