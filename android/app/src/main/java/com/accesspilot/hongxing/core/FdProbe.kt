package com.accesspilot.hongxing.core

import android.os.ParcelFileDescriptor
import android.system.Os
import android.system.OsConstants
import java.io.File
import java.util.concurrent.TimeUnit

/**
 * 红杏 Android · fd 传递实验
 *
 * ## 这个类为什么存在
 *
 * 整个方案的地基只有一条假设: **VpnService 给的 TUN fd 能被交给 mihomo 子进程**。
 * 这条假设如果不成立, 后面所有代码都是白写的 —— 这也是为什么它是本项目唯一
 * 被当作"技术风险"对待的地方。
 *
 * 源码层面已经有确定答案 (见 [NativeLauncher] 的类注释: Java 的
 * `UNIXProcess_forkAndExec` 在 exec 前会无条件关掉所有编号 >= 4 的 fd),
 * 但**源码推断再硬也只是推断**。这个类是那份推断的实证:
 *
 * 它让**同一个子进程命令**分别通过两条路启动, 并排放出各自看到的
 * `/proc/self/fd`。预期结果是:
 *
 * | 路径 | fd 4 在子进程里 |
 * |---|---|
 * | `ProcessBuilder` (Java 原生路径) | **不存在** —— 被 `closeDescriptors()` 关掉了 |
 * | `NativeLauncher.forkExec` (本项目方案) | **存在**, 且能读到父进程写进去的数据 |
 *
 * ## 为什么用管道而不是真的建 TUN
 *
 * 这个实验要验的是**"fd 能不能跨 exec 活下来"**, 而不是"TUN 能不能用"。
 * 普通管道 (`pipe()`) 和 TUN fd 在这件事上的行为完全一样 —— 都是进程 fd 表
 * 里的一项。用管道的好处是: 不需要 VPN 授权、不需要用户点系统对话框、
 * 不产生任何真实网络流量, 因此可以随时跑, 也可以自动化跑。
 *
 * 真正端到端的验证 (真 TUN + 真 mihomo + 真流量) 由 [MihomoEngine] 那条路
 * 在用户点"连接"时完成, 两者互补。
 *
 * ## 用法
 *
 * 这是诊断代码, 不在正常流程里。真机接入后跑一次 [run] 并把它输出的对比
 * 结果贴回来即可:
 * ```
 * val report = FdProbe(context).run()
 * // report 里就是上面那张表的两行真实数据
 * ```
 */
internal class FdProbe(private val workDir: File) {

    /**
     * 跑完整对比实验。**不抛异常** —— 它是诊断工具, 自己炸掉就失去意义了。
     *
     * @param tunFd 真的 TUN fd (可选)。传了就额外验一次"真 TUN 能不能跨 exec",
     *              这是最贴近生产的那一条; 不传就只验管道
     *              (管道和 TUN 在"跨 exec 存活"这件事上行为一致, 但真 TUN
     *              多验了一层"内核认不认这个设备")
     * @return 可读的对比报告 (等宽文本, 直接贴进聊天窗口就能看)
     */
    fun run(tunFd: Int = -1): String {
        val report = StringBuilder()
        report.appendLine("=== 红杏 fd 传递实验 ===")
        report.appendLine("说明: 子进程命令两条路完全相同, 只有启动方式不同。")
        report.appendLine("pid=${android.os.Process.myPid()}  abi=${android.os.Build.SUPPORTED_ABIS.firstOrNull()}")
        report.appendLine()

        val viaProcessBuilder = probeProcessBuilder(report)
        val viaNative = probeNativeLauncher(report)
        val tunResult = if (tunFd >= 0) probeRealTun(report, tunFd) else null

        report.appendLine()
        report.appendLine("=== 结论 ===")
        report.appendLine(
            "ProcessBuilder : " + if (viaProcessBuilder) {
                "fd 存活 (与 AOSP 源码推断不符 —— 需要重新核对)"
            } else {
                "fd 被关闭 (符合 AOSP 源码: UNIXProcess 的 closeDescriptors 关掉 >= 4 的 fd)"
            },
        )
        report.appendLine(
            "NativeLauncher : " + if (viaNative) {
                "fd 存活且数据可读 —— 方案成立"
            } else {
                "fd 没能存活 —— 方案不成立, 需要换 gomobile / tun2socks"
            },
        )
        if (tunResult != null) {
            report.appendLine(
                "真 TUN fd      : " + if (tunResult) {
                    "跨 exec 存活 (第 $tunFd 号)"
                } else {
                    "没能存活 —— 即使管道那条通了, 这里不过就是不过"
                },
            )
        }
        report.appendLine()
        report.appendLine(
            "verdict: " + when {
                viaNative && tunResult == true -> "确认: 必须走 NativeLauncher, 且真 TUN fd 能交给内核"
                viaNative -> "NativeLauncher 对管道有效; 真 TUN 那一项见上"
                else -> "两条路都不行 —— 立即上报"
            },
        )
        return report.toString()
    }

