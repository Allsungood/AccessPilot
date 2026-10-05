package com.accesspilot.hongxing.core

import android.content.Context
import android.system.Os
import android.system.OsConstants
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.NonCancellable
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.coroutines.withContext
import kotlinx.coroutines.withTimeoutOrNull
import java.io.File
import java.util.concurrent.atomic.AtomicLong

/**
 * 红杏 Android · 引擎运行时
 *
 * 这是**唯一**持有真实状态的地方。隧道活在一个前台服务里, 而 Activity 随时
 * 可能被系统回收、转屏、被切到后台再回来 —— 如果状态由 Activity 持有, 这些
 * 时刻界面就会和服务对不上。所以状态放在一个进程级单例里, 服务和界面都只是
 * 它的使用者和观察者。
 *
 * ## 一个进程一个引擎
 *
 * [engine] 只有一份, 而且整个 start/stop 流程都串在 [gate] 这把锁上 —— 这是
 * "幂等"的**实现方式**, 不是靠调用方自觉。用户连点那个大开关时, 两次
 * `start()` 会在这里排队: 第二次进来看到 already connected 就直接返回成功,
 * 绝不会起第二个隧道。
 *
 * 之所以强调这点: 两个 mihomo 同时去绑 `127.0.0.1:9090` 只会有一个成功,
 * 另一个退出; 而两个 VpnService 实例抢同一个 TUN 的表现是隧道时通时断 ——
 * 这类 bug 在真机上极难复现, 在代码里堵死最省事。
 *
 * ## 生命周期
 *
 * 由 [HongxingVpnService] 在 `onCreate` 里 [init], 在 `onDestroy` 里
 * [shutdown]。界面侧拿到的 [status] 是一个 `StateFlow`, 所以即便界面晚于
 * 服务启动, 一订阅就能拿到当前值, 不存在"错过了一次状态变化"。
 */
