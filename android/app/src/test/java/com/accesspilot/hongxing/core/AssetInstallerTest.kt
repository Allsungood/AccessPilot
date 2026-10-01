package com.accesspilot.hongxing.core

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.junit.runners.JUnit4

/**
 * 红杏 Android · 资源版本判据的单元测试
 *
 * ## 为什么这段逻辑值得单独测
 *
 * `AssetInstaller` 整体要 `Context` / `AssetManager`, 在纯 JVM 测试里跑不起来。
 * 但"什么时候该覆盖已安装的资源"恰恰是**最容易写错、错了也最难发现**的一段:
 *
 *  - 判据写成"文件存不存在" → 用户升级 App 后新规则集**永远不生效**, 而且
 *    没有任何报错, 只是分流结果慢慢变得不对;
 *  - 判据写成"versionCode 相等就跳过" → 开发期 `build_assets.py` 重新生成了
 *    规则集但版本号没动时, 改了规则却怎么都不生效。
 *
 * 所以把判据摘成纯对象 [AssetVersion] 单独测。这也正是它被摘出来的原因。
 */
@RunWith(JUnit4::class)
class AssetInstallerTest {

    // ------------------------------------------------------------ versionCode

    @Test
    fun `从没装过时一定要安装`() {
        assertTrue(AssetVersion.needsInstall(packageVersionCode = 1, installedVersionCode = 0))
    }

    @Test
    fun `升级 App 后要重新安装资源`() {
        assertTrue(
            "这是最常见的那条路: 用户升级了 App, 包里的规则集是新的, 必须覆盖",
            AssetVersion.needsInstall(packageVersionCode = 2, installedVersionCode = 1),
        )
    }

    @Test
    fun `同一个版本不需要重复安装`() {
        assertFalse(
            "每次启动都复制 40 MB 会让冷启动变得无法忍受",
            AssetVersion.needsInstall(packageVersionCode = 3, installedVersionCode = 3),
        )
    }

    @Test
    fun `降级安装不回退资源`() {
        assertFalse(
            "装回旧版 APK 时不该把资源也退回旧的 —— 系统包管理器自己会处理降级, " +
                "我们在这里跟着回退只会让规则集莫名其妙地变旧",
            AssetVersion.needsInstall(packageVersionCode = 1, installedVersionCode = 2),
        )
    }

    // ------------------------------------------------------------ versionName

    @Test
    fun `versionName 比较忽略首尾空白和大小写`() {
        assertTrue(AssetVersion.sameVersionName("1.0.0", " 1.0.0 "))
        assertTrue(AssetVersion.sameVersionName("1.0.0-DEBUG", "1.0.0-debug"))
    }

    @Test
    fun `versionName 变了就是变了`() {
        assertFalse(AssetVersion.sameVersionName("1.0.0", "1.0.1"))
    }

    @Test
    fun `刻意不做语义版本比较 —— 后缀不该让 1_10_0 小于 1_9_0`() {
        // 这里只断言"不相等", 因为 sameVersionName 回答的是"变没变",
        // 不是"谁更新"。判断谁更新是 versionCode 的活。
        assertFalse(AssetVersion.sameVersionName("1.10.0", "1.9.0"))
    }

    @Test
    fun `两个空 versionName 算相同`() {
        assertTrue(AssetVersion.sameVersionName("", ""))
    }

    // ---------------------------------------------------------------- 指纹

    @Test
    fun `条目数或总字节数任一变化, 指纹就变`() {
        val base = AssetVersion.fingerprintOf(entryCount = 24, totalBytes = 40_000_000)
        assertNotEquals(base, AssetVersion.fingerprintOf(entryCount = 25, totalBytes = 40_000_000))
        assertNotEquals(base, AssetVersion.fingerprintOf(entryCount = 24, totalBytes = 40_000_001))
    }

    @Test
    fun `同样的条目数和字节数得到同样的指纹`() {
        assertEquals(
            AssetVersion.fingerprintOf(24, 12345),
            AssetVersion.fingerprintOf(24, 12345),
        )
    }

    @Test
    fun `指纹是稳定的字符串, 不依赖对象身份`() {
        // 指纹要跨进程持久化到 SharedPreferences, 所以必须是确定性文本,
        // 不能是 hashCode 之类在同一台机器上也会变的东西。
        val fp = AssetVersion.fingerprintOf(24, 40_000_000)
        assertEquals("24:40000000", fp)
    }

    // ------------------------------------------------------------------ 常量

    @Test
    fun `必需的 geodata 文件一个都不能少`() {
        // 内核的配置模板里写的是 geodata-mode: false, 走的就是 geosite.dat /
        // geoip.metadb 这两个文件; 少一个内核会**直接退出**, 而不是降级。
        assertEquals(
            listOf("geosite.dat", "geoip.metadb", "country.mmdb"),
            AssetInstaller.GEODATA_FILES,
        )
    }

    @Test
    fun `需要跨重装保留的运行时文件包含 cache_db`() {
        // 配置模板开了 store-selected / store-fake-ip, 这两个状态都落在
        // 工作目录的 cache.db 里 (和配置文件并列)。谁要是把安装逻辑改成
        // "清空工作目录再铺", 必须先看到这条断言。
        assertTrue("cache.db" in AssetInstaller.PRESERVED_RUNTIME_FILES)
    }
}
