package com.accesspilot.hongxing.core

import android.system.Os
import android.system.OsConstants
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.withContext
import kotlinx.coroutines.withTimeoutOrNull
import java.io.File
import java.io.IOException

/**
 * 红杏 Android · 内核进程管理
 *
 * 负责 mihomo 子进程的整个生命周期: 起它、盯它、停它。
 * 它**不碰** VpnService, 也**不碰**界面状态 —— 那两件事分别在
 * [HongxingVpnService] 和 [EngineRuntime] 里, 这样进程管理这块能单独测。
 *
 * ## 为什么用 [NativeLauncher] 而不是 `ProcessBuilder`
 *
 * 唯一的原因: 要把 TUN 文件描述符交给子进程。Java 的 `ProcessBuilder` 做不到,
 * 它在 exec 前会把编号 >= 4 的 fd 全部关掉 (详细推导见 [NativeLauncher] 的
 * 类注释)。这个决定不是风格偏好, 是没有第二条路。
 *
 * 代价是拿不到 `java.lang.Process` 对象, 所以有三件事要自己办:
 *  1. **等进程退出** —— 没有 `waitFor()`, 改用轮询 `/proc/<pid>`。
 *  2. **收尸** —— fork 出来的子进程归我们管, 不收就是僵尸进程。
 *     Android SDK 里既没有 `waitpid` 也没有 `ProcessHandle` (后者是 Java 9
 *     的 API), 所以这件事由 [NativeLauncher.reapExited] 在 native 侧做。
 *     见 [reap]。
 *  3. **发送信号** —— 走 `android.system.Os.kill()`。
 *
 * ## 停止时的顺序 (反了会让在途流量走直连)
 *
 * 先 SIGTERM, 给它 [STOP_GRACE_MS] 自己退; 超时才 SIGKILL。
 * 为什么不能一上来就 SIGKILL: mihomo 收到 SIGTERM 会关掉 TUN 并落盘
 * `cache.db` (存着用户选中的节点)。直接打死的话缓存可能写坏, 下次启动
 * 用户会发现节点被重置了 —— 一个和"断开连接"看不出关系的问题。
 */
/**
 * 就绪探测的结论。
 *
 * 为什么不是一个 `Boolean`: 那会把三件**修法完全不同**的事压成同一个 false ——
 * 「内核没起来」「内核起来又死了」「内核好着、是我们的请求被明文策略拦了」。
 * 上一轮真机上就是被这个压缩害的: 分不清, 只能猜, 耗了很久。
 */
internal sealed interface ReadyOutcome {
    /** 内核在听端口, REST 也通。 */
    data object Ready : ReadyOutcome

    /**
     * 内核在听 TCP, 但 HTTP 一直不通 —— **内核是好的, 不要杀它**。
     *
     * 这几乎只可能是 Android 的网络明文策略拦住了 `HttpURLConnection`
     * (内核的控制接口是明文 `http://127.0.0.1:9090`)。判据是
     * [MihomoApi.isPortOpen]: 裸 TCP 连接不经过任何策略, 所以"TCP 通而
     * HTTP 不通"这个组合本身就是策略拦截的指纹。
     */
    data class ApiBlocked(val waitedMs: Long, val lastError: String) : ReadyOutcome

    /** 进程中途死了 —— 配置错、规则集缺失, 这类要早失败。 */
    data object ProcessDied : ReadyOutcome

    /** 进程还活着, 但端口一直没开: 真的还在慢启动(或压根没绑上)。 */
    data class PortNeverOpened(val waitedMs: Long) : ReadyOutcome
}

internal class MihomoEngine(private val api: MihomoApi) {

    /** 子进程 pid; -1 = 没有在跑的内核。 */
    @Volatile
    var pid: Int = -1
        private set

    /** stdout + stderr 落地的文件。内核崩溃时这是唯一的现场。 */
    lateinit var logFile: File
        private set

    private lateinit var pidFile: File

    // ---------------------------------------------------------------- 启动

