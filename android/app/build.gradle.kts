// 红杏 Android · app 模块
plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("org.jetbrains.kotlin.plugin.compose")
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
}
