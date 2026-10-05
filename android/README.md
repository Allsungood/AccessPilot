# 红杏 · 安卓端

Kotlin + Jetpack Compose 的 Android VPN 客户端。和桌面端共用同一套节点、
同一份 mihomo 内核，界面也刻意做成一样的一键开关。

```
VpnService 建 TUN → 拿到文件描述符(fd)
    → 启动随包的 mihomo(arm64 原生二进制) 子进程
    → 配置里写 tun.file-descriptor: <fd>
    → mihomo 负责全部转发与分流
```

| 项 | 值 |
|---|---|
| minSdk / targetSdk | 26 (Android 8.0) / 35 |
| applicationId | `com.accesspilot.hongxing`（debug 加 `.debug` 后缀，可与正式版共存） |
| ABI | 目前只打包 `arm64-v8a` |
| 界面 | Compose + Material 3，无任何第三方 UI 库 |
| 测试 | 66 个单元测试（AssetInstaller 16 / ConfigBuilder 26 / MihomoApi 24） |

---

## 构建

### 0. 前置

* JDK 17（`JAVA_HOME`）
* Android SDK（`ANDROID_HOME`），需 `platforms;android-35`、`build-tools;34.0.0`
* NDK **27.2.12479018** —— 版本必须和 `app/build.gradle.kts` 里的 `ndkVersion`
  **完全一致**，否则 AGP 直接报 `[CXX1100]`。这个值同时被 `ndkPath` 用，
  两处对不上会在配置阶段就失败。

### 1. 先补资源（**必做，不能跳**）

内核二进制和随包资源都是**可再生成的**，所以没进仓库（mihomo 单个 61 MB，
GitHub 单文件超过 50 MB 就告警）：

```bash
python android/tools/fetch_core.py      # 下载 mihomo → jniLibs/arm64-v8a/libmihomo.so
python android/tools/build_assets.py    # 从桌面端配置生成 assets/
```

**这两个目录不能长期缺失**：缺 geodata 时 mihomo 直接 exit 1，缺
`libmihomo.so` 时 APK 里根本没有内核 —— 而这两种情况下 APK **照样能构建成功**，
装到手机上才报错。构建前务必先跑这两条。

`build_assets.py` 的 `nodes.yaml` 和 `config.template.yaml` 出自**同一份
proxies**：两边各自生成过一次，结果模板里的策略组引用了 `nodes.yaml` 里没有的
出口，内核 fatal 退出（`proxy group[1]: ♻️ 自动选择: '☁️ WARP' not found`）。

### 2. 构建

**debug**（界面/自测用）：

```bash
cd android
./gradlew assembleDebug             # 产物 app/build/outputs/apk/debug/app-debug.apk
```

Gradle 版本由 wrapper 固定（8.11.1）。第一次跑 `./gradlew` 会去
`services.gradle.org` 下载那份发行包（约 130 MB）；不想下载、或者机器上已经装了
Gradle 8.11.1 的话，用绝对路径那条即可（**这也是这台开发机上实际在用的方式**，
仓库过去没有 wrapper，README 里的 `./gradlew` 谁都跑不起来 —— 审计 N12）：

```bash
D:\Android\gradle-8.11.1\bin\gradle.bat -p C:\Users\Administrator\AccessPilot\android :app:assembleDebug
```

**release**（要发出去的包）需要一份签名凭据，缺了它**构建会直接失败**：

```bash
D:\Android\gradle-8.11.1\bin\gradle.bat -p C:\Users\Administrator\AccessPilot\android :app:assembleRelease
```

凭据的四个查找位置（命令行 > 环境变量 > 仓库内 > 用户主目录）：

| 来源 | 说明 |
|---|---|
| `-Phongxing.keystoreProperties=<路径>` | 命令行显式指定 |
| `$env:HONGXING_KEYSTORE_PROPERTIES` | 环境变量 |
| `android/keystore.properties` | 仓库内（已被 `.gitignore`），方便 CI |
| `%USERPROFILE%\hongxing-keystore.properties` | 本机在用的位置 |

properties 里要四项：`storeFile` / `storePassword` / `keyAlias` / `keyPassword`。
本机的 keystore、密码和备份说明在 `C:\Users\Administrator\hongxing-keystore-README.txt`
—— **那两个文件丢了就再也发不了更新**，务必异地备份。

