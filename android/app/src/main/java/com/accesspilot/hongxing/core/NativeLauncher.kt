package com.accesspilot.hongxing.core

/**
 * 红杏 Android · 内核启动桥 (Kotlin 侧)
 *
 * 职责只有一件事: 把"一个原生进程 + 一份要交给它的 fd"这件事交给 libhongxing_launcher.so。
 *
 * ## 为什么不能用 `ProcessBuilder`
 *
 * 这是本模块最反直觉、也最容易踩的一个坑, 结论来自 AOSP 源码:
 *
 * Java 的 `ProcessBuilder.start()` 最终落到 libcore 的 `UNIXProcess_forkAndExec`
 * (`ojluni/src/main/native/UNIXProcess_md.c`)。它的 JNI 签名是
 * `([B[BI[BI[B[IZ)I` —— **只收 std_fds[0..2] 三个描述符**。子进程在 execve
 * 之前会调 `closeDescriptors()`, 遍历 `/proc/self/fd` 把 **编号 >= 4 的 fd
 * 全部 close 掉** (`int from_fd = FAIL_FILENO + 1;`, 而 FAIL_FILENO == 3)。
 *
 * 注意它是**直接 close**, 不是靠 `FD_CLOEXEC` 生效的。所以即使
 * `ParcelFileDescriptor.detachFd()` 已经把 CLOEXEC 清干净, 那个 TUN fd 仍然
 * 会在 exec 前被关掉 —— 表现就是 mihomo 报 "tun: file descriptor is invalid",
 * 而真正的原因离报错信息十万八千里。
 *
 * `android.system.Os` 也没有公开 `fork()` / `posix_spawn()` (SDK 只给了
 * `execv`/`execve`/`dup2`/`fcntlInt`), 所以纯 Kotlin 这条路是死的。
 *
 * ## 为什么这样就对了
 *
 * 原生 `fork()` 出来的子进程天然继承父进程**整份** fd 表 —— 而且 fork 时不检查
 * CLOEXEC。随后的 `execve()` 只关带 `FD_CLOEXEC` 的 fd, 所以只要清掉 TUN fd 的
 * CLOEXEC, 它就能活到 mihomo 里。完全绕开 Java 那层 `closeDescriptors()`。
 *
 * 另外这条路还顺手解决了两个问题:
 *  - 子进程是我们自己 fork 的, 所以能精确控制 argv/envp/工作目录, 不受
 *    `ProcessBuilder` 那套封装限制;
 *  - mihomo 的 stdout/stderr 由 native 侧在 fork 前就重定向到日志文件,
 *    不需要 Java 侧常驻抽干线程 (抽慢了会把内核阻塞在 write 上, 拖慢整个隧道)。
 */
internal object NativeLauncher {

    /**
     * 启动库是否加载成功。
     *
     * 单独记一个标志而不是让 `System.loadLibrary` 直接抛: 这个库只影响
     * "能不能起内核实进程", 编译期/预览/单元测试等场景下拿不到它是正常的,
     * 上层需要能优雅降级成一条可读的错误, 而不是一个 UnsatisfiedLinkError。
     */
    val available: Boolean
    var loadError: String = ""
        private set

    init {
        var ok = false
        var err = ""
        try {
            System.loadLibrary("hongxing_launcher")
            ok = true
        } catch (t: Throwable) {
            // UnsatisfiedLinkError / ExceptionInInitializerError 都要接 ——
            // 后者在某些 ABI 不匹配的情况下才会出现, 而它是个 Error 不是 Exception。
            err = t.message ?: t.javaClass.simpleName
        }
        available = ok
        loadError = err
    }

    /**
     * fork + execve 一个完全脱离 Java 的子进程。
     *
     * @param cmd    argv, 第 0 项是可执行文件绝对路径
     * @param dir    子进程的工作目录 (mihomo 的 `-d`)
     * @param env    环境变量, `KEY=VALUE` 形式
     * @param tunFd  要交给子进程的 fd (TUN)。父进程继续持有它 —— 拿到的是同一份
     *               打开文件描述的两个引用, TUN 这种设备型 fd 正好需要这个语义
     * @param logPath 子进程 stdout+stderr 落地的文件
     * @param pidPath 子进程 pid 落地的文件。**必须有**: pid 一旦丢了, 停止时就
     *                只能盲目 kill 或者干等, 而 mihomo 卡在死连接上时不会自己退
     * @param trustedDir 只允许 exec 这个目录 (realpath 之后) 下的可执行文件;
     *                `null` = 不检查。**生产路径必须传** nativeLibraryDir ——
     *                这个入口的语义是"跑任意路径 + 把 App 的 fd 表交给它", 不
     *                加限制就是一个通用机关 (审计 N14)。唯一传 null 的是
     *                [FdProbe]: 它故意要跑 `/system/bin/sh -c`, 而那条路只在
     *                debug 包里存在 (`BuildConfig.DEBUG_ENTRYPOINTS`)。
     * @return 子进程 pid
     * @throws java.io.IOException fork 或 execve 失败 (消息里带 errno);
     *         等待 execve 结果超过 5 秒也会抛 (子进程会被杀掉, 不会留下悬空进程)
     */
    @Throws(java.io.IOException::class)
    external fun forkExec(
        cmd: Array<String>,
        dir: String?,
        env: Array<String>?,
        tunFd: Int,
        logPath: String?,
        pidPath: String?,
        trustedDir: String?,
    ): Int

    /**
     * 非阻塞收尸。返回退出码, -1 = 还没退出或已经收过了。
     *
     * **为什么收尸要专门开一个 native 入口**: Android SDK 里没有任何
     * `waitpid` (`android.system.Os` 只有 `kill` / `chmod` / `access`), 而
     * `java.lang.ProcessHandle` 是 Java 9 的 API, Android 上根本没有这个类 ——
     * 在 Kotlin 里写 `ProcessHandle.of(pid)` 是**编译不过**的。
     *
     * 不收尸的后果是僵尸进程一直占着 pid 表项, 反复连断之后 fork 不出来,
     * 用户看到的是"用久了就连不上, 重启 App 才好"。所以这二十行 C 值得写。
     */
    external fun reapExited(targetPid: Int): Int
}
