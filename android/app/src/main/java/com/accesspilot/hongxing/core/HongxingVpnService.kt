package com.accesspilot.hongxing.core

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.net.VpnService
import android.os.Build
import android.os.IBinder
import android.os.ParcelFileDescriptor
import androidx.core.app.NotificationCompat
import com.accesspilot.hongxing.BuildConfig
import com.accesspilot.hongxing.R
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

/**
 * 红杏 Android · 前台服务 + VpnService
 *
 * 这个类是"隧道"这两个字的物理载体, 它负责三件事, 顺序不能错:
 *
 * ```
 *   establish() 拿 TUN fd  ->  交给内核 (EngineRuntime.start)  ->  startForeground
 *   停内核 (EngineRuntime.stop)  ->  关 TUN fd  ->  stopForeground + stopSelf
 * ```
 *
 * ## TUN fd 的所有权只有一份 (审计 K1)
 *
 * [tunnel] 这个 `ParcelFileDescriptor` 是 TUN 的**唯一所有者**: `establish()`
 * 返回它, `close()` 关掉它, 而"隧道还在不在"这个事实就等于"它是不是 null"。
 * VpnService 的语义正是如此 —— 隧道活到这个 fd 被关掉 (或者进程死掉) 为止。
 *
 * 传给内核的是它的一个 `dup()` ([coreTunnel]): 子进程通过 fork 继承的是
 * **同一个打开文件描述的另一个引用**, 父进程那份在 fork 完成之后就可以关了。
 * 改之前这里是 `detachFd()`, 之后父进程手上只剩一个整数, **没有任何对象能负责
 * 关它** —— 于是 `closeTunnel()` 永远是空操作: 每连一次漏一个 fd, 而"断开"
 * 之后 tun0 和 `0.0.0.0/0` 路由还挂在一个没有读者的接口上, 整台设备上不了网,
 * 直到进程被杀。
 *
 * ## 为什么是前台服务, 而不是让 Activity 自己拿着
 *
 * Activity 随时可能被回收 (转屏、切后台、内存压力)。VPN 隧道必须活得比
 * Activity 长 —— 用户锁屏之后还指望流量在走。前台服务是 Android 上唯一
 * 一个"系统知道你在干活、不会随便杀"的形态, 而且它顺带把"正在保护网络"
 * 这个状态放到通知栏, 用户随时能看见和断开。
 *
 * ## 两个会让人踩坑的地方
 *
 * 1. **`addDisallowedApplication(packageName)` 是必需的**。不加的话内核
 *    自己的出站流量 (它要去连代理服务器) 也会被我们这个 TUN 抓走, 于是
 *    内核要把流量发给代理, 代理连接又要经过内核 —— 死循环。现象是
 *    "显示连上了, 但什么都打不开", 而且**没有任何报错**。
 * 2. **`startForeground` 必须在内核确认就绪之后调**。前台服务的启动有严格
 *    时间预算 (几秒), 而内核冷启动要拉规则集、可能超过这个预算。所以
 *    先做耗时的事、再 `startForeground`, 中间这段时间服务仍然算"已启动"
 *    (由 `startForegroundService` 给的一段宽限期兜着)。
 */
