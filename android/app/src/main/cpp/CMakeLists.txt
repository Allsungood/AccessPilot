# 红杏 Android · 内核启动桥的构建脚本
#
# 为什么这里需要一个独立的 native 库, 而不是用 Java 的 ProcessBuilder:
# 见 hongxing_launcher.c 文件头那段 —— Java 的 UNIXProcess 在 exec 前会把
# 编号 >= 4 的 fd 全部关掉, 而 VpnService 给的 TUN fd 必然 >= 4。
#
# 这个库**不是**第三方依赖: 源码就在同目录, 是本项目自己的一部分。
# 它只做一件事 —— fork + execve, 连 liblog 都不链 (日志由子进程自己重定向)。

cmake_minimum_required(VERSION 3.22.1)
project(hongxing_launcher C)

add_library(hongxing_launcher SHARED hongxing_launcher.c)

# 只编 arm64-v8a: 与 abiFilters 一致 (mihomo 二进制也只有 arm64 一份)。
# 这里显式写出来而不是依赖 ANDROID_ABI 推断, 是为了让"这个库只可能在
# arm64 上被加载"这件事在构建脚本里一眼可见。
set_target_properties(hongxing_launcher PROPERTIES
        C_STANDARD 11
        C_STANDARD_REQUIRED ON)

# -Wall -Wextra: 这个文件里全是 fd / errno / fork 这些容易写错的东西,
# 让编译器帮忙盯一眼。O2 是因为它只有几百行, 编大点无所谓。
target_compile_options(hongxing_launcher PRIVATE -Wall -Wextra -O2)

# 只依赖 libc。**刻意不链 android/log**: 子进程的日志走文件重定向,
# 父进程这一侧没有任何需要打日志的地方 (出错是抛 Java 异常)。
# 依赖越少, 这个库在 ABI 层面出问题的机会就越少。
target_link_libraries(hongxing_launcher PRIVATE log)