release 构建有两道闸门，都是必须过的（过去只有 `logger.warn`，于是"没有内核 /
没有 launcher / 没有资源"的 APK 能被当成正式包发出去 —— 审计 N15）：

1. `verifyReleaseReadiness`：查签名凭据、NDK、`jniLibs/arm64-v8a/libmihomo.so`、
   `assets/nodes.yaml`、`assets/ruleset/*` 的数量；
2. `packageRelease` 之后：把打好的 APK 当 zip 打开，确认内核、启动桥、geodata、
   规则集**真的在里面**，再用 SDK 的 `apksigner verify --print-certs` 验签名，
   见到 `CN=Android Debug` 直接失败（审计 N1）。

手工复核签名：

```bash
D:\Android\Sdk\build-tools\35.0.0\apksigner.bat verify --print-certs ^
    app\build\outputs\apk\release\app-release.apk
# 期望: Signer #1 certificate DN: CN=hongxing, OU=AccessPilot Android, ...
```

已知坑：**不要并发跑多个 Gradle 调用**。并发的增量构建会产生假错误
（`values-es-rUS` 找不到、`graph.bin` 缺失、几百条 `Unresolved reference`），
看起来像代码坏了。要重来就串行跑，或用 `-Pkotlin.incremental=false`。

---

## 最难的一处：子进程怎么拿到 TUN 的 fd

这是整个方案唯一的真风险，所以先验证再写代码。

**`ProcessBuilder` 拿不到。** libcore 的 `UNIXProcess_md_forkAndExec` JNI 签名是
`([B[BI[BI[B[IZ)I`，只收 `std_fds[0..2]`；execve 前 `closeDescriptors()` 遍历
`/proc/self/fd`，`from_fd = FAIL_FILENO + 1 = 4`，**无条件 close**。

这里有个很容易踩的误解：它是**直接 close，不是靠 `FD_CLOEXEC` 生效**的，
所以 `ParcelFileDescriptor.detachFd()` 清 CLOEXEC 那一招**没有用**。
`android.system.Os` 也没有公开的 `fork()` / `posix_spawn()`（用 API 35 的
android.jar 逐个方法核过）。

于是自己写一个：`app/src/main/cpp/hongxing_launcher.c`，
`fork` + `execve` + `setsid`，另有 `reapExited(pid)` 用 `waitpid(WNOHANG)` ——
因为 `java.lang.ProcessHandle` 是 Java 9 API（安卓没有），SDK 也没暴露 `waitpid`。

### 真机实测（华为 BON-AL00 / Android 12 / arm64-v8a）

```
A ProcessBuilder : fd-exists=no                              ← 反证
B NativeLauncher : fd-exists=yes 且子进程读到 HONGXING-FD-OK  ← 两条都满足
C 真 TUN fd      : /proc/self/fd/140 -> /dev/tun             ← 真的是字符设备
```

A 那条是**同机、同命令、只差启动方式**的反证 —— 有它才能排除"其实本来就拿得到"。

之后引擎链路全通：

```
tun0: <POINTOPOINT,UP,LOWER_UP> mtu 8500                  与配置一致
files/hongxing.yaml: file-descriptor: 155                 fd 注入正确
mihomo.log: RESTful API listening at: 127.0.0.1:9090
run-as curl /version → 200  {"meta":true,"version":"v1.19.32"}
mihomo.log: [TCP] mihomo --> 8.8.8.8:443 match Match using 🐟 兜底分流
```

真正的上网失败是**节点本身**（`XTLS Vision server responded unknown UUID` /
`409 Conflict`），不是引擎。

---

## 排查这个 App 时要知道的两件事

### 1. 这台华为 ROM 上，应用自己的 logcat 完全不可见

`adb logcat -d -b all | grep -i hongxing` 只有系统侧的 `wm_on_create_called`
之类，应用自己的 `Log.i` **一条都没有**。

所以：**"logcat 里没有我们的 tag" 不能作为"代码没执行"的证据** —— 照这个判断
会把"跑过了"当成"没跑"。

绕过办法是文件版诊断，每次 `start()` 失败时自动落地：

```bash
adb shell run-as com.accesspilot.hongxing.debug cat files/selfcheck.txt
```

里面有启动桥状态、内核 pid/存活、阶段、授权、错误原文、节点数、工作目录实际
内容、以及就绪探测的原始错误。

