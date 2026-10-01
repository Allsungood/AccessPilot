@file:OptIn(ExperimentalFoundationApi::class)

package com.accesspilot.hongxing.ui.component

import androidx.compose.animation.core.FastOutSlowInEasing
import androidx.compose.animation.core.LinearEasing
import androidx.compose.animation.core.RepeatMode
import androidx.compose.animation.core.Spring
import androidx.compose.animation.core.animateFloat
import androidx.compose.animation.core.animateFloatAsState
import androidx.compose.animation.core.infiniteRepeatable
import androidx.compose.animation.core.rememberInfiniteTransition
import androidx.compose.animation.core.spring
import androidx.compose.animation.core.tween
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.ExperimentalFoundationApi
import androidx.compose.foundation.clickable
import androidx.compose.foundation.combinedClickable
import androidx.compose.foundation.interaction.MutableInteractionSource
import androidx.compose.foundation.interaction.collectIsPressedAsState
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.aspectRatio
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.StrokeCap
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.graphics.graphicsLayer
import androidx.compose.ui.semantics.Role
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import com.accesspilot.hongxing.core.EnginePhase
import com.accesspilot.hongxing.ui.theme.HongxingColors
import kotlinx.coroutines.delay

/**
 * 红杏 Android · 那颗大圆钮。
 *
 * 整个 App 的主角, 也是唯一一个"用户需要学会"的东西: 点一下连, 再点一下断。
 * 所以它占了屏幕上半部分, 而且四种状态**全部用颜色和动效说话**, 不靠文字:
 *
 * | 状态   | 圆盘           | 动效             | 文字色   |
 * |--------|----------------|------------------|----------|
 * | 未连接 | 中性灰白       | 无               | 中性     |
 * | 连接中 | 主色半透明     | 呼吸 + 外圈转    | 主色     |
 * | 已连接 | 主色实心       | 缓慢呼吸的光晕   | onPrimary|
 * | 出错   | 中性 + 红描边  | 无(静止=不对劲)  | 红       |
 *
 * 为什么用 Canvas 手绘而不是切图片:
 * 圆钮要在 5 寸到 11 寸之间都好看, 位图一放大就糊; 而且描边宽度、光晕半径都得
 * 跟着尺寸走 (`size.minDimension` 的比例), 只有画出来才能做到。
 *
 * 为什么出错态**不加动效**: 动效在这个界面里的语义是"正在进行", 一个静止的
 * 红圈比闪烁的红圈更像"停下来了, 等你处理", 也更不吵。
 */
