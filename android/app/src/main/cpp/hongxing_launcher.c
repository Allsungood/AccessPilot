/*
 * 红杏 Android · 内核启动桥 (native fork/exec)
 *
 * 为什么必须有这个文件 —— 这是整个方案唯一的硬骨头, 结论来自 AOSP 源码与
 * 真机实测, 不是猜的:
 *
 *   Java 的 ProcessBuilder 走 libcore 的 UNIXProcess_forkAndExec(), 它的
 *   JNI 签名 `([B[BI[BI[B[IZ)I` 只收 std_fds[3] 这三个描述符; 子进程在
 *   execve 之前会调 closeDescriptors() 遍历 /proc/self/fd, 把 **编号 >= 4 的
 *   所有 fd 无条件关掉** (见 ojluni/src/main/native/UNIXProcess_md.c:
 *   `int from_fd = FAIL_FILENO + 1;` 且 FAIL_FILENO == 3)。
 *
 *   关键点: 它是"直接 close", 不是靠 FD_CLOEXEC 生效。所以
 *   ParcelFileDescriptor.detachFd() 清掉 CLOEXEC 也没用 —— 那个 TUN fd
 *   照样在 exec 前被关掉, 子进程拿不到。
 *
 *   android.system.Os 里也**没有**公开的 fork()/posix_spawn(), SDK 只给了
 *   execv/execve/dup2/fcntlInt —— 换句话说纯 Kotlin 无解。
 *
 * 所以这里直接用最原始也最可靠的一招: 原生 fork() + execve()。
 *   - fork() 出来的子进程天然拥有父进程那一整份 fd 表 —— CLOEXEC 的状态在
 *     fork 时不被检查;
 *   - execve() 只关掉带 FD_CLOEXEC 的 fd, 所以只要把 TUN fd 的 CLOEXEC
 *     清掉, 它就能活到 mihomo 里去。
 *   - 这条路完全绕开了 Java 那层 closeDescriptors(), 与 ClashMetaForAndroid
 *     等同类实现的做法一致。
 *
 * 为什么不用 vfork(): vfork 会挂起父线程 —— 而父线程正是等下要跑
 *   Os.waitpid 的那个, 会自锁。fork() 慢一点 (只有一页表的拷贝) 但没有这个问题。
 *
 * 为什么 fork 之后一行 java/ART 代码都不能再碰:
 *   fork() 只复制调用线程, 其它线程留下的堆锁在新进程里永远不会释放。
 *   子进程里任何一次 malloc 都可能死锁在 ART 的 mutex 上。所以下面
 *   child_exec() 里全部是 fork-safe 的东西: fcntl/dup2/close/setsid/
 *   unsetenv/execve —— 唯一的"分配"是 strdup 出来的 extra_envp, 那个
 *   在 fork **之前**就准备好了。
 */

#include <jni.h>
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <poll.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/wait.h>
#include <unistd.h>

/*
 * 错误回报通道:
 *   fork 之后子进程只剩一条命, 它失败了也不能 printf (会死锁), 更不能抛
 *   Java 异常 (那要回到 ART 里)。POSIX 的标准解法是留一根 CLOEXEC 管道:
 *   - execve 成功 -> 管道写端被内核自动关掉 -> 父进程 read() 得到 0 字节;
 *   - execve 失败 -> 子进程把 errno 写进去 -> 父进程 read() 得到 4 字节。
 *   父进程就靠"读到几个字节"判断 exec 到底成没成, 这比 sleep 轮询可靠得多,
 *   也顺便拿到了准确的 errno。
 */
static int write_all(int fd, const void *buf, size_t n) {
    const char *p = (const char *) buf;
    while (n > 0) {
        ssize_t k = write(fd, p, n);
        if (k < 0) {
            if (errno == EINTR) continue;
            return -1;
        }
        p += k;
        n -= (size_t) k;
    }
    return 0;
}

/*
 * 子进程里唯一允许做的事。返回只表示"exec 失败了", 成功的话这一行永远到不了。
 */