### 2. `launchMode="singleTask"` 时 `am start` 不走 `onCreate`

第二次 `am start` 会进 `onNewIntent`。调试用的 `--ez fdprobe true` /
`--ez connect true` 两个入口在 `onCreate` 和 `onNewIntent` **都**接了，
否则只有第一次能触发，看起来像探针坏了。

**这两个 extra 只在 debug 包里有效**（审计 K4）。它们是 `exported="true"` 的
launcher Activity 上的入口，任何 App 或 adb 都能塞进来：`connect` 会替用户把
VPN 拉起来，`fdprobe` 会建 TUN 并经启动桥跑 `/system/bin/sh`。所以正式包里
它们被 `BuildConfig.DEBUG_ENTRYPOINTS`（构建类型写死的编译期常量，release =
false）挡死了 —— 服务侧的 `ACTION_FD_PROBE` 同样挡了一道。

```bash
adb shell am start -n com.accesspilot.hongxing.debug/com.accesspilot.hongxing.MainActivity --ez connect true
```

（包名带 `.debug` 后缀 —— 正式包的 `com.accesspilot.hongxing` 上这两条命令
什么都不会发生，这是**设计如此**，不是坏了。）

---

## 已知限制

* **只打包 `arm64-v8a`**。32 位设备和 x86 模拟器装不上（内核只下了 arm64）。
* **`READY_TIMEOUT_MS` = 60s、`CONNECT_TIMEOUT_MS` = 90s**，比直觉长得多。
  冷启动要载 geodata + 20 个规则集 + 6000 个节点，20 秒时内核仍存活但 9090
  还没绑上。这两个值必须保持 `CONNECT > READY` 的明显差距，否则会在内核
  正常慢启动时把它杀掉。等待期间每 3 秒把已等待秒数写进 `status.message` ——
  光调大超时只会换来"转圈一分钟不动"。
* **未在真机复验的一项**：上面那组超时调整 + `network_security_config.xml`
  （放开 `127.0.0.1` 的明文，安卓从 9.0 起默认禁明文，**loopback 并不自动豁免**）
  是在设备掉线之后才进的 APK。复验步骤：

  ```bash
  adb install -r android/app/build/outputs/apk/debug/app-debug.apk
  adb shell am force-stop com.accesspilot.hongxing.debug
  adb shell am start -n com.accesspilot.hongxing.debug/com.accesspilot.hongxing.MainActivity --ez connect true
  # 等 60~90 秒
  adb shell "ps -A | grep mihomo; ip addr show tun0 | head -2"
  adb shell run-as com.accesspilot.hongxing.debug cat files/selfcheck.txt
  ```

---

## 只能上真机才能确认的四件事（v1.0.0 之前必须跑一遍）

代码这一侧已经改到位，但下面每一条的**结论都依赖设备行为**，静态检查给不出答案。
用 debug 包（`--ez connect true` / 界面上的大开关）跑：

1. **断开之后 TUN 真的没了**（对应 K1/fd 所有权）
   ```bash
   # 连上 -> 点"断开"
   adb shell ip addr show tun0            # 期望: 报 "does not exist"
   adb shell "ls -l /proc/$(pidof com.accesspilot.hongxing.debug)/fd | grep -c tun"   # 期望: 0
   ```
   改之前这里是"接口还在、没人读"—— 表现是断开之后**整台设备上不了网**。

2. **第一次点开关不会马上弹"连接失败"**（对应 K2/每次尝试一个 id）
   冷启动 App，点一次大开关：红色横幅**不应该**出现；按钮应该走
   正在连接… → 已连接。

3. **没有孤儿内核**（对应 K3/N3/N4）
   ```bash
   adb shell am force-stop com.accesspilot.hongxing.debug   # 连接状态下强杀
   adb shell ps -A | grep libmihomo                          # 期望: 没有残留
   # 再启动 App 并连接, 然后:
   adb shell run-as com.accesspilot.hongxing.debug cat files/mihomo.pid
   adb shell ps -A | grep libmihomo                          # 期望: 只有一个, 且 pid 对得上
   ```

4. **DNS 不再监听 0.0.0.0**（对应 N8）
   连接后从同一 Wi-Fi 下的另一台机器：
   ```bash
   dig @<手机IP> -p 1053 example.com     # 期望: 超时/拒绝, 而不是给出答案
   ```
