// 红杏 Android · app 模块
import java.io.File
import java.util.Properties
import java.util.zip.ZipFile

plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("org.jetbrains.kotlin.plugin.compose")
}

//: SDK 位置。先读 local.properties(AGP 自己也是这么找的), 再退环境变量,
//: 最后是这台机器的实际路径。只用于拼 NDK 的候选路径, 拼错了也只是找不到
//: NDK 然后优雅降级, 不会把构建卡死。
val sdkDirPath: String = run {
    val props = rootProject.file("local.properties")
    val fromProps = if (props.isFile) {
        props.readLines()
            .firstOrNull { it.trim().startsWith("sdk.dir") }
            ?.substringAfter("=")?.trim()?.replace("\\\\", "/")?.replace("\\:", ":")
    } else null
    fromProps ?: System.getenv("ANDROID_HOME") ?: "D:/Android/Sdk"
}

// ------------------------------------------------------------------ #
// release 签名凭据 —— **绝不回退 debug key**
// ------------------------------------------------------------------ #
// 为什么这一段必须存在 (2026-10 审计 N1): 之前 release 直接
// `signingConfig = signingConfigs.getByName("debug")`, 于是 v1.0.0 的包是
// 用**公开的** Android debug key 签的 (证书主题 CN=Android Debug, 密码就是
// "android")。后果有两层: Google Play 直接拒收 debug 签名; 而且任何人都能
// 用同一把公开私钥签一个"看起来是官方更新"的包 —— 签名这件事一旦错了,
// 想改只能换包名重发, 用户数据全丢。
//
// 所以这里: 读不到凭据就**不配签名**, 再由 verifyReleaseReadiness 把 release
// 构建直接判失败, 绝不悄悄退回 debug key。
//
// 找凭据的顺序 (命令行 > 环境变量 > 仓库内 > 当前用户主目录):
//   1. -Phongxing.keystoreProperties=<路径>
//   2. 环境变量 HONGXING_KEYSTORE_PROPERTIES
//   3. <android>/keystore.properties
//   4. <用户主目录>/hongxing-keystore.properties   ← 本机在用的那个
val keystorePropsFile: File? = run {
    val explicit = (findProperty("hongxing.keystoreProperties") as String?)
        ?: System.getenv("HONGXING_KEYSTORE_PROPERTIES")
    if (!explicit.isNullOrBlank()) {
        file(explicit).takeIf { it.isFile }
    } else {
        listOf(
            rootProject.file("keystore.properties"),
            File(System.getProperty("user.home"), "hongxing-keystore.properties"),
        ).firstOrNull { it.isFile }
    }
}

val releaseKeystore: Properties? = keystorePropsFile?.let { f ->
    Properties().apply { f.inputStream().use { load(it) } }
}

/** keystore.properties 里缺了哪几项 (空 = 齐了)。 */
val keystoreProblems: List<String> =
    if (releaseKeystore == null) {
        emptyList()
    } else {
        listOf("storeFile", "storePassword", "keyAlias", "keyPassword")
            .filter { releaseKeystore.getProperty(it).isNullOrBlank() }
    }

val releaseSigningUsable: Boolean = releaseKeystore != null && keystoreProblems.isEmpty()

// ------------------------------------------------------------------ #
// 随包资源与内核二进制: release 包的硬性输入
// ------------------------------------------------------------------ #
// 缺了它们 APK 照样能编出来、能装上、界面也正常 —— 只是每次连接都失败
// (没有 launcher 就拿不到 TUN fd; 没有 nodes.yaml 就是"节点列表是空的")。
// 这种"构建成功、装上才发现"的失败正是 verifyReleaseReadiness 要挡掉的。
val coreBinaryFile = file("src/main/jniLibs/arm64-v8a/libmihomo.so")
val nodesAssetFile = file("src/main/assets/nodes.yaml")
val rulesetDir = file("src/main/assets/ruleset")

