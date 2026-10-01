package com.accesspilot.hongxing.core

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.junit.runners.JUnit4

/**
 * 红杏 Android · 内核 REST API 响应解析的单元测试
 *
 * ## 为什么重点测解析, 而不是测请求
 *
 * 请求那半边 (`HttpURLConnection`) 要真起一个 HTTP 服务才能测, 而解析这半边
 * 是**纯字符串进、对象出**, 却恰恰是最容易写错的地方: `/proxies` 的响应是
 * 三层嵌套的 map (`proxies` → 节点名 → 节点属性), 少剥一层不会报错, 只会
 * 得到一份空列表 —— 界面上的表现是"节点列表永远拉不出来", 但日志里
 * 一片安静, 因为**没有异常**。
 *
 * JSON 样本按 mihomo 的真实响应形状写 (摘自 `/proxies` 与
 * `/group/{name}/delay` 的文档与实测响应)。
 */
@RunWith(JUnit4::class)
class MihomoApiTest {

    private val group = "🚀 节点选择"

    // ---------------------------------------------------------------- /proxies

    @Test
    fun `从策略组里取出节点, 带上延迟和当前选中标记`() {
        val nodes = MihomoParser.parseNodes(PROXIES_JSON, group)

        assertEquals("应当只列出策略组 all 里的项", 4, nodes.size)
        assertEquals(listOf("香港1", "香港2", "日本1", "DIRECT"), nodes.map { it.name })
    }

    @Test
    fun `延迟从 history 最后一条取, 正数才算活着`() {
        val nodes = MihomoParser.parseNodes(PROXIES_JSON, group).associateBy { it.name }

        assertEquals(123, nodes.getValue("香港1").latencyMs)
        assertTrue("123ms 是有效延迟, 节点应当算活着", nodes.getValue("香港1").alive)

        assertEquals(
            "内核测不通时写的是 delay 0, 而 0ms 的真实延迟不存在, 必须映射成未知",
            -1,
            nodes.getValue("香港2").latencyMs,
        )
        assertFalse(nodes.getValue("香港2").alive)
    }

    @Test
    fun `没有 history 的内建项延迟是未知而不是 0`() {
        val direct = MihomoParser.parseNodes(PROXIES_JSON, group).first { it.name == "DIRECT" }
        assertEquals(-1, direct.latencyMs)
        assertFalse(direct.alive)
    }

    @Test
    fun `负延迟也当成未知`() {
        val node = JSONObject("""{"history":[{"time":"t","delay":-1}]}""")
        assertEquals(-1, MihomoParser.lastDelay(node))
    }

    @Test
    fun `history 为空数组时是未知`() {
        assertEquals(-1, MihomoParser.lastDelay(JSONObject("""{"history":[]}""")))
    }

    @Test
    fun `当前选中的节点被标出来`() {
        val nodes = MihomoParser.parseNodes(PROXIES_JSON, group)
        val current = nodes.filter { it.current }

        assertEquals("now 只有一个值, 所以只该有一个 current", 1, current.size)
        assertEquals("香港2", current.first().name)
    }

    @Test
    fun `列表里混着 DIRECT 这类内建项是正常的, 不能被过滤掉`() {
        // 策略组的 all 里真的有 DIRECT / REJECT —— 它们是合法的可选出口,
        // 过滤掉会让用户没法把某个组切回直连。
        val names = MihomoParser.parseNodes(PROXIES_JSON, group).map { it.name }
        assertTrue("DIRECT" in names)
    }

    @Test
    fun `策略组不存在时抛出并点名是哪个组`() {
        val error = runCatching {
            MihomoParser.parseNodes(PROXIES_JSON, "不存在的组")
        }.exceptionOrNull()

        assertTrue("应当抛 IOException", error is java.io.IOException)
        assertTrue(
            "错误信息必须带上组名 —— 模板换了策略组名时, 这是唯一的线索。实际: ${error?.message}",
            error?.message?.contains("不存在的组") == true,
        )
    }

    @Test
    fun `没有 all 字段的组返回空列表而不是抛异常`() {
        val json = """{"proxies":{"G":{"type":"URLTest","now":"a"}}}"""
        assertEquals(emptyList<NodeInfo>(), MihomoParser.parseNodes(json, "G"))
    }

    @Test
    fun `all 里的空字符串被跳过`() {
        val json = """{"proxies":{"G":{"all":["a","","b"],"now":"a"},"a":{},"b":{}}}"""
        assertEquals(listOf("a", "b"), MihomoParser.parseNodes(json, "G").map { it.name })
    }

    @Test
    fun `响应不是合法 JSON 时抛出, 由 ApiResult 兜住`() {
        assertTrue(runCatching { MihomoParser.parseNodes("<html>502</html>", group) }.isFailure)
    }

    // ------------------------------------------------------------ 批量测速

