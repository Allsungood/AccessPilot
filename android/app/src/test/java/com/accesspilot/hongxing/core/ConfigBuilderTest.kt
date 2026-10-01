package com.accesspilot.hongxing.core

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.junit.runners.JUnit4
import java.io.File

/**
 * 红杏 Android · 配置组装的单元测试
 *
 * ## 这些测试刻意用**真的资源文件**跑
 *
 * `config.template.yaml` 有 28,000 多行、`nodes.yaml` 有近 70,000 行, 都是
 * `android/tools/build_assets.py` 生成的。用一小段假模板测"占位符能不能换掉"
 * 很容易全绿, 但真正的风险从来不在替换本身, 而在**缩进**:
 *
 * 模板里 `{{PROXIES}}` 是顶格的, 而生成的 `nodes.yaml` 每一项已经领先两个
 * 空格。多缩一层, YAML 会把它解析成 `proxies` 映射里的一个怪键 —— 内核报的
 * 是 `yaml: line N: did not find expected key`, 指向 `proxies:` 上一行,
 * 和真正的原因隔着十万八千里。
 *
 * 所以这里的断言是"生成出来的配置里, 节点项和模板里的策略组项**缩进一致**",
 * 拿真文件比。这样的话, 哪天模板生成脚本换一种 dump 方式把节点弄顶格了,
 * 测试会当场红。
 */
@RunWith(JUnit4::class)
class ConfigBuilderTest {

    // ------------------------------------------------------------ 基本替换

    @Test
    fun `三个占位符都被替换掉, 结果里一个都不剩`() {
        val template = readAsset(ConfigBuilder.TEMPLATE_ASSET)
        val result = ConfigBuilder.build(template, SAMPLE_NODES, fd = 42, secret = "s3cret")

        assertFalse("{{FD}} 没被替换", result.contains(ConfigBuilder.PLACEHOLDER_FD))
        assertFalse("{{SECRET}} 没被替换", result.contains(ConfigBuilder.PLACEHOLDER_SECRET))
        assertFalse("{{PROXIES}} 没被替换", result.contains(ConfigBuilder.PLACEHOLDER_PROXIES))
    }

    @Test
    fun `fd 和 secret 出现在该出现的地方`() {
        val template = readAsset(ConfigBuilder.TEMPLATE_ASSET)
        val result = ConfigBuilder.build(template, SAMPLE_NODES, fd = 137, secret = "abc123")

        assertTrue(
            "tun.file-descriptor 必须是纯数字, 否则内核解析失败",
            result.contains("file-descriptor: 137"),
        )
        assertTrue(
            "secret 必须带引号 —— 模板里写的就是 secret: \"{{SECRET}}\"",
            result.contains("secret: \"abc123\""),
        )
    }

    @Test
    fun `节点内容原样进入配置`() {
        val template = readAsset(ConfigBuilder.TEMPLATE_ASSET)
        val result = ConfigBuilder.build(template, SAMPLE_NODES, fd = 1, secret = "s")

        assertTrue(result.contains("name: 香港|@ripaojiedian"))
        assertTrue(result.contains("server: 1.2.3.4"))
    }

