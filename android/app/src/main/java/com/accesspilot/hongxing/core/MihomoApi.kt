package com.accesspilot.hongxing.core

import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.async
import kotlinx.coroutines.awaitAll
import kotlinx.coroutines.coroutineScope
import kotlinx.coroutines.withContext
import org.json.JSONObject
import java.io.BufferedReader
import java.io.IOException
import java.net.HttpURLConnection
import java.net.URL
import java.net.URLEncoder

/**
 * 红杏 Android · 内核 REST API 客户端
 *
 * 内核在 `127.0.0.1:9090` 上开了一个 external-controller, 这个对象是它唯一的
 * 调用入口。界面不直接碰它 —— 所有网络调用都藏在 [EngineControllerImpl] 后面。
 *
 * ## 为什么是 `HttpURLConnection` 而不是 okhttp
 *
 * 这个项目的传统是零第三方依赖, 而这里需要的只有 GET / PUT / PATCH 三种
 * 请求加 `org.json` 解析 —— 平台自带的能力刚好够。为这点需求引一个
 * okhttp + 它的一串 transitive 依赖, 收益是负数。
 *
 * ## 安全上的两条硬要求
 *
 * 1. **secret 必须带**。同一个手机上任何 App 都能连 `127.0.0.1:9090`,
 *    没有 secret 等于把控制面敞开 (切你的节点、读你的连接记录)。这里的
 *    secret 由 [SettingsStore] 随机生成并落盘, 和写进配置的那个是同一个。
 * 2. **错误信息里绝不带 secret**。异常消息是要显示给用户、也可能被用户贴到
 *    群里的, 泄露了凭据等于把上面那条作废 —— 见 [redact]。
 *
 * ## 错误处理约定
 *
 * 所有 public 方法返回 [ApiResult], **一个都不抛异常**。内核随时可能因为
 * 内存压力被系统杀掉, "墙塌了"是这里的常态而不是意外, 用返回值表达比
 * 满屏 try/catch 干净得多。
 */
