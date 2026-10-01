package com.accesspilot.hongxing.core

import java.io.File

/**
 * 红杏 Android · 内核配置组装
 *
 * 只做一件事: 把随包的 `assets/config.template.yaml` 里的三个占位符替换掉,
 * 写成内核能吃的最终配置。**规则本身一个字都不改** —— 那份模板是桌面端真机
 * 验证过的同一套策略组 / rule-providers / DNS 防污染 / fake-ip, 在安卓端
 * 重写一遍等于把踩过的坑再踩一遍, 而且两边会慢慢漂移 (桌面改了规则安卓没跟上)。
 *
 * 三个占位符:
 *  - `{{FD}}`      —— VpnService 的 TUN 文件描述符号
 *  - `{{SECRET}}`  —— 内核 REST API 的凭据
 *  - `{{PROXIES}}` —— 节点列表
 *
 * ## `{{PROXIES}}` 的缩进契约 (这一条错了内核会直接起不来)
 *
 * 模板里是:
 * ```
 * proxies:
 * {{PROXIES}}
 * ```
 * 也就是占位符**顶格**。而 `build_assets.py` 生成的 `nodes.yaml` 里每一项
 * 已经领先两个空格:
 * ```
 *   - name: ...
 *     type: vmess
 * ```
 * (那个脚本从 PyYAML 的 dump 结果里砍掉 `x:` 这一层再补两个空格, 就是为了
 * 让这里的替换退化成一次纯粹的字符串拼接。)
 *
 * 所以 [render] 必须**原样**拼进去, 不能再加缩进; [indentProxies] 只在
 * 拿到一份意外顶格的节点列表时兜底。多缩一层的话 YAML 会把它解析成
 * `proxies` 映射里的一个怪键, 报错信息离真正的原因很远。
 *
 * ## 为什么不用正则做替换
 *
 * [String.replace] 是字面量替换, 节点名里带着 `$` / `\` / `&`
 * (免费池里的名字什么字符都有) 也不会被当成替换模式解释。这一条是桌面端
 * 那次"6027 个节点整份报废"之后加的教训, 别退回去用 `Regex.replace`。
 */
internal object ConfigBuilder {

    /** 模板里的三个占位符。改动模板时必须同步改这里, 由 [assertPlaceholders] 兜底。 */
    const val PLACEHOLDER_FD = "{{FD}}"
    const val PLACEHOLDER_SECRET = "{{SECRET}}"
    const val PLACEHOLDER_PROXIES = "{{PROXIES}}"

    /** 最终配置在内核工作目录里的文件名。 */
    const val CONFIG_FILE_NAME = "hongxing.yaml"

    /** 配置模板在 assets 里的路径。 */
    const val TEMPLATE_ASSET = "config.template.yaml"

    /**
     * 序列项在 `proxies:` 下面应有的缩进宽度。
     *
     * 模板里 `{{PROXIES}}` 是**顶格**的 (第 195 行), 所以节点行的缩进完全由
     * 这里决定, 必须和模板里 `proxy-groups:` 下的 `- name:` 一致 (也是 2)。
     */
    const val SEQUENCE_INDENT = 2

    /**
     * 组装最终配置文本。
     *
     * @param template 模板原文 (由调用方从 assets 读出来传进来 —— 这个对象
     *                 刻意不碰 `Context`, 这样拼装逻辑能在 JVM 单测里直接跑)
     * @param proxies  节点列表的 YAML 片段, 见类注释里的缩进契约
     * @param fd       TUN 文件描述符号
     * @param secret   REST API 凭据
     * @throws IllegalArgumentException 模板缺占位符, 或节点列表是空的 ——
     *         这两种情况拼出来的配置都必然起不来, 与其让内核报一个含糊的
     *         解析错, 不如在这里就带着确切原因失败
     */
    fun build(template: String, proxies: String, fd: Int, secret: String): String {
        assertPlaceholders(template)

        val body = indentProxies(proxies)
        require(body.isNotBlank()) {
            "节点列表是空的: 没有 proxies 的配置起不来, 也不该悄悄起一个直连"
        }
        require(fd >= 0) { "非法的 TUN fd: $fd" }

        return template
            .replace(PLACEHOLDER_SECRET, secret)
            .replace(PLACEHOLDER_FD, fd.toString())
            .replace(PLACEHOLDER_PROXIES, body)
    }

