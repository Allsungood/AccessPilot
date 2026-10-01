package com.accesspilot.hongxing

import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp

/**
 * 红杏 Android · 唯一的 Activity。
 *
 * 现在是最小骨架, 用来先把构建链路 (Gradle + AGP + Compose + 随包 so) 验通。
 * 真正的界面在 `ui/` 包里, 由 ui 那一侧实现; 这里只负责:
 *   1. 申请 VPN 权限 (VpnService.prepare 的 activity result 必须挂在 Activity 上)
 *   2. 把 EngineController 和界面接起来
 *
 * 为什么 Activity 不直接碰 VpnService:
 * VPN 隧道活在一个**前台服务**里, Activity 随时可能被系统回收。状态必须由
 * 服务持有, Activity 只是它的一个观察者 —— 否则转个屏、切到后台再回来,
 * 界面就和服务对不上了。
 */
class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContent {
            MaterialTheme {
                Surface(modifier = Modifier.fillMaxSize()) {
                    Scaffold()
                }
            }
        }
    }
}

@Composable
private fun Scaffold() {
    Column(
        modifier = Modifier.fillMaxSize().padding(24.dp),
        verticalArrangement = Arrangement.Center,
        horizontalAlignment = Alignment.CenterHorizontally,
    ) {
        Text("红杏", style = MaterialTheme.typography.headlineLarge)
        Text("构建链路自检通过", style = MaterialTheme.typography.bodyMedium)
    }
}