static void child_exec(int err_fd, int tun_fd, int log_fd,
                       char *const argv[], char *const envp[]) {
    /*
     * 先把自己挪出会话: mihomo 是长驻进程, 挂在 App 的会话里的话, 父进程
     * 退出时它可能收到连带的 SIGHUP。
     */
    setsid();

    /*
     * SIGHUP/SIGPIPE 恢复默认。App 进程里这两个信号多半被 ART 或某个
     * native 库改过处理器, 而信号处置是**跨 exec 保留**的 —— 不清掉的话
     * mihomo 里一次正常的管道写就可能被吞掉或直接杀进程。
     */
    signal(SIGHUP, SIG_DFL);
    signal(SIGPIPE, SIG_DFL);

    /*
     * TUN fd 的 CLOEXEC 必须清掉。父进程用的是 ParcelFileDescriptor.dup()
     * (见 HongxingVpnService.coreTunnel), dup 出来的 fd 本身不带 CLOEXEC,
     * 但这里再清一次是零成本的保险: 一旦它带着 CLOEXEC, execve 会当场把它
     * 关掉, 表现就是 mihomo 报 "file descriptor is not valid" —— 而那个报错
     * 离真正的原因 (一个 flag) 隔着十万八千里, 值得多写这一行。
     */
    int flags = fcntl(tun_fd, F_GETFD);
    if (flags >= 0) fcntl(tun_fd, F_SETFD, flags & ~FD_CLOEXEC);

    /*
     * 日志走文件而不是管道: mihomo 的 stdout/stderr 输出量不小 (每条 dial
     * 失败都写一行), 用管道就要有 Java 侧持续抽干的线程, 一旦抽慢了内核
     * 会阻塞在 write 上拖慢整个隧道。落文件最省事, 排错时也留得下现场。
     */
    if (log_fd >= 0) {
        dup2(log_fd, STDOUT_FILENO);
        dup2(log_fd, STDERR_FILENO);
        if (log_fd > STDERR_FILENO) close(log_fd);
    }

    /*
     * stdin 接到 /dev/null。不接的话 mihomo 会继承 App 的 stdin —— 那是
     * 个没有读者的管道, 万一它真去读就会一直挂着。
     */
    int devnull = open("/dev/null", O_RDONLY);
    if (devnull >= 0) {
        dup2(devnull, STDIN_FILENO);
        if (devnull > STDERR_FILENO) close(devnull);
    }

    execve(argv[0], argv, envp);

    /* 只有失败才会执行到这里。 */
    int err = errno;
    write_all(err_fd, &err, sizeof(err));
    _exit(127);
}

/*
 * 把 pid 写成十进制文本。fork-safe: 全栈上操作, 不碰 malloc。
 * 为什么不用 snprintf: 它不是 async-signal-safe, 在 fork 出来的子进程里
 * 调用有死锁风险 (虽然这里其实只在父进程调用, 但保持整个文件风格一致,
 * 免得以后有人把这段挪进子进程)。
 */
static int write_pid_file(const char *path, pid_t pid) {
    if (!path) return -1;

    char buf[24];
    int n = 0;
    unsigned int v = (unsigned int) pid;
    char tmp[12];
    int t = 0;
    if (v == 0) tmp[t++] = '0';
    while (v > 0) { tmp[t++] = (char) ('0' + (v % 10)); v /= 10; }
    while (t > 0) buf[n++] = tmp[--t];
    buf[n++] = '\n';

    int fd = open(path, O_WRONLY | O_CREAT | O_TRUNC | O_CLOEXEC, 0644);
    if (fd < 0) return -1;
    int rc = write_all(fd, buf, (size_t) n);
    close(fd);
    return rc;
}

/*
 * 父进程等待 execve 结果的截止时间。
 *
 * execve 要么立刻成功, 要么立刻带着 errno 失败, 中间只有"内核卡在某个文件系统
 * 上"这一种情况会超时。5 秒远大于任何正常值, 又短到不会让界面觉得死了。
 */
#define EXEC_WAIT_TIMEOUT_MS 5000

