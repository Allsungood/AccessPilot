package com.accesspilot.hongxing.ui.component

import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.rounded.AutoAwesome
import androidx.compose.material.icons.rounded.Bolt
import androidx.compose.material.icons.rounded.Check
import androidx.compose.material.icons.rounded.Public
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.unit.dp
import com.accesspilot.hongxing.R

/**
 * 红杏 Android · 分流模式三选一。
 *
 * 这一块是**整个界面里唯一需要"翻译"的地方**。用户看不懂 `rule / global / direct`,
 * 但看得懂"只有被墙的网站走代理, 国内网站直连"。所以每个选项都是
 * **标题(人话) + 一句解释(更人话)**, 而且解释是常驻的, 不做 tooltip ——
 * 需要长按才知道什么意思的说明, 等于没有。
 *
 * 为什么用竖排三行而不是横排三个胶囊:
 * 横排时每个选项只有 1/3 屏宽, 一句中文解释要折成三四行, 中文折行后每行只有
 * 五六个字, 读起来是碎的。竖排牺牲了一点"紧凑感", 换来的是真能读懂。
 */

/** 三个模式在引擎侧的取值, 和 `EngineController.setMode` 的约定一致。 */
const val MODE_RULE = "rule"
const val MODE_GLOBAL = "global"
const val MODE_DIRECT = "direct"

/** 模式名的人话版本。状态卡和这里共用一份, 免得两处翻译不一致。 */
@Composable
fun modeDisplayName(mode: String): String = when (mode) {
    MODE_GLOBAL -> stringResource(R.string.mode_global_title)
    MODE_DIRECT -> stringResource(R.string.mode_direct_title)
    else -> stringResource(R.string.mode_rule_title)
}

private data class ModeOption(
    val key: String,
    val titleRes: Int,
    val descRes: Int,
    val icon: ImageVector,
)

@Composable
fun ModePicker(
    current: String,
    enabled: Boolean,
    onSelect: (String) -> Unit,
    modifier: Modifier = Modifier,
) {
    // 选项表在 composable 里建, 因为它引用的是资源 id, 而资源 id 是运行时查的
    val options = listOf(
        ModeOption(MODE_RULE, R.string.mode_rule_title, R.string.mode_rule_desc, Icons.Rounded.AutoAwesome),
        ModeOption(MODE_GLOBAL, R.string.mode_global_title, R.string.mode_global_desc, Icons.Rounded.Public),
        ModeOption(MODE_DIRECT, R.string.mode_direct_title, R.string.mode_direct_desc, Icons.Rounded.Bolt),
    )

    HongxingCard(modifier = modifier) {
        SectionLabel(text = stringResource(R.string.mode_section_title))
        Spacer(modifier = Modifier.height(4.dp))
        Text(
            text = stringResource(R.string.mode_section_hint),
            style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
        Spacer(modifier = Modifier.height(14.dp))
        options.forEachIndexed { index, option ->
            if (index > 0) Spacer(modifier = Modifier.height(6.dp))
            ModeRow(
                title = stringResource(option.titleRes),
                description = stringResource(option.descRes),
                icon = option.icon,
                selected = option.key == current,
                enabled = enabled,
                onClick = { onSelect(option.key) },
            )
        }
    }
}

@Composable
private fun ModeRow(
    title: String,
    description: String,
    icon: ImageVector,
    selected: Boolean,
    enabled: Boolean,
    onClick: () -> Unit,
) {
    val scheme = MaterialTheme.colorScheme
    val titleColor = if (selected) scheme.onPrimaryContainer else scheme.onSurface
    val descColor = if (selected) {
        scheme.onPrimaryContainer.copy(alpha = 0.78f)
    } else {
        scheme.onSurfaceVariant
    }

    Surface(
        modifier = Modifier
            .fillMaxWidth()
            .clip(MaterialTheme.shapes.large)
            .clickable(enabled = enabled && !selected, onClick = onClick),
        shape = MaterialTheme.shapes.large,
        // 未选中完全透明: 三个选项都铺底色的话, 卡片里会出现三块方块,
        // 而"选中的那块有底色"本身就是最清楚的选中提示
        color = if (selected) scheme.primaryContainer else Color.Transparent,
    ) {
        Row(
            modifier = Modifier.padding(horizontal = 14.dp, vertical = 12.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Icon(
                imageVector = icon,
                contentDescription = null,
                tint = if (selected) scheme.onPrimaryContainer else scheme.onSurfaceVariant,
                modifier = Modifier.size(20.dp),
            )
            Spacer(modifier = Modifier.width(13.dp))
            Column(modifier = Modifier.weight(1f)) {
                Text(
                    text = title,
                    style = MaterialTheme.typography.titleSmall,
                    color = titleColor,
                )
                Spacer(modifier = Modifier.height(2.dp))
                Text(
                    text = description,
                    style = MaterialTheme.typography.bodySmall,
                    color = descColor,
                )
            }
            if (selected) {
                Spacer(modifier = Modifier.width(10.dp))
                Icon(
                    imageVector = Icons.Rounded.Check,
                    contentDescription = stringResource(R.string.mode_selected_desc),
                    tint = scheme.onPrimaryContainer,
                    modifier = Modifier.size(18.dp),
                )
            }
        }
    }
}