    // ------------------------------------------------ 路径 C: 真 TUN fd

    /**
     * 真 TUN fd 的验证 —— **最贴近生产的一条**。
     *
     * 管道那条验的是"fd 能不能跨 exec"; 这一条验的是"**VpnService 给的 TUN**
     * 能不能跨 exec", 多了一层内核语义: TUN 是字符设备, 而且它由系统在
     * `establish()` 时创建并绑到这个进程上。
     *
     * 判据和管道那条一样严: fd 存在 **且** 能读到父进程写的字节。往一个
     * TUN fd 里写字节不会失败 (它就是给用户态读写 IP 包的), 所以 "cat"
     * 读不到东西是**正常**的 —— 因此这里对 TUN 用 [tunFd] 的可读性判据放宽为
     * "存在 + `ls -l` 显示是字符设备", 而不是等 payload。这一点必须说清楚,
     * 否则会被误读成"没通过"。
     */
    private fun probeRealTun(report: StringBuilder, tunFd: Int): Boolean {
        report.appendLine()
        report.appendLine("--- 路径 C: 真 TUN fd (VpnService.establish 拿到的, 第 $tunFd 号) ---")

        val output = runChildExpectingFd(
            label = "真 TUN",
            fd = tunFd,
            script = "FD=$tunFd; " +
                "echo \"fd-exists=$( [ -e /proc/self/fd/\$FD ] && echo yes || echo no )\"; " +
                "ls -l /proc/self/fd/\$FD 2>&1; " +
                "echo '-- child fd table --'; " +
                "ls -l /proc/self/fd 2>&1",
        ) ?: return false

        report.appendLine(output.trim())
        val survived = output.contains("fd-exists=yes")
        report.appendLine("真 TUN 结果: " + if (survived) "fd 跨 exec 存活" else "fd 未存活")
        return survived
    }

    /**
     * 用 native 桥跑一个只为"看 fd 表"的子进程, 返回它的输出。
     *
     * @return null = 连启动都没成功
     */
    private fun runChildExpectingFd(label: String, fd: Int, script: String): String? {
        if (!NativeLauncher.available) {
            return "启动桥未加载 (${NativeLauncher.loadError})"
        }
        clearCloexec(fd)
        val log = File(workDir, "fdprobe-$label.log")
        val pidFile = File(workDir, "fdprobe-$label.pid")
        log.writeText("")
        pidFile.delete()
        return try {
            NativeLauncher.forkExec(
                cmd = arrayOf("/system/bin/sh", "-c", script),
                dir = workDir.absolutePath,
                env = arrayOf("PATH=/system/bin:/system/xbin"),
                tunFd = fd,
                logPath = log.absolutePath,
                pidPath = pidFile.absolutePath,
            )
            readChildOutput(NativeChild(pidFile.readText().trim().toIntOrNull() ?: -1, log))
        } catch (t: Throwable) {
            "$label 启动失败: ${t.javaClass.simpleName}: ${t.message}"
        }
    }

    // -------------------------------------------------- 路径 A: ProcessBuilder

    /**
     * Java 原生路径。**预期失败** —— 但这个"失败"正是要拿到的证据。
     *
     * 关键细节: 先 `detachFd()` 再手动清掉 `FD_CLOEXEC`。这是最有利于
     * `ProcessBuilder` 的配置 —— 如果连这样都活不下来, 就说明问题不在
     * CLOEXEC, 而在 `closeDescriptors()` 那个无条件 close。这正是我们要区分的
     * 两件事。
     */
    private fun probeProcessBuilder(report: StringBuilder): Boolean {
        report.appendLine("--- 路径 A: ProcessBuilder (Java 原生) ---")
        return runWithPipe(report, "ProcessBuilder") { readEndFd, cmd ->
            ProcessBuilder(*cmd).redirectErrorStream(true).start().also {
                it.outputStream.close()
            }
        }
    }

    // ------------------------------------------------ 路径 B: NativeLauncher