/*
 * fork 之前那些失败的出口要统一收尾: 释放 argv/envp 的堆内存, 以及每一个
 * GetStringUTFChars 换来的 JNI 字符串。
 *
 * 为什么要专门写一个函数: 这些出口有四个 (pipe 失败、fork 失败、exec 失败、
 * 等超时), 手工各写一遍必然漏 —— 而漏掉 GetStringUTFChars 的 Release 是
 * **真的会出事**的: JNI 的局部引用表只有几百项, 每次连接漏几个, 连断几十次
 * 之后就是 "JNI ERROR (app bug): local reference table overflow" 直接崩溃。
 * (原来这里确实每次都漏: argv/envp 和四个字符串全都没释放。)
 */
static void cleanup_before_fork(char **argv, jsize cmd_len, char **envp, jsize env_len,
                                JNIEnv *env, jstring dir_string, const char *dir,
                                jstring log_path, const char *log_path_c,
                                jstring pid_path, const char *pid_path_c,
                                jstring trusted_dir_string, const char *trusted_dir) {
    for (jsize i = 0; i < cmd_len; i++) free(argv[i]);
    free(argv);
    for (jsize i = 0; i < env_len; i++) free(envp[i]);
    free(envp);
    if (dir && dir_string) (*env)->ReleaseStringUTFChars(env, dir_string, dir);
    if (log_path_c && log_path) (*env)->ReleaseStringUTFChars(env, log_path, log_path_c);
    if (pid_path_c && pid_path) (*env)->ReleaseStringUTFChars(env, pid_path, pid_path_c);
    if (trusted_dir && trusted_dir_string) {
        (*env)->ReleaseStringUTFChars(env, trusted_dir_string, trusted_dir);
    }
}

/**
 * 起一个完全脱离 Java 的子进程。
 *
 * @param tun_fd  要传给子进程的 fd (VpnService 的 TUN)。父进程继续持有它,
 *                子进程拿到的是同一份打开文件描述的两个引用 —— 这正是 TUN
 *                这种"设备型 fd"需要的语义。
 * @param log_path 子进程 stdout+stderr 落地的文件。
 * @param pid_path 子进程 pid 落地文件。**必须有**: pid 一旦丢失, 停止时就只能
 *                靠盲目 kill 或等它自己退, 而 mihomo 卡在死连接上时不会自己退。
 * @param trusted_dir 只允许 exec 这个目录(realpath 之后)下的可执行文件;
 *                **传 NULL = 不检查**。生产路径 (MihomoEngine.spawn) 必须传
 *                nativeLibraryDir: 这个 JNI 入口是一个通用的"拿任意路径 + 任意 fd
 *                起进程"的机关, 而通用机关一旦能被别处调用, 就等于把"执行任意
 *                代码"这件事外包给了调用方 (审计 N14)。唯一传 NULL 的是
 *                FdProbe —— 它故意要跑 `/system/bin/sh -c`, 而那条路只在
 *                debug 包里存在 (见 BuildConfig.DEBUG_ENTRYPOINTS)。
 * @return 子进程 pid。
 * @throws IOException fork/execve 失败 (消息里带 errno)。
 */