    /**
     * 起内核。
     *
     * @param exePath  可执行文件绝对路径 (必须是 `nativeLibraryDir` 下的那个,
     *                 见 [resolveExecutable])
     * @param workDir  内核工作目录 (mihomo 的 `-d`), 里面要有 geodata / ruleset
     * @param configPath 最终配置文件
     * @param tunFd    要交出去的 TUN 文件描述符
     * @throws IOException 起不来。调用方负责转成状态里的 error。
     */
    fun spawn(exePath: String, workDir: File, configPath: File, tunFd: Int) {
        check(pid <= 0) { "内核已经在跑 (pid=$pid), 先 stop()" }
        if (!NativeLauncher.available) {
            throw IOException(
                "启动桥没有加载成功 (${NativeLauncher.loadError}); " +
                    "没有它就无法把 TUN 的 fd 交给内核进程",
            )
        }
        val exe = File(exePath)
        if (!exe.isFile || !exe.canExecute()) {
            throw IOException("内核不可执行: ${exe.absolutePath}")
        }

        logFile = File(workDir, LOG_FILE_NAME)
        // 每次启动都清空: 日志混着好几次会话时, "它到底是这次崩的还是上次崩的"
        // 这个问题会浪费掉大量时间。
        logFile.writeText("")
        pidFile = File(workDir, PID_FILE_NAME)
        pidFile.delete()

        pid = NativeLauncher.forkExec(
            cmd = arrayOf(
                exePath,
                "-d", workDir.absolutePath,
                "-f", configPath.absolutePath,
            ),
            dir = workDir.absolutePath,
            // 刻意只给一个最小的环境: 继承 App 的 environ 会把 CLASSPATH /
            // ANDROID_ROOT 这些对 mihomo 毫无意义的东西带进去, 而那些变量
            // 恰恰是 Go 运行时会去看的 (比如 TMPDIR)。
            env = arrayOf(
                "PATH=/system/bin:/system/xbin",
                "HOME=${workDir.absolutePath}",
                "TMPDIR=${workDir.absolutePath}",
            ),
            tunFd = tunFd,
            logPath = logFile.absolutePath,
            pidPath = pidFile.absolutePath,
            // 只允许执行 nativeLibraryDir 下的文件。这个 JNI 入口本身不知道
            // "我们要跑的是 mihomo", 所以边界必须由调用方画出来 —— 而
            // **[exe].parentFile 就是 nativeLibraryDir** (见 resolveExecutable)。
            trustedDir = exe.parentFile?.absolutePath,
        )
    }

    // ------------------------------------------------------------ 就绪等待

    /**
     * 等 REST API 就绪。
     *
     * 为什么"起进程"和"起来了"必须分开: `execve` 返回成功只说明 mihomo 这个
     * 二进制被加载了, 它还要读配置、拉规则集、建 TUN、绑 9090 端口。这期间
     * `establish()` 已经生效、流量已经被接进隧道, 但**没人转发** ——
     * 这时候把状态报成"已连接"就是在骗用户, 他会以为某个 App 卡是网络问题,
     * 而不是隧道其实还没通。所以只有 [api] 应答了才算连上。
     *
     * ## 超时值为什么要给到 60 秒, 以及为什么要有进度回调
     *
     * 第一版给的是 20 秒, 真机上出过这样一条时间线: 20 秒到点判失败、把内核
     * 杀掉, 而**几十秒后**同样的 `/version` 请求返回 200。原因是首次启动要
     * 加载 geodata + 20 个规则集 + 6000 多个节点, 低端机 / 冷启动下确实可能
     * 超过 20 秒 —— 也就是说那个超时值是**在跟一个正常的慢启动抢跑**。
     *
     * 但光把超时调大又会换来"界面卡在'正在启动内核…'一分钟"的体验。所以
     * [onProgress] 每 [PROGRESS_INTERVAL_MS] 报一次已等待秒数: 用户看到数字
     * 在走就知道没死, 我们也能从这一行区分"慢"和"挂死"。
     *
     * @param onProgress 已等待秒数 -> 给界面的一句话
     * @return true = 就绪; false = 超时或进程中途死了
     */
    suspend fun awaitReady(
        timeoutMs: Long = READY_TIMEOUT_MS,
        onProgress: ((Long) -> Unit)? = null,
    ): ReadyOutcome {
        val startedAt = System.currentTimeMillis()
        val deadline = startedAt + timeoutMs
        var nextReport = startedAt + PROGRESS_INTERVAL_MS
        var lastError = ""

        while (System.currentTimeMillis() < deadline) {
            val probe = api.version()
            // 注意 ApiResult 是**文件顶层**的 internal sealed class, 不是
            // MihomoApi 的嵌套类 —— 写成 MihomoApi.ApiResult 解析不了(编译期
            // 报 Unresolved reference)。而且 Err 是非泛型的, 可以直接 is 判断,
            // 不需要星投影。
            if (probe.isOk) return ReadyOutcome.Ready
            if (probe is ApiResult.Err) lastError = probe.error
            // 内核可能在启动过程中就退了 (配置写错、规则集缺失)。
            // 与其干等到超时, 不如现在就报出来, 用户能早几秒看到真正的错因。
            if (!isAlive()) return ReadyOutcome.ProcessDied

            val now = System.currentTimeMillis()
            val waited = now - startedAt

            // 端口已经开了、但 HTTP 一直不通 —— 这几乎只可能是我们的请求
            // 被明文策略拦住了, 而**内核本身是好的**。
            //
            // 这一段是本文件里最要紧的改动。改之前: awaitReady 只看 HTTP,
            // 于是"策略拦住"和"内核没起来"表现完全一样, 一路等到超时, 然后
            // 调用方把**好着的内核杀掉** —— 用户失去的是一条本来能用的隧道,
            // 只因为我们的控制面连不上它。
            //
            // 宽限期是为了不误判: 内核刚绑上端口的那一小段时间里 REST 可能
            // 还没就绪, 那属于正常启动, 不是被拦。
            if (waited >= API_GRACE_MS && api.isPortOpen()) {
                return ReadyOutcome.ApiBlocked(waited, lastError)
            }

            if (onProgress != null && now >= nextReport) {
                nextReport = now + PROGRESS_INTERVAL_MS
                onProgress(waited / 1000)
            }
            delay(READY_POLL_MS)
        }
        // 最后一次机会: 超时那一刻可能刚好通了。
        return if (api.isUp()) {
            ReadyOutcome.Ready
        } else {
            ReadyOutcome.PortNeverOpened(System.currentTimeMillis() - startedAt)
        }
    }