    @Test
    fun `批量测速结果里 0 和负数都算测不通`() {
        val delays = MihomoParser.parseDelayMap("""{"香港1":123,"香港2":0,"香港3":-1,"香港4":45}""")

        assertEquals(123, delays.getValue("香港1"))
        assertEquals(-1, delays.getValue("香港2"))
        assertEquals(-1, delays.getValue("香港3"))
        assertEquals(45, delays.getValue("香港4"))
    }

    @Test
    fun `批量测速的空结果解析成空 map`() {
        assertEquals(emptyMap<String, Int>(), MihomoParser.parseDelayMap("{}"))
    }

    // ---------------------------------------------------------------- 流量

    @Test
    fun `累计流量取 downloadTotal 和 uploadTotal`() {
        val traffic = MihomoParser.parseTraffic(
            """{"downloadTotal":1048576,"uploadTotal":2048,"connections":[]}""",
        )
        assertEquals(1_048_576L, traffic.downloadBytes)
        assertEquals(2_048L, traffic.uploadBytes)
    }

    @Test
    fun `没有流量字段时是 0 而不是抛异常`() {
        val traffic = MihomoParser.parseTraffic("""{"connections":[]}""")
        assertEquals(0L, traffic.downloadBytes)
        assertEquals(0L, traffic.uploadBytes)
    }

    // ---------------------------------------------------------------- 版本

    @Test
    fun `版本号解析`() {
        assertEquals("v1.19.32", MihomoParser.parseVersion("""{"version":"v1.19.32","meta":true}"""))
    }

    @Test
    fun `版本响应缺字段时返回空串, 不抛异常`() {
        assertEquals("", MihomoParser.parseVersion("{}"))
        assertEquals("", MihomoParser.parseVersion("不是 JSON"))
    }

    // ---------------------------------------------------------------- 模式

    @Test
    fun `模式解析`() {
        assertEquals("rule", MihomoParser.parseMode("""{"mode":"rule","port":7890}"""))
    }

    @Test
    fun `模式响应异常时返回空串`() {
        assertEquals("", MihomoParser.parseMode("{}"))
    }

    // ------------------------------------------------------------ ApiResult

    @Test
    fun `ApiResult 的 map 只在成功时执行`() {
        var called = false
        val err: ApiResult<Int> = ApiResult.Err("boom")
        val mapped = err.map { called = true; it + 1 }

        assertFalse("失败时不该执行 transform", called)
        assertEquals("boom", mapped.errorOrNull())
    }

    @Test
    fun `ApiResult 的 map 把解析异常转成 Err 而不是抛出去`() {
        val ok: ApiResult<String> = ApiResult.Ok("不是 JSON")
        val mapped = ok.map { org.json.JSONObject(it).getString("x") }

        assertTrue("解析失败必须是 Err —— UI 调的方法不许抛异常", mapped is ApiResult.Err)
    }

    @Test
    fun `ApiResult 的 errorOr 优先报自己的错`() {
        val a: ApiResult<Int> = ApiResult.Err("我的错")
        val b: ApiResult<Int> = ApiResult.Err("你的错")
        assertEquals("我的错", a.errorOr(b))
    }

    @Test
    fun `ApiResult 的 errorOr 在自己没话说时用兜底那句`() {
        val a: ApiResult<Int> = ApiResult.Ok(1)
        val b: ApiResult<Int> = ApiResult.Err("兜底")
        assertEquals("兜底", a.errorOr(b))
    }

    // ---------------------------------------------------------------- 校验

    @Test
    fun `模式白名单只有三个值`() {
        assertEquals(listOf("rule", "global", "direct"), MihomoApi.VALID_MODES)
    }

    private companion object {
        /**
         * 按 mihomo 的真实响应形状构造。
         *
         * 刻意包含这几种情况: 有正常延迟的、delay 为 0 的 (测不通)、
         * 完全没有 history 的 (内建项)、以及列表里混着的内建 DIRECT。
         */
        val PROXIES_JSON: String = """
        {
          "proxies": {
            "🚀 节点选择": {
              "type": "Selector",
              "now": "香港2",
              "all": ["香港1", "香港2", "日本1", "DIRECT"]
            },
            "香港1": {
              "type": "vmess",
              "history": [
                {"time": "2026-10-01T10:00:00Z", "delay": 200},
                {"time": "2026-10-01T10:05:00Z", "delay": 123}
              ]
            },
            "香港2": {
              "type": "vmess",
              "history": [{"time": "2026-10-01T10:05:00Z", "delay": 0}]
            },
            "日本1": {
              "type": "trojan",
              "history": [{"time": "2026-10-01T10:05:00Z", "delay": 88}]
            },
            "DIRECT": {"type": "Direct"}
          }
        }
        """.trimIndent()
    }
}