internal class MihomoApi(
    private val secret: String,
    private val host: String = DEFAULT_HOST,
    private val port: Int = DEFAULT_PORT,
) {

    private val base = "http://$host:$port"

    // ---------------------------------------------------------------- 版本

    /**
     * `GET /version`。同时是"内核起来了吗"的探针 —— 隧道建立流程轮询它,
     * 拿到 response 才算真的连上。
     */
    suspend fun version(): ApiResult<String> = get("/version").map { body ->
        runCatching { JSONObject(body).optString("version") }.getOrNull().orEmpty()
    }

    /** `/version` 是否可达。比解析版本号更轻, 用于就绪轮询。 */
    suspend fun isUp(): Boolean = version().isOk

    // ---------------------------------------------------------------- 配置

    /** `GET /configs` 里的 `mode` 字段。 */
    suspend fun mode(): ApiResult<String> = get("/configs").map { body ->
        runCatching { JSONObject(body).optString("mode") }.getOrNull().orEmpty()
    }

    /**
     * 切换分流模式。
     *
     * 先发 JSON, 被拒再用表单重试 —— mihomo 的 `PATCH /configs` 吃 JSON,
     * 而老一些的 Clash 内核只吃 `application/x-www-form-urlencoded`。
     * 两种都试一遍的成本是零 (失败分支才会走到第二次), 换来的是对内核
     * 版本差异的免疫。
     */
    suspend fun setMode(mode: String): ApiResult<Unit> {
        require(mode in VALID_MODES) { "非法模式: $mode" }
        val json = patch("/configs", """{"mode":"$mode"}""")
        if (json.isOk) return ApiResult.Ok(Unit)
        val form = patch("/configs", "mode=$mode")
        return if (form.isOk) {
            ApiResult.Ok(Unit)
        } else {
            // 报第一条的错: 它是 JSON 那条路, 也是 mihomo 的正常路径。
            ApiResult.Err(json.errorOr(form))
        }
    }

    // ---------------------------------------------------------------- 节点

    /**
     * `GET /proxies`, 解析出指定策略组里的可选节点。
     *
     * 取策略组的 `all` 而不是遍历全部 proxies: `proxies` 里还混着
     * DIRECT / REJECT / GLOBAL 这些内建项和别的策略组, 全列出来界面上会出现
     * 一堆不能选的"节点"。
     */
    suspend fun proxies(group: String): ApiResult<List<NodeInfo>> = get("/proxies").map { body ->
        MihomoParser.parseNodes(body, group)
    }

    /** 切节点: `PUT /proxies/{组名}`。 */
    suspend fun selectNode(group: String, name: String): ApiResult<Unit> =
        put("/proxies/${encode(group)}", """{"name":${JSONObject.quote(name)}}""").map { }

    /**
     * 让内核把所有节点重测一遍延迟。
     *
     * 优先走 `GET /group/{组名}/delay` —— 一次请求让内核**并发**测完整个组,
     * 这是 mihomo 提供的批量接口。免费池动辄几千个节点, 逐个节点发
     * `/proxies/{name}/delay` 意味着几千次 HTTP 往返, 界面会卡到没法用。
     *
     * 只有在批量接口不可用时 (老内核没有这个路由) 才退回逐个测, 并且用
     * [PROBE_CONCURRENCY] 限并发 —— 不限的话几千个协程同时开 socket,
     * 内核还没被墙打死就先被自己的客户端打死了。
     *
     * @return 节点名 -> 延迟毫秒 (-1 表示测不通)
     */
    suspend fun testDelay(group: String, nodeNames: List<String>): ApiResult<Map<String, Int>> {
        val batch = get(
            "/group/${encode(group)}/delay?timeout=$DELAY_TIMEOUT_MS&url=${encode(DELAY_TEST_URL)}",
            timeoutMs = DELAY_TIMEOUT_MS + HTTP_SLACK_MS,
        )
        if (batch.isOk) {
            val parsed = batch.valueOrNull()
                ?.let { runCatching { MihomoParser.parseDelayMap(it) }.getOrNull() }
            if (parsed != null && parsed.isNotEmpty()) return ApiResult.Ok(parsed)
        }

        if (nodeNames.isEmpty()) return ApiResult.Err(batch.errorOrNull() ?: "没有可测速的节点")

        val results = coroutineScope {
            nodeNames.chunked(PROBE_CONCURRENCY).flatMap { chunk ->
                chunk.map { name ->
                    async(Dispatchers.IO) {
                        val r = get(
                            "/proxies/${encode(name)}/delay" +
                                "?timeout=$DELAY_TIMEOUT_MS&url=${encode(DELAY_TEST_URL)}",
                            timeoutMs = DELAY_TIMEOUT_MS + HTTP_SLACK_MS,
                        )
                        name to r.fold(
                            onOk = { b -> runCatching { JSONObject(b).optInt("delay", -1) }.getOrDefault(-1) },
                            onErr = { -1 },
                        )
                    }
                }.awaitAll()
            }.toMap()
        }
        return ApiResult.Ok(results)
    }

    // ---------------------------------------------------------------- 流量

    /**
     * `GET /connections` 里的累计上下行。
     *
     * 用 `downloadTotal`/`uploadTotal` 而不是 `/traffic`: `/traffic` 是
     * **WebSocket** 推流, `HttpURLConnection` 做不了 (要自己实现 101 升级和
     * 帧解析)。而这两个字段本来就是"本次内核启动以来的累计值", 正是界面要
     * 显示的那个数, 还省掉了自己累加和去重的麻烦。
     */
    suspend fun traffic(): ApiResult<Traffic> = get("/connections").map { body ->
        val o = JSONObject(body)
        Traffic(
            uploadBytes = o.optLong("uploadTotal", 0L),
            downloadBytes = o.optLong("downloadTotal", 0L),
        )
    }

    // ------------------------------------------------------------ HTTP 底座

    private suspend fun get(path: String, timeoutMs: Int = HTTP_TIMEOUT_MS): ApiResult<String> =
        request("GET", path, null, timeoutMs)

    private suspend fun put(
        path: String,
        body: String,
        timeoutMs: Int = HTTP_TIMEOUT_MS,
    ): ApiResult<String> = request("PUT", path, body, timeoutMs)

    private suspend fun patch(
        path: String,
        body: String,
        timeoutMs: Int = HTTP_TIMEOUT_MS,
    ): ApiResult<String> = request("PATCH", path, body, timeoutMs)

    private suspend fun request(
        method: String,
        path: String,
        body: String?,
        timeoutMs: Int,
    ): ApiResult<String> = withContext(Dispatchers.IO) {
        var conn: HttpURLConnection? = null
        try {
            conn = (URL(base + path).openConnection() as HttpURLConnection).apply {
                requestMethod = method
                connectTimeout = timeoutMs
                readTimeout = timeoutMs
                // 内核是本机进程, 但它的响应要经过解析/规则匹配, 慢一点正常;
                // 关掉连接复用以外的所有缓存, 免得拿到过期的节点列表。
                useCaches = false
                setRequestProperty("Authorization", "Bearer $secret")
                if (body != null) {
                    doOutput = true
                    setRequestProperty("Content-Type", "application/json")
                }
            }

            if (body != null) {
                conn.outputStream.use { it.write(body.toByteArray(Charsets.UTF_8)) }
            }

            val code = conn.responseCode
            if (code !in 200..299) {
                val detail = conn.errorStream?.bufferedReader()?.use(BufferedReader::readText)
                return@withContext ApiResult.Err(
                    redact("内核返回 HTTP $code" + (detail?.take(200)?.let { ": $it" } ?: "")),
                )
            }
            ApiResult.Ok(conn.inputStream.bufferedReader().use(BufferedReader::readText))
        } catch (e: IOException) {
            // 连不上是常态: 内核没起、正在起、或者刚被系统杀掉。
            // 这里一定要把"连不上"和"内核拒绝了"分开表达, 否则界面会把
            // "还没起来"显示成"配置错误"。
            ApiResult.Err(redact("无法连接内核 (${e.javaClass.simpleName}): ${e.message}"))
        } catch (e: Exception) {
            ApiResult.Err(redact("内核请求失败: ${e.javaClass.simpleName}: ${e.message}"))
        } finally {
            conn?.disconnect()
        }
    }

    /**
     * 抹掉消息里可能出现的 secret。
     *
     * 异常消息会被显示、被截图、被贴进群里。`IOException` 的消息里
     * 有时会带上完整的 URL, 而我们把 secret 放在 header 里正是为了避免
     * 它出现在 URL 上 —— 那就在这里再兜一道, 保证任何路径都不会漏出去。
     */
    private fun redact(message: String?): String =
        (message ?: "未知错误").replace(secret, "***")

    private fun encode(raw: String): String =
        URLEncoder.encode(raw, "UTF-8").replace("+", "%20")

    companion object {
        const val DEFAULT_HOST = "127.0.0.1"
        const val DEFAULT_PORT = 9090

        const val MODE_RULE = "rule"
        const val MODE_GLOBAL = "global"
        const val MODE_DIRECT = "direct"

        /** 允许的模式。写进配置、发给内核之前都要过这一关。 */
        val VALID_MODES = listOf(MODE_RULE, MODE_GLOBAL, MODE_DIRECT)

        /** 批量测速的探测目标。和桌面端保持一致: 这个地址国内直连也能通,
         *  所以它的延迟反映的是"到代理出口再出来"的真实成本。 */
        const val DELAY_TEST_URL = "http://www.gstatic.com/generate_204"

        const val DELAY_TIMEOUT_MS = 5_000
        const val HTTP_TIMEOUT_MS = 2_000
        const val HTTP_SLACK_MS = 1_500

        /** 逐个测速时的并发上限, 见 [testDelay] 的注释。 */
        const val PROBE_CONCURRENCY = 8
    }
}