// ------------------------------------------------------------------ #
// NDK 候选路径
// ------------------------------------------------------------------ #
// 教训(2026-10-01): 我先写成 `ndkPath = "D:/Android/android-ndk-r27c"` 写死,
// 而那个目录当时还不存在(NDK 还在下载)。结果是 **配置期** 就报
//   [CXX1101] Location specified by android.ndkPath did not contain a valid NDK
// 于是连 `:app:compileDebugKotlin` 都跑不了 —— 界面那边的 teammate 一行都
// 编不过, 我自己却因为是在改动之前跑的构建而没发现。
//
// 所以这里改成: 依次找 ANDROID_NDK_ROOT / 官方 zip 解压目录 / SDK 标准目录,
// **找到才启用 native 构建**, 找不到就只警告 (debug 侧界面照常能编)。
// 但 release 侧不再只是"很响的警告"了: verifyReleaseReadiness 会因为缺
// launcher 直接失败 —— 一个没有启动桥的正式包等于一个连不上的 App。
val ndkCandidates = listOfNotNull(
    System.getenv("ANDROID_NDK_ROOT")?.takeIf { it.isNotBlank() },
    "D:/Android/android-ndk-r27c",
    "$sdkDirPath/ndk/27.2.12479018",
)
val ndkRoot = ndkCandidates.firstOrNull { File(it).isDirectory }

