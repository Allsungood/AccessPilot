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
    ): Boolean {
        val startedAt = System.currentTimeMillis()
        val deadline = startedAt + timeoutMs
        var nextReport = startedAt + PROGRESS_INTERVAL_MS

        while (System.currentTimeMillis() < deadline) {
            if (api.isUp()) return true
            // 内核可能在启动过程中就退了 (配置写错、规则集缺失)。
            // 与其干等到超时, 不如现在就报出来, 用户能早几秒看到真正的错因。
            if (!isAlive()) return false

            val now = System.currentTimeMillis()
            if (onProgress != null && now >= nextReport) {
                nextReport = now + PROGRESS_INTERVAL_MS
                onProgress((now - startedAt) / 1000)
            }
            delay(READY_POLL_MS)
        }
        return api.isUp()
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
    suspend fun awaitExit(): Int {
        val p = pid
        if (p <= 0) return -1
        while (isAlive()) delay(EXIT_POLL_MS)
        return reap(p)
    }
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

    // ---------------------------------------------------------------- 日志

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

        /** 就绪等待上限。冷启动要拉 geodata + 20 个规则集 + 6000 多个节点,
         *  低端机上确实可能超过 20 秒 —— 见 [awaitReady] 里那段真实时间线。 */
        const val READY_TIMEOUT_MS = 60_000L
        const val READY_POLL_MS = 200L
        const val EXIT_POLL_MS = 300L

        /** 每等这么久就给界面报一次已等待秒数, 免得看起来像卡死。 */
        const val PROGRESS_INTERVAL_MS = 3_000L

        /** SIGTERM 之后的宽限期。mihomo 落缓存是毫秒级的, 1.5 秒很宽裕。 */
        const val STOP_GRACE_MS = 1_500L
        const val KILL_GRACE_MS = 1_000L

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
            File(nativeLibraryDir, "libmihomo.so").absolutePath

        /** 启动桥自己的库名, 供自检/诊断使用。 */
        const val BRIDGE_LIBRARY = "hongxing_launcher"
    }
}