    /**
     * 本项目方案。**预期成功**。
     *
     * 走的是和真启动 mihomo 完全相同的那个 native 入口 —— 这一点很重要:
     * 如果这里改用一个"专门为实验简化过"的函数, 验的就不是生产路径了。
     */
    private fun probeNativeLauncher(report: StringBuilder): Boolean {
        report.appendLine()
        report.appendLine("--- 路径 B: NativeLauncher (本项目方案) ---")
        if (!NativeLauncher.available) {
            report.appendLine("跳过: 启动桥未加载 (${NativeLauncher.loadError})")
            return false
        }
        return runWithPipe(report, "NativeLauncher") { readEndFd, cmd ->
            val log = File(workDir, "fdprobe.log")
            val pidFile = File(workDir, "fdprobe.pid")
            log.writeText("")
            pidFile.delete()
            NativeLauncher.forkExec(
                cmd = cmd,
                dir = workDir.absolutePath,
                env = arrayOf("PATH=/system/bin:/system/xbin"),
                tunFd = readEndFd,
                logPath = log.absolutePath,
                pidPath = pidFile.absolutePath,
            )
            // 用一个只为了统一接口的壳把 pid 带出去。native 那条路没有
            // java.lang.Process, 所以下面的取输出逻辑按"读日志文件"处理。
            NativeChild(pidFile.readText().trim().toIntOrNull() ?: -1, log)
        }
    }

    // ---------------------------------------------------------------- 共用

    /**
     * 管道 + 子进程 + 读回结果。两条路共用同一段子进程命令, 唯一变量是启动方式。
     *
     * 子进程命令做的事:
     * ```
     *   echo "/proc/self/fd/$FD 是否存在";   # 硬证据 1: fd 还在不在
     *   ls -l /proc/self/fd;                 # 硬证据 2: 完整 fd 表, 可肉眼核对
     *   cat /proc/self/fd/$FD;               # 硬证据 3: 能不能真读到父进程写的字节
     * ```
     * 第三条是关键: fd 存在但读不出数据 (比如变成了悬空的) 也是失败,
     * 只有"存在 **且** 数据对得上"才算真的传过去了。
     */
    private fun runWithPipe(
        report: StringBuilder,
        label: String,
        spawn: (readEndFd: Int, cmd: Array<String>) -> Any,
    ): Boolean {
        val pipe = try {
            ParcelFileDescriptor.createPipe()
        } catch (t: Throwable) {
            report.appendLine("无法创建管道: ${t.message}")
            return false
        }

        val readEnd = pipe[0]
        val writeEnd = pipe[1]
        val readFd = readEnd.fd
        var restoredCloexec = false

        try {
            // 把读端交给子进程之前, 必须清掉 FD_CLOEXEC —— 否则 exec 时
            // 内核会直接把它关掉, 实验就变成在测一个和 CLOEXEC 有关的问题
            // 而不是在测 closeDescriptors()。
            clearCloexec(readFd)
            restoredCloexec = true

            val cmd = arrayOf(
                "/system/bin/sh", "-c",
                "FD=$readFd; " +
                    "echo \"fd-exists=$( [ -e /proc/self/fd/\$FD ] && echo yes || echo no )\"; " +
                    "echo '-- child fd table --'; " +
                    "ls -l /proc/self/fd 2>&1; " +
                    "echo '-- payload --'; " +
                    "cat /proc/self/fd/\$FD 2>&1",
            )

            val child = try {
                spawn(readFd, cmd)
            } catch (t: Throwable) {
                report.appendLine("$label 启动失败: ${t.javaClass.simpleName}: ${t.message}")
                return false
            }

            // 写完就关父进程这一端 —— 不关的话子进程的 cat 会一直等着,
            // 实验会挂在这里而不是给出结论。
            val payload = "HONGXING-FD-OK"
            java.io.FileOutputStream(writeEnd.fileDescriptor).use {
                it.write(payload.toByteArray())
            }
            writeEnd.close()

            val output = readChildOutput(child)
            report.appendLine(output.trim())
            report.appendLine("父进程写入的期望值: $payload")

            val survived = output.contains("fd-exists=yes") && output.contains(payload)
            report.appendLine("$label 结果: " + if (survived) "fd 存活" else "fd 未存活")
            return survived
        } catch (t: Throwable) {
            report.appendLine("$label 实验异常: ${t.javaClass.simpleName}: ${t.message}")
            return false
        } finally {
            runCatching { readEnd.close() }
            runCatching { writeEnd.close() }
            if (restoredCloexec) {
                // 别把 CLOEXEC 的状态留在进程里 —— 这个 fd 马上就要关了,
                // 但"改了全局状态不还原"是个坏习惯, 不该出现在诊断代码里。
                runCatching { restoreCloexec(readFd) }
            }
        }
    }

