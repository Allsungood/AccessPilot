@file:OptIn(ExperimentalFoundationApi::class)

package com.accesspilot.hongxing.ui.component

import androidx.compose.foundation.ExperimentalFoundationApi
import androidx.compose.foundation.combinedClickable
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.rounded.Close
import androidx.compose.material.icons.rounded.ErrorOutline
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.unit.dp
import com.accesspilot.hongxing.R

/**
 * 红杏 Android · 错误提示条。
 *
 * 契约里写着 `status.error` 非空**必须**显示出来, 不能静默 —— 这是从桌面端学来
 * 的教训: 引擎报错被界面吞掉的时候, 用户看到的是"点了没反应", 除了重装什么都做
 * 不了; 而把原文摆出来, 至少能搜、能发给别人。
 *
 * 为什么长按复制而不是放个复制按钮:
 * 错误场景下用户要的是"把这段话弄出去", 而这一屏平时根本不该出现错误条, 为它
 * 常驻一个按钮不值。长按是零成本的, 而且安卓用户对长按复制文本是有肌肉记忆的。
 *
 * 颜色用 errorContainer 而不是描边红色: 出错时界面需要"跳出来", 一整块淡红比
 * 一根红细线更难被忽略。
 */
@Composable
fun ErrorBanner(
    text: String,
    copied: Boolean,
    onCopy: () -> Unit,
    onDismiss: () -> Unit,
    modifier: Modifier = Modifier,
) {
    val contentColor = MaterialTheme.colorScheme.onErrorContainer
    Surface(
        modifier = modifier.fillMaxWidth(),
        shape = MaterialTheme.shapes.extraLarge,
        color = MaterialTheme.colorScheme.errorContainer,
    ) {
        Row(
            modifier = Modifier.padding(start = 18.dp, top = 16.dp, end = 6.dp, bottom = 16.dp),
            verticalAlignment = Alignment.Top,
        ) {
            Icon(
                imageVector = Icons.Rounded.ErrorOutline,
                contentDescription = null,
                tint = contentColor,
                modifier = Modifier.size(20.dp),
            )
            Spacer(modifier = Modifier.width(12.dp))
            Column(
                modifier = Modifier
                    .weight(1f)
                    .combinedClickable(onClick = {}, onLongClick = onCopy),
            ) {
                Text(
                    text = stringResource(R.string.error_banner_title),
                    style = MaterialTheme.typography.titleSmall,
                    color = contentColor,
                )
                Spacer(modifier = Modifier.height(4.dp))
                Text(
                    text = text,
                    style = MaterialTheme.typography.bodySmall,
                    color = contentColor,
                )
                Spacer(modifier = Modifier.height(6.dp))
                Text(
                    text = if (copied) {
                        stringResource(R.string.error_copied)
                    } else {
                        stringResource(R.string.error_copy_hint)
                    },
                    style = MaterialTheme.typography.labelSmall,
                    color = contentColor.copy(alpha = 0.75f),
                )
            }
            IconButton(onClick = onDismiss) {
                Icon(
                    imageVector = Icons.Rounded.Close,
                    contentDescription = stringResource(R.string.error_dismiss),
                    tint = contentColor,
                    modifier = Modifier.size(18.dp),
                )
            }
        }
    }
}