    /**
     * 把配置写进内核工作目录。返回写好的文件。
     *
     * 先写 `.tmp` 再 rename: 这份文件是内核启动时读的, 写到一半被系统杀掉
     * 会留下一个截断的 YAML, 下次启动内核会以一个和"上次没写完"毫无关系的
     * 报错退出。rename 在同一文件系统上是原子的, 顺手把这个窗口关掉。
     */
    fun writeTo(workDir: File, content: String): File {
        if (!workDir.exists() && !workDir.mkdirs()) {
            throw java.io.IOException("无法创建内核工作目录: ${workDir.absolutePath}")
        }
        val target = File(workDir, CONFIG_FILE_NAME)
        val tmp = File(workDir, "$CONFIG_FILE_NAME.tmp")
        tmp.writeText(content)
        if (target.exists() && !target.delete()) {
            throw java.io.IOException("无法替换旧配置: ${target.absolutePath}")
        }
        if (!tmp.renameTo(target)) {
            throw java.io.IOException("无法写入配置: ${target.absolutePath}")
        }
        return target
    }

    /**
     * 校验模板完整性。
     *
     * 单独一个方法而不是散在 [build] 里: 构建资源时 (`build_assets.py`) 和
     * 运行时都要这份清单, 而且缺哪个占位符必须报出**名字** —— 只报"模板
     * 无效"的话, 排查要从 28,000 行 YAML 里找一个拼错的括号。
     */
    fun assertPlaceholders(template: String) {
        val missing = PLACEHOLDERS.filterNot { template.contains(it) }
        require(missing.isEmpty()) {
            "配置模板缺少占位符: ${missing.joinToString(", ")} (模板可能被重新生成过, " +
                "检查 android/tools/build_assets.py 里的模板生成段)"
        }
    }

    /** 只抽取一个占位符是否还在, 给单测和自检用。 */
    fun placeholders(template: String): List<String> = PLACEHOLDERS.filter { template.contains(it) }

    /**
     * 把节点列表归一化成"序列项缩进正好两个空格"的形态。
     *
     * ## 为什么要做归一化, 而不是直接拼进去
     *
     * 直觉上这里只要做一次字符串拼接 —— `build_assets.py` 生成的 `nodes.yaml`
     * 已经缩进好了 (它从 PyYAML 的 dump 结果里砍掉 `x:` 那一层再补两个空格)。
     * 但**实测真机资源不是那样**: 仓库里现存的 `nodes.yaml` 是 **4 个空格**的
     * 缩进 + **CRLF** 换行, 和生成脚本现在的输出不一致 (脚本被改过, 或者资源
     * 是更早一版生成的)。
     *
     * 多缩一层不会让内核报"缩进错了", 它会报
     * `yaml: line N: did not find expected key`, 指向 `proxies:` 上面一行 ——
     * 而真正的原因在 70,000 行之外的一个空格上。所以这里不赌"生成侧是对的",
     * 而是按**最小缩进**把整块顶格化, 再统一补两个空格:
     *
     *  - 正确形态 (2 空格): 最小缩进 = 2 → 减掉 2 → 顶格 → 补 2 → 原样;
     *  - 多缩 (4 空格):     最小缩进 = 4 → 减掉 4 → 顶格 → 补 2 → 修正;
     *  - 顶格 (0 空格):     最小缩进 = 0 → 不动   → 顶格 → 补 2 → 修正;
     *  - 三种输入产出**同一个结果**, 所以这个函数是幂等的。
     *
     * 只在"第一行有缩进"时走这条慢路: 形态正常时 [String.lines] 的开销也要
     * 避免 —— `nodes.yaml` 有近 70,000 行, 这是用户点"连接"时的关键路径。
     */
    fun indentProxies(proxies: String): String {
        val normalized = proxies.replace("\r\n", "\n").replace('\r', '\n')
        if (normalized.isBlank()) return ""

        val lines = normalized.lines()
        val firstContent = lines.firstOrNull { it.isNotBlank() } ?: return ""

        // 快路: 已经是"正好两个空格"的正确形态 (生成脚本正常工作时走这里)。
        // 判断依据是首行缩进恰好为 2 —— 4 空格和 0 空格都要走下面的归一化。
        val firstIndent = firstContent.length - firstContent.trimStart().length
        if (firstIndent == SEQUENCE_INDENT) return normalized.trimEnd()

        val dedented = dedent(lines)
        return dedented.joinToString("\n") { line ->
            if (line.isNotBlank()) " ".repeat(SEQUENCE_INDENT) + line else line
        }.trimEnd()
    }

