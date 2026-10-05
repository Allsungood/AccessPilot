package com.accesspilot.hongxing.core

import android.app.Activity
import android.content.Context
import android.content.Intent
import android.net.VpnService
import androidx.core.content.ContextCompat
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.withContext
import kotlinx.coroutines.withTimeoutOrNull

/**
 * 红杏 Android · [EngineController] 的实现
 *
 * 这是界面和引擎之间那层契约 (`EngineState.kt`) 的唯一实现。界面只认
 * `EngineController` 这个接口, 所以它能拿一个假实现去跑预览和 UI 测试 ——
 * 桌面端就是靠这条规矩, 才能在引擎反复重构的过程中一次都没跟着坏。
 *
 * ## 它做的事: 把"界面的一次点击"翻译成"服务的一个 Intent"
 *
 * `VpnService.establish()` 是**服务的实例方法**, Activity 拿不到它。所以真正的
 * 建隧道逻辑在 [HongxingVpnService] 里, 这个类负责:
 *  1. 按需申请 VPN 授权 (`VpnService.prepare`);
 *  2. 给服务发 START/STOP;
 *  3. 把 [EngineRuntime.status] 这个唯一状态源暴露成接口要求的 `StateFlow`;
 *  4. 把"服务那边最终成功了没有"翻译回 `Result` —— 界面点完开关要立刻知道
 *     成败, 不能只靠看状态流。
 *
 * ## 为什么状态不在这里自己维护一份
 *
 * 服务可能在 Activity 被回收之后继续运行。谁维护状态, 谁就得在"界面重启"时
 * 把状态找回来 —— 那是重复的状态机, 两个人的实现迟早会漂移, 表现就是
 * "界面说连着呢, 其实早就断了"。所以状态只有 [EngineRuntime] 一份,
 * 这里只是转发。
 *
 * ## 关于可见性
 *
 * 类是 public 的, 因为它要被 Activity 构造; 但界面代码**应该**拿
 * [EngineController] 这个接口类型 ([create] 返回的就是它), 这样将来换实现
 * 时界面一行都不用改。
 */
class EngineControllerImpl(private val appContext: Context) : EngineController {

    /** 唯一状态源, 直接转发 [EngineRuntime]。 */
    override val status: StateFlow<EngineStatus> = EngineRuntime.status

    /**
     * 等 VPN 授权结果。
     *
     * 用 `CompletableDeferred` 而不是"轮询 status.vpnPermission": 授权对话框
     * 是**模态**的, 用户可能盯着它想十秒。轮询要么浪费 CPU, 要么在用户点
     * "拒绝"之后还要多转一圈才反应过来。
     */
    @Volatile
    private var permissionWaiter: CompletableDeferred<Boolean>? = null

    // ---------------------------------------------------------------- 连接

    /**
     * 连接。幂等 —— 已经连着时直接返回成功, 不会起第二个隧道。
     *
     * 授权缺失时这里会**先弹系统的 VPN 授权对话框, 并等它**。为什么不等下次:
     * 用户点的是"连接", 他期望的是连上, 而不是"弹了个框然后什么都没发生"。
     * 等他点完"确定", 同一次调用继续往下走, 隧道就起来了。
     */
    override suspend fun start(): Result<Unit> {
        val current = EngineRuntime.status.value
        if (current.phase == EnginePhase.Connected) return Result.success(Unit)

        if (!ensurePermission()) {
            return Result.failure(
                IllegalStateException("没有拿到 VPN 授权, 无法建立隧道 (用户取消或系统拒绝)"),
            )
        }

        // 先领一个这次的 id, 再发 Intent —— 顺序不能反: 服务可能非常快地
        // 完成并从 `finishAttempt(id)` 回来, 而那时我们还没开始等。
        val attempt = EngineRuntime.beginAttempt()

        return try {
            ContextCompat.startForegroundService(
                appContext,
                Intent(appContext, HongxingVpnService::class.java)
                    .setAction(HongxingVpnService.ACTION_START)
                    .putExtra(HongxingVpnService.EXTRA_ATTEMPT, attempt),
            )
        } catch (t: Throwable) {
            // Android 12+ 在后台启动前台服务会抛 ForegroundServiceStartNotAllowedException。
            // 这不是"连接失败", 而是"现在不能连" —— 文案要能让用户知道
            // 得先把 App 切到前台。
            val error = "无法启动 VPN 服务 (${t.javaClass.simpleName}): ${t.message}"
            // 这次尝试不会有人来回答了, 自己把它落地, 免得等的人白等到超时。
            EngineRuntime.finishAttempt(attempt, error)
            return Result.failure(IllegalStateException(error))
        }.let { awaitOutcome(attempt) }
    }