    // ---------------------------------------------------------------- 存活

    /** 进程是否还活着。读 `/proc/<pid>` 而不是 `kill(pid, 0)`: 后者对僵尸
     *  进程也返回成功, 而僵尸恰恰是"已经死了"的一种。 */
    fun isAlive(): Boolean {
        val p = pid
        if (p <= 0) return false
        val stat = File("/proc/$p/stat")
        if (!stat.exists()) return false
        return try {
            // 第 3 个字段是状态; 进程名里可能带空格和括号, 所以从最后一个
            // ')' 之后开始切。
            val raw = stat.readText()
            val state = raw.substringAfterLast(") ").trim().firstOrNull() ?: return false
            state != 'Z' && state != 'X'
        } catch (_: IOException) {
            false
        }
    }

    /**
     * 轮询等进程退出, 退出后收尸。
     *
     * 这是 [java.lang.Process.waitFor] 的替身。用轮询而不是信号处理:
     * 装 `SIGCHLD` 处理器要动 `android.system.Os.signal`, 而那条路上任何
     * 一个疏忽都会影响整个 App 进程 (信号处置是进程级的), 为了一个
     * "什么时候通知我"的语义不值得。
     */
    /**
     * 收尸。返回退出码, -1 = 还没退出 / 收不到。
     *
     * ## 为什么这件事在 native 侧
     *
     * 常识里收尸应该调 `waitpid()`, 但 **Android SDK 没有暴露它**:
     * `android.system.Os` 只有 `kill` / `chmod` / `access` 这类 (拿 android.jar
     * 用 javap 逐个方法核过)。而平台上的替代品 `java.lang.ProcessHandle` 是
     * **Java 9 的 API, Android 上不存在这个类** —— 写出来直接编译不过
     * (这不是猜的, 第一版构建就是这么红的)。
     *
     * 所以收尸这件事只能自己提供, 见 [NativeLauncher.reapExited]。
     * 不收的后果: 僵尸占着 pid 表项, 反复连断几百次之后 fork 不出来,
     * 用户看到的是"用久了就连不上, 重启 App 才好"。
     */
    fun reap(targetPid: Int = pid): Int {
        if (targetPid <= 0) return -1
        if (!NativeLauncher.available) return -1
        return try {
            NativeLauncher.reapExited(targetPid)
        } catch (_: Throwable) {
            // 收不到不是错误 —— 有两种无害的情况: 子进程还活着 (WNOHANG
            // 返回 0), 或者已经被收过了 (ECHILD)。
            -1
        }
    }

