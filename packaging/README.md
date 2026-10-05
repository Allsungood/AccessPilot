# 红杏 桌面客户端 —— 单文件 exe 打包说明

> 目标: **给不懂技术的用户一个 `红杏.exe`，双击就能用。用户机器上不装 Python，
> 不装任何第三方包，不需要命令行。**

本文档写给要重新构建 exe 的人（维护者）。普通用户不需要看这些。

本仓库的"打包"其实有**两条产物链**，别混：

| 产物链 | 产出 | 给谁 | 章节 |
|---|---|---|---|
| **主程序** | `dist\红杏.exe`（单文件，约 13 MiB） | 已经会用命令行 / 想手动下载的用户 | §1–§9 |
| **安装程序** | `dist\红杏-Setup-v<版本>.exe` 与 `-full.exe`（24 / 56 MB） | 绝大多数 Windows 用户：双击装、有快捷方式、能在"设置 → 应用"里卸载 | §4.1、§11 |

---

## 0. 一句话

```powershell
python packaging/build.py          # 构建 -> dist\红杏.exe
python packaging/smoke_test.py     # 验证: 真的把 exe 跑起来逐项核对
python packaging/build_installer.py --full   # 再包一层 -> dist\红杏-Setup-v<版本>-full.exe
```

产物是**单个文件** `dist\红杏.exe`（约 12–13 MiB），拷到任何 64 位 Windows 10/11
上双击即可运行。

---

## 0.1 实测结果快照（可复现的命令都在下面）