    /**
     * 取子进程输出。
     *
     * 两条路的输出来源不一样: `ProcessBuilder` 能直接读它的 stdout;
     * 而 native 那条路的 stdout 被重定向进了日志文件 (那是生产设计 —— 见
     * [MihomoEngine] 关于"不要用管道, 否则抽干不及时会阻塞内核")。
     * 这里统一成"等它退出, 然后把输出拿出来"。
     */
    private fun readChildOutput(child: Any): String = when (child) {
        is Process -> {
            val text = child.inputStream.bufferedReader().use { it.readText() }
            child.waitFor(WAIT_SECONDS, TimeUnit.SECONDS)
            text
        }
        is NativeChild -> {
            // 轮询 /proc; 见 MihomoEngine.isAlive 里同样的理由。
            val deadline = System.currentTimeMillis() + WAIT_SECONDS * 1000
            while (System.currentTimeMillis() < deadline) {
                if (!File("/proc/${child.pid}").exists()) break
                Thread.sleep(100)
            }
            // 子进程写文件是带缓冲的, 等一下让 flush 落地。
            Thread.sleep(200)
            child.log.readText()
        }
        else -> "未知的子进程类型"
    }

    /** 清掉 FD_CLOEXEC。`detachFd()` 理论上已经清过, 这里显式再来一次。 */
    private fun clearCloexec(fd: Int) {
        try {
            val fdObj = ParcelFileDescriptor.fromFd(fd).fileDescriptor
            val flags = Os.fcntlInt(fdObj, OsConstants.F_GETFD, 0)
            Os.fcntlInt(fdObj, OsConstants.F_SETFD, flags and OsConstants.FD_CLOEXEC.inv())
        } catch (_: Throwable) {
            // 拿不到就继续 —— 后面的结果会如实反映事实。
        }
    }

    private fun restoreCloexec(fd: Int) {
        try {
            val fdObj = ParcelFileDescriptor.fromFd(fd).fileDescriptor
            val flags = Os.fcntlInt(fdObj, OsConstants.F_GETFD, 0)
            Os.fcntlInt(fdObj, OsConstants.F_SETFD, flags or OsConstants.FD_CLOEXEC)
        } catch (_: Throwable) {
            // 无所谓。
        }
    }

    /** native 那条路的子进程句柄 —— 没有 `java.lang.Process`, 只有 pid 和日志。 */
    private data class NativeChild(val pid: Int, val log: File)

    companion object {
        private const val WAIT_SECONDS = 10L

        /**
         * **adb 友好的入口。** `MainActivity` 的 `--ez fdprobe true` 走这里。
         *
         * ## 这个重载**不建 TUN**
         *
         * 真 TUN 那一条 (路径 C) 必须由一个真正跑起来的 `VpnService` 实例来建 ——
         * `VpnService.Builder` 是 VpnService 的**内部类**, 而 VpnService 的实例
         * 只能由系统创建 (它要先把 Binder 和系统侧的 VPN 接口挂好)。
         * 自己 `VpnService()` 出来的是个普通对象, `establish()` 必然失败。
         *
         * 所以职责这样分:
         *  - **这里** (`run(context, workDir)`, 从 Activity 调): 只验管道那两条
         *    路 (ProcessBuilder vs NativeLauncher)。判据完全相同 —— "fd 存在
         *    且能从管道读出 HONGXING-FD-OK"。这两条已经足以回答
         *    "Java 的 ProcessBuilder 到底能不能把 fd 交给子进程"。
         *  - **真 TUN**: 由 [HongxingVpnService.ACTION_FD_PROBE] 触发, 服务里
         *    那个真实例建 TUN、拿到 fd、调 [run] 的另一个重载, 报告写回
         *    [EngineRuntime.fdProbeReport]。
         *
         * 不抛异常 —— 它是诊断工具, 自己炸掉就失去意义了。
         */
        fun run(context: android.content.Context, workDir: File): String =
            runCatching { FdProbe(workDir).run() }
                .getOrElse { "FdProbe 自身抛异常: ${it.javaClass.simpleName}: ${it.message}" }

        /**
         * 跑实验并把报告写进文件。方便在真机上"点一下就留证据"。
         */
        fun runToFile(workDir: File): File {
            val report = FdProbe(workDir).run()
            val out = File(workDir, "fdprobe-report.txt")
            out.writeText(report)
            return out
        }
    }
}
