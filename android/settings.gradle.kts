// 红杏 Android · 工程设置
//
// 为什么把仓库配在 settings 里而不是根 build.gradle:
// Gradle 7 之后 pluginManagement 必须在这里声明, 否则 AGP 的版本解析会走
// 默认仓库并在国内网络下超时。这里显式给出阿里云镜像 + 官方源双保险。
pluginManagement {
    repositories {
        maven("https://maven.aliyun.com/repository/gradle-plugin")
        maven("https://maven.aliyun.com/repository/google")
        maven("https://maven.aliyun.com/repository/public")
        google {
            content {
                includeGroupByRegex("com\\.android.*")
                includeGroupByRegex("com\\.google.*")
                includeGroupByRegex("androidx.*")
            }
        }
        mavenCentral()
        gradlePluginPortal()
    }
}

dependencyResolutionManagement {
    repositoriesMode.set(RepositoriesMode.FAIL_ON_PROJECT_REPOS)
    repositories {
        maven("https://maven.aliyun.com/repository/google")
        maven("https://maven.aliyun.com/repository/public")
        google()
        mavenCentral()
    }
}

rootProject.name = "红杏"
include(":app")
