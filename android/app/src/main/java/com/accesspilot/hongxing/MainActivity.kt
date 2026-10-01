package com.accesspilot.hongxing

import android.Manifest
import android.app.Activity
import android.content.Intent
import android.content.pm.PackageManager
import android.net.VpnService
import android.os.Build
import android.os.Bundle
import android.util.Log
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.lifecycle.lifecycleScope
import com.accesspilot.hongxing.core.EngineControllerImpl
import com.accesspilot.hongxing.core.EngineRuntime
import com.accesspilot.hongxing.core.HongxingVpnService
import com.accesspilot.hongxing.ui.HongxingRoot
import com.accesspilot.hongxing.ui.theme.HongxingTheme
import kotlinx.coroutines.launch
import kotlin.concurrent.thread

/**
 * 红杏 Android · 唯一的 Activity —— 界面与引擎的接线层。
 *
 * 和桌面端 `gui/__init__.py` 是同一个角色: 把引擎交给界面, 自己不做业务。
 * 职责只有三件:
 *   1. 构造 [EngineControllerImpl] 并交给 `HongxingRoot`;
 *   2. 处理 **VPN 授权** —— 它必须挂在 Activity 的 ActivityResult 上, 不能放在
 *      服务或引擎里, 所以这是这一层不可推卸的责任;
 *   3. 留一个 adb 可触发的 fd 实验入口 (`--ez fdprobe true`)。
 *
 * ## 为什么这里一个状态字段都没有
 *
 * 隧道活在前台服务里, Activity 随时可能被转屏/回收/切后台。状态全部由
 * `core/EngineRuntime` 那个进程级单例持有, 界面只是观察者
 * (`collectAsStateWithLifecycle`)。这样从后台回来看到的一定是真实状态,
 * 而不是 Activity 自己记的、早就过期的快照。
 *
 * ## 为什么 [controller] 用 lateinit
 *
 * [EngineControllerImpl] 的构造要 `Context`, 而 `applicationContext` 在
 * `onCreate` 之前拿不到(字段初始化早于 `attachBaseContext`)。
 */
class MainActivity : ComponentActivity() {

    private lateinit var controller: EngineControllerImpl

    /**
     * VPN 授权结果。
     *
     * `VpnService.prepare()` 返回非 null = 还没授权, 要把那个 Intent 拿去让用户
     * 确认; 返回 null = 已经授权过。用户点"确定"后回调到这里, 我们把结果交给
     * 引擎 —— 引擎不自己弹这个框, 那是 Activity 的活。
     */
    private val vpnPermission =
        registerForActivityResult(ActivityResultContracts.StartActivityForResult()) { result ->
            val granted = result.resultCode == Activity.RESULT_OK
            Log.i(TAG, "VPN 授权结果: granted=$granted")
            controller.onVpnPermissionResult(granted)
        }

    /** Android 13+ 的通知权限 —— 没有它前台服务的常驻通知不显示。 */
    private val notificationPermission =
        registerForActivityResult(ActivityResultContracts.RequestPermission()) { granted ->
            Log.i(TAG, "通知权限: granted=$granted")
        }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        controller = EngineControllerImpl(applicationContext)

        // 调试入口:
        //   adb shell am start -n com.accesspilot.hongxing.debug/com.accesspilot.hongxing.MainActivity --ez fdprobe true
        intent?.let { handleDebugExtras(it) }

        askNotificationPermissionIfNeeded()