internal object EngineRuntime {

    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Default)

    /** 所有状态变化的唯一出口。界面 `collectAsState()` 它。 */
    private val _status = MutableStateFlow(EngineStatus())
    val status: StateFlow<EngineStatus> = _status.asStateFlow()

    /** start/stop 的串行闸门, 见类注释。 */
    private val gate = Mutex()

    private var appContext: Context? = null
    private var store: SettingsStore? = null
    private var assets: AssetInstaller? = null

    /** 内核进程管理器。`null` = 还没 [init]。 */
    var engine: MihomoEngine? = null
        private set

    private var api: MihomoApi? = null

    /** 正在跑的那个"盯着内核死没死"的协程。 */
    private var watchdog: Job? = null

    /**
     * 服务手上还开着 TUN 吗。
     *
     * 这是"隧道到底还在不在"的**事实**, 由持有 fd 的 [HongxingVpnService] 维护。
     * 为什么需要它: 判断一次"断开"有没有真的成功, 只能看这个事实 —— 看
     * "服务还在不在"是错的 (见 [EngineControllerImpl.stop] 里那段)。
     */
    @Volatile
    var tunnelHeld: Boolean = false
        private set

    fun setTunnelHeld(held: Boolean) {
        tunnelHeld = held
    }

    /**
     * 内核**意外**死亡时的回调, 由服务在 `onCreate` 里挂上、`onDestroy` 里摘掉。
     *
     * 为什么需要它: fd 的所有者是服务, 只有它能关掉 TUN。而内核一死, 那个 TUN
     * 就变成一个"没有读者的接口"—— 所有流量被吸进去然后消失, 用户看到的是
     * 整台设备断网, 而界面只是从"已连接"变成"出错了"。关掉它, 流量至少能
     * 回到直连。
     */
    @Volatile
    var onEngineDied: (() -> Unit)? = null

    /**
     * "把 TUN 关掉"的请求方, 由持有 fd 的 [HongxingVpnService] 在 `onCreate` 里挂上。
     *
     * 为什么需要它: 关 TUN 是服务的能力 (只有它有那个 `ParcelFileDescriptor`),
     * 而 **`startService` 有可能失败** —— Android 12+ 的后台启动限制是最常见的
     * 原因。那种情况下界面仍然必须能把隧道断掉 (否则用户手上只剩"杀 App"),
     * 所以退路是: 由 [EngineControllerImpl] 在进程内直接调 [stop], 而"关 TUN"
     * 这一步通过这个回调完成 (审计 N7)。
     */
    @Volatile
    var tunnelCloser: (() -> Unit)? = null

    /** 请求服务关掉 TUN。没有服务时是空操作 (那时也没有 TUN 可关)。 */
    fun requestTunnelDown() {
        runCatching { tunnelCloser?.invoke() }
    }

    /** 当前连接开始的时间点, 用来算 uptime。 */
    @Volatile
    private var connectedAt: Long = 0L

    // ---------------------------------------------------------------- 初始化

    /**
     * 准备运行时。幂等 —— `onCreate` 可能被调用多次 (服务被杀后重启)。
     *
     * 刻意**不做**任何耗时操作 (解压资源、起进程) 在主线程上: 前台服务的
     * `onCreate` 有严格的时间预算, 在这里复制 40 MB 资源会被判定成 ANR。
     *
     * ## 但**必须**顺手把离线节点列表填上
     *
     * 用户的原始抱怨就是"节点列表缺失"。上一轮把根因定位在 `-d` 工作目录
     * 不一致, 那只解决了"内核找不到 nodes.yaml"; **还有一半没解决**:
     * [refreshNodes] 本来只在两条路径上被调用 ——
     *   1. `start()` 成功后 (`scope.launch { refreshNodes() }`)
     *   2. 界面上的"刷新"按钮
     * 也就是说: **内核没起来时, 除非用户主动点刷新, 列表永远是空的**。
     * 而对一个还没连上的用户来说,"有哪些节点可选"正是他点那个大开关之前
     * 最需要看到的东西 —— 否则就是在盲赌。
     *
     * 所以初始化时就异步跑一次 [refreshNodes]。它此时走的是离线分支
     * (只读 `nodes.yaml`, 不发任何网络请求), 代价是解析约 70,000 行 YAML,
     * 放在 [scope] 上不阻塞任何东西。
     */
    fun init(context: Context) {
        val app = context.applicationContext
        if (appContext === app) return
        appContext = app
        val s = SettingsStore(app)
        store = s
        assets = AssetInstaller(app, s)
        _status.update { it.copy(mode = s.mode, node = s.selectedNode) }
        loadOfflineNodes()
    }

    /**
     * 把离线节点列表填进状态。
     *
     * 用 [scope] 而不是新开一个作用域: 它和看门狗/流量轮询一样属于"引擎运行时
     * 的活", 意图上就该跟 [shutdown] 一起停。而且它是**一次性的短任务**,
     * 即使被取消也只是少填一次列表, 不会有副作用。
     *
     * 失败不报错: 这时内核可能压根没装过资源, 列表空是合理的初始状态,
     * 不该在用户还没做任何操作时就在界面上弹一条红字。
     */
    private fun loadOfflineNodes() {
        scope.launch {
            runCatching {
                val nodes = offlineNodes()
                if (nodes.isNotEmpty()) {
                    _status.update { it.copy(nodes = nodes, nodeCount = nodes.size) }
                }
            }
        }
    }

    val secret: String get() = requireStore().secret

    private fun requireStore(): SettingsStore =
        store ?: error(ENGINE_NOT_READY_MESSAGE)

    /**
     * 内核的工作目录 (mihomo 的 `-d`)。
     *
     * ## 它必须是 [Context.getFilesDir] 本身, 不能是它的子目录
     *
     * mihomo 的 geodata / rule-provider 路径都是**相对 `-d`** 解析的, 而
     * [AssetInstaller] 把资源解压到 `filesDir` 根下。这两处只要对不上, 内核
     * 就会找不到 `geosite.dat` 和 `nodes.yaml`。
     *
     * 这个坑真踩过: 第一版这里返回 `filesDir/mihomo`, 而资源装在 `filesDir`,
     * 于是界面上报 "节点列表缺失: …/files/mihomo/nodes.yaml (资源安装没跑完?)"
     * —— 看起来像安装逻辑坏了, 实际上是两边对"工作目录"的理解不一致。
     *
     * 所以**只有这一个地方**决定 `-d` 是哪儿, [AssetInstaller] 跟着它,
     * 谁都不许再各写一份。`profileInstalled` / `assets-staging` 这些也在同一个
     * 目录里, 但它们对 mihomo 是无害的 (它只按名字找自己要的文件)。
     */
    private fun workDir(): File = requireAppContext().filesDir

    /**
     * 拿 Application context, 没 [init] 过就给一句人话。
     *
     * 改之前这里是 `requireNotNull(appContext)`, 于是新装的应用 (服务从没起来过)
     * 点一下节点列表里的"刷新", 界面上显示的是 Kotlin 自己的
     * `Required value was null.` —— 一句对用户毫无意义、对我们也定位不到东西的
     * 英文内部错误。根因 (没 init) 已经在 `MainActivity.onCreate` 里补掉了
     * (审计 N6), 但这里也不该再把内部实现漏到界面上: `refreshNodes` 会把这条
     * 消息原样显示在红色横幅里, 所以它必须是一句用户能看懂、也知道该干什么的话。
     */
    private fun requireAppContext(): Context =
        appContext ?: error(ENGINE_NOT_READY_MESSAGE)

    /** 公开的 workDir, 给 fd 实验 ([FdProbe]) 用 —— 它要和服务用同一个目录。 */
    fun workDir(context: Context): File = context.applicationContext.filesDir

    /**
     * fd 传递实验的报告。
     *
     * 用进程内的一个字段来传, 而不是让 `MainActivity` 直接等结果: 实验跑在
     * 服务的协程里 (因为建 TUN 必须由真实例做), 而 Activity 只想拿到那段
     * 文本打 logcat。`@Volatile` 是因为一个写、另一个读, 跨线程。
     */
    @Volatile
    var fdProbeReport: String? = null

    // ------------------------------------------------------ 一次尝试的结论 (K2)

    /**
     * 一次"连接"或"断开"尝试的结论。
     *
     * ## 为什么不能靠 [status] 推断
     *
     * 界面点一下大开关, 走的是 `startForegroundService` —— 它**只是一个 binder
     * 调用**, 返回时 `onStartCommand` 还没跑, 状态流里很可能还是上一次留下的
     * `Idle` 或 `Error`。所以任何形如
     * `status.first { it.phase != Connecting }` 的等待, 求值的对象是**当前值**,
     * 于是:
     *  - 第一次点: 当前是 `Idle`, 谓词立刻为真 → 界面弹"连接失败", 而隧道其实
     *    正在起来 (审计 K2 的用户可见症状);
     *  - 失败过一次之后: 当前是上一次的 `Error`, 于是**上一次的错误文案**被当成了
     *    这一次的结论。
     *
     * 所以每次尝试都带一个自增 id: 请求方发出 Intent 之前先 [beginAttempt] 拿到
     * 自己的 id, 服务做完之后用同一个 id [finishAttempt]。等待方只认
     * "id 相同 **且** 已落地"的那个信号 —— 上一次的结论 id 不同, 天然不可能满足
     * 这一次的等待。
     */
    data class AttemptState(
        /** 自增 id; 0 = 还没有过任何尝试。 */
        val id: Long = 0L,
        /** false = 还在进行中。 */
        val settled: Boolean = false,
        /** `settled && error != null` = 这一次失败了, 里面是给用户看的原因。 */
        val error: String? = null,
    )

    private val attemptSeq = AtomicLong(0L)

    private val _attempt = MutableStateFlow(AttemptState())
    val attempt: StateFlow<AttemptState> = _attempt.asStateFlow()

    /** 开一次新尝试并返回它的 id。调用方负责把它随 Intent 带给服务。 */
    fun beginAttempt(): Long {
        val id = attemptSeq.incrementAndGet()
        _attempt.value = AttemptState(id = id, settled = false)
        return id
    }

    /**
     * 给某一次尝试落结论。
     *
     * @param id [beginAttempt] 返回的那个; `<= 0` = 这次不是界面发起的
     *           (比如通知栏那个"断开"按钮), 没有人等结论, 直接忽略。
     */
    fun finishAttempt(id: Long, error: String?) {
        if (id <= 0L) return
        _attempt.update { cur ->
            // 只有"当前这一次"能被写结论。加入这个判断是为了挡住迟到的旧结论:
            // 上一次尝试的收尾如果落在新一次开始之后, 不加判断就会把用户正在等的
            // 这一次直接判死 (或者判活)。
            if (cur.id == id && !cur.settled) AttemptState(id, settled = true, error = error) else cur
        }
    }

    // ------------------------------------------------------------ VPN 授权

    /**
     * 记下 VPN 授权结果。
     *
     * 单独一个字段而不是每次去问 `VpnService.prepare()`: 那个调用会弹系统
     * 对话框, 不能在渲染路径上碰。授权结果由 Activity 的回调推进来。
     */
    fun setVpnPermission(granted: Boolean) {
        _status.update { it.copy(vpnPermission = granted) }
    }

    // ---------------------------------------------------------------- 连接

    /**
     * 建立隧道。幂等。
     *
     * ## 顺序是硬性的, 反了会出两种很难查的问题
     *  1. **先 establish 再起内核**。反过来内核会因为 `file-descriptor` 指向
     *     一个还不存在的 fd 直接退出。
     *  2. **内核确认就绪才算成功**。`establish()` 成功只说明系统给了我们一个
     *     TUN —— 内核万一没起来, 隧道是通的但没人转发, 表现是"连上了但什么
     *     都打不开", 用户会去怀疑节点, 而真正的问题在几千行之外。
     *
     * @param establish 由 VpnService 提供的、同步返回 TUN fd 的回调
     * @param onTunnelUp 隧道**确认可用之后**才能做的事 (比如 startForeground)
     * @return 成功 = 真的连上了;
     *         失败 = 隧道已经收干净, 调用方应该让服务退到后台;
     *         **成功但值是错误文案** = 隧道其实没起来, 但 fd 还在调用方手上,
     *         必须由它决定怎么收尾 (见返回类型说明)
     */
    suspend fun start(
        establish: () -> Int,
        onTunnelUp: (() -> Unit)? = null,
    ): Result<String> = gate.withLock {
        val current = _status.value
        if (current.phase == EnginePhase.Connected) return@withLock Result.success("")
        if (current.phase == EnginePhase.Connecting) return@withLock Result.success("")

        _status.update {
            it.copy(phase = EnginePhase.Connecting, error = "", message = "正在准备规则数据…")
        }

        var failure: String? = null
        /** fd 是不是已经真的建立起来了 —— 决定失败时隧道该不该关。 */
        var fdEstablished = false
        /** 这一次起的那个内核。**spawn 之前就挂上**, 见下面的注释。 */
        var mgr: MihomoEngine? = null

        try {
            val st = requireStore()
            val installer = requireNotNull(assets)

            // ---- 先把上一轮留下的内核清掉 (审计 K3) ----
            //
            // mihomo 是 setsid() 出去的长驻进程, **它活得比 App 进程长**。App 被
            // 系统杀掉之后, 内核会继续跑着、继续占着 9090 和它那份 TUN。这时候
            // 再起一个新的: 新的绑不上 9090 直接退出, 而 awaitReady 的 /version
            // 探测被**那个孤儿**回答了 —— 界面显示"已连接"(绿的), 但真正在服务
            // 9090 的是拿着旧 TUN 的孤儿, 新隧道没有读者。用户看到的是"连上了
            // 但什么都打不开", 而且看门狗一秒后又把它翻成"内核意外退出"。
            //
            // 所以顺序是: 先杀旧的, 再建新的。清不干净就**不要开始** —— 带着一个
            // 会回答 /version 的孤儿开局, 得到的必然是一个假的"已连接"。
            val leftover = clearLeftoverCore()
            if (leftover != null) {
                failure = leftover
                return@withLock Result.success(leftover)
            }

            // 资源安装放在最前面: 内核起不来最常见的原因就是缺 geodata / ruleset,
            // 而它的报错长得像配置语法错误, 会把人带偏。
            withContext(Dispatchers.IO) { installer.ensureInstalled() }

            val work = workDir()
            work.mkdirs()

            val template = requireAppContext().assets
                .open(ConfigBuilder.TEMPLATE_ASSET).bufferedReader().use { it.readText() }
            val nodesFile = File(work, AssetInstaller.NODES_FILE)
            if (!nodesFile.isFile) {
                failure = "节点列表缺失: ${nodesFile.absolutePath} (资源安装没跑完?)"
                return@withLock Result.success(failure!!)
            }
            val nodesText = nodesFile.readText()

            // fd 要在拼配置**之前**拿到: 配置里的 tun.file-descriptor 就是它。
            val fd = establish()
            if (fd < 0) {
                failure = "系统没有返回可用的 TUN 文件描述符 (establish 返回 $fd)"
                return@withLock Result.success(failure!!)
            }
            fdEstablished = true

            val configText = ConfigBuilder.build(template, nodesText, fd, st.secret)
            val configFile = ConfigBuilder.writeTo(work, configText)

            _status.update { it.copy(message = "正在启动内核…") }

            val apiClient = MihomoApi(st.secret)
            val core = MihomoEngine(apiClient)
            mgr = core

            // **先把 handle 挂到自己身上, 再去等在就绪上** (审计 N4)。
            //
            // awaitReady 最长会挂 60 秒。这段时间里服务可能被销毁 (scope.cancel),
            // 协程可能被取消, 也可能 OOM —— 只要 handle 还是 null, 下面 finally
            // 里的清理就没有对象可清。而刚 fork 出来的 mihomo 是 setsid 出去的,
            // 它不会跟着 App 一起死: 它会带着 TUN fd 一直活着, 顺手把 9090 占住,
            // 下一次连接就变成 K3 那个"孤儿回答 /version"的现场。
            engine = core
            api = apiClient
            core.spawn(
                exePath = MihomoEngine.resolveExecutable(
                    requireAppContext().applicationInfo.nativeLibraryDir,
                ),
                workDir = work,
                configPath = configFile,
                tunFd = fd,
            )

            // 就绪探测返回三种结论, 不是一个 Boolean —— 因为「内核没起来」和
            // 「内核好着、是我们的请求被明文策略拦了」这两件事的**修法完全不同**,
            // 而旧的 Boolean 把它们压成同一个 false, 于是只能靠猜。
            var apiNote = ""
            when (val outcome = core.awaitReady(onProgress = { waited ->
                    // 让界面上的"正在启动内核…"带上秒数: 首次启动要加载
                    // geodata + 规则集, 几十秒是正常的, 但一个不动的转圈
                    // 会被当成卡死 —— 而用户唯一的出路是杀 App, 那更糟。
                    _status.update { it.copy(message = "正在启动内核… ${waited}s") }
                })) {
                is ReadyOutcome.Ready -> Unit

                // 内核在听 TCP, 只是我们的 HTTP 打不通。
                //
                // **这里绝对不能 stop()。** 改之前就是那样: 一路等到超时,
                // 然后把内核杀掉 —— 用户失去的是一条本来能用的隧道, 只因为
                // 我们的控制面连不上它。降级成"能上网, 但节点切换不可用"
                // 显然好得多。
                is ReadyOutcome.ApiBlocked -> {
                    lastReadyProbe = "TCP 已就绪但 REST 不通 " +
                        "(等了 ${outcome.waitedMs}ms): ${outcome.lastError}"
                    android.util.Log.w("HongxingMain", "内核就绪探测: $lastReadyProbe")
                    apiNote = "内核已启动，但控制接口连不上，节点切换暂不可用"
                }

                else -> {
                    // 内核起了又死 / 或者压根没起来。日志是唯一能说明原因的东西,
                    // 所以把它接在错误信息后面 —— 让用户能直接截图给我们。
                    val log = core.tailLog(LOG_IN_ERROR_BYTES)
                    // 顺带把"就绪探测"那一步的**原始错误**也记下来: "内核启动失败"
                    // 和"内核起来了但我们连不上它的 REST API"是两件完全不同的事,
                    // 而它们的表象一模一样。没有这一行就只能靠猜。
                    val probe = apiClient.version().fold(
                        onOk = { "OK (version=$it)" },
                        onErr = { "失败: $it" },
                    )
                    val why = when (outcome) {
                        is ReadyOutcome.ProcessDied -> "内核进程已退出"
                        is ReadyOutcome.PortNeverOpened ->
                            "内核存活, 但控制端口始终没开 (已等 ${outcome.waitedMs}ms)"
                        else -> "未知原因"
                    }
                    lastReadyProbe = "pid=${core.pid} 存活=${core.isAlive()} /version $probe"
                    android.util.Log.w("HongxingMain", "内核就绪探测: $lastReadyProbe")
                    core.stop()
                    failure = buildString {
                        append("内核启动失败或提前退出")
                        append("\n").append(why)
                        append("\n就绪探测: /version ").append(probe)
                        if (log.isNotBlank()) append("\n").append(log)
                    }
                    return@withLock Result.success(failure!!)
                }
            }

            connectedAt = System.currentTimeMillis()

            _status.update {
                it.copy(
                    phase = EnginePhase.Connected,
                    vpnPermission = true,
                    error = "",
                    // 正常情况下是空的; 只有"内核好着但 REST 连不上"时才会
                    // 带一句话上来 —— 那种情况我们必须连上, 但要让用户知道
                    // 节点切换暂时用不了, 而不是等他点了没反应才发现。
                    message = apiNote,
                    mode = st.mode,
                    node = st.selectedNode,
                )
            }

            // startForeground 之类的事情由调用方做。它失败了不该把已经
            // 起来的隧道拆掉 —— 那是两个独立的问题, 混在一起只会更难查。
            runCatching { onTunnelUp?.invoke() }

            startWatchdog(core)
            startTrafficPolling()

            // 连上之后顺手把节点列表拉一次。放在启动路径的**末尾**:
            // 它是锦上添花, 不该拖慢"隧道可用"这个关键路径, 失败了也不影响连接。
            scope.launch { runCatching { refreshNodes() } }

            Result.success("")
        } catch (e: CancellationException) {
            // **取消不是失败, 但它必须继续往上走。**
            //
            // 改之前这里是 `catch (t: Throwable)`, 把 CancellationException 一起
            // 吞掉了: 协程被取消之后调用方收不到取消信号, 会以为这次启动"正常
            // 结束了"; 更糟的是 finally 里那些本该在"取消"路径上跑的清理会被
            // 当成普通失败路径来跑 (审计 N4)。所以这里先记下现场, 再原样抛出。
            failure = ENGINE_CANCELED_MESSAGE
            throw e
        } catch (t: Throwable) {
            // 意外错误 (编程错误、OOM 之类)。走到这里说明 fd 可能已经建好,
            // 必须让调用方知道要去关隧道。
            failure = t.message ?: t.javaClass.simpleName
            Result.failure(t)
        } finally {
            if (failure != null) {
                // 失败/取消路径的收尾: 把可能已经 fork 出来的内核收掉, 别留下
                // 一个"没有隧道却在跑"的孤儿进程占着 9090 端口。
                //
                // 注意用的是**非挂起**的 [MihomoEngine.killBlocking] 而不是
                // stop(): 取消路径上协程已经是 cancelled 状态, 任何 suspend 调用
                // 都会立刻抛 CancellationException, 于是"清理"变成空操作 —— 而
                // mihomo 是 setsid 出去的, 它会一直活着 (审计 N4 的核心)。
                if (mgr != null && !killAndCheck(mgr)) {
                    // 杀不掉: 留下 mihomo.pid 当线索, 下一次启动的
                    // clearLeftoverCore 会再试一遍, 并且会因此拒绝开始连接
                    // (带着一个会回答 /version 的孤儿开局 = 假的"已连接")。
                    android.util.Log.w("HongxingMain", "启动失败收尾时没能杀掉内核 pid=${mgr.pid}")
                }
                if (engine === mgr) {
                    engine = null
                    api = null
                }
                shutdown()
                // fd 还在调用方手上 (它才持有 ParcelFileDescriptor), 这里
                // 只如实报出"隧道没起来", 由它决定关不关 —— 但状态必须
                // 说真话: 显示 Idle 而隧道还开着, 就等于把流量悄悄送进黑洞。
                _status.update {
                    it.copy(
                        phase = if (fdEstablished) EnginePhase.Error else EnginePhase.Idle,
                        error = failure.orEmpty(),
                        message = "",
                    )
                }
                // 失败现场落文件。为什么不能只靠 logcat: 实测这台 ROM 上应用
                // 自己的 Log.i 根本不进 logcat (见 writeSelfCheck 的说明), 而
                // "为什么没连上"恰恰是最需要现场的那个问题。
                writeSelfCheck()
            }
        }
    }

    /**
     * 把上一轮留下的内核清掉。返回非 null = 没清干净, 里面是给用户看的原因。
     *
     * 判据有两层, 缺一不可:
     *  1. **内存里还追踪着的那个** ([engine]) —— 只有"内核停不掉时保留了隧道"
     *     那条路会留下它;
     *  2. **pid 文件里的那个** —— 这是 App 进程被杀过之后唯一的线索。
     *     但 pid 会循环复用, 所以杀之前必须确认 `/proc/<pid>/cmdline` 里
     *     真的是 libmihomo: 杀错一个无关进程的后果比留着一个孤儿严重得多
     *     (Android 上 pid 复用得很快, 而 1 号进程以下的进程我们都没有权限,
     *      但"没有权限"不等于"不该检查")。
     */
    private suspend fun clearLeftoverCore(): String? {
        engine?.let { prev ->
            // 用 NonCancellable 包住: 这一步是"必须做完的清理", 不能因为调用方
            // 的取消而在半路上停下 —— 停下来就留下一个占着 9090 的孤儿。
            val stopped = withContext(NonCancellable) { prev.stop() }
            if (!stopped) {
                return "上一个内核还没退干净 (pid=${prev.pid}), " +
                    "请先在通知栏点「断开」再重连"
            }
            engine = null
            api = null
        }
        shutdown()

        val pidFile = File(workDir(), MihomoEngine.PID_FILE_NAME)
        val stale = runCatching { pidFile.readText().trim().toIntOrNull() }.getOrNull()
        if (stale != null && stale > 1) {
            if (MihomoEngine.isCoreProcess(stale)) {
                android.util.Log.w("HongxingMain", "清掉上次留下的内核 pid=$stale")
                // 放到 IO 线程上: 这里面有 Thread.sleep (`killPidBlocking` 在等
                // /proc 里的进程消失), 而 start() 的协程跑在主线程上 —— 不能在
                // 这条路上再添一处卡界面 (本来这里唯一能接受的花费是"连接慢一点")。
                withContext(Dispatchers.IO) { killPidBlocking(stale, STALE_KILL_GRACE_MS) }
            } else {
                // cmdline 对不上 = pid 已经被别的进程复用了。只删文件, 不杀。
                android.util.Log.w("HongxingMain", "mihomo.pid 里的 $stale 不是我们的内核, 跳过")
            }
        }
        runCatching { pidFile.delete() }

        // 端口上还有人在应答吗。判据是**裸 TCP 连接** (不经过任何策略), 所以
        // 它问的是"9090 上到底有没有监听者"。有的话, 这一轮连接注定会被它骗过去
        // (awaitReady 只看 /version 有没有 200), 于是我们会给用户一个绿色的
        // "已连接", 而真正在服务 9090 的是别人。宁可现在就报清楚。
        val apiProbe = MihomoApi(requireStore().secret)
        if (withContext(NonCancellable) { apiProbe.isPortOpen() }) {
            return "控制端口 ${MihomoApi.DEFAULT_PORT} 上还有别的进程在应答, " +
                "现在连接会被它骗成「已连接」, 请重启手机后再试"
        }
        return null
    }

    /**
     * 杀掉一个**不是自己子进程**的残留内核 (上一轮 App 进程已经死了, 它被 init
     * 收养), 并且等它真的从 /proc 里消失。
     *
     * 收尸不归我们: 我们不是它的父进程, `waitpid` 只会返回 ECHILD。init 会负责。
     */
    private fun killPidBlocking(pid: Int, graceMs: Long) {
        runCatching { Os.kill(pid, OsConstants.SIGTERM) }
        var waited = 0L
        while (waited < graceMs && File("/proc/$pid").exists()) {
            Thread.sleep(KILL_POLL_MS)
            waited += KILL_POLL_MS
        }
        if (File("/proc/$pid").exists()) {
            runCatching { Os.kill(pid, OsConstants.SIGKILL) }
            waited = 0L
            while (waited < graceMs && File("/proc/$pid").exists()) {
                Thread.sleep(KILL_POLL_MS)
                waited += KILL_POLL_MS
            }
        }
    }

    /**
     * 非挂起地杀掉内核并回报有没有杀掉。
     *
     * 返回 false = 它还在跑 (调用方要把 pid 记进 [pendingReapPids])。
     */
    private fun killAndCheck(mgr: MihomoEngine): Boolean =
        runCatching { mgr.killBlocking() }.getOrDefault(false)

    // ---------------------------------------------------------------- 断开

    /**
     * 断开隧道。幂等。
     *
     * 顺序与 [start] 严格相反: **先停内核**, 再让调用方关 TUN。
     *
     * ## 为什么这个顺序不能反
     *
     * 先关 fd 的话, 内核手上那个 fd 会瞬间变成悬空的 —— 它已经建立的连接会
     * 失去出口。这时候如果系统还有在途流量 (TUN 关闭本身是有延迟的), 那些
     * 包会因为找不到路由而**走直连**出去。对一个代理客户端来说这是最不能
     * 接受的一类 bug: 用户以为流量在隧道里, 实际上目标 IP 直接暴露了。
     *
     * ## 内核停不掉时不关隧道
     *
     * 这是一个刻意的选择。内核没退干净就关 TUN, 会同时得到"隧道关了"和
     * "内核还在跑并占着 9090"两个坏结果, 下次连接必然失败。所以这时候
     * 宁可保持隧道开着、状态报错、让 [HongxingVpnService] 继续做前台服务,
     * 用户至少能看到错误并对症处理 (再点一次断开 / 杀掉 App)。
     *
     * @param onTunnelDown 由 VpnService 提供的、关闭 TUN fd 的回调。
     *                     **只有在内核确认停掉之后**才会被调用。
     */
    suspend fun stop(onTunnelDown: (() -> Unit)? = null): Result<Unit> = gate.withLock {
        val current = _status.value
        if (current.phase == EnginePhase.Idle && engine == null) {
            // 幂等: 本来就没连。但仍然要保证 TUN 是关的 —— 服务可能因为
            // 别的原因留着 fd, 那次调用方自己负责。
            runCatching { onTunnelDown?.invoke() }
            return@withLock Result.success(Unit)
        }
        if (current.phase == EnginePhase.Disconnecting) return@withLock Result.success(Unit)

        _status.update {
            it.copy(phase = EnginePhase.Disconnecting, message = "正在断开…", error = "")
        }

        var failure: String? = null
        try {
            shutdown()

            val mgr = engine
            engine = null
            api = null

            // 先停内核 (见上面的顺序说明)
            val kernelStopped = mgr == null || mgr.stop()

            if (kernelStopped) {
                // 内核确认停掉之后, 才轮到调用方关 TUN。
                runCatching { onTunnelDown?.invoke() }
            } else {
                failure = "内核进程没有在预期时间内退出 (pid=${mgr?.pid}), " +
                    "已保留隧道以免流量走直连, 请重试断开"

                // **内核还活着, 所以继续追踪它** (审计 N3 的连带项)。
                //
                // 改之前这里把 engine 扔成 null 就完事了, 于是:
                //  - 用户按提示"重试断开"时 mgr == null → 我们只关了 TUN,
                //    却把 mihomo 留成了一个占着 9090 的孤儿 —— 下一次连接
                //    正好会被它骗成"已连接" (K3);
                //  - 看门狗被 [shutdown] 取消之后没人再管它, 它什么时候自己
                //    退的、退了几次, 我们一无所知。
                // 把它留着 + 重启看门狗, 两件事一起解决。
                engine = mgr
                startWatchdog(mgr)
            }
        } catch (t: Throwable) {
            failure = t.message ?: t.javaClass.simpleName
        } finally {
            connectedAt = 0L
            if (failure != null) {
                _status.update {
                    it.copy(phase = EnginePhase.Error, error = failure, message = "")
                }
            } else {
                _status.update {
                    it.copy(
                        phase = EnginePhase.Idle,
                        message = "",
                        error = "",
                        uploadBytes = 0,
                        downloadBytes = 0,
                        uptimeSeconds = 0,
                        nodeCount = 0,
                        nodes = emptyList(),
                    )
                }
            }
        }

        if (failure != null) Result.failure(IllegalStateException(failure)) else Result.success(Unit)
    }

    // ---------------------------------------------------------------- 旁观

    /**
     * 盯着内核进程, 它意外死了要立刻反映到状态里。
     *
     * 为什么不能只在操作时检查: 内核会因为内存压力、配置里的某个节点让它崩、
     * 或者被用户在开发者选项里"停止后台进程"而随时消失。没有这个看门狗的话,
     * 界面上会一直显示"已连接", 而实际上所有流量都在黑洞里 —— 用户完全
     * 无从判断该不该重连。
     */
    private fun startWatchdog(mgr: MihomoEngine) {
        watchdog?.cancel()
        watchdog = scope.launch {
            while (true) {
                kotlinx.coroutines.delay(WATCHDOG_INTERVAL_MS)

                // 顺手看一眼日志大小 (一次 stat, 代价可以忽略)。见 [MihomoEngine.capLogFile]:
                // 内核按 info 级别跑, 每条 dial 失败都写一行, 挂几天能到几百 MB,
                // 而这里只读最后 8 KB —— 没有上限的日志是纯粹的磁盘泄漏。
                mgr.capLogFile()

                if (!mgr.isAlive()) {
                    // **先收尸, 再丢引用** (审计 N3)。
                    //
                    // 顺序不能反: 一旦下面把 engine 置成 null, 内存里就再也没有
                    // 这个 pid 了, 而僵尸进程会一直占着 pid 表项活到 App 进程结束
                    // —— "反复连断几百次之后 fork 不出来, 重启 App 才好" 正是这么
                    // 攒出来的。原来的代码在这条路上什么都没做。
                    val exit = mgr.reap()

                    // 只有"当前跑的仍然是这一份内核"时才改状态 ——
                    // 否则一次正常的 stop + start 会被旧实例的看门狗误报成崩溃。
                    if (engine === mgr) {
                        val log = mgr.tailLog(LOG_IN_ERROR_BYTES)
                        engine = null
                        api = null
                        connectedAt = 0L
                        _status.update {
                            it.copy(
                                phase = EnginePhase.Error,
                                error = buildString {
                                    append("内核进程意外退出")
                                    if (exit >= 0) append(" (退出码 $exit)")
                                    if (log.isNotBlank()) append("\n").append(log)
                                },
                                message = "",
                            )
                        }
                        // 内核没了, 但 TUN fd 还在服务手上 —— 那个接口现在没有
                        // 读者, 所有流量被吸进去就消失 (用户看到的是整台设备
                        // 断网)。fd 只有服务能关, 所以由它回调过来收尾。
                        runCatching { onEngineDied?.invoke() }
                    }
                    return@launch
                }
            }
        }
    }

    /**
     * 起一个每秒刷新的流量/时长协程。
     *
     * 为什么 1 秒一次: 界面上的速率和时长都是给人看的, 再快人眼也分辨不出来,
     * 而每次轮询要过一次 HTTP + JSON —— 高频轮询会和隧道抢 CPU, 得不偿失。
     */
    fun startTrafficPolling() {
        if (trafficJob?.isActive == true) return
        trafficJob = scope.launch {
            while (true) {
                kotlinx.coroutines.delay(TRAFFIC_INTERVAL_MS)
                val client = api ?: continue
                val result = client.traffic()
                if (result is ApiResult.Ok) {
                    val t = result.value
                    val up = connectedAt.takeIf { it > 0 }
                        ?.let { (System.currentTimeMillis() - it) / 1000 } ?: 0
                    _status.update {
                        it.copy(
                            uploadBytes = t.uploadBytes,
                            downloadBytes = t.downloadBytes,
                            uptimeSeconds = up,
                        )
                    }
                }
            }
        }
    }

    private var trafficJob: Job? = null

    // ---------------------------------------------------------------- 节点

    /**
     * 拉节点列表。
     *
     * ## 内核没在跑的时候不能只报错
     *
     * 第一版这里是"内核没跑 → 直接返回失败", 结果是**连之前界面上一共 0 个节点**,
     * 用户点那个大开关等于盲赌, 而且看起来就是个 bug (用户就是这么报的)。
     *
     * 节点列表本来就躺在设备上 (`nodes.yaml` + 生成好的配置里有策略组的成员
     * 清单), 没有理由看不到。所以内核没起来时[退化到离线列表][offlineNodes]:
     * 名字都有, 只是没有延迟数据 (界面显示"未测速")。内核一起来就以 REST API
     * 为准 —— **离线那份绝不能盖掉内核的真实数据**, 否则用户会看到一份永远
     * 不更新、也没有延迟的假列表。
     */
    suspend fun refreshNodes(): Result<Unit> = withContext(Dispatchers.IO) {
        val client = api
        if (client == null) {
            // 离线路径。它同样不该让调用方炸掉 —— 拿不到就返回空列表 + 说明。
            return@withContext try {
                val nodes = offlineNodes()
                _status.update {
                    it.copy(
                        nodes = nodes,
                        nodeCount = nodes.size,
                        message = if (nodes.isEmpty()) "" else "",
                    )
                }
                Result.success(Unit)
            } catch (t: Throwable) {
                Result.failure(t)
            }
        }

        val group = SELECTOR_GROUP
        val result = client.proxies(group)
        when (result) {
            is ApiResult.Ok -> {
                val nodes = result.value
                val current = nodes.firstOrNull { it.current }?.name ?: _status.value.node
                _status.update {
                    it.copy(nodes = nodes, nodeCount = nodes.size, node = current)
                }
                runCatching { store?.selectedNode = current }
                Result.success(Unit)
            }
            is ApiResult.Err -> {
                // 内核在跑但 API 没答上 —— 这可能是刚开始启动。退到离线列表,
                // 至少界面上不是一片空白; 错误仍然如实报出来。
                val fallback = runCatching { offlineNodes() }.getOrDefault(emptyList())
                _status.update {
                    it.copy(
                        nodes = fallback.ifEmpty { it.nodes },
                        nodeCount = fallback.size.takeIf { n -> n > 0 } ?: it.nodeCount,
                        error = result.error,
                    )
                }
                Result.failure(IllegalStateException(result.error))
            }
        }
    }

    /**
     * 离线节点列表 —— 不依赖内核, 直接从已安装的资源里读。
     *
     * ## 为什么读两份文件而不是只读 nodes.yaml
     *
     * `nodes.yaml` 是给配置用的 **proxy 定义列表** (`- name:` + `type:` + …),
     * 里面每一项都是节点。看起来只读它就够了 —— 但那样读出来的列表和内核
     * REST API 给的**不是一回事**: 内核给的只是"主策略组里能选的那些", 而
     * `nodes.yaml` 里有全部节点定义, 两者不一定相等。
     *
     * 为了"连之前看到的"和"连之后看到的"尽量一致 (不然列表会在连上的瞬间
     * 突然变样, 用户会以为节点丢了), 这里取**两者的交集**: 以生成好的配置里
     * 策略组的成员清单为准, 用 `nodes.yaml` 兜底 (配置还没生成时, 比如
     * 装完 App 还没点过连接)。
     */
    private fun offlineNodes(): List<NodeInfo> {
        val dir = workDir()

        // 首选: 生成好的配置里, 所有策略组成员并集 —— 这正是内核会报给我们的
        // 那一组名字 (含 "♻️ 自动选择" 这类组内组, 界面上也是可选项)。
        val config = File(dir, ConfigBuilder.CONFIG_FILE_NAME)
        if (config.isFile) {
            val members = ConfigBuilder.parseGroupMembers(config.readText())
            if (members.isNotEmpty()) {
                return members.map { name ->
                    NodeInfo(name = name, latencyMs = -1, alive = false, current = false)
                }
            }
        }

        // 兜底: 直接列 nodes.yaml 里的节点名。
        val nodesFile = File(dir, AssetInstaller.NODES_FILE)
        if (!nodesFile.isFile) return emptyList()
        return ConfigBuilder.parseProxyNames(nodesFile.readText()).map { name ->
            NodeInfo(name = name, latencyMs = -1, alive = false, current = false)
        }
    }

    /** 手动选节点。内核没在跑时只记下来, 下次连接生效。 */
    suspend fun selectNode(name: String): Result<Unit> = withContext(Dispatchers.IO) {
        if (name.isBlank()) return@withContext Result.failure(
            IllegalArgumentException("节点名不能为空"),
        )
        runCatching { requireStore().selectedNode = name }
        _status.update { it.copy(node = name) }

        val client = api ?: return@withContext Result.success(Unit) // 没连着, 记住就行
        when (val r = client.selectNode(SELECTOR_GROUP, name)) {
            is ApiResult.Ok -> {
                // 立刻把 current 标记刷成本地这份, 不等下一次拉列表 ——
                // 否则界面上的选中态会滞后一两秒, 看起来像没点动。
                _status.update { st ->
                    st.copy(nodes = st.nodes.map { it.copy(current = it.name == name) })
                }
                Result.success(Unit)
            }
            is ApiResult.Err -> {
                _status.update { it.copy(error = r.error) }
                Result.failure(IllegalStateException(r.error))
            }
        }
    }

    /** 切换分流模式。 */
    fun setMode(mode: String): Result<Unit> {
        if (mode !in MihomoApi.VALID_MODES) {
            return Result.failure(IllegalArgumentException("非法模式: $mode"))
        }
        runCatching { requireStore().mode = mode }
        _status.update { it.copy(mode = mode) }

        val client = api ?: return Result.success(Unit)
        scope.launch {
            when (val r = client.setMode(mode)) {
                is ApiResult.Ok -> Unit
                is ApiResult.Err -> _status.update { it.copy(error = r.error) }
            }
        }
        return Result.success(Unit)
    }

    /** 让内核重测所有节点延迟。见 [MihomoApi.testDelay] 关于批量的说明。 */
    suspend fun testLatency(): Result<Unit> = withContext(Dispatchers.IO) {
        val client = api ?: return@withContext Result.failure(
            IllegalStateException("内核没在跑, 先连接"),
        )
        val names = _status.value.nodes.map { it.name }
        if (names.isEmpty()) {
            return@withContext refreshNodes()
        }

        _status.update { it.copy(message = "正在逐个平台实测…") }
        val result = client.testDelay(SELECTOR_GROUP, names)
        _status.update { it.copy(message = "") }

        when (result) {
            is ApiResult.Ok -> {
                val delays = result.value
                _status.update { st ->
                    st.copy(
                        nodes = st.nodes.map { n ->
                            val d = delays[n.name] ?: -1
                            n.copy(latencyMs = d, alive = d >= 0)
                        },
                    )
                }
                Result.success(Unit)
            }
            is ApiResult.Err -> {
                _status.update { it.copy(error = result.error) }
                Result.failure(IllegalStateException(result.error))
            }
        }
    }

    // ---------------------------------------------------------------- 自检

    /** 记录最近一次"就绪探测"的结果, 给 [selfCheck] 和状态错误文案用。 */
    @Volatile
    private var lastReadyProbe: String = ""

    /**
     * 诊断信息。给"连不上"这类问题一个能一键复制的现场。
     *
     * 包含 fd 传递这条路的关键事实 —— 启动桥有没有加载、内核在哪、日志多长。
     * 没有这个, 用户报"连不上"时我们只能靠猜。
     */
    fun selfCheck(): String = buildString {
        appendLine("启动桥: " + if (NativeLauncher.available) "已加载" else "未加载 (${NativeLauncher.loadError})")
        appendLine("引擎: " + (engine?.let { "pid=${it.pid} 存活=${it.isAlive()}" } ?: "未运行"))
        // 隧道到底还在不在 —— 排查"断开之后上不了网"时, 这一行是第一现场:
        // fd 没关 = tun0 和 0.0.0.0/0 路由还挂在一个没有读者的接口上。
        appendLine("TUN: " + if (tunnelHeld) "仍开着 (服务持有 fd)" else "已关闭")
        appendLine("阶段: ${_status.value.phase}")
        appendLine("VPN 授权: ${_status.value.vpnPermission}")
        appendLine("错误: ${_status.value.error.ifBlank { "(无)" }}")
        appendLine("节点: count=${_status.value.nodeCount} list=${_status.value.nodes.size}")
        appendLine("本次尝试: id=${_attempt.value.id} 已落地=${_attempt.value.settled}")
        if (lastReadyProbe.isNotBlank()) appendLine("就绪探测: $lastReadyProbe")
        val ctx = appContext
        if (ctx != null) {
            appendLine("内核路径: " + MihomoEngine.resolveExecutable(ctx.applicationInfo.nativeLibraryDir))
            appendLine("工作目录: " + workDir().absolutePath)
            appendLine(
                "工作目录内容: " + workDir().list()?.sorted()?.joinToString(", ").orEmpty(),
            )
        }
        val log = engine?.tailLog(2 * 1024).orEmpty()
        if (log.isNotBlank()) {
            appendLine("--- 内核日志尾部 ---")
            append(log)
        }
    }

    /**
     * 把自检写进一个文件, 供 `adb shell run-as <pkg> cat files/selfcheck.txt` 读取。
     *
     * ## 为什么需要它 (而不是只打 logcat)
     *
     * 实测这台华为 ROM 上**应用自己的 `Log.i` 根本不进 logcat** ——
     * `adb logcat -s HongxingMain:V` 一条都没有, 而同一个进程里的
     * `wm_on_create_called` 之类系统日志却正常。于是"引擎为什么没起来"
     * 这个问题在 adb 侧完全不可见, 只能靠猜。
     *
     * 落文件绕开了这一层: 不依赖 ROM 的日志策略, 也不需要设备有 root。
     */
    fun writeSelfCheck(): File? {
        val ctx = appContext ?: return null
        return runCatching {
            File(ctx.filesDir, SELF_CHECK_FILE).apply { writeText(selfCheck()) }
        }.getOrNull()
    }

    // ---------------------------------------------------------------- 收尾

    /**
     * 进程级收尾。
     *
     * watchdog / 流量协程一起停掉: 它们是 `Dispatchers.Default` 上永不退出
     * 的循环, 留着会让这个单例在服务重启后出现两份轮询。
     *
     * @param killCore 是否**连内核一起杀掉**。服务 `onDestroy` 必须传 true:
     *   它是非挂起的 (onDestroy 里没有等待的余地, 而且调用点可能已经在取消
     *   路径上, 任何 suspend 都会立刻抛), 而且是唯一能保证"服务没了就不会
     *   留下一个带着 TUN fd 的 mihomo"的地方 —— 改之前 `onDestroy` 只停两个
     *   协程, 内核是 setsid 出去的, 它会带着 TUN 和 9090 一直活到下一次启动
     *   (审计 N4 + K3)。
     *
     *   而 [stop] 自己调这里时**不能**传 true: 那条路要走"先 SIGTERM 让它落
     *   缓存, 再 SIGKILL"的优雅流程, 而不是在这里被人从背后一枪打死。
     */
    fun shutdown(killCore: Boolean = false) {
        watchdog?.cancel()
        watchdog = null
        trafficJob?.cancel()
        trafficJob = null

        if (killCore) {
            val mgr = engine
            engine = null
            api = null
            if (mgr != null && !killAndCheck(mgr)) {
                // 杀不掉时**不要**删 mihomo.pid: 那里面是唯一的线索, 下一次
                // 启动的 clearLeftoverCore 还要靠它把残留内核认出来。
                android.util.Log.w("HongxingMain", "onDestroy 没能杀掉内核 pid=${mgr.pid}")
            }
        }
    }

    /**
     * 主策略组名。模板 `config.template.yaml:197` 里定义的第一个组。
     *
     * 写死而不是"取第一个 select 类型的组": 模板里还有"🤖 AI 服务"等好几个
     * select 组, 靠顺序猜会在模板调整时静默选错 —— 而选错的后果是用户切了
     * 节点却没生效。名字对不上时宁可报错 (见 [MihomoApi.proxies] 的提示)。
     */
    const val SELECTOR_GROUP = "🚀 节点选择"

    private const val WATCHDOG_INTERVAL_MS = 1_000L
    private const val TRAFFIC_INTERVAL_MS = 1_000L
    private const val LOG_IN_ERROR_BYTES = 1_200

    /**
     * 清残留内核时的等待上限 (SIGTERM 之后给它多久; SIGKILL 之后同样再等一次)。
     *
     * 调用点在 `Dispatchers.IO` 上 (见 [clearLeftoverCore]), 所以这里的等待不会
     * 卡界面; 但它仍然要小 —— 真的遇到杀不掉的残留内核时, 宁可早一点报
     * "清不干净, 请重启手机", 也不要让用户对着一个转圈的按钮等下去。
     */
    private const val STALE_KILL_GRACE_MS = 1_200L
    private const val KILL_POLL_MS = 100L

    /**
     * 没 [init] 就来读引擎数据时的提示。
     *
     * 必须是**用户能看懂的一句话**: 它会经 `refreshNodes` → `Result` →
     * `HomeScreen.noteResult` 原样显示在红色横幅里。改之前这里是
     * `requireNotNull(appContext)`, 界面上显示的是 Kotlin 自己的
     * `Required value was null.` (审计 N6)。
     */
    const val ENGINE_NOT_READY_MESSAGE = "引擎还在启动中, 请退出重进一次 App 再试"

    /** 启动过程中被取消时写进状态的原因 (取消本身仍然会继续往上抛)。 */
    const val ENGINE_CANCELED_MESSAGE = "连接已取消"

    /** [writeSelfCheck] 落地的文件名。 */
    const val SELF_CHECK_FILE = "selfcheck.txt"
}