    /**
     * 节点名里带 `$` / `\` 时不能被当成替换模式解释。
     *
     * 这一条是桌面端那次"6027 个节点整份报废"的教训: 用 `Regex.replace` 或
     * `Matcher.replaceAll` 时, 替换串里的 `$1` / `\` 有特殊含义, 会直接抛
     * 异常或产出错乱的配置。免费池里的节点名什么字符都有。
     */
    @Test
    fun `节点名里的美元符号和反斜杠不会被当成替换模式`() {
        val template = readAsset(ConfigBuilder.TEMPLATE_ASSET)
        val nasty = "  - name: \"a\$1b\\\\c&d\"\n    type: direct\n"
        val result = ConfigBuilder.build(template, nasty, fd = 1, secret = "s")

        assertTrue("字面量必须原样保留", result.contains("""name: "a$1b\\c&d""""))
    }

    // ------------------------------------------------------------ 离线解析
    //
    // 这两个函数服务的是"内核没起来时也要能看到节点列表"(见
    // EngineRuntime.offlineNodes)。它们跑在**已安装资源**上, 所以这里的
    // 用例尽量用真文件, 而不是手写一段理想化的 YAML —— 真文件里的形态
    // (引号、缩进、CRLF、emoji) 才是会出错的地方。

    @Test
    fun `从真配置里解析出策略组成员并集`() {
        val template = readAsset(ConfigBuilder.TEMPLATE_ASSET)
        val nodes = readAsset(AssetInstaller.NODES_FILE)
        val config = ConfigBuilder.build(template, nodes, fd = 7, secret = "s")

        val members = ConfigBuilder.parseGroupMembers(config)

        assertTrue("至少要解析出主策略组的成员, 实际 ${members.size} 个", members.size > 10)
        assertTrue("主策略组名必须在列表里", "🚀 节点选择" in members)
        assertTrue("自动选择组也要在 (它是可选出口)", "♻️ 自动选择" in members)
        assertTrue("DIRECT 也要在 —— 用户得能把某个组切回直连", "DIRECT" in members)
        assertTrue(
            "不能把 `name:` 这个键名当成名字带出来 —— 否则界面上会出现一堆 " +
                "\"name: xxx\" 的假节点",
            members.none { it.startsWith("name:") },
        )
    }

    @Test
    fun `策略组成员不重复`() {
        val template = readAsset(ConfigBuilder.TEMPLATE_ASSET)
        val nodes = readAsset(AssetInstaller.NODES_FILE)
        val members = ConfigBuilder.parseGroupMembers(
            ConfigBuilder.build(template, nodes, fd = 7, secret = "s"),
        )
        assertEquals("去重没生效的话界面上会出现重复行", members.size, members.toSet().size)
        // 策略组本身也是可选项, 名字同样要收 (它们是"组内组")
        assertTrue("AI 服务组名也要收", members.any { it.contains("AI") })
    }

    @Test
    fun `没有 proxy-groups 段时返回空列表而不是抛异常`() {
        assertEquals(emptyList<String>(), ConfigBuilder.parseGroupMembers("mixed-port: 7890\n"))
    }

    @Test
    fun `解析组成员时不会把别段的 name 捞进来`() {
        val config = """
            proxy-groups:
              - name: "G1"
                type: select
                proxies:
                  - "A"
                  - "B"
            rules:
              - DOMAIN-SUFFIX,x.com,G1
              - name: 这行不该出现
        """.trimIndent()

        assertEquals(listOf("G1", "A", "B"), ConfigBuilder.parseGroupMembers(config))
    }

    @Test
    fun `解析组成员时碰到下一个字段就停止收集`() {
        val config = """
            proxy-groups:
              - name: "G1"
                type: select
                proxies:
                  - "A"
                url: "http://x"
              - name: "G2"
                type: select
                proxies:
                  - "B"
        """.trimIndent()

        assertEquals(listOf("G1", "A", "G2", "B"), ConfigBuilder.parseGroupMembers(config))
    }

    @Test
    fun `成员名两边的引号会被去掉`() {
        val config = """
            proxy-groups:
              - name: G1
                proxies:
                  - "带双引号"
                  - '带单引号'
                  - 裸的
        """.trimIndent()

        assertEquals(
            listOf("G1", "带双引号", "带单引号", "裸的"),
            ConfigBuilder.parseGroupMembers(config),
        )
    }

    @Test
    fun `从真 nodes_yaml 里解析出节点名`() {
        val names = ConfigBuilder.parseProxyNames(readAsset(AssetInstaller.NODES_FILE))

        assertTrue("真 nodes.yaml 里有近万个节点, 实际 ${names.size}", names.size > 1000)
        assertEquals("节点名不该重复", names.size, names.toSet().size)
        assertTrue(
            "不能把节点定义里更深的字段 (ws-opts / headers / Host) 捞进来 —— " +
                "判据是必须以 \"- name:\" 开头",
            names.none { it.contains(":") && it.length < 3 },
        )
    }

    @Test
    fun `解析节点名只认顶层序列项`() {
        val yaml = """
              - name: 真节点
                type: vmess
                ws-opts:
                  path: "/"
                  headers:
                    Host: not-a-node
              - name: 另一个真节点
                type: direct
        """.trimIndent()

        assertEquals(listOf("真节点", "另一个真节点"), ConfigBuilder.parseProxyNames(yaml))
    }

    @Test
    fun `节点名里的 emoji 和加号不会被弄坏`() {
        val yaml = "  - name: \"🇭🇰_HK_中国香港->🇯🇵_JP_日本\"\n    type: vmess\n"
        assertEquals(
            listOf("🇭🇰_HK_中国香港->🇯🇵_JP_日本"),
            ConfigBuilder.parseProxyNames(yaml),
        )
    }

    @Test
    fun `节点名里的冒号不会被截断`() {
        // 配置模板里真有这种名字: "🇭🇰_HK_中国香港->🇯🇵_JP_日本"。冒号出现在
        // YAML 值里是合法的, 用 split(":") 去取就会把它砍掉。
        val yaml = "  - name: \"a:b:c\"\n    type: vmess\n"
        assertEquals(listOf("a:b:c"), ConfigBuilder.parseProxyNames(yaml))
    }

    // ---------------------------------------------------------------- 缩进契约

    /**
     * 这是本文件最重要的断言: 节点项的缩进必须和模板里策略组项的缩进一致。
     */
    @Test
    fun `真实 nodes_yaml 的缩进和模板里的策略组一致`() {
        val template = readAsset(ConfigBuilder.TEMPLATE_ASSET)
        val nodes = readAsset(AssetInstaller.NODES_FILE)

        val result = ConfigBuilder.build(template, nodes, fd = 7, secret = "s")

        val nodeIndent = indentationOfFirstProxy(result)
        val groupIndent = indentationOfFirstGroup(template)

        assertEquals(
            "节点项和策略组项必须同层, 否则 YAML 会把节点解析进 proxies 映射里",
            groupIndent,
            nodeIndent,
        )
    }

    @Test
    fun `已经是缩进好的节点列表不会被再缩一层`() {
        val once = ConfigBuilder.indentProxies(SAMPLE_NODES)
        val twice = ConfigBuilder.indentProxies(once)
        assertEquals("缩进必须幂等 —— 缩两次就散架了", once, twice)
    }

    /**
     * 仓库里现存的 `nodes.yaml` 实际是 **4 个空格**缩进的, 和
     * `build_assets.py` 现在会生成的 2 空格不一致。这一条把这个真实形态钉住。
     */
    @Test
    fun `多缩一层的节点列表会被归一化到两个空格`() {
        val overIndented = "    - name: a\n      type: direct\n"
        val fixed = ConfigBuilder.indentProxies(overIndented)

        assertTrue("必须减到 2 个空格, 实际首行: >${fixed.lineSequence().first()}<", fixed.startsWith("  - name: a"))
        assertTrue("相对缩进要保留 (子字段比父字段多两格)", fixed.contains("\n    type: direct"))
    }

    @Test
    fun `CRLF 换行会被归一化成 LF`() {
        val crlf = "  - name: a\r\n    type: direct\r\n"
        val fixed = ConfigBuilder.indentProxies(crlf)

        assertFalse("残留的 \\r 会让 YAML 把回车当成内容的一部分", fixed.contains('\r'))
        assertTrue(fixed.startsWith("  - name: a"))
    }

    @Test
    fun `两种缩进形态归一化后完全一致`() {
        val correct = "  - name: a\n    type: direct\n"
        val overIndented = "    - name: a\n      type: direct\n"
        assertEquals(
            "不管生成侧给的是 2 格还是 4 格, 最终配置必须一模一样",
            ConfigBuilder.indentProxies(correct),
            ConfigBuilder.indentProxies(overIndented),
        )
    }

    @Test
    fun `顶格的节点列表会被兜底补上两个空格`() {
        val flat = "- name: a\n  type: direct\n"
        val fixed = ConfigBuilder.indentProxies(flat)
        assertTrue(fixed.startsWith("  - name: a"))
        assertTrue("第二行也要跟着缩进", fixed.contains("\n    type: direct"))
    }

    @Test
    fun `空白节点列表得到空串, 而不是一堆缩进`() {
        assertEquals("", ConfigBuilder.indentProxies(""))
        assertEquals("", ConfigBuilder.indentProxies("\n\n   \n"))
    }

    // ---------------------------------------------------------------- 失败路径

    @Test
    fun `模板缺占位符时抛出并指出是哪一个`() {
        val broken = readAsset(ConfigBuilder.TEMPLATE_ASSET)
            .replace(ConfigBuilder.PLACEHOLDER_FD, "8500")

        val error = runCatching {
            ConfigBuilder.build(broken, SAMPLE_NODES, fd = 1, secret = "s")
        }.exceptionOrNull()

        assertTrue("应当抛 IllegalArgumentException", error is IllegalArgumentException)
        assertTrue(
            "错误信息必须点名 {{FD}}, 否则要从 28000 行 YAML 里找一个括号。实际: ${error?.message}",
            error?.message?.contains(ConfigBuilder.PLACEHOLDER_FD) == true,
        )
    }

    @Test
    fun `空节点列表直接失败, 不允许悄悄起一个没有 proxies 的配置`() {
        val template = readAsset(ConfigBuilder.TEMPLATE_ASSET)
        val error = runCatching {
            ConfigBuilder.build(template, "   \n", fd = 1, secret = "s")
        }.exceptionOrNull()
        assertTrue(error is IllegalArgumentException)
    }

    @Test
    fun `负数 fd 被拒绝`() {
        val template = readAsset(ConfigBuilder.TEMPLATE_ASSET)
        val error = runCatching {
            ConfigBuilder.build(template, SAMPLE_NODES, fd = -1, secret = "s")
        }.exceptionOrNull()
        assertTrue(error is IllegalArgumentException)
    }

    // ---------------------------------------------------------------- 写文件

    @Test
    fun `写入是原子的 —— 内容对了, 而且不留 tmp 文件`() {
        val dir = createTempDir("config-builder")
        try {
            val target = ConfigBuilder.writeTo(dir, "mode: rule\n")
            assertEquals("mode: rule\n", target.readText())
            assertEquals(ConfigBuilder.CONFIG_FILE_NAME, target.name)
            assertFalse(
                "暂存文件没清掉的话, 下次写入会撞上它",
                File(dir, "${ConfigBuilder.CONFIG_FILE_NAME}.tmp").exists(),
            )
        } finally {
            dir.deleteRecursively()
        }
    }

    @Test
    fun `重复写入会替换旧配置`() {
        val dir = createTempDir("config-builder-2")
        try {
            ConfigBuilder.writeTo(dir, "old")
            val target = ConfigBuilder.writeTo(dir, "new")
            assertEquals("new", target.readText())
        } finally {
            dir.deleteRecursively()
        }
    }

    // ---------------------------------------------------------------- 工具

    /**
     * 配置里 `proxies:` 下面第一个条目的缩进宽度。
     *
     * 刻意**从 `proxies:` 那一行往下找**, 而不是全文扫第一个带缩进的
     * `- name:`: 模板里 `proxy-groups`、`rule-providers` 等段落也有 `- name:`,
     * 全文扫会量到一段和节点无关的缩进 (第一版就是这么写的, 结果量出个 4 来,
     * 白白怀疑了半天节点列表)。
     */
    private fun indentationOfFirstProxy(config: String): Int {
        val lines = config.lines()
        val start = lines.indexOfFirst { it.trimEnd() == "proxies:" }
        check(start >= 0) { "生成的配置里找不到 proxies: 段" }
        val first = lines.drop(start + 1).firstOrNull { it.isNotBlank() }
            ?: error("proxies: 段后面是空的")
        return first.length - first.trimStart().length
    }

    /** 模板里第一个策略组项的缩进宽度 —— 节点缩进必须和它对齐。 */
    private fun indentationOfFirstGroup(template: String): Int =
        template.lines()
            .first { it.trimStart().startsWith("- name:") }
            .let { it.length - it.trimStart().length }

    private fun createTempDir(prefix: String): File {
        val f = File.createTempFile(prefix, "")
        check(f.delete()) { "无法清理临时文件占位: ${f.absolutePath}" }
        check(f.mkdirs()) { "无法创建临时目录: ${f.absolutePath}" }
        return f
    }

    private companion object {
        /**
         * 项目根。
         *
         * 单测的工作目录是 Gradle 传进来的 `android/app` (不是仓库根), 所以要
         * 往上退两级才能找到 `android/app/src/main/assets`。这里不写死绝对路径 ——
         * 写死的话换台机器/换个人克隆就跑不了, 而这类测试恰恰是新人第一次
         * 跑 `gradlew test` 时最需要能跑起来的东西。
         *
         * `parentFile` 是可空的 (File API 如此), 所以退两级时要显式处理 ——
         * 用 `check` 而不是 `!!`: 万一目录结构变了, 报出来的是"我在哪、找不到谁",
         * 而不是一个 NPE 堆栈。
         */
        val ROOT: File = run {
            val cwd = File(System.getProperty("user.dir") ?: ".")
            if (cwd.name != "app") {
                cwd
            } else {
                val androidDir = cwd.parentFile
                val repoRoot = androidDir?.parentFile
                check(repoRoot != null) { "从 $cwd 往上找不到仓库根" }
                repoRoot
            }
        }

        val ASSETS: File = File(ROOT, "android/app/src/main/assets")

        /**
         * 读真资源。文件缺失时**直接抛**而不是跳过测试:
         * 资源是 APK 的一部分, 它不在了说明构建链断了, 这种时候让测试红掉
         * 才是对的。
         */
        fun readAsset(relative: String): String {
            val f = File(ASSETS, relative)
            check(f.isFile) { "找不到资源文件: ${f.absolutePath} (当前根目录 ${ROOT.absolutePath})" }
            return f.readText()
        }

        /**
         * 一小段形态正确的节点列表 —— 缩进和 `nodes.yaml` 一致 (两个空格)。
         * 需要构造"坏数据"的用例在各自的方法里现写, 免得这个常量被改坏。
         */
        const val SAMPLE_NODES: String = """  - name: 香港|@ripaojiedian
    type: vmess
    server: 1.2.3.4
    port: 443
"""
    }
}