    // ---------------------------------------------------------------- 停止

    /**
     * 停内核: 先 SIGTERM, 宽限期内自己退最好; 超时再 SIGKILL。
     *
     * SIGTERM 优先的理由见类注释 —— 要让 mihomo 有机会把 `cache.db` 落完。
     *
     * ## 为什么收尸放在 `finally` 里, 而且不管成没成都要收
     *
     * 第一版是"只有确认它退干净了才 `reap()`", 于是没退干净时直接 `return false`
     * 跳过了收尸。**真机上当场就看到了后果**: `ps -A` 里躺着 `Z [libmihomo.so]`
     * —— 进程已经是僵尸, 但 pid 表项还占着。反复连断几百次之后 `fork` 就会
     * 失败, 而用户看到的只是"用久了就连不上"。
     *
     * 关键在于: **进程死了 = 该收尸**, 和"我们有没有成功杀掉它"是两件独立的
     * 事。僵尸不需要被杀, 它只需要被 wait。所以只要 [isAlive] 为假就一定要收,
     * 哪怕我们判定"停止失败"。
     *
     * @return true = 进程已经不在了 (正常); false = 它还在跑 (严重, 会占着 9090)
     */
    suspend fun stop(graceMs: Long = STOP_GRACE_MS): Boolean {
        val p = pid
        if (p <= 0) return true

        var stillRunning = false
        try {
            signal(p, OsConstants.SIGTERM)

            val exited = withTimeoutOrNull(graceMs) {
                while (isAlive()) delay(EXIT_POLL_MS)
                true
            } ?: false

            if (!exited) {
                // 走到这里说明它卡住了 —— 常见于某个节点的连接挂在半开状态。
                // 这时候只能硬杀, 因为留着它隧道就永远断不干净。
                signal(p, OsConstants.SIGKILL)
                stillRunning = withTimeoutOrNull(KILL_GRACE_MS) {
                    while (isAlive()) delay(EXIT_POLL_MS)
                    true
                } ?: false
            }
        } finally {
            // 见上面那段: 只要它已经死了就必须收, 否则留僵尸。
            if (!isAlive()) {
                reap(p)
                pid = -1
                runCatching { pidFile.delete() }
            } else {
                // 还活着: pid 保留, 这样调用方还能重试停止 (App 退出时
                // 系统会把子进程交给 init 收养, 不会永久泄漏)。
                stillRunning = true
            }
        }
        return !stillRunning
    }

    private fun signal(target: Int, sig: Int) {
        try {
            Os.kill(target, sig)
        } catch (_: Throwable) {
            // ESRCH = 已经死了, 这正是我们想要的结果。别的 errno 在这里
            // 也没法补救 (我们没有权限问题, 因为是自己的子进程)。
        }
    }

    /**
     * **非挂起**的硬杀: 直接 SIGKILL, 有界地等一小会儿, 然后收尸。
     *
     * ## 为什么不能拿 [stop] 顶替它
     *
     * [stop] 是 suspend 的, 而它的调用点有两处是**不能挂起**的:
     *  - 服务 `onDestroy` (那里没有等待的余地);
     *  - 取消路径上的 `finally` —— 协程已经是 cancelled 状态, 任何 suspend
     *    调用都会立刻抛 `CancellationException`, 于是"清理"变成空操作, 而
     *    mihomo 是 `setsid()` 出去的, 它会带着 TUN fd 和 9090 一直活着
     *    (审计 N4 的核心)。
     *
     * 为什么这里直接上 SIGKILL 而不是先 SIGTERM: 走到这条路的场景全是"必须
     * 立刻收干净" (服务要销毁 / 启动已经失败), 没有再等 1.5 秒的余地。代价是
     * mihomo 可能来不及把 `cache.db` 落完 —— 相对于"留下一个占着 9090 的孤儿
     * 进程", 这个代价可以接受。正常断开走的仍然是 [stop] 那条优雅路径。
     *
     * @return true = 进程确实不在了 (顺带已经收尸); false = 它还在跑
     */
    fun killBlocking(graceMs: Long = KILL_BLOCK_GRACE_MS): Boolean {
        val p = pid
        if (p <= 0) return true

        signal(p, OsConstants.SIGKILL)
        val deadline = System.currentTimeMillis() + graceMs
        while (isAlive() && System.currentTimeMillis() < deadline) {
            Thread.sleep(EXIT_POLL_MS)
        }

        // 不管判定成没成功, 都先试一次收尸: **进程死了就该被 wait**, 这和我们
        // 有没有成功杀掉它是两件独立的事 (见 [stop] 里那段真实事故: 真机上当场
        // 看到 `Z [libmihomo.so]` 躺在 ps 里)。
        val collected = reap(p) >= 0
        if (collected || !isAlive()) {
            reap(p)
            pid = -1
            runCatching { pidFile.delete() }
            return true
        }
        return false
    }

