package com.accesspilot.hongxing.ui

import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import com.accesspilot.hongxing.core.EngineController
import com.accesspilot.hongxing.ui.screen.HomeScreen
import com.accesspilot.hongxing.ui.theme.HongxingTheme

/**
 * 红杏 Android · 界面总入口。
 *
 * MainActivity 只需要:
 * ```
 * setContent {
 *     HongxingRoot(
 *         controller = engine,
 *         onPermissionNeeded = { vpnPermissionLauncher.launch(prepareIntent) },
 *     )
 * }
 * ```
 *
 * 为什么把主题也包进来: 主题是界面的一部分, 让调用方记得手写 `HongxingTheme { }`
 * 是没必要的负担 —— 忘一次, 整个 App 就退回 Material 默认紫色, 而那种错误在
 * 代码评审里几乎看不出来。
 *
 * [onPermissionNeeded] 是可选的, 传 null 就表示"权限申请由 controller 自己负责":
 * 没授权时点圆钮仍然走 `toggle()`, 由引擎实现去触发 `VpnService.prepare`。
 * 两种接线都支持, 因为真正能决定的是 VpnService 在哪一层持有。
 */
@Composable
fun HongxingRoot(
    controller: EngineController,
    modifier: Modifier = Modifier,
    onPermissionNeeded: (() -> Unit)? = null,
) {
    HongxingTheme {
        Surface(
            modifier = modifier.fillMaxSize(),
            color = MaterialTheme.colorScheme.background,
        ) {
            HomeScreen(
                controller = controller,
                onPermissionNeeded = onPermissionNeeded,
            )
        }
    }
}
