package com.accesspilot.hongxing.core

import android.content.Context
import java.security.SecureRandom

/**
 * 红杏 Android · 引擎侧的唯一持久化入口。
 *
 * 为什么用 `SharedPreferences` 而不是文件/DataStore: 这里存的都是**几十字节的
 * 标量** (密钥、选中的节点名、模式、上次状态), 没有任何结构化数据。DataStore
 * 要引入额外依赖, 自己写文件又要处理并发写和原子性问题 —— SharedPreferences
 * 的 `apply()` 恰好把"异步落盘 + 进程内立即可见"这两件事一起办了, 正是这里
 * 需要的。真正的结构化大文件 (节点列表、规则集) 走 [AssetInstaller] 落到
 * 私有目录, 不放这里。
 *
 * 所有读取方法都**带默认值且不抛异常**: 引擎在建立隧道的过程中读这些值,
 * 这时候炸掉会留下一个半开的隧道, 比读到一个默认值糟糕得多。
 */
internal class SettingsStore(context: Context) {

    private val prefs = context.applicationContext
        .getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)

    // ---------------------------------------------------------------- 密钥

    /**
     * 内核 REST API 的 secret, 首次访问时随机生成并落盘。
     *
     * 为什么必须随机、必须持久化:
     *  - **随机**: 这个值写在配置文件的 `secret:` 上, 是访问 `127.0.0.1:9090`
     *    的唯一凭据。同机其它 App 也能连 127.0.0.1, 写死一个常量等于把控制面
     *    敞开 —— 它们能切你的节点、读你的连接记录。
     *  - **持久化**: 每次启动换一个新 secret 本身不影响安全, 但会让"上一秒
     *    拿到的 secret"在下一次连接后失效, 界面侧正在轮询的请求会被 401 打回,
     *    表现成"连上以后列表刷不出来"。落盘就没这个抖动。
     */
    val secret: String
        get() {
            prefs.getString(KEY_SECRET, null)?.takeIf { it.isNotBlank() }?.let { return it }
            val generated = generateSecret()
            prefs.edit().putString(KEY_SECRET, generated).apply()
            return generated
        }

    /** 32 位十六进制随机串。`SecureRandom` 而不是 `Random`: 这是安全凭据。 */
    private fun generateSecret(): String {
        val bytes = ByteArray(16)
        SecureRandom().nextBytes(bytes)
        return bytes.joinToString("") { "%02x".format(it) }
    }

    // ---------------------------------------------------------------- 节点

    /**
     * 上次选中的节点名。空 = 还没选过, 引擎会退回策略组默认项。
     *
     * 存名字而不是存下标: 节点列表会随订阅更新而重排, 下标一变用户就会被
     * 悄悄切到另一个出口 —— 这种问题极难排查, 用户只会觉得"有时候能上有时候不能"。
     */
    var selectedNode: String
        get() = prefs.getString(KEY_NODE, "").orEmpty()
        set(value) = prefs.edit().putString(KEY_NODE, value).apply()

    // ---------------------------------------------------------------- 模式

    /**
     * 分流模式, 只允许 [MihomoApi.MODE_RULE] / [MihomoApi.MODE_GLOBAL] /
     * [MihomoApi.MODE_DIRECT]。
     *
     * 非法值**读的时候**就被挡掉: 这个字符串会被直接拼进发给内核的
     * `PATCH /configs` 请求体, 一个脏值会让内核返回 400, 而界面上只会看到
     * "切换失败"却不知道为什么。挡在入口最省事。
     */
    var mode: String
        get() = prefs.getString(KEY_MODE, MihomoApi.MODE_RULE)
            ?.takeIf { it in MihomoApi.VALID_MODES }
            ?: MihomoApi.MODE_RULE
        set(value) {
            val safe = if (value in MihomoApi.VALID_MODES) value else MihomoApi.MODE_RULE
            prefs.edit().putString(KEY_MODE, safe).apply()
        }

    // ------------------------------------------------------------ 连接状态

    /**
     * 上次是不是连着。**只用于界面冷启动时先把圆钮画成"上次的样子"**,
     * 绝不作为"现在真的连着"的依据:
     * 进程可能被杀、隧道可能被系统回收, 真实状态永远只认 [EngineRuntime.status]。
     */
    var lastConnected: Boolean
        get() = prefs.getBoolean(KEY_LAST_CONNECTED, false)
        set(value) = prefs.edit().putBoolean(KEY_LAST_CONNECTED, value).apply()

    // -------------------------------------------------------------- 资源版本

    /** [AssetInstaller] 已安装的资源版本号, 见那边的版本比较逻辑。 */
    var installedAssetsVersion: Long
        get() = prefs.getLong(KEY_ASSETS_VERSION, 0L)
        set(value) = prefs.edit().putLong(KEY_ASSETS_VERSION, value).apply()

    /** [AssetInstaller] 已安装资源的指纹, 用于同一 versionCode 下的资源变更。 */
    var installedAssetsFingerprint: String
        get() = prefs.getString(KEY_ASSETS_FINGERPRINT, "").orEmpty()
        set(value) = prefs.edit().putString(KEY_ASSETS_FINGERPRINT, value).apply()

    /** [AssetInstaller] 已安装资源对应的 versionName, 见那边的三级判据。 */
    var installedAssetsName: String
        get() = prefs.getString(KEY_ASSETS_NAME, "").orEmpty()
        set(value) = prefs.edit().putString(KEY_ASSETS_NAME, value).apply()

    private companion object {
        const val PREFS_NAME = "hongxing_engine"
        const val KEY_SECRET = "secret"
        const val KEY_NODE = "selected_node"
        const val KEY_MODE = "mode"
        const val KEY_LAST_CONNECTED = "last_connected"
        const val KEY_ASSETS_VERSION = "assets_version"
        const val KEY_ASSETS_FINGERPRINT = "assets_fingerprint"
        const val KEY_ASSETS_NAME = "assets_name"
    }
}