    /**
     * 等**这一次**连接出结果 (审计 K2)。
     *
     * ## 为什么不能再看状态流
     *
     * 改之前这里等的是 `status.first { it.phase != EnginePhase.Connecting }`,
     * 而那个谓词是对**当前值**求值的: `startForegroundService` 只是一个 binder
     * 调用, 返回时 `onStartCommand` 还没跑, 状态流里还是上一次留下的 `Idle`
     * 或 `Error`。于是
     *  - 第一次点开关: 当前是 `Idle`, 谓词立刻为真 → 界面马上弹"连接失败",
     *    而隧道其实正在起来 (用户看到红字和"正在连接…"同时出现);
     *  - 失败过一次之后: 当前是上一次的 `Error`, 于是把**上一次的错误文案**
     *    当成了这一次的结论。
     *
     * 现在只认 [EngineRuntime.AttemptState] 里 **id 相同** 且已落地的那个信号:
     * 上一次的结论 id 不同, 天然不可能满足这一次的等待。
     */
    private suspend fun awaitOutcome(attemptId: Long): Result<Unit> =
        withTimeoutOrNull(CONNECT_TIMEOUT_MS) {
            EngineRuntime.attempt.first { it.id == attemptId && it.settled }
        }.let { settled ->
            when {
                settled == null ->
                    Result.failure(IllegalStateException("连接超时, 请稍后重试"))
                settled.error != null ->
                    Result.failure(
                        IllegalStateException(settled.error.ifBlank { "连接失败" }),
                    )
                else -> Result.success(Unit)
            }
        }

    /**
     * 断开。幂等。界面上的大开关和通知栏的"断开"走的是同一条路。
     *
     * 同样会等服务给出结果 —— "点了断开但界面还显示已连接"是最让人不安的
     * 一种反馈, 比等一秒糟糕得多。
     */
    override suspend fun stop(): Result<Unit> {
        val current = EngineRuntime.status.value
        if (current.phase == EnginePhase.Idle && EngineRuntime.engine == null) {
            return Result.success(Unit)
        }

        val attempt = EngineRuntime.beginAttempt()

        return try {
            appContext.startService(
                Intent(appContext, HongxingVpnService::class.java)
                    .setAction(HongxingVpnService.ACTION_STOP)
                    .putExtra(HongxingVpnService.EXTRA_ATTEMPT, attempt),
            )
        } catch (t: Throwable) {
            // ## 为什么这里不能报"成功" (审计 N7)
            //
            // 改之前的注释是"服务都没了 = 隧道肯定也没了", 但这个前提在**这个**
            // 代码库里是错的: 隧道由两样东西撑着 —— 服务持有的 TUN fd, 和
            // `setsid()` 出去、活得比 App 进程还长的 mihomo。`startService` 抛异常
            // 只说明"没叫动服务" (Android 12+ 的后台启动限制是最常见的原因),
            // 完全可能服务还活着、隧道还开着。
            //
            // 所以这里退一步**在进程内**直接停: EngineRuntime 知道内核在哪, 能
            // 自己把它停掉; "关 TUN"这一步交给服务挂上来的回调 (只有它有那个
            // ParcelFileDescriptor)。然后按**事实**回答 —— 隧道真的没了才算成功。
            val inProcess = EngineRuntime.stop { EngineRuntime.requestTunnelDown() }
            val error = when {
                inProcess.isSuccess && !EngineRuntime.tunnelHeld -> null
                else -> buildString {
                    append("无法停止 VPN 服务 (${t.javaClass.simpleName})")
                    if (EngineRuntime.tunnelHeld) {
                        append("; 隧道可能还开着, 请在通知栏点「断开」")
                    } else {
                        append("; ").append(inProcess.exceptionOrNull()?.message ?: "请稍后重试")
                    }
                }
            }
            EngineRuntime.finishAttempt(attempt, error)
            if (error == null) Result.success(Unit) else Result.failure(IllegalStateException(error))
        }.let { awaitDisconnect(attempt) }
    }

    /**
     * 等**这一次**断开出结果。与 [awaitOutcome] 同源, 见那里的说明。
     *
     * 改之前这里等的是"阶段不是 Disconnecting", 而 `startService` 返回时阶段
     * 仍然是 `Connected` → 谓词立刻为真 → 在隧道**还没断开之前**就返回成功;
     * 如果当时是 `Error`, 则把上一次的错误当成这次的结果。
     */
    private suspend fun awaitDisconnect(attemptId: Long): Result<Unit> =
        withTimeoutOrNull(DISCONNECT_TIMEOUT_MS) {
            EngineRuntime.attempt.first { it.id == attemptId && it.settled }
        }.let { settled ->
            when {
                settled == null ->
                    Result.failure(IllegalStateException("断开超时, 内核可能没有退干净"))
                settled.error != null ->
                    Result.failure(IllegalStateException(settled.error.ifBlank { "断开失败" }))
                else -> Result.success(Unit)
            }
        }