    // ---------------------------------------------------------------- 日志

    /**
     * 日志超过上限就把它清空, 从头再写。
     *
     * ## 为什么必须要有这一步 (审计 N11)
     *
     * 原生侧把 mihomo 的 stdout+stderr 一起重定向进这个文件, 而模板里是
     * `log-level: info` —— **每条 dial / DNS 失败都写一行**。一个挂了几天的
     * 隧道在烂节点上能把这个文件写到几百 MB: 全是 filesDir 里的磁盘占用, 界面
     * 上没有任何提示, 而且它挤的是同一块空间 (设备写满之后连资源安装都会失败)。
     * 而读它的地方只有 [tailLog], 最多看最后 8 KB —— 历史一行都不需要。
     *
     * ## 为什么可以就地清空 (而不是改名/滚动)
     *
     * 写端是 mihomo 进程里那个 **O_APPEND** 的 fd: 它的偏移量在每次 write 时由
     * 内核重新按"文件当前末尾"计算, 所以 ftruncate 到 0 之后, 下一行会从 0 开始
     * 写, 不会留出一个塞满 NUL 的稀疏文件。改名反而做不到 —— 写端拿着的是
     * inode, 改名对它没有影响, 结果是"新文件永远是空的、旧的越写越大"。
     *
     * 清空而不是截掉前面一段: 后者的代价是一次几 MB 的读+写, 而 [tailLog] 只
     * 关心最新几行 —— 直接清掉最省事, 也最不容易写错。
     *
     * @return true = 这次真的清了一次 (调用方一般只需要忽略)
     */
    fun capLogFile(maxBytes: Long = LOG_MAX_BYTES): Boolean {
        if (!::logFile.isInitialized) return false
        return try {
            if (!logFile.isFile || logFile.length() <= maxBytes) return false
            // 追加一个标记再清空: 用户把日志贴给我们时, 至少能看出"这里被截过",
            // 而不会以为中间那几小时的记录是我们弄丢的。
            logFile.appendText("\n--- 日志超过 ${maxBytes / 1024 / 1024} MB, 已从这里清空重写 ---\n")
            java.io.FileOutputStream(logFile, false).use { }
            true
        } catch (_: IOException) {
            // 清不掉不是错误: 日志文件的唯一用途是排错, 不该因为它反过来拖垮隧道。
            false
        }
    }

    /**
     * 读日志尾部。
     *
     * mihomo 把 dial/DNS 失败写 stdout, 进程级错误写 stderr —— 两路都被
     * 原生侧重定向进了同一个文件, 所以排错时一个 [tailLog] 就够了, 不用
     * 再去猜"这个错会走哪一路"。
     */
    fun tailLog(maxBytes: Int = LOG_TAIL_BYTES): String {
        if (!::logFile.isInitialized || !logFile.exists()) return ""
        return try {
            val len = logFile.length()
            if (len <= maxBytes) {
                logFile.readText()
            } else {
                java.io.RandomAccessFile(logFile, "r").use { raf ->
                    raf.seek(len - maxBytes)
                    // 从中间切进去多半是半行, 丢掉第一行避免显示乱码。
                    raf.readLine()
                    buildString {
                        while (true) {
                            val line = raf.readLine() ?: break
                            append(line).append('\n')
                        }
                    }
                }
            }
        } catch (_: IOException) {
            ""
        }
    }