    /**
     * 按最小缩进把整块顶格化, 同时保留行与行之间的相对缩进。
     *
     * 用"最小缩进"而不是"首行缩进": 万一首行是个顶格的注释或空行, 用首行
     * 去减会把后面每一行都减成负数。最小缩进对这两种情况都安全。
     */
    private fun dedent(lines: List<String>): List<String> {
        val minIndent = lines
            .filter { it.isNotBlank() }
            .minOfOrNull { it.length - it.trimStart().length }
            ?: return lines
        if (minIndent == 0) return lines.map { it.trimEnd() }

        return lines.map { line ->
            when {
                line.isBlank() -> ""
                line.length >= minIndent -> line.substring(minIndent).trimEnd()
                else -> line.trimStart()
            }
        }
    }

    private val PLACEHOLDERS = listOf(PLACEHOLDER_FD, PLACEHOLDER_SECRET, PLACEHOLDER_PROXIES)

    // ------------------------------------------------------------ 离线解析
    //
    // 下面两个函数是给"内核还没起来时也要能看到节点列表"用的 (见
    // [EngineRuntime.offlineNodes])。都是纯字符串处理, 所以能在 JVM 单测里跑。
    //
    // 为什么是手写扫描而不是引一个 YAML 库: 这个项目的传统是零第三方依赖,
    // 而这里要提取的东西结构极其固定 (我们自己生成的配置和节点列表), 用手写
    // 扫描既够用又好测。**但它不是通用 YAML 解析器** —— 只认我们自己生成的
    // 那一份形态, 文档里写清楚, 免得以后有人拿它去解析用户导入的订阅。