class HongxingVpnService : VpnService() {

    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main.immediate)

    /**
     * 系统给的 TUN —— **唯一所有者**。见类注释。
     *
     * 必须持有到内核停掉为止: 提前 close 等于断隧道, 而且内核手上那份引用
     * 也会变成"接不到新流量"的空壳。
     */
    @Volatile
    private var tunnel: ParcelFileDescriptor? = null

    /**
     * 交给内核的那一份 `dup()`。
     *
     * 它只需要活到 `fork()` 完成 —— 那一刻子进程已经拿到了同一份打开文件描述
     * 的另一个引用, 父进程这一份就是多余的。留着它的坏处很具体: 多一个引用计数
     * 之后, "谁还开着这个 TUN"变得难以判断, 而断开时我们要的正是"内核一死,
     * 关掉我们这份, 接口就必须消失"。
     */
    @Volatile
    private var coreTunnel: ParcelFileDescriptor? = null

    private var foregroundStarted = false

    // ------------------------------------------------------------ 服务生命周期

    override fun onCreate() {
        super.onCreate()
        EngineRuntime.init(this)
        // 内核**意外**死亡时由运行时回调这里。为什么必须是服务: TUN 的 fd 归它,
        // 也只有它能关 (见 [closeTunnel] 与 EngineRuntime 的看门狗)。
        EngineRuntime.onEngineDied = {
            scope.launch {
                // 内核已经死了, 这个 TUN 现在没有读者 —— 留着它等于把整台设备的
                // 流量吸进黑洞。关掉至少让流量回到直连, 然后按"没在连接"收尾。
                closeTunnel()
                teardown()
            }
        }
        // `startService` 失败时 (Android 12+ 的后台启动限制) 界面仍然要能断开,
        // 那条退路在进程内直接调 EngineRuntime.stop, 而"关 TUN"只能由服务做。
        EngineRuntime.tunnelCloser = { closeTunnel() }
        createNotificationChannel()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        // 这一次请求的尝试 id (见 EngineRuntime.AttemptState)。`<= 0` = 不是界面
        // 发起的 (通知栏那个"断开"按钮), 没有人等结论。
        val attempt = intent?.getLongExtra(EXTRA_ATTEMPT, NO_ATTEMPT) ?: NO_ATTEMPT
        when (intent?.action) {
            ACTION_STOP -> {
                // 通知栏那个"断开"按钮走这里。
                scope.launch { disconnectAndStop(attempt) }
            }

            ACTION_START -> {
                // 一条硬性的平台要求: startForegroundService 之后必须在几秒内
                // startForeground, 否则系统直接抛 ForegroundServiceDidNotStartInTime
                // 把进程干掉。所以这里先挂一个"正在连接"的通知占位, 等内核
                // 真的起来了再换成正式文案 —— 不能让"等内核"这段时间裸奔。
                startForegroundWith(getString(R.string.notification_connecting))
                scope.launch { connect(attempt) }
            }

            ACTION_FD_PROBE -> {
                // fd 传递实验。**不建隧道、不起内核**, 只借这个真实例建一个
                // TUN 拿 fd, 然后交给 FdProbe 去验"它能不能跨 exec"。
                //
                // 为什么必须由服务来做: `Builder` 是 VpnService 的内部类,
                // 需要一个由系统创建并挂好 Binder 的真实例, 外面 new 不出来。
                //
                // 这一整条路 (以及 Activity 那边的 --ez fdprobe) 在正式包里是死的:
                // 它会建 TUN、还会经启动桥跑 `/system/bin/sh`, 而入口是导出的
                // launcher Activity —— 任何 App 或 adb 都能塞 extra 进来。开关是
                // **编译期常量**, 见 MainActivity 里那段说明。
                if (BuildConfig.DEBUG_ENTRYPOINTS) {
                    scope.launch { runFdProbe() }
                }
            }

            else -> {
                // 裸启动 (比如系统在 START_STICKY 之后重启服务): 没有 tunnel
                // 也没有内核, 无事可做。返回 NOT_STICKY 让系统别再拉我们起来。
            }
        }
        return START_NOT_STICKY
    }

    /**
     * 用户的"断开"意图 —— 无论是通知栏按钮还是界面上的开关。
     *
     * 对界面来说这只是一次普通的 stop, 所以不单独区分。
     */
    private suspend fun disconnectAndStop(attemptId: Long) {
        val result = EngineRuntime.stop { closeTunnel() }

        // 内核停不掉时 [EngineRuntime.stop] 会**刻意保留隧道** (关 TUN 会让在途
        // 流量走直连, 那是这个 App 最不能接受的一类泄漏)。那时候通知必须留在
        // 通知栏上, 而且文案要改成"断开失败, 点这里重试" —— 否则用户失去的是
        // 唯一的断开入口, 只剩"杀 App"这一条路。
        if (result.isFailure && foregroundStarted) {
            startForegroundWith(getString(R.string.notification_disconnect_failed))
        }

        teardown()
        // 结论只回给"这一次"尝试 (K2)。
        EngineRuntime.finishAttempt(attemptId, result.exceptionOrNull()?.message)
    }

    /**
     * 跑一次 fd 对比实验, 结果留在 [EngineRuntime.fdProbeReport] 里,
     * 由 `MainActivity` 取走打 logcat。
     *
     * 顺序有讲究: **先建 TUN 拿 fd, 再跑实验**。建 TUN 会占住系统里那唯一的
     * VPN 名额, 所以实验也必须自己建、自己放 —— 不能借已经连着的那个, 那会
     * 要求在"验证能不能连上"之前先连上, 是循环论证。
     */
    private suspend fun runFdProbe() {
        var probeTun: ParcelFileDescriptor? = null
        var note = ""
        try {
            if (VpnService.prepare(this) == null) {
                probeTun = establishProbeTun(this)
                if (probeTun == null) note = "establish() 返回 null, 真 TUN 未验证"
            } else {
                note = "还没有 VPN 授权: 先在界面上点一次开关拿到授权, 再重跑本实验"
            }
        } catch (t: Throwable) {
            note = "建 TUN 失败: ${t.javaClass.simpleName}: ${t.message}"
        }

        val report = withContext(Dispatchers.IO) {
            runCatching {
                FdProbe(EngineRuntime.workDir(this@HongxingVpnService))
                    .run(probeTun?.fd ?: -1)
            }.getOrElse { "FdProbe 自身抛异常: ${it.javaClass.simpleName}: ${it.message}" }
        }

        EngineRuntime.fdProbeReport =
            if (note.isBlank()) report else "$report\n注: $note\n"

        // 实验用掉的 TUN 必须还回去 —— 留着它会一直占着 VPN 名额, 用户接下来
        // 点"连接"会直接失败, 一个和实验毫无关系的怪现象。
        //
        // 这里**不**走 detachFd: 实验全程由本进程持有这个 fd, 用 PFD 自己
        // close 才是最干净的收尾 (detach 出去之后就只剩一个整数, 没有对象
        // 能负责关它了)。
        runCatching { probeTun?.close() }
        stopSelf()
    }

    /**
     * 建一个和正式连接**同参数**的 TUN, 给 fd 实验用。
     *
     * 参数刻意和 [establishTunnel] 完全一致 (走同一段 `configureTunBuilder`):
     * 实验条件和服务运行时不一样的话, 验出来的结论就不能往生产上套。
     */
    private fun establishProbeTun(context: Context): ParcelFileDescriptor? {
        val builder = configureTunBuilder(Builder()).setSession("红杏 fd 实验")
        builder.addDisallowedApplication(context.packageName)
        return builder.establish()
    }

    private fun connect(attemptId: Long) {
        scope.launch {
            var error: String? = null
            try {
                val result = EngineRuntime.start(
                    establish = { establishTunnel() },
                    onTunnelUp = { startForegroundWith(getString(R.string.notification_protecting)) },
                )
                val message = result.getOrNull().orEmpty()
                error = when {
                    result.isFailure ->
                        result.exceptionOrNull()?.message?.takeIf { it.isNotBlank() } ?: "连接失败"
                    // 见 EngineRuntime.start 的返回值说明: "成功但值是错误文案" =
                    // 隧道其实没起来。
                    message.isNotEmpty() -> message
                    else -> null
                }
            } catch (t: CancellationException) {
                // 服务正在销毁 / 协程被取消。清理仍然要跑 (finally), 但取消必须
                // 继续往上抛 —— 吞掉它会让调用方以为这次启动"正常结束了"。
                error = "连接已取消"
                throw t
            } catch (t: Throwable) {
                error = t.message?.takeIf { it.isNotBlank() }
                    ?: "连接失败 (${t.javaClass.simpleName})"
            } finally {
                // ---- 收尾必须在 finally 里, 而且必须全是不挂起的操作 ----
                //
                // 取消路径上协程已经是 cancelled, 任何 suspend 调用都会立刻抛。
                // 而这里要做的两件事都关系到"会不会留下一台断网的手机":
                //   1. 把交给内核的那份 dup 关掉 (fork 早就完成了);
                //   2. 隧道没起来的话, 把 TUN 也关掉 —— EngineRuntime 只如实
                //      报了状态, fd 还在我们手上, 留着它就是留着一个"流量全部
                //      被吸进黑洞"的 TUN, 而界面显示的是没连上。
                closeCoreFd()
                if (error != null) {
                    closeTunnel()
                    teardown()
                }
                // 结论落在 finally 里: 成功、失败、取消三条路都要有人回答,
                // 否则界面会一直等到超时才说话。
                EngineRuntime.finishAttempt(attemptId, error)
            }
        }
    }

    // ------------------------------------------------------------ VpnService

    /**
     * 建 TUN, 并把**一份 dup** 交给内核。返回要传下去的 fd, 失败返回 -1。
     *
     * 这里**不** catch `Exception` 后静默: 每一个失败原因都对应一句用户能
     * 看懂的话, 而 `establish()` 返回 null 是其中最常见也最容易被忽略的一个
     * (没授权、或者别的 VPN 正在跑)。返回 -1 让上层统一报错。
     */
    private fun establishTunnel(): Int {
        // 保险: 上一次的 fd 必须先关掉, 否则第二次 establish() 会把前一个 TUN
        // 直接漏掉 (它没有别的所有者了)。正常路径上上游已经关过了, 这里是
        // 幂等的兜底。
        closeTunnel()

        return try {
            val builder = configureTunBuilder(Builder())
                .setSession(getString(R.string.app_name))

            val pfd = builder.establish()
            if (pfd == null) {
                // establish() 返回 null 的两种原因: 没授权 (用户在系统对话框里
                // 点了取消), 或者系统里已经有另一个 VPN 在跑。两种情况界面
                // 都要提示用户去处理, 所以把话写清楚。
                EngineRuntime.setVpnPermission(false)
                return -1
            }
            EngineRuntime.setVpnPermission(true)

            // 原件由服务掌管 —— 它才是"隧道还在不在"的判据, 也是断开时真正
            // 让 tun0 消失的那一下 (见 [closeTunnel])。
            tunnel = pfd

            // 给内核的那一份。用 dup() 而不是 detachFd(): detach 之后这个 fd
            // 就没有所有者了, 谁都不会关它 (审计 K1 的根因)。dup 出来的是
            // **同一个打开文件描述的另一个引用**, 对 fork/execve 来说完全等价,
            // 但它有一个明确的对象负责关闭。
            val dup = ParcelFileDescriptor.dup(pfd.fileDescriptor)
            coreTunnel = dup
            EngineRuntime.setTunnelHeld(true)
            dup.fd
        } catch (t: Throwable) {
            // SecurityException (没授权) / IllegalArgumentException (参数被拒)
            // / IOException (dup 失败) 都归到这里。返回 -1 让上层统一处理。
            closeTunnel()
            -1
        }
    }

    /**
     * TUN 的地址/路由/DNS/MTU 配置 —— **正式连接和 fd 实验共用这一段**。
     *
     * 抽出来不是为了少写几行, 而是为了让"实验条件 == 生产条件"这件事由代码
     * 结构保证: 哪天有人调了这里的 MTU 或路由, 两边会一起变。如果实验那边
     * 复制一份参数, 它会悄悄漂移, 于是 fd 实验绿着、生产却挂在别的原因上 ——
     * 那种"实验通过但功能不对"最难查。
     */
    private fun configureTunBuilder(builder: Builder): Builder {
        builder
            // 172.19.0.1/30: 刻意选一个不在家用网段里的地址。
            // 用 192.168.x 会和很多路由器撞, 撞上以后内网设备会变得
            // 时通时不通 —— 这类问题极难归因到"IP 段选错了"。
            .addAddress(TUN_ADDRESS, TUN_PREFIX)
            // 全部流量进隧道。分流由内核按规则做, 这里不筛。
            .addRoute("0.0.0.0", 0)
            .addDnsServer(TUN_DNS)
            .setMtu(TUN_MTU)

        // IPv6 一起接管。内核那边 `ipv6: true`, 不接管的话在双栈网络下
        // 部分应用会走 v6 直连绕过隧道 —— 又是一个"显示已连接但漏流量"。
        runCatching {
            builder.addAddress(TUN_ADDRESS_V6, TUN_PREFIX_V6)
            builder.addRoute("::", 0)
        }

        // 见类注释第 1 条: 少了这一行, 内核自己的出站流量也会被这个 TUN 抓走,
        // 形成"内核要把流量发给代理, 而代理连接又要经过内核"的死循环。
        builder.addDisallowedApplication(packageName)
        return builder
    }

    /**
     * 关掉交给内核的那一份 dup。**只能在 fork 完成之后调**。
     *
     * 见 [coreTunnel]: 子进程继承的是同一份打开文件描述, 父进程这一份在
     * `forkExec` 返回之后就没有用途了。提前调 (fork 之前) 等于把要交给内核的
     * fd 关掉, 内核会报 "file descriptor is not valid" 然后退出。
     */
    private fun closeCoreFd() {
        runCatching { coreTunnel?.close() }
        coreTunnel = null
    }

    /**
     * 关掉 TUN。
     *
     * ## 只有在**内核已经停掉之后**才能调
     *
     * 见 [EngineRuntime.stop] 里那段关于"先关 fd 会让在途流量走直连"的说明。
     * 这个顺序是安全属性, 不是优化。反过来说, 内核已经死了的时候**必须**关
     * (看门狗那条路): 一个没有读者的 TUN 会把整台设备的流量吸进黑洞。
     *
     * ## 这里关的是原件, 而它真的会拆掉接口
     *
     * 这一点靠的是"谁还持有这个 TUN": 内核进程死了 + 交给它的那份 dup 也关了
     * 之后, [tunnel] 就是最后一个引用, 关掉它内核就会把 tun0 和 `0.0.0.0/0`
     * 路由一起撤掉。所以这里绝不能用 `detachFd()` 那种"把 fd 送出去"的写法 ——
     * 送出去就没有对象能关了 (审计 K1)。
     */
    private fun closeTunnel() {
        // dup 先关: 它只是"给内核用的那一份", 真正让 tun0 消失的是下面这个原件。
        // 反过来的顺序不会出错 (dup 是独立引用), 但先关它语义更清楚。
        closeCoreFd()
        runCatching { tunnel?.close() }
        tunnel = null
        // 这个标志是给"服务外面"判断隧道还在不在用的 (见 EngineRuntime.tunnelHeld):
        // 判断一次断开有没有真的成功, 只能看这个事实, 不能看"服务还在不在"。
        EngineRuntime.setTunnelHeld(false)
    }

    /**
     * 系统回收了 VPN 授权。
     *
     * 触发场景: 用户从系统设置里撤销、或者另一个 VPN App 抢走了。
     * **当成用户主动断开处理** —— 这种情况下隧道其实已经被系统拆了,
     * 我们能做的就是赶紧把内核停掉, 别让它对着一个不存在的 TUN 空转
     * (那会不停地刷 dial 失败日志, 白耗电)。
     */
    override fun onRevoke() {
        scope.launch {
            EngineRuntime.stop { closeTunnel() }
            teardown()
        }
        super.onRevoke()
    }

    override fun onDestroy() {
        // 服务被系统销毁时可能还连着 —— 这里要做的是**真的收拾干净**, 而不是
        // 只停两个协程:
        //  * 内核是 setsid() 出去的长驻进程, 不杀它就会带着 TUN fd 活到下一次
        //    启动, 变成审计 K3 里那个"孤儿回答 /version → 界面显示已连接而
        //    隧道其实是死的";
        //  * TUN fd 不关, tun0 和 0.0.0.0/0 路由就会留在一个没有读者的接口上
        //    —— 表现是整台设备上不了网, 直到进程被杀 (审计 K1)。
        //
        // 顺序仍然不能反: 先停内核, 再关 TUN (见 EngineRuntime.stop 的说明)。
        // 这里用的全是**非挂起**调用: onDestroy 里没有等待的余地, 而且这个
        // 调用点可能已经处在取消路径上 (任何 suspend 都会立刻抛)。
        EngineRuntime.onEngineDied = null
        EngineRuntime.tunnelCloser = null
        EngineRuntime.shutdown(killCore = true)
        closeCoreFd()
        closeTunnel()
        scope.cancel()
        super.onDestroy()
    }

    override fun onBind(intent: Intent?): IBinder? = null

    // ---------------------------------------------------------------- 通知

    /**
     * 换通知文案 / 首次进入前台。
     *
     * `foregroundServiceType` 在 API 34+ 是**必填**的: targetSdk 35 下不传
     * 会直接抛 `MissingForegroundServiceTypeException`。用 specialUse 是因为
     * VpnService 在平台的 type 枚举里没有专属项 (清单里也对应声明了
     * `foregroundServiceType="specialUse"`)。
     */
    private fun startForegroundWith(text: String) {
        val notification = buildNotification(text)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.UPSIDE_DOWN_CAKE) {
            startForeground(NOTIFICATION_ID, notification, ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE)
        } else {
            startForeground(NOTIFICATION_ID, notification)
        }
        foregroundStarted = true
    }

    /**
     * 退到后台并结束自己。
     *
     * ## 判据是"隧道还开着吗", 不是"阶段标签写着什么" (审计 N2)
     *
     * 改之前这里判的是 `phase == Connected`, 而"内核停不掉"那条路会把阶段
     * 设成 `Error` **同时刻意保留隧道** (见 [EngineRuntime.stop]: 关 TUN 会让
     * 在途流量走直连, 那是这个 App 最不能接受的一类泄漏)。于是那段代码做了
     * 正好相反的事: 通知消失、`stopSelf()`、`onDestroy` 再取消看门狗 ——
     * 用户被告知"已保留隧道, 请重试断开", 却同时失去了通知栏那个"断开"按钮
     * 和唯一的界面入口, 而 mihomo 还在跑、TUN 还开着。
     *
     * 所以判据换成事实本身: [tunnel] 不是 null = 隧道还在 = 服务必须活着。
     * 阶段标签只是给人看的文字, 它可以有四种写法, 而"隧道还在不在"只有两种。
     */
    private fun teardown() {
        if (tunnel != null) return
        if (foregroundStarted) {
            runCatching { stopForeground(STOP_FOREGROUND_REMOVE) }
            foregroundStarted = false
        }
        stopSelf()
    }

    /**
     * 常驻通知。带一个"断开"按钮 —— 用户不开 App 就能断。
     *
     * 这不是可选的贴心: VPN 一旦建立, 用户如果找不到断开入口, 唯一能做的
     * 就是去系统设置里"忘记 VPN"或者卸载 App, 那体验是灾难性的。
     */
    private fun buildNotification(text: String): Notification {
        val openApp = PendingIntent.getActivity(
            this,
            REQUEST_OPEN,
            Intent().setClassName(packageName, MAIN_ACTIVITY),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
        )

        val disconnect = PendingIntent.getService(
            this,
            REQUEST_DISCONNECT,
            Intent(this, HongxingVpnService::class.java).setAction(ACTION_STOP),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
        )

        return NotificationCompat.Builder(this, CHANNEL_ID)
            .setContentTitle(getString(R.string.app_name))
            .setContentText(text)
            .setSmallIcon(notificationIcon())
            .setContentIntent(openApp)
            // ongoing + 不可滑掉: 这是"正在生效的系统级改变", 和音乐播放
            // 是同一类。能被随手划掉的状态栏通知会让用户以为 VPN 也停了。
            .setOngoing(true)
            .setSilent(true)
            .setPriority(NotificationCompat.PRIORITY_LOW)
            .setCategory(NotificationCompat.CATEGORY_SERVICE)
            .addAction(0, getString(R.string.notification_disconnect), disconnect)
            .build()
    }

    /**
     * 通知栏小图标。
     *
     * 用 App 自己的图标 (`applicationInfo.icon`) 而不是 `android.R.drawable`
     * 里的某个系统图标: 后者在 API 35 的公开资源里**没有一个稳妥的 VPN 类
     * 图标** (`stat_sys_vpn_ic` 是系统内部资源, 应用引用不到), 而且用系统图标
     * 会让用户分不清这条通知是谁发的。
     *
     * 已知不足: 直接复用 launcher 图标在状态栏里会显得偏大/偏彩色 (状态栏
     * 图标本该是单色剪影)。这需要一枚专门的 `ic_stat_hongxing` 单色小图,
     * 属于界面资源, 已报给 lead / android-ui; 在它到位之前用 launcher 图标
     * 至少能保证"通知显示得出来", 而不是让前台服务直接启动失败。
     */
    private fun notificationIcon(): Int =
        applicationInfo.icon.takeIf { it != 0 } ?: android.R.drawable.ic_dialog_info

    private fun createNotificationChannel() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) return
        val manager = getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        if (manager.getNotificationChannel(CHANNEL_ID) != null) return
        manager.createNotificationChannel(
            NotificationChannel(
                CHANNEL_ID,
                getString(R.string.notification_channel_name),
                // IMPORTANCE_LOW: 状态是给用户"随时能看到"的, 不是用来打扰他的。
                // 用 DEFAULT 的话每次连接都震一下, 一天连十次就会让人想卸载。
                NotificationManager.IMPORTANCE_LOW,
            ).apply {
                description = getString(R.string.notification_channel_desc)
                setShowBadge(false)
            },
        )
    }

    companion object {
        /** 建立隧道。只有界面会用 —— 服务自己不会平白无故连。 */
        const val ACTION_START = "com.accesspilot.hongxing.action.START"

        /** 断开。通知栏按钮和界面开关共用。 */
        const val ACTION_STOP = "com.accesspilot.hongxing.action.STOP"

        /**
         * 这一次请求的尝试 id (见 `EngineRuntime.AttemptState`)。
         *
         * 为什么把它塞进 Intent 而不是让服务自己去问: 服务可能还在处理上一次
         * 请求, 而"这一次"的结论必须回到"这一次"的发起者手上 (审计 K2)。
         * 通知栏那条路不带这个 extra, 服务会当成"没人等结论"处理。
         */
        const val EXTRA_ATTEMPT = "com.accesspilot.hongxing.extra.ATTEMPT"

        /** 没有尝试 id 时的哨兵值。见 [EXTRA_ATTEMPT]。 */
        const val NO_ATTEMPT = -1L

        /**
         * 跑一次 fd 传递实验 (诊断用, 见 [FdProbe])。
         *
         * 做成一个 Service action 而不是在 Activity 里直接跑, 是因为它必须
         * 建一个真实的 TUN 才能验"真 TUN fd 能不能跨 exec" —— 而
         * `VpnService.Builder` 是 VpnService 的**内部类**, 只有一个由系统
         * 创建、挂好 Binder 的实例才能用。自己 `VpnService()` 出来的对象
         * 底层没有系统分配的 VPN 接口, `establish()` 必然失败。
         *
         * adb 触发:
         * ```
         * adb shell am start -n <pkg>/com.accesspilot.hongxing.MainActivity --ez fdprobe true
         * ```
         */
        const val ACTION_FD_PROBE = "com.accesspilot.hongxing.action.FD_PROBE"

        const val TUN_ADDRESS = "172.19.0.1"
        const val TUN_PREFIX = 30
        const val TUN_ADDRESS_V6 = "fdfe:dcba:9876::1"
        const val TUN_PREFIX_V6 = 126

        /**
         * DNS 交给内核处理 (模板里 `dns-hijack: any:53` 会把它截下来),
         * 所以这里填任意一个可达的地址都行 —— 它的作用只是让 TUN 接口
         * 有一个 DNS, 否则应用解析域名时会直接失败。
         */
        const val TUN_DNS = "172.19.0.2"

        /** 和模板里的 `tun.mtu` 保持一致。两处不一致会导致分片行为诡异。 */
        const val TUN_MTU = 8500

        private const val CHANNEL_ID = "hongxing_vpn"
        private const val NOTIFICATION_ID = 1001
        private const val REQUEST_OPEN = 100
        private const val REQUEST_DISCONNECT = 101
        private const val MAIN_ACTIVITY = "com.accesspilot.hongxing.MainActivity"
    }
}