JNIEXPORT jint JNICALL
Java_com_accesspilot_hongxing_core_NativeLauncher_forkExec(
        JNIEnv *env, jclass clazz,
        jobjectArray cmd_array, jstring dir_string, jobjectArray env_array,
        jint tun_fd, jstring log_path, jstring pid_path, jstring trusted_dir_string) {
    /* jclass 是 JNI 的固定参数 (静态 native 方法签名要求), 这里用不到。 */
    (void) clazz;

    /* ---- 以下全部是 fork 之前的准备: 这里还能安全地 malloc / 调 ART ---- */

    jsize cmd_len = (*env)->GetArrayLength(env, cmd_array);
    if (cmd_len < 1) {
        jclass iae = (*env)->FindClass(env, "java/lang/IllegalArgumentException");
        if (iae) (*env)->ThrowNew(env, iae, "cmd 不能为空");
        return -1;
    }

    char **argv = (char **) calloc((size_t) cmd_len + 1, sizeof(char *));
    if (!argv) {
        jclass oom = (*env)->FindClass(env, "java/lang/OutOfMemoryError");
        if (oom) (*env)->ThrowNew(env, oom, "calloc argv 失败");
        return -1;
    }
    /*
     * 每一项都要检查返回值。这不是形式主义: GetStringUTFChars 在内存紧张时
     * 会返回 NULL 而不是抛异常 (它自己也可能抛 OutOfMemoryError, 那时返回值
     * 同样不可用), strdup(NULL) 或者 strdup 自己失败 (OOM) 都会让 argv 里出现
     * 一个 NULL —— 而 argv 里的 NULL 就是"参数到此结束": execve 会拿着一个
     * **少了一半参数的命令行**去启动内核, 报出来的错却和参数无关 (审计 N14)。
     */
    for (jsize i = 0; i < cmd_len; i++) {
        jstring s = (jstring) (*env)->GetObjectArrayElement(env, cmd_array, i);
        if (s == NULL) {
            for (jsize k = 0; k < i; k++) free(argv[k]);
            free(argv);
            jclass iae = (*env)->FindClass(env, "java/lang/IllegalArgumentException");
            if (iae) (*env)->ThrowNew(env, iae, "cmd 里有 null 项");
            return -1;
        }
        const char *utf = (*env)->GetStringUTFChars(env, s, NULL);
        if (utf == NULL) {
            (*env)->DeleteLocalRef(env, s);
            for (jsize k = 0; k < i; k++) free(argv[k]);
            free(argv);
            jclass oom = (*env)->FindClass(env, "java/lang/OutOfMemoryError");
            if (oom) (*env)->ThrowNew(env, oom, "GetStringUTFChars(cmd) 返回 NULL");
            return -1;
        }
        argv[i] = strdup(utf);
        (*env)->ReleaseStringUTFChars(env, s, utf);
        (*env)->DeleteLocalRef(env, s);
        if (argv[i] == NULL) {
            for (jsize k = 0; k < i; k++) free(argv[k]);
            free(argv);
            jclass oom = (*env)->FindClass(env, "java/lang/OutOfMemoryError");
            if (oom) (*env)->ThrowNew(env, oom, "strdup(cmd) 失败");
            return -1;
        }
    }

    /*
     * argv[0] 必须是绝对路径; 给了 trusted_dir 时还必须落在它下面。
     *
     * 为什么值得单独校验: 这个函数是"跑一个可执行文件 + 塞一个 fd 给它", 而
     * `trusted_dir` 就是这条边界的全部 —— 没有它, 任何拿到这个 JNI 入口的代码
     * 都能 exec 任意路径 (包括 /system/bin/sh -c "...") 并把 App 的 fd 表交给它。
     * 用 realpath 而不是字符串前缀: 前缀比较会被 `../` 和符号链接绕过去。
     */
    if (argv[0][0] != '/') {
        for (jsize i = 0; i < cmd_len; i++) free(argv[i]);
        free(argv);
        jclass iae = (*env)->FindClass(env, "java/lang/IllegalArgumentException");
        if (iae) (*env)->ThrowNew(env, iae, "可执行文件必须是绝对路径");
        return -1;
    }

    const char *trusted_dir = trusted_dir_string
            ? (*env)->GetStringUTFChars(env, trusted_dir_string, NULL) : NULL;
    if (trusted_dir != NULL) {
        char resolved_dir[PATH_MAX];
        char resolved_exe[PATH_MAX];
        int ok = realpath(trusted_dir, resolved_dir) != NULL
                 && realpath(argv[0], resolved_exe) != NULL;
        if (ok) {
            size_t dl = strlen(resolved_dir);
            /* 必须以 "<dir>/" 开头 —— 少了这个 '/', /data/app/x-evil 也会通过。 */
            ok = strncmp(resolved_exe, resolved_dir, dl) == 0 && resolved_exe[dl] == '/';
        }
        if (!ok) {
            char msg[512];
            snprintf(msg, sizeof(msg), "拒绝执行 %s: 不在允许的目录 %s 下",
                     argv[0], trusted_dir);
            (*env)->ReleaseStringUTFChars(env, trusted_dir_string, trusted_dir);
            for (jsize i = 0; i < cmd_len; i++) free(argv[i]);
            free(argv);
            jclass ioe = (*env)->FindClass(env, "java/io/IOException");
            if (ioe) (*env)->ThrowNew(env, ioe, msg);
            return -1;
        }
    }

    /*
     * 环境变量。App 侧传进来什么就是什么 —— 刻意**不**继承 App 的 environ:
     * App 的 environ 里有 ANDROID_ROOT / CLASSPATH 之类对 mihomo 毫无意义
     * 甚至有害的项。传空数组时退回一个最小的 PATH。
     */
    char **envp = NULL;
    jsize env_len = env_array ? (*env)->GetArrayLength(env, env_array) : 0;
    envp = (char **) calloc((size_t) env_len + 2, sizeof(char *));
    if (!envp) {
        if (trusted_dir) (*env)->ReleaseStringUTFChars(env, trusted_dir_string, trusted_dir);
        for (jsize i = 0; i < cmd_len; i++) free(argv[i]);
        free(argv);
        jclass oom = (*env)->FindClass(env, "java/lang/OutOfMemoryError");
        if (oom) (*env)->ThrowNew(env, oom, "calloc envp 失败");
        return -1;
    }
    for (jsize i = 0; i < env_len; i++) {
        jstring s = (jstring) (*env)->GetObjectArrayElement(env, env_array, i);
        if (s == NULL) continue;   /* null 项直接跳过: 环境变量少一个不影响启动 */
        const char *utf = (*env)->GetStringUTFChars(env, s, NULL);
        if (utf != NULL) {
            envp[i] = strdup(utf);
            (*env)->ReleaseStringUTFChars(env, s, utf);
        }
        (*env)->DeleteLocalRef(env, s);
        /* strdup 失败时留一个空项 —— 上面 calloc 出来的是 NULL, 而 envp 里
         * 的 NULL 就是数组结束, 所以这里补一个空串, 免得后面所有变量都被丢掉。 */
        if (envp[i] == NULL) envp[i] = strdup("");
    }
    if (env_len == 0) {
        envp[env_len] = strdup("PATH=/system/bin:/system/xbin");
        if (envp[env_len] != NULL) env_len++;
    }

    const char *dir = dir_string ? (*env)->GetStringUTFChars(env, dir_string, NULL) : NULL;
    const char *log_path_c = log_path ? (*env)->GetStringUTFChars(env, log_path, NULL) : NULL;
    const char *pid_path_c = pid_path ? (*env)->GetStringUTFChars(env, pid_path, NULL) : NULL;

    /* 日志文件在 fork 之前打开: open() 会 malloc, 不能留到子进程里做。 */
    int log_fd = -1;
    if (log_path_c) {
        log_fd = open(log_path_c, O_WRONLY | O_CREAT | O_APPEND | O_CLOEXEC, 0644);
    }

    /*
     * 日志 fd 清掉 CLOEXEC: 否则 execve 时它先被关掉, dup2 过去的
     * stdout/stderr 就成了悬空的 fd, mihomo 一写就 EBADF。
     */
    if (log_fd >= 0) {
        int f = fcntl(log_fd, F_GETFD);
        if (f >= 0) fcntl(log_fd, F_SETFD, f & ~FD_CLOEXEC);
    }

    int err_pipe[2] = {-1, -1};
    if (pipe(err_pipe) != 0) {
        int err = errno;
        cleanup_before_fork(argv, cmd_len, envp, env_len,
                            env, dir_string, dir, log_path, log_path_c,
                            pid_path, pid_path_c, trusted_dir_string, trusted_dir);
        jclass ioe = (*env)->FindClass(env, "java/io/IOException");
        if (ioe) {
            char msg[128];
            snprintf(msg, sizeof(msg), "pipe() 失败: %s", strerror(err));
            (*env)->ThrowNew(env, ioe, msg);
        }
        return -1;
    }
    /* 写端设 CLOEXEC, 这样 exec 成功时父进程能读到 EOF 而不是永久阻塞。 */
    fcntl(err_pipe[1], F_SETFD, FD_CLOEXEC);

    /* ---- fork 点: 之后子进程里不能再有任何非 async-signal-safe 调用 ---- */

    pid_t pid = fork();
    if (pid < 0) {
        int err = errno;
        close(err_pipe[0]);
        close(err_pipe[1]);
        if (log_fd >= 0) close(log_fd);
        cleanup_before_fork(argv, cmd_len, envp, env_len,
                            env, dir_string, dir, log_path, log_path_c,
                            pid_path, pid_path_c, trusted_dir_string, trusted_dir);
        jclass ioe = (*env)->FindClass(env, "java/io/IOException");
        if (ioe) {
            char msg[128];
            snprintf(msg, sizeof(msg), "fork() 失败: %s", strerror(err));
            (*env)->ThrowNew(env, ioe, msg);
        }
        return -1;
    }

    if (pid == 0) {
        close(err_pipe[0]);
        if (dir) chdir(dir);
        child_exec(err_pipe[1], (int) tun_fd, log_fd, argv, envp);
        /* 不会到这里 */
        _exit(127);
    }

    /* ---- 父进程 ---- */
    close(err_pipe[1]);
    if (log_fd >= 0) close(log_fd);

    /*
     * **带超时地**等 execve 的结果。
     *
     * 原来是裸 read(): 只要子进程既没 exec 成功 (那会关掉写端) 也没能回报 errno,
     * 这个 read 就会永远挂着。而调用它的线程是**界面线程** (MihomoEngine.spawn
     * 由服务的协程在主线程上调用) —— 一个卡住的内核会把整个 App 冻在那里,
     * 用户看到的只有"点了没反应", 连日志都没有 (审计 N14)。
     *
     * poll 只回答"写端有没有动静", 语义上和 read 完全一致 (有数据 = 可读,
     * exec 成功 = 写端关闭 = POLLHUP 也可读), 但它能给出一个截止时间。
     * 5 秒是个宽松到不可能误伤的值: execve 要么立刻成功, 要么立刻带着 errno
     * 失败, 中间只有"内核卡在某个文件系统上"这一种情况会超时 —— 那种情况下
     * 把它杀掉并如实报错, 比让界面一直转圈好得多。
     */
    int child_err = 0;
    ssize_t got = -1;
    for (int attempt = 0; attempt < 2; attempt++) {
        struct pollfd wait_fd;
        wait_fd.fd = err_pipe[0];
        wait_fd.events = POLLIN;
        wait_fd.revents = 0;
        int pr = poll(&wait_fd, 1, EXEC_WAIT_TIMEOUT_MS);
        if (pr < 0 && errno == EINTR) continue;   /* 被信号打断, 重来一次 */
        if (pr <= 0) {
            /* 超时 (0) 或 poll 自己失败 (<0) = 我们不知道 exec 成没成。 */
            got = -1;
            break;
        }
        got = read(err_pipe[0], &child_err, sizeof(child_err));
        break;
    }
    close(err_pipe[0]);

    /*
     * 判定 execve 到底成没成 (原理见 child_exec 上方 err_pipe 的说明):
     *   - 读到 0 字节 = 写端已被内核在 exec 时关掉 = exec 成功;
     *   - 读到 sizeof(int) 字节 = 子进程把 errno 写回来了 = exec 失败。
     * 这一步**必须在释放 argv 之前做完**, 因为失败时要用 argv[0] 报错。
     */
    if (got < 0) {
        /*
         * 等不到结论。不能把子进程留成一个"不知道在干什么"的进程: 它可能正卡在
         * execve 里, 也可能已经 exec 成功只是写端没关 (内核 bug 级的情况)。
         * 这里选**杀掉并报错** —— 留着一个半启动的内核占着 9090 和 TUN fd,
         * 下一次连接会被它骗成"已连接", 那是更难查的一种坏。
         */
        const char *prog_for_msg = argv[0] ? argv[0] : "?";
        char msg[512];
        snprintf(msg, sizeof(msg),
                 "等待 execve 结果超时 (%d ms), 已放弃启动: %s",
                 EXEC_WAIT_TIMEOUT_MS, prog_for_msg);

        kill(pid, SIGKILL);
        int status = 0;
        waitpid(pid, &status, 0);

        cleanup_before_fork(argv, cmd_len, envp, env_len,
                            env, dir_string, dir, log_path, log_path_c,
                            pid_path, pid_path_c, trusted_dir_string, trusted_dir);

        jclass ioe = (*env)->FindClass(env, "java/io/IOException");
        if (ioe) (*env)->ThrowNew(env, ioe, msg);
        return -1;
    }

    if (got == (ssize_t) sizeof(child_err)) {
        /*
         * execve 失败。子进程此时已经在 child_exec 里 _exit(127), 必须
         * waitpid 收尸 —— 不收就是僵尸进程, 反复连断会攒出一堆僵尸,
         * 最后 fork 不出来, 表现是"用久了就连不上, 重启 App 才好"。
         */
        const char *prog_for_msg = argv[0] ? argv[0] : "?";
        char msg[512];
        snprintf(msg, sizeof(msg), "execve 失败 (%s): %s", strerror(child_err), prog_for_msg);

        int status = 0;
        waitpid(pid, &status, 0);

        cleanup_before_fork(argv, cmd_len, envp, env_len,
                            env, dir_string, dir, log_path, log_path_c,
                            pid_path, pid_path_c, trusted_dir_string, trusted_dir);

        jclass ioe = (*env)->FindClass(env, "java/io/IOException");
        if (ioe) (*env)->ThrowNew(env, ioe, msg);
        return -1;
    }

    /*
     * exec 已经确认成功, 这时候才落 pid 文件 —— 一个已经死掉的 pid 写进去
     * 只会让停止逻辑去 kill 一个不存在的进程, 或者更糟: 一个刚好复用了这个
     * pid 号的无关进程。
     */
    write_pid_file(pid_path_c, pid);

    /* 释放 fork 前准备的内存 (子进程已经 exec 掉, 用不到了) */
    cleanup_before_fork(argv, cmd_len, envp, env_len,
                        env, dir_string, dir, log_path, log_path_c,
                        pid_path, pid_path_c, trusted_dir_string, trusted_dir);

    return (jint) pid;
}

