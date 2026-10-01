package com.accesspilot.hongxing.core

import android.content.Context
import java.io.File
import java.io.IOException

/**
 * 红杏 Android · 资源安装 (首次启动 / 升级后解压 assets)
 *
 * mihomo 的 `-d` 工作目录里必须真实存在这些东西, 缺一个内核就直接起不来:
 *  - `geosite.dat` / `geoip.metadb` / `country.mmdb` —— 规则里 GEOSITE/GEOIP
 *    的解析数据, 配置模板用 `geodata-mode: false`, 走的就是这两个文件;
 *  - `ruleset/` 下的那 20 个 yaml —— `rule-providers` 的本地缓存;
 *  - `nodes.yaml` —— 节点列表, [ConfigBuilder] 把它注进 `proxies:`。
 *
 * APK 里的 assets 是**只读且不能 mmap 给内核用**的 (它们在 APK 内部,
 * 内核拿到的是 zip 条目不是文件), 所以必须解压一份到私有目录。
 *
 * ## 装到哪里 —— 这里有一个踩过的坑
 *
 * **装到 `filesDir` 根下, 不装到子目录里。** 因为 mihomo 的 `-d` 就是
 * `filesDir/mihomo`, 而它的 geodata / rule-provider 路径都是**相对 `-d`** 解析的。
 * 第一版这里装到了 `filesDir`, 而 [EngineRuntime] 把 `-d` 指向 `filesDir/mihomo`,
 * 于是内核找不到 geodata 和节点列表 —— 而界面上的表现是
 * "节点列表缺失: …/files/mihomo/nodes.yaml (资源安装没跑完?)",
 * 看起来像安装逻辑坏了, 实际上是两边对"工作目录"的理解不一致。
 *
 * 所以 `-d` 指向的目录该由 [EngineRuntime] 决定, 而且只能有一个地方决定。
 * 现在: `-d` = `filesDir` 根 (见 [EngineRuntime.workDir]), 本类也装到那里。
 * 暂存目录 `assets-staging` 是它的兄弟目录而不是子目录, 免得内核在自己的
 * 工作目录里看到一个它不认识的半成品目录。
 *
 * ## 版本标记机制 (这是本文件存在的主要理由)
 *
 * 解压只在"包内资源比已安装的新"时发生。判据是三级, 从粗到细:
 *  1. **versionCode** —— 用户升级 App 后必然变。这是最常见的那条路。
 *  2. **versionName** —— versionCode 相同时再比一次 (比如本地装了
 *     `assembleDebug` 覆盖安装, versionCode 没动)。
 *  3. **指纹** —— 前两者都没变时, 用"资源条目数 + 总字节数"再兜一道。
 *     这一级是为了开发期: `build_assets.py` 重新生成了规则集但 versionCode
 *     没改, 只比版本号的话新规则**永远不会生效**, 而现象是"我明明改了规则
 *     怎么还是老样子" —— 这种问题查起来极费时间, 一道指纹就能免掉。
 *
 * 反过来说, 判据绝不能只有"文件存不存在": 那样用户升级 App 后新规则集
 * 永远不生效, 而且**没有任何报错**, 只是分流结果慢慢变得不对。
 *
 * ## 为什么用暂存目录 + 替换, 而不是就地覆盖
 *
 * 就地覆盖有两个坑: 一是复制过程中 App 被杀会留下半套资源, 下次启动
 * "文件都在"于是跳过安装, 内核读到一个截断的 `.dat` 直接崩; 二是 mihomo
 * 正在跑的时候覆盖 `ruleset/` 里的 yaml, 它会读到写了一半的文件。
 * 所以先在 `assets-staging/` 里整套铺好, 再逐个搬进来。
 */