> ⚠️ **这张表会过期，别把它当永久事实。** 下面每行都写了"这一行是怎么得到的"，
> 发布前请自己重跑一遍再抄数字 —— 本项目已经栽过一次：README 里同时流通着
> 308 / 219 / 380 三个测试数，以及一个和 `dist\` 里文件对不上的 SHA256。

| 项 | 结果 | 怎么得到的 |
|---|---|---|
| 构建机 | Windows 10 10.0.19045 x64 / Python 3.11.9 / PyInstaller 6.22.3 | `python -VV`、`python -m PyInstaller --version` |
| 发布版产物 | `dist\红杏.exe` | `python packaging/build.py` |
| 体积 | **13,015,519 字节**（12.41 MiB） | `(Get-Item dist\红杏.exe).Length` |
| SHA256 | `6A1761579FCD66CAE257F4E9E6DFEAE0E43624E6E842731C7FF5643B051109E6` | `(Get-FileHash dist\红杏.exe -Algorithm SHA256).Hash` |
| PE 子系统 | `IMAGE_SUBSYSTEM_WINDOWS_GUI`(2) → 双击**不弹黑框** | `packaging/smoke_test.py` 第 7 项 |
| PE 版本资源 | `FileVersion=1.0.0 ProductVersion=1.0.0 ProductName=红杏` | `(Get-Item dist\红杏.exe).VersionInfo` |
| 排错版产物 | `dist\红杏-debug.exe`，12,976,802 字节，子系统 `WINDOWS_CUI`(3) → 有控制台 | `python packaging/build.py --debug` |
| 冒烟测试 | `python packaging/smoke_test.py` → **7/7 全部通过** | 同左 |
| 源码测试 | `python runtests.py` → 421 项（判定标准是退出码 0 与 `OK`；满载时的偶发超时见下面的说明） | 同左 |
| 测试文件 | `tests/test_*.py` 共 25 个 | `(Get-ChildItem tests\test_*.py).Count` |
| GUI 实测 | 双击等价方式启动后，窗口正常出现：标题 `红杏 · 一键通行`，主界面渲染完整 | 人工双击 `dist\红杏.exe` |

> **测试计数别当常数用。** `tests/` 一直在长（写这份文档期间就从 380 涨到 421），
> 判定标准是 `runtests.py` 的退出码和 `OK` / `FAILED`。
> 另外如实记一条观察：本机内存被挤到只剩约 1 GB（同时有 gradle 构建和另一个
> `红杏.exe` 在跑）时，`tests/test_installcmd.py` 那个"从别的目录调用包装器"的
> 用例会因为子进程 90 秒超时而 `TimeoutExpired`；同一台机器上空载单独跑它
> 0.47 秒通过，把含它的 245 个用例一起跑也是 `OK`。机器不忙时全套是绿的。

> SHA256 / 体积是**某一次构建**的指纹，源码改一行它就变。它唯一的用途是让
> "我下载到的这个文件和文档里说的是不是同一个"可被验证 —— 所以发布时请把
> **这次构建**的输出（`packaging/build.py` 结尾会打印体积与 SHA256）原样抄进
> 发布说明，而不是抄这张表。

复现命令：

```powershell
python runtests.py                   # -> OK(计数随 tests/ 增长, 看退出码)
python packaging/build.py            # -> dist\红杏.exe(结尾打印体积 + SHA256)
python packaging/smoke_test.py       # -> 7/7
python packaging/build_installer.py --full   # -> dist\红杏-Setup-v<版本>-full.exe
```

---

## 1. 为什么"运行时零依赖"和"打包期用 PyInstaller"不矛盾

这是本项目最容易被误解的一点，先把话说清楚。

**项目原则**（见 `accesspilot/__init__.py`）：`accesspilot` 包在**运行时**只用
Python 标准库。目标用户在中国大陆，很多机器连 PyPI 都打不开，让用户"先装个
Python，再 pip install 一堆东西"等于劝退；而且每多一个第三方依赖，就多一份
供应链风险和"装不上"的客服成本。

**PyInstaller 不违反这条原则**，因为二者的适用范围完全不同：

| | 运行时（用户机器） | 打包期（维护者机器） |
|---|---|---|
| 谁在跑 | 普通用户双击 exe | 维护者执行 `python packaging/build.py` |
| 需要 Python | **不需要**（解释器被塞进 exe 里了） | 需要（3.9+） |
| 需要 PyInstaller | **不需要** | 需要 |
| 需要联网 | 只有下载内核/订阅时才需要 | 只有第一次装 PyInstaller 时需要 |
| 依赖清单 | 零（标准库） | PyInstaller 及其几个小依赖 |

判断标准很直接：**`accesspilot/` 里任何一行 `import` 都不许出现 PyInstaller 或
其它第三方包**。PyInstaller 只做一件事——把 CPython 解释器、标准库和
`accesspilot` 的字节码打成一个自解压 exe，它自己不会进入 `accesspilot` 的
import 图，也不会出现在用户机器上。

打个比方：PyInstaller 是印刷机，不是书里的内容。读者拿到的是书，不需要家里
有一台印刷机。

**什么情况才算破坏这条原则**：如果哪天为了打包而在 exe 里**随包分发**一个
运行期必需的第三方库（例如用 Pillow 处理图标、用 pywin32 做托盘），那才是真的
破例，需要单独讨论。目前没有这种情况——托盘是纯 `ctypes` 调 Win32，图标是纯
`struct`+`zlib` 手写 ICO（见 `accesspilot/gui/icon.py`），连 Pillow 都不需要。

---

## 2. 构建机环境要求

| 项 | 要求 | 本次实测 |
|---|---|---|
| 操作系统 | Windows 10/11 **64 位** | Windows 10 10.0.19045 |
| Python | 3.9+ | 3.11.9 (`E:\Python311`) |
| PyInstaller | 6.x | 6.22.3（hooks-contrib 2026.8） |
| 磁盘 | `build\` 约 40 MB + `dist\` 约 13 MB | — |
| 其它 | **无**。不需要 UPX、不需要 Visual Studio、不需要 NSIS/Inno Setup | — |

装 PyInstaller：

```powershell
python -m pip install pyinstaller
# 国内直连 pypi.org 经常超时（本次实测就是这样），换清华源：
python -m pip install -i https://pypi.tuna.tsinghua.edu.cn/simple pyinstaller
```

> 实测记录：本机 `pip install pyinstaller` 直连 pypi.org 全部 `ReadTimeoutError`，
> 换 `pypi.tuna.tsinghua.edu.cn` 后 7.6 MB/s，一次装好
> （pyinstaller 6.22.3 + altgraph + pefile + pyinstaller-hooks-contrib + pywin32-ctypes）。

---

## 3. 怎么构建

```powershell
python packaging/build.py              # 发布版: dist\红杏.exe（无控制台，双击即用）
python packaging/build.py --debug      # 排错版: dist\红杏-debug.exe（带控制台，能看到日志）
python packaging/build.py --clean      # 先清 build\ 缓存（换了 PyInstaller 版本/依赖后用）
python packaging/build.py --no-icon    # 不尝试生成图标
```

脚本会依次做四件事：

1. **检查 PyInstaller 在不在** —— 不在就打印可直接照抄的安装命令（含镜像地址），
   而不是丢一个 `ModuleNotFoundError` 让人猜。
2. **尽量生成图标** —— 调 `accesspilot/gui/icon.py` 的 `ensure_ico()`（幂等），
   产出 `accesspilot/gui/assets/hongxing.ico` 和 `hongxing_off.ico`。
   拿不到就跳过，不影响构建。
3. **跑 PyInstaller** —— 执行 `packaging/hongxing.spec`，实时转发构建日志，
   完整日志另存到 `build/pyinstaller.log`。
4. **发布** —— 把 `dist\hongxing.exe` 拷成 `dist\红杏.exe`，打印
   **绝对路径 / 字节数 / SHA256**。

构建失败时不会只给一个非零退出码，而是打印日志尾部 + 常见原因
（缺模块 / 资源缺失 / 文件被占用 / 杀软锁目录）。

### 产物

```
dist\红杏.exe                   <- 给用户的最终产物（单文件，13,015,519 字节）
dist\红杏-v<版本>-win64.exe     <- 同一份二进制的**发布资产名**，见 §4.2
dist\红杏-debug.exe             <- 只有带 --debug 构建时才有（12,976,802 字节, 带控制台）
dist\hongxing.exe               <- PyInstaller 的原始产物, 与 红杏.exe 是同一份二进制
build\                          <- 中间缓存(约 28 MB), 可随时删除；build\pyinstaller.log 里有完整日志
```

`dist/` 和 `build/` 都在 `.gitignore` 里，不会进版本库。

> `红杏-v<版本>-win64.exe` 不是"多打一份好看"：自动更新器
> （`packaging/installer.py` 的 `pick_asset`）只认白名单里的文件名，而 GitHub
> Release 附件的名字就是本地文件名。**发布时必须把它一起上传**，否则用户点
> 「检查更新」时更新器找不到主程序，会直接报错退出（这是刻意的：宁可不更新，
> 也不能猜错 —— 见 §11.3）。

### `--debug` 排错版

同一个 spec，只是 `console=True`，产物叫 `红杏-debug.exe`。用途：

* 双击运行时能看到控制台里的报错；
* 运行 `红杏-debug.exe doctor`、`红杏-debug.exe status` 这类子命令时输出最直观。

发布版（`console=False`）也有终端输出能力，见 [§5.1](#51-窗口化构建的-stdout-是-none)。

---

## 4. 打包链路里都有什么

### 4.1 主程序链（§1–§9 讲的就是它）

| 文件 | 职责 |
|---|---|
| `packaging/entry.py` | **冻结入口点**。只属于打包链路，不属于 `accesspilot` 运行时包。它替运行时挡掉三件"只有冻结后才会发生"的事，见 §6。 |
| `packaging/hongxing.spec` | PyInstaller 规格：onefile、资源收集、hiddenimports、图标、版本资源、excludes。 |
| `packaging/build.py` | 一键构建：前置检查 → 生成图标 → 调 PyInstaller → 拷成中文名 → 报告体积/哈希。 |
| `packaging/smoke_test.py` | 产物冒烟测试：**真的运行 exe**，逐项核对（7 项，见 §7.1）。 |
| `packaging/README.md` | 本文档。 |

### 4.2 安装程序链（Setup.exe，见 §11）

| 文件 | 职责 |
|---|---|
| `packaging/installer.py` | **安装器的全部逻辑**（安装 / 卸载 / 更新 / 图形向导）。同一份代码有两种跑法：源码直跑（调试用）和冻结成 `红杏-Setup-*.exe`。 |
| `packaging/installer.spec` | 安装器的 PyInstaller 规格：把 `build/installer-payload/` 整个塞进 `_MEIPASS/payload/`，带版本资源与图标。 |
| `packaging/build_installer.py` | 一键构建安装器：准备 payload（含**凭据剥离**与版本闸）→ 调 PyInstaller → 拷成 `dist\红杏-Setup-v<版本>[-full].exe` 并打印 SHA256。 |

**这些文件都不会被打进运行时依赖**：`accesspilot/` 目录里没有任何一行代码
引用 `packaging/`（安装器反过来会调用 `红杏.exe stop`，那是**子进程调用**，
不是 import）。

---

## 5. 资源收集：收了什么、为什么

### 5.1 为什么这一步最容易出事

PyInstaller onefile 会把所有数据文件解压到运行时临时目录，并把该目录暴露为
`sys._MEIPASS`。源码里那些"相对 `__file__`"的读取方式，只有当**包内目录结构
被原样保留**时才继续成立：

```
源码:  accesspilot/webgui.py  ->  Path(__file__).parent / "assets" / "dashboard.html"
冻结:  <TEMP>\_MEIxxxxxx\accesspilot\webgui.py
       -> <TEMP>\_MEIxxxxxx\accesspilot\assets\dashboard.html   ← 必须存在