    companion object {
        const val LOG_FILE_NAME = "mihomo.log"
        const val PID_FILE_NAME = "mihomo.pid"

        /** 内核可执行文件在 `nativeLibraryDir` 里的名字。见 [resolveExecutable]。 */
        const val EXECUTABLE_NAME = "libmihomo.so"

        /**
         * 日志大小上限。超过就地清空重写, 见 [capLogFile]。
         *
         * 4 MB: 出问题时最后这 4 MB 里一定有原因 (而 [tailLog] 只看最后 8 KB),
         * 平时它占的空间也可以忽略。
         */
        const val LOG_MAX_BYTES = 4L * 1024 * 1024

        /** 就绪等待上限。冷启动要拉 geodata + 20 个规则集 + 6000 多个节点,
         *  低端机上确实可能超过 20 秒 —— 见 [awaitReady] 里那段真实时间线。 */
        const val READY_TIMEOUT_MS = 60_000L

        /**
         * 判定"端口开着但 HTTP 不通"之前要等的宽限期。
         *
         * 不能一上来就判: 内核刚绑上端口的那一小段时间里 REST 可能还没就绪,
         * 那属于正常启动。给够宽限, 才不会把正常启动误判成策略拦截。
         */
        const val API_GRACE_MS = 8_000L
        const val READY_POLL_MS = 200L
        const val EXIT_POLL_MS = 300L

        /** 每等这么久就给界面报一次已等待秒数, 免得看起来像卡死。 */
        const val PROGRESS_INTERVAL_MS = 3_000L

        /** SIGTERM 之后的宽限期。mihomo 落缓存是毫秒级的, 1.5 秒很宽裕。 */
        const val STOP_GRACE_MS = 1_500L
        const val KILL_GRACE_MS = 1_000L

        /**
         * [killBlocking] 在 SIGKILL 之后的等待上限。
         *
         * 必须比 [KILL_GRACE_MS] 短得多: 它的两个调用点一个是服务 `onDestroy`
         * (主线程), 一个是取消路径上的 finally —— 那里花掉的时间是用户直接
         * 感觉到的卡顿, 而 SIGKILL 之后进程几乎立刻就没了, 400 ms 足够。
         */
        const val KILL_BLOCK_GRACE_MS = 400L

        const val LOG_TAIL_BYTES = 8 * 1024

        /**
         * 内核可执行文件的绝对路径。
         *
         * **只能从 `nativeLibraryDir` 取, 不能把 assets 解压出来再 exec。**
         * Android 10+ 的 W^X 策略不允许 App 执行私有数据目录里的文件
         * (`/data/data/<pkg>/files/...` 挂的是 `noexec` 语义), 解压出来的
         * 二进制一 exec 就是 `EACCES`。而 `jniLibs` 落地的目录是系统专门
         * 为可执行代码准备的, 只有它允许 exec。
         *
         * 这也是为什么 61 MB 的 mihomo 要当 `.so` 打进 `jniLibs` 而不是
         * 塞进 assets —— 名字看着别扭, 但那是唯一能被执行的去处。
         */
        fun resolveExecutable(nativeLibraryDir: String): String =
            File(nativeLibraryDir, EXECUTABLE_NAME).absolutePath

        /**
         * `/proc/<pid>` 里那个进程**是不是我们起的 mihomo**。
         *
         * ## 为什么不能只信 pid 数字 (审计 K3)
         *
         * `mihomo.pid` 是给"App 被杀死之后下一次启动"用的线索。但 Android 的 pid
         * 是**循环复用**的: 上一次那个内核早就没了, 而它的 pid 号可能已经属于
         * 任何一个别的进程 —— 拿着 pid 文件就 `kill` 等于随机杀进程, 后果可能
         * 比"留下一个孤儿"严重得多。
         *
         * cmdline 是内核给出的、我们改不了的事实: mihomo 是以
         * `<nativeLibraryDir>/libmihomo.so -d <dir> -f <cfg>` 起的, argv[0] 就是
         * 它。所以判据是"cmdline 里出现过 [EXECUTABLE_NAME]"。
         */
        fun isCoreProcess(targetPid: Int): Boolean {
            if (targetPid <= 1) return false
            val raw = runCatching { File("/proc/$targetPid/cmdline").readBytes() }.getOrNull()
                ?: return false
            // cmdline 的各项之间是 NUL, 直接当文本找子串就够了 (文件名本身不含 NUL)。
            return raw.toString(Charsets.UTF_8).contains(EXECUTABLE_NAME)
        }

        /** 启动桥自己的库名, 供自检/诊断使用。 */
        const val BRIDGE_LIBRARY = "hongxing_launcher"
    }
}
