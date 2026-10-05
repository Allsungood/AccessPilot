# 更新日志

本文件记录**面向使用者**的变化。格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

> ⚠️ **这份 1.0.0 条目是发布前草稿。** 标了 `TODO(lead)` 的地方需要发布负责人
> 核对后再删掉标记：有的是"已经改了但还没重新构建产物"，有的是"只在开发机上实测过"。
> 写日志的原则和写代码一样 —— 不写还没发生的事。

---

## [1.0.0] — 未发布

第一个正式版。桌面端（Windows，Python 3.11，**运行时零第三方依赖**）、
命令行、系统托盘、本地图形控制台，外加安卓端（Kotlin + Compose）。

### 新增

* **红杏桌面客户端**：一个大圆钮的 Tkinter 界面（已连接 / 未连接）、节点列表
  （含"是否实测能上 ChatGPT"）、10 个平台的真实连通性体检、系统托盘、
  单实例互斥、per-monitor-v2 DPI 感知。
* **轻量安装程序 `红杏-Setup-v1.0.0.exe`**（本版新增的发布形态）：图形向导 /
  `--silent` / `--dir` / `--uninstall` / `--uninstall --purge` / `--update`，
  用户级安装（不需要管理员、不弹 UAC），自动创建开始菜单与桌面快捷方式，
  并注册到「设置 → 应用」。
* **完整安装包 `红杏-Setup-v1.0.0-full.exe`**：把 mihomo 内核、TUN 驱动、
  GeoIP/GeoSite 数据与节点档一起打进包里，用户装完**不用再跑 `init`**。
* **安卓端**（`android/`）：`VpnService` 建 TUN → 把文件描述符交给随包的 mihomo
  子进程；因为 `ProcessBuilder` 在 execve 前会无条件关闭 fd，另写了一个
  `fork + execve` 的 native 启动桥。
* **公开免费节点一条龙**（`accesspilot free auto`）：抓取 → 并发测速 →
  **平台级验证** → 只留真正可用的节点。
* **免节点直连加速**（`accesspilot accel`）：本地 SOCKS5 + 真实 IP 优选，
  面向 GitHub 系域名；有明确边界（只对"IP 段被丢弃"这一类封锁有效）。
* **Cloudflare WARP**（`accesspilot warp`）：零成本注册出口，纯标准库实现
  RFC 7748 的 X25519，不为一个功能引入第三方密码学依赖。
* **本地图形控制台**（`accesspilot dashboard`）：带 CSRF 防护（自定义头 +
  Host/Origin 校验），只监听 `127.0.0.1`。

### 安全

* **安装包不再携带任何真实凭据。** 打包时会把 `state.json` 里的 `api_secret`
  清空并断言真的清空了，安装器在**目标机器上**重新随机生成；
  `last_start` 这类只属于打包机的字段也不再随包分发。
  （此前发布出去的完整版安装包里冻着维护者本机的 external-controller 密钥。）
* **自动更新只认白名单文件名，且必须校验 SHA256。** 更新器显式排除
  `Setup` / `installer` / `安装` 类资产；发布方没提供校验值（GitHub 附件 digest、
  发布说明里的哈希、或 `SHA256SUMS.txt`）时**默认拒绝更新**；
  校验不过绝不动用户正在用的主程序。
  （此前"找不到就挑第一个 .exe"的兜底逻辑会把用户的主程序换成安装器。）
* 系统代理的三道防护：**自愈**（任意命令调用时检测"内核已死但代理还开着"并还原）、
  **看门狗**（内核消失即还原）、**不擅自改动**（自动化命令一律不碰系统代理）。
* 卸载/更新前先让红杏自己收尾（`红杏.exe stop`），避免留下指向死端口的系统代理
  ——那会让整台机器上不了网。
* Android 启动桥做了 16 KB 页对齐（16 KB 页设备上不做的话连接必然失败）。

### 修复

* `free auto` 擅自在测速期间开启系统代理，内核退出后导致整台机器断网 → 改为显式
  不触碰系统代理，并补上自愈与看门狗。
* 内核对外的 `/group/{name}/delay` 返回扁平整数格式，代码只认对象格式 →
  369 个节点的测速结果被全部丢弃，表现为"节点全挂了"。