    /**
     * 从生成好的配置里取出所有策略组成员的并集。
     *
     * 配置里每个策略组长这样:
     * ```yaml
     * proxy-groups:
     *   - name: "🚀 节点选择"
     *     type: select
     *     proxies:
     *       - "♻️ 自动选择"
     *       - "香港1"
     * ```
     * 取并集而不是只取主策略组: 模板里还有 "🤖 AI 服务" / "📺 流媒体" 等组,
     * 它们各有自己的成员; 界面上这些名字都是可选的, 少列出来用户会以为节点
     * 被吞了。
     *
     * ## 为什么要区分"项"和"字段"
     *
     * 策略组本身也是一个列表项 (`- name: "🚀 节点选择"`), 而它的名字**也是一个
     * 可选项** —— 用户能把某个组切到另一个组。所以项名要收。
     *
     * 但项下面的**字段**不行: `url:` / `interval:` 这些跟节点无关, 而嵌套的
     * `proxies:` 列表又是要收的。所以这里维护两个状态: 当前项的缩进, 以及
     * "是不是正走在某一层的 proxies: 列表里"。第一版没区分, 结果把组名
     * ("🚀 节点选择" / "🤖 AI 服务" …) 全漏掉了 —— 单测当场抓出来。
     *
     * @return 去重后的成员名 (保持首次出现的顺序, 让界面上的排序稳定)
     */
    fun parseGroupMembers(configText: String): List<String> {
        val lines = configText.replace("\r\n", "\n").lines()
        val start = lines.indexOfFirst { it.trimEnd() == GROUP_SECTION }
        if (start < 0) return emptyList()

        val out = LinkedHashSet<String>()
        /** 当前列表项 (`- …`) 的缩进; -1 = 还不在任何项里。 */
        var itemIndent = -1
        /** 正在收集的 `proxies:` 列表的缩进; -1 = 不在收集状态。 */
        var proxiesListIndent = -1

        for (i in start + 1 until lines.size) {
            val line = lines[i]
            if (line.isBlank()) continue

            val indent = line.length - line.trimStart().length
            // 回到顶层 = proxy-groups 段结束, 后面是 rules 之类。
            if (indent == 0) break

            val trimmed = line.trim()

            // 进了 proxies: 列表 —— 开始收集, 并记住这一层的缩进。
            if (trimmed == "$PROXIES_KEY:") {
                proxiesListIndent = indent
                continue
            }

            if (trimmed.startsWith("- ")) {
                val raw = trimmed.removePrefix("- ").trim()
                if (raw.isEmpty()) continue

                when {
                    // 这是一个新项 (缩进比任何字段都浅)。项名本身也是可选项 ——
                    // 用户能把某个组切到另一个组, 所以它要收进列表; 但要先剥掉
                    // `name:` 这个键, 否则界面上会出现一堆 "name: xxx" 的假节点。
                    // (第一版就是漏了这一步, 单测第二次抓出来。)
                    itemIndent < 0 || indent <= itemIndent -> {
                        proxiesListIndent = -1
                        itemIndent = indent
                        val name = unquote(raw.removePrefix("$NAME_KEY:").trim())
                        if (name.isNotEmpty()) out += name
                    }
                    // 还在 proxies: 列表里 —— 是成员, 整段就是名字。
                    proxiesListIndent >= 0 && indent > proxiesListIndent ->
                        unquote(raw).takeIf { it.isNotEmpty() }?.let { out += it }
                    // 其它嵌套列表 (比如某些组的 filter:), 不是成员。
                    else -> Unit
                }
                continue
            }

            // 普通字段行 (`type:` / `url:` …)。出现在项这一层就说明
            // proxies: 列表结束了。
            if (itemIndent >= 0 && indent <= itemIndent) proxiesListIndent = -1
        }
        return out.toList()
    }

    /**
     * 从 `nodes.yaml` 里取出节点名。
     *
     * 只认**顶层序列项** `- name: xxx`: 节点定义里还有 `ws-opts: / headers: /
     * Host:` 这些缩进更深的字段, 它们也可能带 `name:` 字样, 靠"行里有 name"
     * 去抓会把 `Host` 之类的值一起捞进来。
     */
    fun parseProxyNames(nodesYaml: String): List<String> {
        val out = LinkedHashSet<String>()
        nodesYaml.replace("\r\n", "\n").lines().forEach { line ->
            val trimmed = line.trim()
            if (!trimmed.startsWith("- name:")) return@forEach
            val name = unquote(trimmed.removePrefix("- name:").trim())
            if (name.isNotEmpty()) out += name
        }
        return out.toList()
    }

    /** 去掉 YAML 的单/双引号包裹。只处理首尾成对的那种。 */
    private fun unquote(raw: String): String {
        if (raw.length >= 2) {
            val first = raw.first()
            val last = raw.last()
            if ((first == '"' && last == '"') || (first == '\'' && last == '\'')) {
                return raw.substring(1, raw.length - 1)
            }
        }
        return raw
    }

    private const val GROUP_SECTION = "proxy-groups:"
    private const val PROXIES_KEY = "proxies"
    private const val NAME_KEY = "name"
}