internal class AssetInstaller(
    private val context: Context,
    private val store: SettingsStore,
    /** APK 的 versionCode。抽成构造参数是为了单测能构造出"升级"场景。 */
    private val packageVersionCode: Long = readPackageVersionCode(context),
    /** APK 的 versionName, 同上。 */
    private val packageVersionName: String = readPackageVersionName(context),
) {

    /** 资源落地目录 —— 和 [EngineRuntime] 给内核的 `-d` 必须是同一个。 */
    private val targetDir: File get() = context.filesDir

    /**
     * 确保工作目录里的资源是最新的。幂等, 可以每次启动都无脑调。
     *
     * @param progress 进度回调 (0f..1f, 文案)。首次安装要复制约 40 MB,
     *                 不报进度用户会以为卡死了。
     * @return 实际解压了多少个文件 (0 = 已经是最新, 什么都没做)
     * @throws IOException 复制失败。调用方负责转成 [EngineStatus.error]。
     */
    fun ensureInstalled(progress: ((Float, String) -> Unit)? = null): Int {
        val entries = buildManifest()
        val fingerprint = AssetVersion.fingerprintOf(
            entries.size,
            entries.sumOf { it.sizeBytes },
        )

        if (!AssetVersion.needsInstall(packageVersionCode, store.installedAssetsVersion)) {
            // versionCode 没涨, 再看 name 和指纹 —— 两道兜底见类注释。
            val nameChanged = store.installedAssetsVersion == packageVersionCode &&
                store.installedAssetsFingerprint.isNotEmpty() &&
                !AssetVersion.sameVersionName(packageVersionName, store.installedAssetsName)
            val contentChanged = store.installedAssetsFingerprint.isNotEmpty() &&
                store.installedAssetsFingerprint != fingerprint

            if (!nameChanged && !contentChanged) return 0
        }

        return install(entries, packageVersionCode, packageVersionName, fingerprint, progress)
    }

    // ------------------------------------------------------------ 实际安装

    private fun install(
        entries: List<AssetEntry>,
        versionCode: Long,
        versionName: String,
        fingerprint: String,
        progress: ((Float, String) -> Unit)?,
    ): Int {
        val staging = File(context.filesDir, STAGING_DIR)
        staging.deleteRecursively()
        if (!staging.mkdirs() && !staging.isDirectory) {
            throw IOException("无法创建暂存目录: ${staging.absolutePath}")
        }

        val totalBytes = entries.sumOf { it.sizeBytes }.coerceAtLeast(1L)
        var doneBytes = 0L

        try {
            entries.forEachIndexed { index, entry ->
                val target = File(staging, entry.relativePath)
                target.parentFile?.mkdirs()

                context.assets.open(entry.assetPath).use { input ->
                    target.outputStream().use { output -> input.copyTo(output) }
                }

                doneBytes += entry.sizeBytes
                progress?.invoke(
                    (doneBytes.toFloat() / totalBytes).coerceIn(0f, 1f),
                    "正在准备规则数据 ${index + 1}/${entries.size}",
                )
            }

            // 暂存铺好以后, 逐个搬进目标目录。
            entries.forEach { entry ->
                val from = File(staging, entry.relativePath)
                val to = File(targetDir, entry.relativePath)
                to.parentFile?.mkdirs()

                if (!from.renameTo(to)) {
                    // renameTo 在"目标已存在"时可能失败 (不同实现不一样), 所以
                    // 失败了退一步: 先删目标再搬。删和搬之间的窗口极短, 而且
                    // 只有升级路径才会走到 —— 首次安装时目标根本不存在。
                    if (to.exists() && to.delete() && from.renameTo(to)) return@forEach
                    throw IOException("无法安装资源: ${to.absolutePath}")
                }
            }
        } finally {
            // 成功时这里已经是空壳; 失败时它可能留着半套文件 —— 必须清掉,
            // 否则下次启动会看到"暂存目录里有东西"而产生误判。
            staging.deleteRecursively()
        }

        store.installedAssetsVersion = versionCode
        store.installedAssetsName = versionName
        store.installedAssetsFingerprint = fingerprint

        progress?.invoke(1f, "规则数据就绪")
        return entries.size
    }

    // -------------------------------------------------------------- 清单

    private fun buildManifest(): List<AssetEntry> {
        val out = mutableListOf<AssetEntry>()

        // 地球数据: 名字固定, 直接列出而不是扫目录 —— 少一次 I/O,
        // 而且这几个文件缺了内核一定起不来, 显式列出等于一份自检清单。
        GEODATA_FILES.forEach { name ->
            out += AssetEntry(name, name, sizeOf(name))
        }

        // 规则集: 目录内容会随 build_assets.py 变化, 必须扫。
        listAssetDir(RULESET_DIR).forEach { fileName ->
            val rel = "$RULESET_DIR/$fileName"
            out += AssetEntry(rel, rel, sizeOf(rel))
        }

        out += AssetEntry(NODES_FILE, NODES_FILE, sizeOf(NODES_FILE))
        return out
    }

    private fun listAssetDir(dir: String): List<String> =
        try {
            (context.assets.list(dir) ?: emptyArray()).filter { it.isNotBlank() }.sorted()
        } catch (_: IOException) {
            emptyList()
        }

    /** assets 里的条目长度。拿不到就返回 0 —— 它只影响指纹, 不该让安装失败。 */
    private fun sizeOf(assetPath: String): Long =
        try {
            context.assets.openFd(assetPath).use { it.length }
        } catch (_: Throwable) {
            // 压缩过的 asset 打开 openFd 会抛 FileNotFoundException,
            // 这是正常的, 不是错误。
            0L
        }

    private data class AssetEntry(
        val assetPath: String,
        val relativePath: String,
        val sizeBytes: Long,
    )

    internal companion object {
        val GEODATA_FILES = listOf("geosite.dat", "geoip.metadb", "country.mmdb")
        const val RULESET_DIR = "ruleset"
        const val NODES_FILE = "nodes.yaml"
        const val STAGING_DIR = "assets-staging"

        /**
         * 工作目录里需要**跨重装保留**的运行时文件。
         *
         * `cache.db` 是 mihomo 的缓存数据库 (配置模板开了 `store-selected`
         * 和 `store-fake-ip`), 它跟配置文件并排存放。资源安装不碰它, 但这里
         * 显式列出来是给后来人看的: 谁要是把安装逻辑改成"清空工作目录再铺",
         * 就必须先看这份名单, 否则用户每次升级 App 都会被忘掉选中的节点。
         */
        val PRESERVED_RUNTIME_FILES = listOf("cache.db")

        fun readPackageVersionCode(context: Context): Long =
            try {
                val info = context.packageManager.getPackageInfo(context.packageName, 0)
                @Suppress("DEPRECATION")
                info.versionCode.toLong()
            } catch (_: Throwable) {
                1L
            }

        fun readPackageVersionName(context: Context): String =
            try {
                context.packageManager.getPackageInfo(context.packageName, 0)
                    .versionName.orEmpty()
            } catch (_: Throwable) {
                ""
            }
    }
}