        setContent {
            HongxingTheme {
                var pendingStart by remember { mutableStateOf(false) }
                val status by controller.status.collectAsStateWithLifecycle()

                HongxingRoot(
                    controller = controller,
                    onPermissionNeeded = {
                        val prepare = VpnService.prepare(this@MainActivity)
                        if (prepare == null) {
                            // 已经授权过(用户上次点过"始终允许"), 直接放行
                            controller.onVpnPermissionResult(true)
                        } else {
                            pendingStart = true
                            vpnPermission.launch(prepare)
                        }
                    },
                )

                // 授权回来后, 如果这次授权是"用户点了开关才去要的", 就接着把连接
                // 走完 —— 否则用户点一次开关、授权完还得再点一次, 这在蓝灯那类
                // 客户端里是很明显的体验缺陷。
                //
                // LaunchedEffect 本身就是协程作用域, 所以能直接调 suspend 的 toggle()。
                LaunchedEffect(status.vpnPermission, pendingStart) {
                    if (pendingStart && status.vpnPermission) {
                        pendingStart = false
                        controller.toggle()
                    }
                }
            }
        }
    }

    /**
     * 第二次 `am start` 会走到这里, 而不是 [onCreate]。
     *
     * 真实事故(2026-10-01): `MainActivity` 在清单里是 `launchMode="singleTask"`,
     * 所以 App 已经在前台时再发一次
     * `am start ... --ez fdprobe true`, 系统**不会重建 Activity**, 只调
     * `onNewIntent` —— 而第一版只在 `onCreate` 里读那个 extra, 于是命令发出去了、
     * 提示也是 "intent has been delivered to currently running top-most instance",
     * 但探针一动不动、logcat 一条都没有。
     *
     * 这类"命令看起来成功了、其实什么都没发生"的坑, 一次就够记一辈子 ——
     * 所以 extra 的读取必须同时挂在两个入口上。
     */
    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        setIntent(intent)
        handleDebugExtras(intent)
    }

    /**
     * adb 可触发的调试入口。
     *
     * 为什么要单独留一个 `--ez connect true`: 真机排查时要能**把"界面点击"和
     * "引擎启动"两件事分开**。实测遇到过"点大圆钮没反应"—— 没有 mihomo 进程、
     * 服务没起来、logcat 一条日志都没有, 但 `dumpsys input` 显示事件投递成功、
     * `uiautomator` 也确认可点区域就在那儿。这种时候如果只能靠手点, 就分不清
     * 是"点击没送到 Compose"还是"送到了但引擎起不来"。
     *
     * 用法:
     *   adb shell am start -n <pkg>/MainActivity --ez connect true
     *   adb shell am start -n <pkg>/MainActivity --ez fdprobe true
     */
    private fun handleDebugExtras(intent: Intent) {
        if (intent.getBooleanExtra(EXTRA_FD_PROBE, false)) {
            runFdProbe()
        }
        if (intent.getBooleanExtra(EXTRA_CONNECT, false)) {
            Log.i(TAG, "调试入口: 直接调 toggle() (绕过界面点击)")
            lifecycleScope.launch {
                val r = controller.toggle()
                Log.i(TAG, "toggle() 结果: ${r.isSuccess} ${r.exceptionOrNull()?.message ?: ""}")
            }
        }
    }

    private fun askNotificationPermissionIfNeeded() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.TIRAMISU) return
        if (checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS)
            == PackageManager.PERMISSION_GRANTED
        ) return
        notificationPermission.launch(Manifest.permission.POST_NOTIFICATIONS)
    }

    /**
     * fd 对比实验: "同一个子进程命令, 走 ProcessBuilder vs 走 NativeLauncher,
     * 子进程里还能不能看到 VpnService 的 fd"。
     *
     * ## 为什么交给服务去做
     *
     * `VpnService.Builder` 是 `VpnService` 的**内部类(非静态)**, 而 VpnService
     * 实例只能由系统创建 —— 自己 `new` 出来的对象底层没有系统分配的 VPN 接口,
     * `establish()` 必然失败。(第一版就是那么写的, 编译期就报
     * `inner class Builder can only be called with a receiver of the containing class`。)
     * 所以 TUN 由真正跑起来的 [HongxingVpnService] 建, 建好把 fd 传进实验,
     * 报告写回 [EngineRuntime.fdProbeReport]。
     *
     * ## 为什么轮询而不是等回调
     *
     * 跨组件传一个字符串不值得为它设计一套回调 —— 而且这是**诊断代码**,
     * 越笨越好: 出问题时不该再引入一个可能出问题的机制。
     */
    private fun runFdProbe() {
        Log.i(TAG, "FdProbe 开始 (真 TUN 由 HongxingVpnService 建) …")
        EngineRuntime.fdProbeReport = null
        startService(
            Intent(this, HongxingVpnService::class.java)
                .setAction(HongxingVpnService.ACTION_FD_PROBE),
        )

        thread(name = "fdprobe-wait") {
            val deadline = System.currentTimeMillis() + 60_000
            var report: String? = null
            while (System.currentTimeMillis() < deadline) {
                report = EngineRuntime.fdProbeReport
                if (report != null) break
                Thread.sleep(300)
            }
            // 逐行打出来: logcat 单条有长度上限, 整段一次打会被截断
            val text = report ?: "FdProbe 超时(60s), 没等到报告"
            text.lines().forEach { Log.i(TAG, "[FdProbe] $it") }
            Log.i(TAG, "FdProbe 结束")
        }
    }

    private companion object {
        const val TAG = "HongxingMain"
        const val EXTRA_FD_PROBE = "fdprobe"
        const val EXTRA_CONNECT = "connect"
    }
}