/** `GET /connections` 的累计流量。 */
internal data class Traffic(val uploadBytes: Long, val downloadBytes: Long)

/**
 * 一次内核调用的结果。
 *
 * 用自定义的密封类而不是 `kotlin.Result`: `Result` 的成功值不能是
 * `Unit` 之外的受限场景, 而且它的失败对象必须是 `Throwable` —— 而"内核
 * 返回 400"根本不是异常, 硬包成异常只是把控制流藏进异常里。
 */
internal sealed class ApiResult<out T> {

    data class Ok<T>(val value: T) : ApiResult<T>()
    data class Err(val error: String) : ApiResult<Nothing>()

    val isOk: Boolean get() = this is Ok

    fun valueOrNull(): T? = (this as? Ok)?.value

    fun errorOrNull(): String? = (this as? Err)?.error

    inline fun <R> map(transform: (T) -> R): ApiResult<R> = when (this) {
        is Ok -> runCatching { Ok(transform(value)) }
            .getOrElse { Err("解析内核响应失败: ${it.javaClass.simpleName}: ${it.message}") }
        is Err -> this
    }

    /** 取错误文案; 自己没有就用 [fallback] 里那句。用于"两条路都失败了, 报哪条"。 */
    fun errorOr(fallback: ApiResult<*>): String =
        errorOrNull() ?: fallback.errorOrNull() ?: "未知错误"