/**
 * 非阻塞收尸。返回退出码, -1 = 还没退出或已经收过了。
 *
 * ## 为什么收尸这件事必须放在 native 侧
 *
 * 常识里收尸应该调 `waitpid()`, 但 **Android SDK 没有暴露它**:
 * `android.system.Os` 只有 `kill` / `chmod` / `access` 这类, 没有 `waitpid`
 * (可以拿 android.jar 用 javap 逐个方法核)。而唯一的平台替代品
 * `java.lang.ProcessHandle` 是 **Java 9 的 API, Android 上没有这个类** ——
 * 在 Kotlin 里写 `ProcessHandle.of(pid)` 会直接编译不过。
 *
 * 于是只剩两条路: 要么不收尸 (僵尸进程会一直占着 pid 表项, 反复连断之后
 * fork 不出来, 表现是"用久了就连不上, 重启 App 才好"), 要么自己提供
 * `waitpid`。这是后者的全部理由 —— 就这一件事, 但它决定了长跑稳定性。
 *
 * WNOHANG 保证不阻塞: 子进程还活着时立刻返回 0, 不会把调用方的线程挂住。
 */
JNIEXPORT jint JNICALL
Java_com_accesspilot_hongxing_core_NativeLauncher_reapExited(
        JNIEnv *env, jclass clazz, jint target_pid) {
    (void) env;
    (void) clazz;

    if (target_pid <= 0) return -1;

    int status = 0;
    pid_t r = waitpid((pid_t) target_pid, &status, WNOHANG);
    if (r <= 0) return -1;   /* 0 = 还活着; -1 = 不是我们的子进程 / 已收过 */

    /*
     * 统一成"退出码": 正常退出取低 8 位, 被信号打断则返回 128+signo ——
     * 和 shell 的习惯一致, 这样日志里看到的数字能直接对上 shell 的经验。
     */
    if (WIFEXITED(status)) return (jint) WEXITSTATUS(status);
    if (WIFSIGNALED(status)) return (jint) (128 + WTERMSIG(status));
    return -1;
}