/**
 * 资源版本判据 —— 抽成纯对象是为了能在**单元测试**里跑。
 *
 * [AssetInstaller] 整体依赖 `Context`/`AssetManager`, 在 JVM 测试里跑不起来;
 * 而"什么时候该覆盖资源"恰恰是最容易写错、也最值得测的一段逻辑 (写错的
 * 后果是用户升级后新规则静默不生效), 所以把它单独摘出来。
 */
internal object AssetVersion {

    /**
     * 包内版本是否比已安装的新。
     *
     * @param packageVersionCode 当前 APK 的 versionCode
     * @param installedVersionCode 已安装资源的 versionCode (0 = 从没装过)
     */
    fun needsInstall(packageVersionCode: Long, installedVersionCode: Long): Boolean =
        packageVersionCode > installedVersionCode

    /**
     * 两个 versionName 是否等价。
     *
     * 只做 trim 后的大小写无关比较, **不解析语义版本**: versionName 是给人看的
     * 自由文本 ("1.0.0-debug" / "1.0.0"), 任何"1.10.0 > 1.9.0"式的比较在遇到
     * 后缀时都会给出离谱结论。这里只需要回答"变没变", 那是字符串比较的活,
     * 判断"谁更新"交给 versionCode。
     */
    fun sameVersionName(a: String, b: String): Boolean =
        a.trim().equals(b.trim(), ignoreCase = true)

    /**
     * 资源指纹: 条目数 + 总字节数。
     *
     * 为什么不用每个文件的 SHA-256: 首次安装要哈希约 40 MB, 在低端机上是
     * 好几秒的白工, 而这两级判据要解决的问题只是"build_assets.py 重新生成过
     * 资源但版本号没动"。条目数或总字节数一变就足以发现这种情况; 真要精确到
     * 内容级, 那是发布流程该做的校验, 不该压在用户的开机路径上。
     */
    fun fingerprintOf(entryCount: Int, totalBytes: Long): String = "$entryCount:$totalBytes"
}