    inline fun <R> fold(onOk: (T) -> R, onErr: (String) -> R): R = when (this) {
        is Ok -> onOk(value)
        is Err -> onErr(error)
    }
}

/**
 * 内核 JSON 响应的解析。
 *
 * 单独摘出来是为了能在**纯 JVM 单元测试**里跑: [MihomoApi] 的方法都要发 HTTP,
 * 而解析这段恰恰是最容易写错的地方 (内核的 `/proxies` 结构是三层嵌套的
 * map, 少一层就是空列表 —— 界面表现为"节点列表永远拉不出来", 但没有任何
 * 报错)。`org.json` 在 JVM 上是 Android 提供的实现, 单测里由
 * `android.jar` 的 stub 顶替, 所以只测纯 JSON 逻辑, 不碰网络。
 */
internal object MihomoParser {

    /**
     * 解析 `GET /proxies`, 取出某个策略组里的可选节点。
     *
     * `/proxies` 的结构:
     * ```json
     * { "proxies": {
     *     "🚀 节点选择": { "type": "Selector", "now": "香港1", "all": ["香港1", "DIRECT"] },
     *     "香港1": { "type": "vmess", "history": [{"time":"...","delay":123}] } } }
     * ```
     * 取策略组的 `all` 而不是遍历整个 `proxies`: 后者还混着 DIRECT / REJECT /
     * GLOBAL 这些内建项和别的策略组, 全列出来界面上会出现一堆不能选的"节点"。
     *
     * @throws java.io.IOException 内核里没有这个策略组 (配置模板和这里的
     *         常量对不上时会走到, 报出来比返回空列表好 —— 空列表看起来像
     *         "节点池是空的", 会把排查方向带偏)
     */
    fun parseNodes(body: String, group: String): List<NodeInfo> {
        val root = JSONObject(body).getJSONObject("proxies")
        val g = root.optJSONObject(group)
            ?: throw IOException("内核里没有策略组 \"$group\" (配置模板换过了?)")
        val all = g.optJSONArray("all") ?: return emptyList()
        val now = g.optString("now")

        return (0 until all.length()).mapNotNull { i ->
            val name = all.optString(i).takeIf { it.isNotBlank() } ?: return@mapNotNull null
            val delay = root.optJSONObject(name)?.let(::lastDelay) ?: -1
            NodeInfo(
                name = name,
                latencyMs = delay,
                alive = delay >= 0,
                current = name == now,
            )
        }
    }

    /**
     * 从节点的 `history` 里取最近一次测速结果。
     *
     * mihomo 测不通时写的是 `delay: 0`, 而 0 ms 的真实延迟不存在 —— 所以
     * 统一映射成 -1 ("未知"), 让界面显示灰色而不是"0 ms 飞快"。
     */
    fun lastDelay(node: JSONObject): Int {
        val history = node.optJSONArray("history") ?: return -1
        if (history.length() == 0) return -1
        val last = history.optJSONObject(history.length() - 1) ?: return -1
        val delay = last.optInt("delay", 0)
        return if (delay > 0) delay else -1
    }

    /**
     * 解析 `GET /group/{name}/delay` 的批量结果。
     *
     * 响应形如 `{"香港1": 123, "香港2": 0, "香港3": -1}`, 其中 0 和负数
     * 都表示测不通, 统一成 -1。
     */
    fun parseDelayMap(body: String): Map<String, Int> {
        val o = JSONObject(body)
        return o.keys().asSequence().associateWith { key ->
            val v = o.optInt(key, -1)
            if (v > 0) v else -1
        }
    }

    /** 解析 `GET /connections` 的累计流量。 */
    fun parseTraffic(body: String): Traffic {
        val o = JSONObject(body)
        return Traffic(
            uploadBytes = o.optLong("uploadTotal", 0L),
            downloadBytes = o.optLong("downloadTotal", 0L),
        )
    }

    /** 解析 `GET /version`。 */
    fun parseVersion(body: String): String =
        runCatching { JSONObject(body).optString("version") }.getOrDefault("")

    /** 解析 `GET /configs` 的 mode。 */
    fun parseMode(body: String): String =
        runCatching { JSONObject(body).optString("mode") }.getOrDefault("")
}
