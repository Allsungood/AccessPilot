package com.accesspilot.hongxing.ui.component

import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import androidx.compose.runtime.Composable
import androidx.compose.runtime.remember
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.res.stringResource
import com.accesspilot.hongxing.R

/**
 * 红杏 Android · 长按复制。
 *
 * 为什么用系统 ClipboardManager 而不是 Compose 的 LocalClipboardManager:
 * 后者在新版本里已经标了废弃, 而这个 App 要在 minSdk 26 到 targetSdk 35 之间
 * 都稳。走平台 API 没有任何版本悬念。
 *
 * 安卓 13 起系统自己会在复制后弹一个小胶囊提示, 所以界面**不要**再弹一遍
 * Toast —— 那会变成两条提示叠在一起。
 */
@Composable
fun rememberCopyToClipboard(): (String) -> Unit {
    val context = LocalContext.current
    // 剪贴板标签在部分机型上会显示给用户 ("红杏: xxx"), 所以它也得是中文资源
    val label = stringResource(R.string.clipboard_label)
    return remember(context, label) {
        val copy: (String) -> Unit = { text ->
            val manager = context.getSystemService(Context.CLIPBOARD_SERVICE) as? ClipboardManager
            manager?.setPrimaryClip(ClipData.newPlainText(label, text))
        }
        copy
    }
}