```

漏掉一个资源不会让构建失败——它只会让**用户机器上的某个功能静默变砖**。
所以 `hongxing.spec` 里做了两层保护：

1. 遍历 `accesspilot/` 下**所有非代码文件**（按原目录结构写进 `datas`）；
2. 对**必须存在**的资源做硬校验，缺了就打印醒目警告并列出"缺了会怎样"。

### 5.2 实际收集到的资源（grep 源码得到的结论）

用 `grep -n "__file__|importlib.resources|read_text\(|open\(|assets"` 扫过
`accesspilot/**/*.py`，运行时真正读取的**包内资源**只有三个：

| 资源 | 谁在读 | 读取方式 | 缺了的后果 |
|---|---|---|---|
| `accesspilot/assets/dashboard.html` | `webgui.dashboard_html()`（`webgui.py:36,52-56`） | `Path(__file__).parent / "assets"` | 网页控制台 `/` 返回 `<h1>dashboard.html missing</h1>`，控制台首页变白板 |
| `accesspilot/gui/assets/hongxing.ico` | `gui/icon.py`（`icon.py:90-101,115-117`） | `sys._MEIPASS` 优先，回退 `__file__` | 托盘/窗口取不到图标，且 exe 自身图标缺失 |
| `accesspilot/gui/assets/hongxing_off.ico` | 同上 | 同上 | "未连接"状态托盘图标取不到 |

其它 `open()` 调用点读的都是**用户数据**（`runtime/`、`logs/`、`profiles/`、
`cache/`，全部位于 `%LOCALAPPDATA%\AccessPilot\`），不随包分发，不需要收集。

**全仓库没有使用 `importlib.resources`**（grep 确认），所以不存在
"资源被 zip 进 PYZ 归档而读不到"的问题；`datas` 里的文件是以真实文件形式解压
到 `_MEIPASS` 的。

`process.py:462,527` 和 `cli.py:1601` 里也有 `Path(__file__).resolve().parent.parent`，
但它们算的是"给子进程用的 PYTHONPATH"和"`install-cmd` 的工程目录"，不是资源读取，
与打包无关（详见 §5.3 第 2 条）。

### 5.3 为什么不用手写清单

`collect_package_data()` 用 `Path.rglob("*")` 遍历整个 `accesspilot/` 包，跳过
`__pycache__` 和 `.py/.pyc/.pyd`，其余全部按原目录结构收集。好处是：以后谁往
`accesspilot/**/assets/` 里加文件，**不需要记得回来改 spec**。

`REQUIRED_DATA` 是那份"必须存在"的短清单（上面表格里的三个），缺失时构建日志里
会出现：

```
!! 警告: 以下运行时资源没有被打进 exe, 打包版会在用户机器上功能残缺:
!!   - accesspilot/gui/assets/hongxing.ico  (gui/icon.py 托盘/窗口图标(已连接))
!! 构建仍然继续, 但这个包**不要发布** —— 缺资源是构建缺陷, 不是"别人还没写完"。
```

**构建不会因为资源缺失而失败**（一个资源缺失不该让你连日志都拿不到），但
**看到这条警告就必须当成构建缺陷处理，重新跑一次 `python packaging/build.py`**：
`accesspilot/gui/` 与 `accesspilot/health.py` 现在都已经完整落地，这三个资源
没有任何"暂时还没有"的正当理由。它们到底有没有真的进去，由
`packaging/smoke_test.py` 在**冻结环境内部**真读一次来把关（见 §7）。

### 5.4 hiddenimports

`collect_submodules()` 直接扫 `accesspilot/**/*.py` 的文件名生成模块列表
（而不是用 PyInstaller 的 `collect_submodules()`），原因是后者会真的 import
一遍——延迟导入的模块（`accesspilot/gui/tray.py` 只在需要托盘时才 import）
在"当前环境跑不起来"时会被漏掉。扫文件名不会被"当前代码能不能跑"带偏。

另外补了一组标准库的显式 hiddenimports：`tkinter.*` 若干子模块和
`ctypes.wintypes`（托盘用），它们在源码里是延迟 import 的，静态分析不一定全抓到。

### 5.5 excludes

运行时零第三方依赖，所以构建机上装的 `numpy/PIL/pandas/matplotlib/pytest` 之类
都不该进包。`EXCLUDES` 里显式排掉了它们（排除不存在的包不会报错）。

---

## 6. 冻结后必须特殊处理的三件事（`entry.py`）

这三件事是"源码跑得好好的，打包后必崩/必静默失效"的典型。全部集中在
`packaging/entry.py` 里解决，**没有改动 `accesspilot/` 的任何源码**。

### 6.1 窗口化构建的 `sys.stdout` 是 `None`

`console=False` 的 exe 没有控制台，Python 会把 `sys.stdout` / `sys.stderr` 置为
`None`。而 `accesspilot/util.py:23` 在**模块顶层**执行：

```python
_COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None
```

不先把流补上，`import accesspilot.util` 就会 `AttributeError: 'NoneType' object
has no attribute 'isatty'` —— 用户双击后看到的是"闪一下，什么都没发生"。

`entry.py` 在 import accesspilot **之前**按三级退路补齐：

| 退路 | 触发场景 | 编码 |
|---|---|---|
| a. 继承的标准句柄 | 父进程重定向了输出（`Start-Process -RedirectStandardOutput`、管道） | UTF-8 |
| b. `AttachConsole(父进程)` | 从 cmd / PowerShell 里调用（父进程有控制台） | 控制台代码页（中文机器是 cp936） |
| c. 日志文件 | 双击启动（既没控制台也没重定向） | UTF-8，写到 `%LOCALAPPDATA%\AccessPilot\logs\hongxing.log` |

所以**发布版 exe 也能在终端里打印**，这是退路 a/b 带来的：

```powershell
& "dist\红杏.exe" --version      # -> AccessPilot 1.0.0
cmd /c "dist\红杏.exe" doctor    # cmd 会等 GUI 程序结束，输出直接可见
```

> 注意：PowerShell 对 **GUI 子系统**程序默认**不等待、不捕获输出**，
> 所以 `& "dist\红杏.exe" --version` 有时看不到东西（`$LASTEXITCODE` 也是空的）。
> 要可靠地拿到输出和退出码，用：
> ```powershell
> Start-Process -FilePath "dist\红杏.exe" -ArgumentList "--version" -Wait -PassThru `
>   -RedirectStandardOutput out.txt -RedirectStandardError err.txt
> ```
> 或者 `cmd /c`（cmd 会等待 GUI 程序）。