* Loyalsoldier 的 `.txt` 规则集实际内容是 Clash YAML payload，误标 `format: text`
  会让内核**静默丢弃整份规则集** → 修正并加防回归测试。
* 一颗带 `auth_aes128_md5` 的 SSR 节点让 1242 个节点的整份配置热重载失败 →
  抓取时过滤内核不支持的参数；`reload_config` 改为先本地校验、失败绝不提交。
* 计划任务保活从来没成功过（相对路径 + 电源条件 + 检出时被换成 LF 换行）。
* 窗口化 exe 的 `sys.stdout` 是 `None`，`accesspilot/util.py` 在 import 阶段就
  `AttributeError` → 由 `packaging/entry.py` 三级退路补齐（继承句柄 / AttachConsole /
  日志文件）。
* 高分屏糊字、窗口启动后被最小化、托盘线程导致的 `Fatal Python error` 崩溃。
* 完整记录见 `README.md` 的「修复记录」一节（21 条，都带实测现象）。

### 打包与发布

* **版本号只有一个来源**：`accesspilot/__init__.py` 的 `__version__`。
  `pyproject.toml` 改为动态版本，两个 spec 和两个构建脚本都从它派生。
* `packaging/build_installer.py` 新增**版本闸**：`--version` 与包内版本不一致时
  拒绝构建（除非显式 `--force-version`），并会核对主程序 exe 的 PE `FileVersion`。
  （此前发出去的安装包叫 `v0.9.0`，而程序本体早就是 `1.0.0`。）
* `packaging/installer.spec` 补上版本资源与图标 —— 用户下载的第一个文件
  「属性 → 详细信息」里不再是一片空白。
* `packaging/build.py` 额外产出发布资产名 `dist\红杏-v<版本>-win64.exe`，
  让"上传哪个文件"和"更新器找哪个文件"是同一个字符串。
* 卸载时留在 `%TEMP%` 的临时卸载器副本现在会自己删掉
  （此前每次卸载都留一份 24–56 MB 的 exe）。

### 文档

* `packaging/README.md` 新增 §11「安装程序」与 §12「发布 v1.0.0 的完整顺序」：
  怎么构建、有哪些参数、装到哪、为什么程序与数据分开、更新为什么必须校验哈希、
  以及卸载器体积的取舍为什么值得。
* `README.md` 新增「不想装 Python？直接下安装包」一节。
* 修掉 README 里那条会把人带沟里的 `pyinstaller -F accesspilot\cli.py` 一行命令
  （它绕开 `packaging/entry.py`，产物要么 import 阶段就崩，要么缺 `dashboard.html`
  和托盘图标）。
* 修正过时的测试数量与产物 SHA256，并给这类"实测快照"加上"会过期、怎么复现"的说明。
* `scripts\install.ps1` 真正创建桌面快捷方式了（此前只写在注释里）。

### 已知限制（发布前请确认已经写进发布说明）

* **exe 没有代码签名**，首次运行会弹 SmartScreen「未知发布者」，需要点
  「更多信息 → 仍要运行」。消除它需要 OV/EV 证书，不是技术问题。
* **安卓端 `release` 构建目前用 debug 签名**，且仓库里没有 Gradle wrapper。
  TODO(lead): 决定 v1.0.0 是否随附 APK；若不随附，请在发布说明里写明。
* **没有在真正的裸机（从未装过 Python 的干净 Windows）上双击验证过**；
  现有的证据是"清空 `PYTHON*`、把 `PATH` 缩到 `C:\Windows\system32` 后依然通过
  全部自检"这一等价性更强的近似。
* TODO(lead): 发布前重跑 `python runtests.py` / `packaging/smoke_test.py` /
  两个安装包的构建，把**这一次**的体积、SHA256 和测试数写进发布说明。

---

## 版本号从哪来

`accesspilot/__init__.py` 的 `__version__` 是**唯一**来源：
`pyproject.toml`（动态版本）、`packaging/hongxing.spec`、`packaging/installer.spec`、
`packaging/build.py`、`packaging/build_installer.py` 全部从它派生或与它核对。
改版本号只改那一个地方。

[1.0.0]: https://github.com/Allsungood/AccessPilot/releases/tag/v1.0.0