@Composable
fun PowerButton(
    phase: EnginePhase,
    title: String,
    caption: String,
    busy: Boolean,
    onClick: () -> Unit,
    modifier: Modifier = Modifier,
    onCaptionLongPress: (() -> Unit)? = null,
) {
    val scheme = MaterialTheme.colorScheme
    val extra = HongxingColors.extra

    val connected = phase == EnginePhase.Connected
    val connecting = phase == EnginePhase.Connecting || phase == EnginePhase.Disconnecting
    val failed = phase == EnginePhase.Error

    // 呼吸和转圈共用一个 InfiniteTransition: 两者生命周期完全一致, 拆成两个
    // 只会多一层帧回调。
    // 不做"按状态创建": Compose 里条件化地创建动画, 相位一切换就会把动画状态
    // 丢掉重来, 观感是"顿一下"。这一屏本来就是常亮的, 那点插值开销可以忽略。
    val transition = rememberInfiniteTransition(label = "power-ring")
    val breath by transition.animateFloat(
        initialValue = 0f,
        targetValue = 1f,
        animationSpec = infiniteRepeatable(
            animation = tween(durationMillis = 1700, easing = FastOutSlowInEasing),
            repeatMode = RepeatMode.Reverse,
        ),
        label = "breath",
    )
    val spin by transition.animateFloat(
        initialValue = 0f,
        targetValue = 360f,
        animationSpec = infiniteRepeatable(
            animation = tween(durationMillis = 1300, easing = LinearEasing),
            repeatMode = RepeatMode.Restart,
        ),
        label = "spin",
    )

    val interactionSource = remember { MutableInteractionSource() }
    val pressed by interactionSource.collectIsPressedAsState()
    val pressScale by animateFloatAsState(
        targetValue = if (pressed && !busy) 0.965f else 1f,
        animationSpec = spring(
            dampingRatio = Spring.DampingRatioMediumBouncy,
            stiffness = Spring.StiffnessMedium,
        ),
        label = "press",
    )

    // busy 时点一下: 不发命令, 只让圆钮缩一下再弹回来 —— 用户能确认"点到了,
    // 只是引擎正忙"。不弹 Toast: 连点五下会刷五条, 那种反馈比没有还烦人。
    var nudgeTick by remember { mutableIntStateOf(0) }
    var nudging by remember { mutableStateOf(false) }
    LaunchedEffect(nudgeTick) {
        if (nudgeTick > 0) {
            nudging = true
            delay(130)
            nudging = false
        }
    }
    val nudgeScale by animateFloatAsState(
        targetValue = if (nudging) 0.97f else 1f,
        animationSpec = spring(stiffness = Spring.StiffnessHigh),
        label = "nudge",
    )

    val scale = pressScale * nudgeScale

    val contentColor = when {
        connected -> scheme.onPrimary
        connecting -> scheme.primary
        failed -> extra.danger
        else -> extra.discContent
    }
    val captionColor = when {
        connected -> scheme.onPrimary.copy(alpha = 0.82f)
        connecting -> scheme.primary.copy(alpha = 0.85f)
        failed -> extra.danger.copy(alpha = 0.90f)
        else -> extra.discContent.copy(alpha = 0.70f)
    }

    Box(
        modifier = modifier
            .aspectRatio(1f)
            .graphicsLayer {
                scaleX = scale
                scaleY = scale
            }
            .clickable(
                interactionSource = interactionSource,
                // 关掉水波纹: 圆钮自己会用缩放回应, 再加一层菱形涟漪就花了
                indication = null,
                role = Role.Button,
                onClick = { if (busy) nudgeTick++ else onClick() },
            ),
        contentAlignment = Alignment.Center,
    ) {
        Canvas(modifier = Modifier.fillMaxSize()) {
            val d = size.minDimension
            val center = Offset(size.width / 2f, size.height / 2f)
            val outer = d / 2f
            val disc = outer * 0.90f
            // 线宽按尺寸取比例, 不写死 dp: 平板上同一个圆钮的描边不该细得像根头发
            val hairline = (d * 0.006f).coerceAtLeast(1.2f)

            // 1) 光晕 —— 贴着圆盘外沿的一圈柔光。
            //    径向渐变从 0.60R 才开始起亮、到 1.00R 又收回透明, 所以看不出
            //    "一个圆", 只看到一圈温度。
            val halo = when {
                connected -> extra.accentBottom
                connecting -> scheme.primary
                failed -> extra.danger
                else -> null
            }
            if (halo != null) {
                val amplitude = if (connected) 0.10f else 0.09f
                val base = if (connected) 0.10f else 0.07f
                drawCircle(
                    brush = Brush.radialGradient(
                        colorStops = arrayOf(
                            0.60f to Color.Transparent,
                            0.90f to halo.copy(alpha = base + breath * amplitude),
                            1.00f to Color.Transparent,
                        ),
                        center = center,
                        radius = outer,
                    ),
                    radius = outer,
                    center = center,
                )
            }

            // 2) 圆盘本身。受光点放在左上方 —— 一个平面圆形只要有了方向性高光,
            //    看上去就是立体的, 比加阴影便宜得多, 也不会有硬边。
            val topColor = when {
                connected -> extra.accentTop
                connecting -> extra.accentTop.copy(alpha = 0.30f + breath * 0.12f)
                else -> extra.discTop
            }
            val bottomColor = when {
                connected -> extra.accentBottom
                connecting -> extra.accentBottom.copy(alpha = 0.18f + breath * 0.10f)
                else -> extra.discBottom
            }
            drawCircle(
                brush = Brush.radialGradient(
                    colors = listOf(topColor, bottomColor),
                    center = Offset(center.x - disc * 0.35f, center.y - disc * 0.45f),
                    radius = disc * 1.85f,
                ),
                radius = disc,
                center = center,
            )

            // 3) 描边。全屏几乎不画线, 只留这一圈 —— 它是界面里唯一的边界,
            //    所以反而要留着, 不然圆钮会"飘"在背景上。
            val ringColor = when {
                failed -> extra.danger.copy(alpha = 0.85f)
                connected -> scheme.primary.copy(alpha = 0.35f)
                connecting -> scheme.primary.copy(alpha = 0.55f)
                else -> extra.discRing
            }
            drawCircle(
                color = ringColor,
                radius = disc,
                center = center,
                style = Stroke(width = hairline * 1.6f),
            )

            // 4) 连接中的转圈: 一段 96° 的圆弧在描边上跑。
            //    圆弧而不是进度条, 因为这里表达的是"在动"而不是"还剩多少" ——
            //    起隧道这件事根本估不出百分比, 给假进度是骗用户。
            if (connecting) {
                drawArc(
                    color = scheme.primary,
                    startAngle = spin - 90f,
                    sweepAngle = 96f,
                    useCenter = false,
                    topLeft = Offset(center.x - disc, center.y - disc),
                    size = Size(disc * 2f, disc * 2f),
                    style = Stroke(width = hairline * 3f, cap = StrokeCap.Round),
                )
            }
        }

        Column(
            // 40dp 是量出来的: 圆钮占屏宽 76%, 圆盘直径是它的 90%, 留 40dp 时
            // 文字离圆盘边缘还有 28dp, 长错误信息能多显示一行
            modifier = Modifier.padding(horizontal = 40.dp),
            horizontalAlignment = Alignment.CenterHorizontally,
        ) {
            Text(
                text = title,
                style = MaterialTheme.typography.headlineMedium,
                color = contentColor,
                textAlign = TextAlign.Center,
            )
            Spacer(modifier = Modifier.height(8.dp))
            Text(
                text = caption,
                style = MaterialTheme.typography.bodyMedium,
                color = captionColor,
                textAlign = TextAlign.Center,
                maxLines = 3,
                overflow = TextOverflow.Ellipsis,
                modifier = if (onCaptionLongPress != null) {
                    // 出错时这句话可能很长(内核日志), 屏幕上放不下全文, 但用户
                    // 需要能把它发给别人 —— 所以长按复制整段。
                    Modifier.combinedClickable(onClick = {}, onLongClick = onCaptionLongPress)
                } else {
                    Modifier
                },
            )
        }
    }
}