外层还包了一个 `_StreamShim`，把 `reconfigure()` 变成空操作：`util.py` 在 import
时会把 stdout 强制重配成 UTF-8，那是为"管道输出统一 UTF-8"设计的，但对真正的
Windows 控制台是错的（控制台按代码页解释字节，UTF-8 的中文会变乱码）。
`HONGXING_STDOUT_ENCODING` 可以覆盖这个选择。

`HONGXING_STREAM=log` 可以**强制**走退路 c（即使当前有可用的终端/管道输出也会
被重定向到日志文件），用来复现"用户说双击没反应"：让用户带这个环境变量跑一次，
就能拿到一份日志。

> **实测补充**：冒烟测试第 5 项用 `Start-Process`（**不带**输出重定向）来模拟
> "资源管理器里双击"。这是唯一能真正复现"`sys.stdout is None`"的启动方式——
> Python 的 `subprocess` 无论怎样都会给子进程一个有效的 stdout 句柄，那样
> `sys.stdout` 就不是 `None`，测不到这条退路。实测结果是日志文件里出现了：
> ```
> ===== 红杏 启动 2026-09-30 21:01:52 pid=21712 argv=['--version'] =====
> AccessPilot 1.0.0
> ```

### 6.2 `sys.executable` 变成了 exe 自己

`accesspilot` 内部有三处用 `sys.executable -m accesspilot <子命令>` 拉起子进程
或写计划任务：

| 位置 | 用途 |
|---|---|
| `process.py:543` | 看门狗：内核一死就还原系统代理（防止整台机器断网） |
| `process.py:479` | 直连加速器（本地 SOCKS5 IP 优选） |
| `control.py:619-620` | 开机自启的计划任务（`set_autostart`） |

冻结之后 `sys.executable` 就是 `红杏.exe`，这些调用会变成
`红杏.exe -m accesspilot _watchdog --pid 123` —— 参数原样落到 exe 的 argv 上。
`entry.py` 的 `_normalized_argv()` 把 `-m accesspilot` 前缀剥掉再转交 cli：

```
红杏.exe -m accesspilot _watchdog --pid 123   ->   cli(["_watchdog", "--pid", "123"])
```

