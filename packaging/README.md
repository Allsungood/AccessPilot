# 红杏 桌面客户端 —— 单文件 exe 打包说明

> 目标: **给不懂技术的用户一个 `红杏.exe`，双击就能用。用户机器上不装 Python，
> 不装任何第三方包，不需要命令行。**

本文档写给要重新构建 exe 的人（维护者）。普通用户不需要看这些。

---

## 0. 一句话

```powershell
python packaging/build.py          # 构建 -> dist\红杏.exe
python packaging/smoke_test.py     # 验证: 真的把 exe 跑起来逐项核对
```

产物是**单个文件** `dist\红杏.exe`（约 12–13 MiB），拷到任何 64 位 Windows 10/11
上双击即可运行。

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
dist\红杏.exe         <- 给用户的最终产物（单文件）
dist\hongxing.exe     <- PyInstaller 的原始产物，与上面是同一份二进制
build\                <- 中间缓存，可随时删除；build\pyinstaller.log 里有完整日志
```

`dist/` 和 `build/` 都在 `.gitignore` 里，不会进版本库。

### `--debug` 排错版

同一个 spec，只是 `console=True`，产物叫 `红杏-debug.exe`。用途：

* 双击运行时能看到控制台里的报错；
* 运行 `红杏-debug.exe doctor`、`红杏-debug.exe status` 这类子命令时输出最直观。

发布版（`console=False`）也有终端输出能力，见 [§5.1](#51-窗口化构建的-stdout-是-none)。

---

## 4. 打包链路里都有什么

| 文件 | 职责 |
|---|---|
| `packaging/entry.py` | **冻结入口点**。只属于打包链路，不属于 `accesspilot` 运行时包。它替运行时挡掉三件"只有冻结后才会发生"的事，见 §5。 |
| `packaging/hongxing.spec` | PyInstaller 规格：onefile、资源收集、hiddenimports、图标、版本资源、excludes。 |
| `packaging/build.py` | 一键构建：前置检查 → 生成图标 → 调 PyInstaller → 拷成中文名 → 报告体积/哈希。 |
| `packaging/smoke_test.py` | 产物冒烟测试：**真的运行 exe**，逐项核对（7 项，见 §6）。 |
| `packaging/README.md` | 本文档。 |

**这些文件都不会被打进运行时依赖**：`accesspilot/` 目录里没有任何一行代码
引用 `packaging/`。

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
!! 构建会继续进行(这几个文件由其它 teammate 并发产出, 可能还没落地)。
```

**构建不会因为资源缺失而失败**，这是刻意的：`accesspilot/gui/` 与
`accesspilot/health.py` 曾经是几个 teammate 并发写的，构建不能因为"他们还没写完"
而挂掉。资源到底有没有真的进去，由 `packaging/smoke_test.py` 在**冻结环境内部**
真读一次来把关（见 §6）。

### 5.4 hiddenimports

`collect_submodules()` 直接扫 `accesspilot/**/*.py` 的文件名生成模块列表
（而不是用 PyInstaller 的 `collect_submodules()`），原因是后者会真的 import
一遍——`gui/app.py` 只写了一半时 import 失败就会漏模块。扫文件名不会被"当前代码
能不能跑"带偏。

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

这个降级在开发期是刚需：`gui` 子命令和 `gui/app.py` 是其它 teammate 并发写的，
构建/试运行不该因为"那一刻还没落地"而打不开。

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

> 实测：`Ran 216 tests ... OK`。任务下达时是 156 个测试，期间其它 teammate
> 往 `tests/` 里加了新的（`test_health.py`、`test_control.py` 等），
> 打包改动没有让任何一个失败。

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
