// 红杏 Android · app 模块
import java.io.File

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

    buildTypes {
        release {
            // v1 不混淆: 这个 app 大量依赖反射之外的 JNI/进程边界, 混淆收益小、
            // 排错成本高。等稳定了再开。
            isMinifyEnabled = false
            // 用 debug 签名, 方便直接装到手机上试 —— 正式发布时再换成自己的 keystore。
            signingConfig = signingConfigs.getByName("debug")
        }
        debug {
            applicationIdSuffix = ".debug"
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions { jvmTarget = "17" }

    buildFeatures { compose = true }

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
    // NDK 与 native 启动器: **找不到 NDK 时必须优雅降级, 不能把整个构建卡死**
    // ------------------------------------------------------------------ #
    // 教训(2026-10-01): 我先写成 `ndkPath = "D:/Android/android-ndk-r27c"` 写死,
    // 而那个目录当时还不存在(NDK 还在下载)。结果是 **配置期** 就报
    //   [CXX1101] Location specified by android.ndkPath did not contain a valid NDK
    // 于是连 `:app:compileDebugKotlin` 都跑不了 —— 界面那边的 teammate 一行都
    // 编不过, 我自己却因为是在改动之前跑的构建而没发现。
    //
    // 所以这里改成: 依次找 ANDROID_NDK_ROOT / 官方 zip 解压目录 / SDK 标准目录,
    // **找到才启用 native 构建**, 找不到就只警告 —— 界面层照常能编。
    // 但要警告得很响: 没有 launcher 时 APK 装上去也起不了内核(拿不到 TUN fd)。
    val ndkCandidates = listOfNotNull(
        System.getenv("ANDROID_NDK_ROOT")?.takeIf { it.isNotBlank() },
        "D:/Android/android-ndk-r27c",
        "$sdkDirPath/ndk/27.2.12479018",
    )
    val ndkRoot = ndkCandidates.firstOrNull { File(it).isDirectory }
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
            "        装 NDK:  sdkmanager --sdk_root=D:\\Android\\Sdk \"ndk;27.2.12479018\"\n" +
            "        或设环境变量 ANDROID_NDK_ROOT 指向已解压的 NDK。"
        )
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