于是 exe 能把 `-m accesspilot <子命令>` 当成自己的子命令来执行，用户机器上既不
需要 Python，也不需要全局 `accesspilot` 命令。**这条参数通路已由冒烟测试第 3 项
实测覆盖**；看门狗/加速器**自身的业务逻辑**不在打包验证范围内（它们要真实内核
和真实网络，见 §8 第 11 条）。

### 6.3 双击时命令行是空的

cli 在没有子命令时只打印状态就退出（窗口化构建连打印都看不见，等于"双击没反应"）。
所以 `entry.py` 默认补上 `gui`，并做了两级优雅降级：

```
红杏.exe                     -> cli(["gui"])            （主路径）
红杏.exe --no-tray           -> cli(["gui", "--no-tray"])
cli 里还没有 gui 子命令时     -> accesspilot.gui.main()
界面包也装不上时              -> cli(["dashboard", "--open"])  （老的网页控制台）
```

这个降级链现在是纯粹的**保险**，不是开发期的常态：`gui` 子命令与
`accesspilot/gui/app.py` 都已经完整落地。保留它的理由是"双击没反应"是这类客户端
最致命的失败模式 —— 与其弹一个用户看不懂的 traceback，不如退到内置网页控制台，
至少还能连上网。

---

## 7. 怎么验证

### 7.1 自动冒烟（推荐，7 项）

```powershell
python packaging/smoke_test.py            # 默认测 dist\红杏.exe
python packaging/smoke_test.py --debug    # 测红杏-debug.exe
python packaging/smoke_test.py --keep-temp
```

真实执行 exe，逐项核对：

| # | 检查项 | 证明了什么 |
|---|---|---|
| 1 | 产物存在且体积 > 5 MiB | exe 存在；体积太小说明 Python 运行时没进去 |
| 2 | `红杏.exe --version` | 冻结运行时能启动，`accesspilot.cli` 能 import |
| 3 | `红杏.exe -m accesspilot --version` | §6.2 的再入口修复有效（看门狗/加速器/计划任务） |
| 4 | `红杏.exe --selftest` | **在冻结环境内部**真读 `dashboard.html`、gui 图标，并真的创建一个 Tk 根窗口（tcl/tk 数据缺失只在这一刻报错） |
| 5 | `Start-Process`（不带重定向）启动 | **双击场景**（`sys.stdout is None`）下输出能落到日志文件 |
| 6 | 清空 `PYTHON*` + PATH 里没有 python | 产物**不依赖宿主 Python**（这是"用户机器不用装 Python"的直接证据） |
| 7 | exe 版本资源（信息性） | 属性页里有产品名/版本号 |

冒烟测试全程在临时目录里跑（`cwd` 也在临时目录），并设置 `ACCESSPILOT_HOME`
隔离，不会碰真实用户数据。

### 7.2 手工验证

```powershell
# 看退出码 + 输出（PowerShell 对 GUI 程序不等待，用这个写法）
Start-Process -FilePath "dist\红杏.exe" -ArgumentList "--version" -Wait -PassThru `
  -RedirectStandardOutput out.txt -RedirectStandardError err.txt
Get-Content out.txt; $LASTEXITCODE

# 直接在终端里看到输出（cmd 会等待 GUI 程序）
cmd /c "dist\红杏.exe" --version
cmd /c "dist\红杏.exe" doctor