    /** 大开关: 连着就断, 没连就连。 */
    override suspend fun toggle(): Result<Unit> =
        if (EngineRuntime.status.value.phase == EnginePhase.Connected) stop() else start()

    // ---------------------------------------------------------------- 节点

    override suspend fun selectNode(name: String): Result<Unit> = EngineRuntime.selectNode(name)

    override suspend fun refreshNodes(): Result<Unit> = EngineRuntime.refreshNodes()

    override suspend fun testLatency(): Result<Unit> = EngineRuntime.testLatency()

    override fun setMode(mode: String): Result<Unit> = EngineRuntime.setMode(mode)

    // ---------------------------------------------------------------- 授权

    override fun onVpnPermissionResult(granted: Boolean) {
        EngineRuntime.setVpnPermission(granted)
        permissionWaiter?.complete(granted)
        permissionWaiter = null
    }

    /**
     * 确保有 VPN 授权。没有就发起申请并等结果。
     *
     * `VpnService.prepare()` 在**已授权**时返回 `null`, 需要授权时返回一个
     * 必须由 Activity 启动的 Intent —— 这是这个 API 最别扭的地方, 也是
     * 为什么授权这件事必须由 Activity 参与 (这里用 `FLAG_ACTIVITY_NEW_TASK`
     * 从 Application context 启动)。
     */
    private suspend fun ensurePermission(): Boolean {
        val pending = VpnService.prepare(appContext)
        if (pending == null) {
            EngineRuntime.setVpnPermission(true)
            return true
        }

        // 每次申请都新建一个 waiter: 用户可能反复点连接、反复取消。
        // 复用同一个的话, 第二次申请会立刻拿到上一次的结果。
        val waiter = CompletableDeferred<Boolean>()
        permissionWaiter = waiter

        try {
            pending.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
            appContext.startActivity(pending)
        } catch (t: Throwable) {
            permissionWaiter = null
            // 没法弹出授权界面 (某些定制系统会拦)。如实返回 false,
            // 上层会把原因显示出来 —— 总比卡在这里等一个永远不会来的回调好。
            return false
        }

        // 用户在系统对话框上犹豫多久都合理, 但也不能无限等 —— 界面上的
        // 大按钮一直转圈同样是坏体验。
        val granted = withTimeoutOrNull(PERMISSION_TIMEOUT_MS) { waiter.await() } ?: false
        permissionWaiter = null
        EngineRuntime.setVpnPermission(granted)
        return granted
    }

    companion object {
        /**
         * 拿控制器。**推荐界面用这个** —— 返回的静态类型就是 [EngineController],
         * 界面拿不到实现里的私有能力, 将来换实现时它一行都不用改。
         *
         * (实现类本身也是 public 的, 直接 `EngineControllerImpl(context)` 也行;
         * 两条路等价。)
         */
        @JvmStatic
        fun create(context: Context): EngineController =
            EngineControllerImpl(context.applicationContext)

        /**
         * 申请 VPN 授权。给**还没有控制器**的场景用 (比如 MainActivity 一
         * 启动就做首次引导)。返回的 Intent 交给 `registerForActivityResult`
         * 启动, 结果再交给 [EngineController.onVpnPermissionResult]。
         */
        @JvmStatic
        fun prepareIntent(context: Context): Intent? = VpnService.prepare(context)

        /** 授权结果码 —— 和 `Activity.RESULT_OK` 的对应关系只在这里出现一次。 */
        @JvmStatic
        fun isGranted(resultCode: Int): Boolean = resultCode == Activity.RESULT_OK

        /**
         * 连接的等待上限。
         *
         * 必须**明显大于** [MihomoEngine.READY_TIMEOUT_MS] (60 秒), 否则这里会
         * 先超时、界面显示"连接超时", 而那边其实还在正常地等内核 —— 用户看到
         * 一个自相矛盾的状态 (刚说超时, 一秒后又连上了)。
         *
         * 90 秒 = 60 秒就绪等待 + 首次安装资源 (约 40 MB 复制) 的余量。
         * 之所以要有上限而不是无限等: 卡住时大按钮会一直转圈, 用户唯一的出路
         * 是杀 App —— 那更糟。
         */
        const val CONNECT_TIMEOUT_MS = 90_000L

        const val DISCONNECT_TIMEOUT_MS = 15_000L

        /** 等用户在系统授权对话框上做决定的上限。 */
        const val PERMISSION_TIMEOUT_MS = 120_000L
    }
}