android {
    namespace = "com.accesspilot.hongxing"
    compileSdk = 35

    defaultConfig {
        applicationId = "com.accesspilot.hongxing"
        minSdk = 26          // Android 8.0: VpnService.Builder 的现代 API 从这代开始齐全
        targetSdk = 35
        versionCode = 1
        versionName = "1.0.0"
    }

    signingConfigs {
        if (releaseSigningUsable) {
            create("release") {
                storeFile = file(releaseKeystore!!.getProperty("storeFile"))
                storePassword = releaseKeystore.getProperty("storePassword")
                keyAlias = releaseKeystore.getProperty("keyAlias")
                keyPassword = releaseKeystore.getProperty("keyPassword")
                // v1 也开: minSdk 26 其实只要 v2 就够, 但多一个方案不花什么代价,
                // 而某些国产 ROM 的分包/改包流程只认 v1 (踩过: 只签 v2 的包在
                // 某些应用市场被拒)。
                enableV1Signing = true
                enableV2Signing = true
                enableV3Signing = true
            }
        }
    }

    buildTypes {
        release {
            // v1 不混淆: 这个 app 大量依赖反射之外的 JNI/进程边界, 混淆收益小、
            // 排错成本高。等稳定了再开。
            isMinifyEnabled = false

            // 自己的 release key。凭据缺失时不配签名 —— 包会是 unsigned,
            // 随后 verifyReleaseReadiness / packageRelease 的校验会直接让
            // 构建失败, 而不是悄悄用 debug key 签出一个能装但发不出去的包。
            if (releaseSigningUsable) {
                signingConfig = signingConfigs.getByName("release")
            }

            // 见 MainActivity.handleDebugExtras: --ez connect / --ez fdprobe
            // 这两个诊断入口在正式包里必须是死的 (任何 App 或 adb 都能给
            // 导出的 launcher Activity 塞 extra, 那等于让外部代码替用户把
            // VPN 拉起来)。做成**编译期常量**而不是读系统属性/Debug 标志:
            // 运行期可改的开关不是安全边界。
            buildConfigField("boolean", "DEBUG_ENTRYPOINTS", "false")
        }
        debug {
            applicationIdSuffix = ".debug"
            buildConfigField("boolean", "DEBUG_ENTRYPOINTS", "true")
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions { jvmTarget = "17" }

    buildFeatures {
        compose = true
        // BuildConfig 在 AGP 8 里默认不生成, 而 DEBUG_ENTRYPOINTS 就住在里面,
        // 所以必须显式打开。
        buildConfig = true
    }

    // mihomo 是随包的可执行文件: 必须原样落到 lib 目录, 不做压缩/对齐处理,
    // 否则 Android 不允许从 nativeLibraryDir 执行它。
    packaging {
        jniLibs {
            useLegacyPackaging = true
            keepDebugSymbols += "**/libmihomo.so"
        }
        resources { excludes += "/META-INF/{AL2.0,LGPL2.1}" }
    }

    // 只打 arm64: 现在还在用 32 位安卓的手机基本没有了, 而 mihomo 二进制
    // 单个就 61 MB, 两套一起打 APK 会翻倍。需要 32 位时把 abiFilters 放开即可。
    defaultConfig {
        ndk { abiFilters += listOf("arm64-v8a") }
        externalNativeBuild {
            cmake {
                // launcher 是纯 C, 不链 libc++ —— 少一个依赖就少一份体积和一类
                // 启动失败的可能。它只调 fork/execve/setsid/signal, 全在 libc 里。
                arguments += listOf("-DANDROID_STL=none")
                cFlags += listOf("-Os", "-Wall", "-Wextra", "-fvisibility=hidden")
            }
        }
    }

    // ------------------------------------------------------------------ #
    // 为什么要自己编一个 native 启动器
    // ------------------------------------------------------------------ #
    // VpnService 建好 TUN 之后要把它交给 mihomo 子进程(配置里的
    // tun.file-descriptor)。但 **Java 的 ProcessBuilder 拿不到这个 fd** ——
    // AOSP 的 UNIXProcess_md.c 在 execve 之前会遍历 /proc/self/fd, 把编号 >= 4
    // 的**全部无条件 close**(closeDescriptors(), from_fd = FAIL_FILENO + 1 = 4)。
    // 注意是直接 close, 不是靠 FD_CLOEXEC 生效, 所以 detachFd() 清 CLOEXEC 也没用。
    // 而 android.system.Os 没有公开的 fork()。
    //
    // 所以自己 fork/exec: fork 出的子进程天然继承父进程整份 fd 表, execve 只关
    // 带 FD_CLOEXEC 的 —— 把 TUN fd 的 CLOEXEC 清掉, 它就能活到 mihomo 里。
    // 源码在 src/main/cpp/, 约 200 行。
    // ------------------------------------------------------------------ #
    if (ndkRoot != null) {
        ndkPath = ndkRoot
        // ndkVersion 必须和 ndkPath 指的**完全一致**, 否则 AGP 直接报
        //   [CXX1100] android.ndkVersion is [27.0.12077973] but android.ndkPath
        //             ... refers to a different version [27.2.12479018]
        // (27.0.12077973 是 AGP 8.7.3 的内置默认值, 而官方 zip 解出来的 r27c
        //  是 27.2.12479018)。两个都写, 别只写一个。
        ndkVersion = "27.2.12479018"
        logger.lifecycle("红杏: 使用 NDK $ndkRoot")
        externalNativeBuild {
            cmake {
                path = file("src/main/cpp/CMakeLists.txt")
                version = "3.22.1"
            }
        }
    } else {
        logger.warn(
            "红杏: 没找到 NDK, **跳过了 native 启动器**(libhongxing_launcher.so)。\n" +
            "        这样编出来的 APK 界面能用, 但连接时会起不了内核 ——\n" +
            "        Java 的 ProcessBuilder 拿不到 VpnService 的 fd(见 cpp/ 里的说明)。\n" +
            "        debug 包可以这样凑合看界面; **release 包会在\n" +
            "        verifyReleaseReadiness 这一步直接失败**, 因为一个连不上的正式包\n" +
            "        比构建失败糟糕得多。\n" +
            "        装 NDK:  sdkmanager --sdk_root=D:\\Android\\Sdk \"ndk;27.2.12479018\"\n" +
            "        或设环境变量 ANDROID_NDK_ROOT 指向已解压的 NDK。"
        )
    }
}

// ------------------------------------------------------------------ #
// release 硬闸门 (一): 输入齐不齐
// ------------------------------------------------------------------ #
// 过去这一整套只有 logger.warn 和 build.gradle.kts 里的一段注释, 于是
// "没有内核 / 没有 launcher / 没有资源"的 APK 能被当成正式包发出去 ——
// 而它装到手机上之后每一个连接都失败, 报错还长得像节点问题。
// 现在: release 任务一跑就先验这些输入, 缺一个就带着"怎么补"一起失败。
val verifyReleaseReadiness = tasks.register("verifyReleaseReadiness") {
    group = "verification"
    description = "release 构建前的硬性检查: 签名凭据 / 内核二进制 / 随包资源 / NDK"
    doLast {
        val problems = mutableListOf<String>()

        // 1) 签名凭据。这是 N1 的直接产物: 没有它就没有可发布的包。
        if (releaseKeystore == null) {
            problems += buildString {
                appendLine("没找到 release 签名凭据 (keystore.properties)。")
                appendLine("    找过这四处: -Phongxing.keystoreProperties= / 环境变量")
                appendLine("    HONGXING_KEYSTORE_PROPERTIES / ${rootProject.file("keystore.properties")}")
                appendLine("    / ${File(System.getProperty("user.home"), "hongxing-keystore.properties")}")
                append("    **不会退回 debug 签名**: 那样签出来的包 Google Play")
                append("直接拒收, 而且使用公开的 debug 私钥 = 谁都能伪造更新。")
            }
        } else if (keystoreProblems.isNotEmpty()) {
            problems += "签名凭据文件 ${keystorePropsFile?.absolutePath} 缺少: " +
                keystoreProblems.joinToString(", ")
        } else {
            val store = file(releaseKeystore.getProperty("storeFile"))
            if (!store.isFile) {
                problems += "签名凭据指向的 keystore 不存在: ${store.absolutePath}"
            }
        }

        // 2) native 启动器。没有 NDK 就没有它, 而没有它就拿不到 TUN fd。
        if (ndkRoot == null) {
            problems += "没找到 NDK (试过: ${ndkCandidates.joinToString(", ")}) —— " +
                "编不出 libhongxing_launcher.so, 内核起不来"
        }

        // 3) 内核二进制。缺了它 APK 里根本没有 mihomo。
        if (!coreBinaryFile.isFile) {
            problems += "内核二进制不存在: ${coreBinaryFile.absolutePath}\n" +
                "    补: python android/tools/fetch_core.py"
        }

        // 4) 随包资源。缺 nodes.yaml 时内核配置里 proxies 是空的;
        //    缺 geodata 时 mihomo 直接 exit 1。
        if (!nodesAssetFile.isFile) {
            problems += "节点列表不存在: ${nodesAssetFile.absolutePath}\n" +
                "    补: python android/tools/build_assets.py"
        }
        val rulesetCount = rulesetDir.listFiles { f -> f.isFile && f.extension == "yaml" }?.size ?: 0
        if (rulesetCount < 10) {
            problems += "规则集不完整: $rulesetDir 里只有 $rulesetCount 个 yaml " +
                "(模板的 rule-providers 引用着 20 个)\n" +
                "    补: python android/tools/build_assets.py"
        }

        if (problems.isNotEmpty()) {
            throw GradleException(
                buildString {
                    appendLine("红杏 release 构建被拦下了 —— 这个包发出去用户连不上:")
                    problems.forEachIndexed { i, p -> appendLine("  ${i + 1}. $p") }
                    append("(只想看界面的话用 :app:assembleDebug, 那条路不受这些限制。)")
                },
            )
        }
        logger.lifecycle(
            "红杏: release 输入检查通过 (签名=${keystorePropsFile?.name}, " +
                "内核=${coreBinaryFile.length() / 1024 / 1024} MB, 规则集=$rulesetCount 个)",
        )
    }
}

// release 的几条入口都挂上这道闸门。用 matching + configureEach 而不是
// named(): 这样即使某个任务改名/不存在也不会让配置阶段就炸掉。
tasks.matching {
    it.name in setOf("assembleRelease", "bundleRelease", "installRelease", "packageRelease")
}.configureEach { dependsOn(verifyReleaseReadiness) }

// ------------------------------------------------------------------ #
// release 硬闸门 (二): 产物里到底有没有东西, 签名对不对
// ------------------------------------------------------------------ #
// 上面查的是"输入在不在", 这里查的是"打包动作真的把它们放进去了吗" ——
// 两者不是一回事 (abiFilters、packaging 的 exclude、asset 过滤都可能让
// 文件在最后一步消失)。签名同样在这里验: **绝不允许一个
// CN=Android Debug 的包从这条流水线上出去**。
val releaseApkDir = layout.buildDirectory.dir("outputs/apk/release")

/** 取 SDK 里版本最高的那个 apksigner.bat; 找不到返回 null。 */
fun findApksigner(): File? =
    File(sdkDirPath, "build-tools").listFiles()
        ?.filter { it.isDirectory }
        ?.sortedByDescending { it.name }
        ?.map { File(it, "apksigner.bat") }
        ?.firstOrNull { it.isFile }

tasks.matching { it.name == "packageRelease" }.configureEach {
    doLast {
        val apk = releaseApkDir.get().asFile
            .listFiles { f -> f.isFile && f.extension == "apk" }
            ?.maxByOrNull { it.lastModified() }
            ?: throw GradleException(
                "packageRelease 跑完了却在 ${releaseApkDir.get().asFile} 里找不到 .apk",
            )

        // ---- 内容 ----
        val requiredEntries = listOf(
            "lib/arm64-v8a/libmihomo.so",
            "lib/arm64-v8a/libhongxing_launcher.so",
            "assets/nodes.yaml",
            "assets/config.template.yaml",
            "assets/geosite.dat",
            "assets/geoip.metadb",
            "assets/country.mmdb",
        )
        val missing = mutableListOf<String>()
        var rulesetInApk = 0
        ZipFile(apk).use { zip ->
            requiredEntries.forEach { name -> if (zip.getEntry(name) == null) missing += name }
            rulesetInApk = zip.entries().asSequence()
                .count { it.name.startsWith("assets/ruleset/") && it.name.endsWith(".yaml") }
        }
        if (missing.isNotEmpty() || rulesetInApk < 10) {
            throw GradleException(
                buildString {
                    appendLine("红杏 release 包内容不全: ${apk.name}")
                    missing.forEach { appendLine("  缺: $it") }
                    if (rulesetInApk < 10) appendLine("  规则集只有 $rulesetInApk 个 (模板引用 20 个)")
                    append("这样的包装上去界面正常、每次连接都失败 —— 不能发。")
                },
            )
        }

        // ---- 签名 ----
        val apksigner = findApksigner()
        if (apksigner == null) {
            throw GradleException(
                "找不到 apksigner ($sdkDirPath/build-tools/*/apksigner.bat), 无法验证 " +
                    "${apk.name} 的签名。正式包**必须**验签名, 所以这里当作失败处理。",
            )
        }
        val proc = ProcessBuilder(
            apksigner.absolutePath, "verify", "--print-certs", apk.absolutePath,
        ).redirectErrorStream(true).apply {
            // apksigner.bat 靠 JAVA_HOME 找 java; 构建用的那个 JDK 肯定能用。
            environment()["JAVA_HOME"] = System.getProperty("java.home")
        }.start()
        val output = proc.inputStream.bufferedReader().use { it.readText() }
        val exit = proc.waitFor()
        if (exit != 0) {
            throw GradleException(
                "apksigner verify 失败 (exit=$exit) —— ${apk.name} 没有可用的签名:\n$output",
            )
        }
        if (output.contains("CN=Android Debug")) {
            throw GradleException(
                "红杏 release 包竟然是用 **Android debug key** 签的 (CN=Android Debug)。\n" +
                    "这种包 Google Play 直接拒收, 而且 debug 私钥是公开的。\n" +
                    "apksigner 输出:\n$output",
            )
        }
        val subject = output.lineSequence().firstOrNull { it.contains("certificate DN") }
            ?.substringAfter(":")?.trim().orEmpty()
        logger.lifecycle("红杏: ${apk.name} 签名校验通过 —— $subject")
    }
}

dependencies {
    implementation(platform("androidx.compose:compose-bom:2024.12.01"))
    implementation("androidx.compose.ui:ui")
    implementation("androidx.compose.ui:ui-graphics")
    implementation("androidx.compose.material3:material3")
    implementation("androidx.compose.material:material-icons-extended")
    implementation("androidx.activity:activity-compose:1.9.3")
    implementation("androidx.lifecycle:lifecycle-runtime-compose:2.8.7")
    implementation("androidx.lifecycle:lifecycle-viewmodel-compose:2.8.7")
    implementation("androidx.core:core-ktx:1.15.0")
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-android:1.9.0")

    // 单元测试。刻意只用 JUnit4 + coroutines-test —— 这个项目的传统是零第三方
    // 依赖, 而这里要测的全是纯逻辑(占位符替换 / 版本比较 / JSON 解析),
    // 引 mockito / robolectric 的收益是负数。
    testImplementation("junit:junit:4.13.2")
    testImplementation("org.jetbrains.kotlinx:kotlinx-coroutines-test:1.9.0")

    // org.json 的**真实实现**, 只进单测。
    //
    // 为什么必须要它: `android.jar` 里的 `org.json.*` 是 stub, 方法体一律是
    // `throw new RuntimeException("Method ... not mocked")`。而 `MihomoApi` 的
    // 解析逻辑 (内核 `/proxies` 是三层嵌套 map) 恰恰是最值得测的一段, 在 JVM
    // 上却一行都跑不了。
    //
    // 不用 `unitTests.isReturnDefaultValues = true` 绕: 那个会把 stub 变成
    // "返回 null/0", 于是 `getJSONObject()` 返回 null, 测试全绿但**什么都没验**——
    // 比不写测试更糟。这里要的是真的能解析。
    //
    // 只影响单测 classpath, 不进 APK; 而且 org.json 是参考实现, 行为与 Android
    // 自带的那个一致。代价是单测 classpath 上多一个 jar, 运行时依赖仍然是零。
    testImplementation("org.json:json:20240303")
}