# 真正的"用户视角"：双击 dist\红杏.exe
```

### 7.3 不要改坏源码侧测试

打包链路不碰 `accesspilot/`，所以源码测试必须保持全绿：

```powershell
python runtests.py
```

> 实测：`Ran 421 tests ... OK`（退出码 0）。这个数字随 `tests/` 增长而变，
> 引用前请自己跑一次 —— 打包改动没有让任何一个失败，因为打包链路完全没有碰
> `accesspilot/`。

---

## 8. 已知限制（诚实清单）

1. **`install-cmd` 在打包版里没有意义**。它写的是
   `"<python>" -m accesspilot ...` 的包装脚本（`cli.py:1592`）。冻结后
   `sys.executable` 是 exe，脚本虽然能跑通（因为 §6.2 的归一化），但对用户没有
   价值——用户直接双击 exe 就行。**打包版不要引导用户用 `install-cmd`。**

2. **开机自启的计划任务绑定 exe 的绝对路径**。`gui/app.py:484-489` 在 frozen
   时传了 `exe=sys.executable`，所以任务指向 `C:\...\红杏.exe` 本身（这是正确的
   —— 用户机器上没有 Python）。**但如果用户移动/重命名了 exe，计划任务会失效**，
   需要在界面里重新关一次、开一次"开机自启"。这是 Windows 计划任务的固有行为，
   不是打包 bug。

3. **没有代码签名**，所以首次运行会弹 SmartScreen "Windows 已保护你的电脑 /
   未知发布者"。用户需要点"更多信息 → 仍要运行"。要消除它必须买 OV/EV 代码
   签名证书（本项目没有预算），不是技术问题。

4. **onefile 每次启动都要解压一次**。首次启动、以及每次拉起看门狗/直连加速器
   子进程，都会把约 12 MB 的内容解压到 `%TEMP%\_MEIxxxxxx\`，耗时约 1–3 秒，
   子进程各占一份临时磁盘/内存。这是 onefile 的固有代价。如果以后更在意启动
   速度和内存，可以改成 onedir（`exclude_binaries=True` + `COLLECT`），代价是
   产物从"一个文件"变成"一个目录"。

5. **杀毒软件可能误报**。无签名 + 自解压的 PyInstaller 产物是启发式引擎的常见
   目标。已经关掉了 UPX（压缩会显著提高误报率）。如果遇到拦截，只能上报白名单。
   需要说明的是：**本次构建没有在真实用户机器上遇到误报**，这条是预防性提示。

6. **只支持 64 位 Windows**。产物架构跟随构建机（本次是 x64）。32 位或 ARM64
   Windows 需要在对应架构的机器/解释器上重新构建。

7. **TUN 模式的权限路径没有变化**。exe 没有加 `uac_admin`（不主动请求提权，
   避免每次启动都弹 UAC），TUN 需要管理员权限时的处理仍走原有代码路径
   （`sysproxy.tun_available()`）。打包不改变这部分行为，但也没有改善它。

8. **中文文件名**。`红杏.exe` 在 Windows 上完全正常；但如果通过某些老旧压缩
   工具/上传工具分发，中文名可能乱码。此时可以直接分发 `dist\hongxing.exe`
   —— 它与 `红杏.exe` 是同一份二进制（`copy2` 出来的），功能完全一致。

9. **窗口化 exe 的控制台输出编码**：从终端调用时用控制台代码页，重定向到
   文件/管道时写 UTF-8。如果要固定，用 `HONGXING_STDOUT_ENCODING=utf-8`。
   `NO_COLOR` 被无条件遵守（`_StreamShim.isatty()` 恒为 False，不输出 ANSI
   颜色码）。

10. **`--debug` 版和发布版是两个独立产物**，改完源码要重新构建；onefile 不支持
    增量更新，每次都是全量约 12 MB。

11. **看门狗 / 直连加速器只验证了"参数通路"，没有端到端实测**。`-m accesspilot`
    的前缀归一化已被冒烟测试覆盖（§6.2），但"内核死掉后看门狗真的还原了系统
    代理""加速器真的起了 SOCKS5 端口"这类行为需要真实内核 + 真实网络，不在
    打包验证范围内。

12. **没有在真正的"裸机"（从未装过 Python 的干净 Windows）上双击验证过**。
    本轮做的是等价性更强的近似：清空 `PYTHON*` 环境变量、把 `PATH` 缩到
    `C:\Windows\system32` 后运行 exe（此时 PATH 里找不到任何 `python`），产物
    照常启动并通过全部自检。加上 onefile 本身内嵌解释器，可以认为不依赖宿主
    Python；但严格意义上的"另一台干净机器"仍需在发布前抽测一次。

---

## 9. 排错手册

| 症状 | 怎么查 |
|---|---|
| 双击没反应 | `set HONGXING_STREAM=log` 再双击，看 `%LOCALAPPDATA%\AccessPilot\logs\hongxing.log` |
| 崩溃对话框有 traceback | 直接看；窗口化构建保留了 PyInstaller 的 traceback 对话框（`disable_windowed_traceback=False`），这是非技术用户唯一能截图发过来的东西 |
| `ModuleNotFoundError` | 延迟 import 的模块没被分析到 → 加进 `hongxing.spec` 的 `EXTRA_HIDDENIMPORTS`，重新构建 |
| 控制台首页白板 | `dashboard.html` 没进包 → 看构建日志里的 `!! 警告` 段，确认 `accesspilot/assets/dashboard.html` 存在 |
| 托盘没有图标 | 看 `--selftest` 的 `gui_icons` / `gui_tray` 两项 |
| `Can't find a usable init.tcl` | tcl/tk 数据没打进去 → 确认 `tkinter` 在 hiddenimports，且 PyInstaller 的 `hook-_tkinter` 有执行（构建日志里能看到） |
| 构建报 `WinError 5/32` | `dist\红杏.exe` 正在运行，或被杀软锁定 → 关掉再构建 |
| 改了 `.ico` 但图标没变 | exe 图标是**编译进资源段**的，必须重新构建；另外 Windows 图标缓存要时间/重启才刷新 |

---

## 10. 改完源码后怎么重新出包

```powershell
python runtests.py            # 1. 源码测试全绿
python packaging/build.py     # 2. 重新构建（约 1 分钟）
python packaging/smoke_test.py # 3. 冒烟测试
```

`packaging/` 和 `accesspilot/` 完全解耦：`accesspilot/` 的改动不需要动打包脚本，
打包脚本的改动也不会影响源码运行（`python -m accesspilot` 依然照常工作）。

---

## 11. 安装程序（`红杏-Setup-v<版本>.exe`）

这是**绝大多数 Windows 用户实际下载的那个文件**。它和 `红杏.exe` 是两回事：
`红杏.exe` 是主程序，Setup 是"把主程序 + 快捷方式 + 卸载入口一次装好"的外壳。

### 11.1 怎么构建

```powershell
python packaging/build.py                     # 先有主程序: dist\红杏.exe
python packaging/build_installer.py           # 轻量包: 不含内核 -> dist\红杏-Setup-v<版本>.exe
python packaging/build_installer.py --full    # 完整包: 带内核, 离线可用 -> dist\红杏-Setup-v<版本>-full.exe
```

| 参数 | 作用 |
|---|---|
| `--full` | 把内核（`mihomo.exe` / `wintun.dll`）与地理数据、节点档一起打进包里。用户装完**不用再跑 `accesspilot init`**。产物约 56 MB。 |
| `--app-exe <路径>` | 指定主程序（默认 `dist\红杏.exe`）。 |
| `--core <目录>` | 内核来源目录（默认 `%LOCALAPPDATA%\AccessPilot\core`）。 |
| `--home <目录>` | 取离线资源（geodata / profiles / state.json）的数据目录（默认 `%LOCALAPPDATA%\AccessPilot`）。 |
| `--with-cache` | 连规则集缓存一起打，首次启动更快，包再大约 13 MB。 |
| `--version <版本>` | **覆盖版本号**。必须与 `accesspilot/__init__.py` 的 `__version__` 一致，否则拒绝构建。 |
| `--force-version` | 明知与包内版本不一致也照建（只在刻意造一个版本号不同的包时用）。 |

轻量包 vs 完整包该发哪个：**两个都发**。轻量包给"已经有内核/想自己下"的人，
完整包给"只想双击一下就能用"的人（也是主推的那个）。

### 11.2 构建时的两道闸（都是被真实事故换来的）

**版本闸.** 版本号只有一个来源：`accesspilot/__init__.py` 的 `__version__`。
`hongxing.spec`、`packaging/build.py`、`build_installer.py` 直接读它；
`installer.spec` 读的是 `build_installer.py` 写进 `payload/version.txt` 的那个值
（读不到才回退到 `__init__.py`），而那个值本身就来自 `__version__`；
`pyproject.toml` 改成 `dynamic = ["version"]`，不再自己写一份。
不传 `--version` 时用它；传了就必须一致，否则拒绝构建。理由是本项目真实发生过的事：
发出去的安装包叫 `红杏-Setup-v0.9.0.exe`，而程序本体早就是 1.0.0 —— 因为版本号在
9 个地方各写一份，没有任何东西对账。附带的一条：主程序 exe 的 PE `FileVersion`
也会被读出来比对，防止"用旧 exe 包了一个新版本号的安装包"。

**凭据闸.** payload 里的 `state.json` **不许带 `api_secret`**。
`build_installer.py` 会在打包时把它清空（并顺手删掉 `last_start` 这类只属于打包机
的字段），然后**再断言一次真的清空了**；安装器在目标机器上还会自己
`secrets.token_hex(16)` 重新生成一个，不信任包里带来的任何值。
这条同样来自真实事故：曾经发布出去的完整版 Setup.exe 里，`payload/state.json`
冻着维护者本机的真实 external-controller 密钥 —— 任何人都能把 56 MB 的安装包解开
拿到它。**凭据一旦进了公开二进制就必须当作已泄露处理**，与"它只监听 127.0.0.1、
实际可利用性低"无关；而且每台机器本来就该有各自不同的密钥。

> 附带说明：payload 里的 `profiles/*.json` 与 `geodata/*` 是有意随包分发的（那是
> "装完即用"的前提），它们里面没有本机凭据。`state.json` 里除密钥外的字段
> （端口、镜像、`active_profile`、选中节点）也都保留，它们只描述"用哪个档"，
> 不含个人信息。

### 11.3 用户怎么用（也是要写进 README 的部分）

```powershell
红杏-Setup-v1.0.0.exe                 # 双击: 图形向导(选目录 -> 安装 / 卸载 / 检查更新)
红杏-Setup-v1.0.0.exe --silent        # 静默安装(装完不启动, 不弹窗)
红杏-Setup-v1.0.0.exe --dir D:\Apps   # 指定安装目录
红杏-Setup-v1.0.0.exe --uninstall     # 卸载(保留节点/配置)
红杏-Setup-v1.0.0.exe --uninstall --purge   # 卸载并删除全部用户数据
红杏-Setup-v1.0.0.exe --update        # 检查 GitHub 最新版并就地更新
红杏-Setup-v1.0.0.exe --console       # 不加参数时也走命令行(不进图形向导)
红杏-Setup-v1.0.0.exe --no-launch / --no-shortcuts / --no-core / --force-core
```

* **装到哪**: 程序在 `%LOCALAPPDATA%\Programs\Hongxing\`，数据在
  `%LOCALAPPDATA%\AccessPilot\`。**都是用户级路径，全程不需要管理员权限**，
  也不会弹 UAC。
* **程序与数据分开是刻意的**：更新只换程序、绝不动数据；卸载默认也只删程序。
  这样"重装一次"不会把用户攒下来的节点和配置清掉。
  （另外内核在**数据目录**（`%LOCALAPPDATA%\AccessPilot\core\`），不在 exe 旁边 ——
  放进程序目录就等于没装，`paths.core_binary()` 找不到它。）
* **卸载/更新前一定先让红杏自己收尾**（跑一次 `红杏.exe stop`）。系统代理开着时
  直接删程序，用户会留下一个指向死端口的代理设置，**整台机器都上不了网**。
  这一步不能省。
* **快捷方式**建在开始菜单和桌面，路径从注册表 `User Shell Folders` 取真实位置
  （有机器把 Desktop 重定向到别的盘，硬拼 `%USERPROFILE%\Desktop` 会"建成了但
  用户永远看不到"）。PowerShell 完全不可用时退化成同目录 `.cmd`。

#### 自动更新的安全约束（这一节是硬要求，不是建议）

`--update` 做的是"**就地覆盖用户正在用的主程序**"，所以它必须保守：

1. **只认白名单文件名**（`installer.py` 的 `APP_ASSET_PATTERNS`：
   `红杏-v<版本>-win64.exe` / `hongxing-v<版本>-win64.exe` / `红杏.exe` / `hongxing.exe`）。
   匹配不上就明确报错退出，**不会退化成"随便挑一个 .exe"**。
   曾经这里就是那个兜底分支：先找名字带 `win64` 的（本仓库从来没有这种产物），
   找不到就挑第一个 `.exe` —— 而发布页上同时躺着安装器，于是用户点一次更新，
   主程序被换成了 Setup。之后「红杏」快捷方式打开的是安装向导，保活任务调用的
   `红杏.exe ensure` 也没人认识，而且全程静默。
2. **显式排除安装器资产**（名字里带 `setup` / `installer` / `安装` / `卸载` 一律不认）。
3. **必须校验 SHA256**，来源依次是 GitHub 附件自带的 `digest`、发布说明里
   `SHA256(<文件名>) = <64位十六进制>` 那一行、或发布页上的 `SHA256SUMS.txt` /
   `*.sha256`。校验不过就放弃，用户手上的程序一个字节都不动。
   发布方**没提供任何校验值**时默认拒绝更新（要跳过必须显式
   `--insecure-skip-verify`）—— 没有校验就等于"下载什么装什么"。
4. 下载完再独立看一次 exe 的版本资源：产品名里带"安装/卸载"就判定认错了，
   直接放弃（读不到版本资源不算失败，主判据是 1–3）。

**发布方要做的**：把 `dist\红杏-v<版本>-win64.exe` 上传成 release 附件，并在说明里
贴一行 `SHA256(红杏-v1.0.0-win64.exe) = <哈希>`。安装器 `dist\红杏-Setup-*.exe`
也照发，但更新器永远不会把它当成主程序。

### 11.4 卸载器为什么是"安装器自己的一份拷贝"（诚实的取舍）

冻结后的 Setup.exe 会把自己复制一份到 `%LOCALAPPDATA%\Programs\Hongxing\红杏-卸载.exe`，
"设置 → 应用"里的卸载入口指向它。**代价是这个文件有 24 MB（轻量包）/ 56 MB（完整包）**，
而且它会被算进"设置 → 应用"里显示的程序体积。

为什么还是这么做：卸载要在**用户已经把下载目录清空**之后仍然能用。把
`UninstallString` 指向用户当初下载的那个 Setup.exe 是最省空间的写法，但只要用户
顺手删掉了下载的安装包（很常见），卸载入口就变成一个点了报错的死链接 —— 而
"程序卸不掉"比"程序目录里多一个 24 MB 的文件"严重得多。

**已知的次生问题**：卸载时如果卸载器自己就在要删的目录里，Windows 不允许删除正在
运行的 exe，所以它会先复制到 `%TEMP%\hongxing-uninstall-<pid>.exe` 再重新执行。
这个临时副本现在会在卸载结束后由它自己派生的 `cmd` 延时删除
（`installer.py` 的 `schedule_self_delete`）；在此之前的版本不会删，每次卸载都在
`%TEMP%` 里留一份 24–56 MB 的 exe，而且文件名带 pid，永远不会被覆盖。

**将来可以更好**：单独做一个只冻结 `installer.py`、不带 payload 的小
`uninstaller.spec`（估计 10–12 MB），作为 `payload/uninstaller.exe` 打进安装包，
安装时复制它而不是复制整个 Setup.exe。这样能省下 12–46 MB，也能让"设置 → 应用"
里的体积数字更接近真实。目前没做，是因为它要给发布流程再加一个产物，
收益（磁盘）远小于风险（发布前改链路）。

### 11.5 排错

| 症状 | 怎么查 |
|---|---|
| 双击 Setup 没反应 | 用 `红杏-Setup-*.exe --console` 跑一次看输出；或用 `--silent` 看退出码 |
| 装完没有桌面图标 | 看安装日志里那一行 `[!] 桌面快捷方式`；有机器把 Desktop 重定向到别的盘 |
| "设置 → 应用"里版本号不对 | `红杏-Setup-*.exe` 的版本号来自 `payload/version.txt`，即构建时的 `--version`。现在它有版本闸拦着，不该再出现 |
| `--update` 说"没有找到主程序资产" | 发布页缺 `红杏-v<版本>-win64.exe`（见 §11.3） |
| `--update` 说"没有提供 SHA256 校验值" | 发布说明里没贴哈希，且 GitHub 附件也没有 digest（见 §11.3 第 3 条） |
| 卸载后 `%TEMP%` 里还有 `hongxing-uninstall-*.exe` | 卸载器被双击时会在最后等回车，延时删除可能来不及；手动删即可 |

---

## 12. 发布 v1.0.0 的完整顺序

```powershell
# 1) 源码侧全绿(数字会随 tests/ 变化, 以本次输出为准)
python runtests.py

# 2) 单独构建两个 exe(主程序 -> 发布资产名; 安装器 -> 轻量与完整两包)
python packaging/build.py
python packaging/build.py --debug          # 可选: 排错版

# 3) 冒烟测试(真的把 exe 跑起来, 7 项)
python packaging/smoke_test.py

# 4) 安装器(版本号自动取自 accesspilot/__init__.py, 不要手动传 --version)
python packaging/build_installer.py
python packaging/build_installer.py --full

# 5) 确认产物名与版本号一致
Get-ChildItem dist | Select-Object Name, Length
Get-Content build\installer-payload\version.txt        # 应等于 __version__
(Get-Item "dist\红杏-Setup-v1.0.0.exe").VersionInfo | Format-List FileVersion, ProductVersion

# 6) 确认发布的包里没有真实凭据(必须无输出)
Select-String -Path build\installer-payload\state.json -Pattern "api_secret" -Context 0,1

# 7) 上传到 GitHub Release 的附件(一个都不能少):
#      dist\红杏-v1.0.0-win64.exe         <- 自动更新**只认这个**名字
#      dist\红杏-Setup-v1.0.0.exe         <- 轻量安装包
#      dist\红杏-Setup-v1.0.0-full.exe    <- 完整安装包(推荐给普通用户)
#    发布说明里贴一行: SHA256(红杏-v1.0.0-win64.exe) = <build.py 打印的哈希>
```

最后两条是硬要求：**少了 `红杏-v1.0.0-win64.exe`，所有老用户的「检查更新」都会
失败**（这是刻意的失败方向，见 §11.3）；**不贴哈希，更新器默认拒绝安装**
（同样刻意）。
