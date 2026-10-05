# AccessPilot 一键安装脚本 (Windows PowerShell)
#
#   powershell -ExecutionPolicy Bypass -File scripts\install.ps1
#
# 作用: 检查 Python 环境 -> 下载 mihomo 内核与 TUN 驱动 -> 注册全局命令 -> 建桌面快捷方式
#
# ⚠️ 本文件必须保存为 **UTF-8 with BOM**。
#    Windows PowerShell 5.1 读无 BOM 的 UTF-8 文件时会按系统 ANSI 代码页
#    (中文机器是 cp936) 解码, 于是下面所有中文字符串都变成乱码 —— 包括要写进
#    快捷方式的那个名字, 用户桌面上就会出现一个名字是乱码的图标, 而且不报错。
#    这个坑和 packaging/installer.py 里"必须走 -EncodedCommand"是同一类问题。

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Write-Host "==> AccessPilot 安装程序" -ForegroundColor Cyan

# 1) 检查 Python
$py = Get-Command python -ErrorAction SilentlyContinue
if (-not $py) {
    Write-Host "[x] 未检测到 Python, 请先从 https://www.python.org/downloads/ 安装 Python 3.9+" -ForegroundColor Red
    exit 1
}
$ver = & python -c "import sys;print('.'.join(map(str,sys.version_info[:2])))"
Write-Host "[+] Python $ver"

# 2) 安装内核
$env:PYTHONPATH = "$root;$env:PYTHONPATH"
Set-Location $root
& python -m accesspilot init
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

# 3) 注册全局命令
#    注意: 不要用 `pip install -e .` —— 本机 Python 布局特殊(scripts 目录被
#    重定向)时 pip 会报 "No pyvenv.cfg file"。写包装器更可靠, 且不依赖构建工具。
& python -m accesspilot install-cmd
if ($LASTEXITCODE -ne 0) {
    Write-Host "[!] 自动注册失败, 可直接使用: $root\accesspilot.cmd" -ForegroundColor Yellow
}

# 4) 桌面快捷方式
#    这一条以前只写在文件头的注释里, 脚本里根本没有对应代码 —— 用户照着 README
#    跑完, 桌面上什么都没有, 只会认为"装了一半"。
#
#    目标选谁:
#      * 有打包好的 dist\红杏.exe 就用它 —— 那是不依赖 Python 的真客户端;
#      * 否则退到仓库自带的 accesspilot.cmd(免安装启动器), 带参数 gui,
#        这样双击出来的是图形界面而不是一个命令行窗口里的状态输出。
#
#    路径取自注册表的 "User Shell Folders" 而不是 %USERPROFILE%\Desktop:
#    实测有机器把 Desktop 整体重定向到别的盘, 硬拼默认路径的后果是
#    "快捷方式建成了、用户桌面上永远看不到", 而且全程不报错。
function New-DesktopShortcut {
    $exe = Join-Path $root "dist\红杏.exe"
    if (Test-Path -LiteralPath $exe) {
        $target = $exe
        $arguments = ""
        $workdir = Split-Path -Parent $exe
        $icon = $exe
    } else {
        $target = Join-Path $root "accesspilot.cmd"
        if (-not (Test-Path -LiteralPath $target)) {
            Write-Host "[!] 没找到 dist\红杏.exe, 也没有 $target, 跳过桌面快捷方式" -ForegroundColor Yellow
            return
        }
        $arguments = "gui"
        $workdir = $root
        $icon = Join-Path $root "accesspilot\gui\assets\hongxing.ico"
    }

    $desktop = $null
    try {
        $desktop = (Get-ItemProperty -Path "HKCU:\Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders" -Name "Desktop" -ErrorAction Stop).Desktop
        $desktop = [Environment]::ExpandEnvironmentVariables($desktop)
    } catch {
        $desktop = $null
    }
    if (-not $desktop -or -not (Test-Path -LiteralPath $desktop)) {
        $desktop = [Environment]::GetFolderPath("Desktop")
    }
    if (-not $desktop -or -not (Test-Path -LiteralPath $desktop)) {
        Write-Host "[!] 找不到桌面目录, 跳过桌面快捷方式" -ForegroundColor Yellow
        return
    }

    $link = Join-Path $desktop "红杏.lnk"
    try {
        $shell = New-Object -ComObject WScript.Shell
        $sc = $shell.CreateShortcut($link)
        $sc.TargetPath = $target
        $sc.Arguments = $arguments
        $sc.WorkingDirectory = $workdir
        $sc.Description = "红杏 - 一键通行"
        if (Test-Path -LiteralPath $icon) { $sc.IconLocation = "$icon,0" }
        $sc.Save()
        Write-Host "[+] 桌面快捷方式: $link" -ForegroundColor Green
    } catch {
        Write-Host "[!] 桌面快捷方式创建失败: $($_.Exception.Message)" -ForegroundColor Yellow
        Write-Host "    不影响使用 —— 直接运行: $target" -ForegroundColor Yellow
    }
}

New-DesktopShortcut

Write-Host ""
Write-Host "验证安装:  accesspilot --version" -ForegroundColor Cyan
Write-Host ""
Write-Host "安装完成。下一步:" -ForegroundColor Cyan
Write-Host "  accesspilot free auto                     # 零成本: 抓公开免费节点并测速"
Write-Host "  accesspilot start --no-sysproxy           # 先只启动内核(不动系统代理)"
Write-Host "  accesspilot test                          # 确认平台可用"
Write-Host "  accesspilot proxy on                      # 确认没问题再开系统代理"
Write-Host ""
Write-Host "想要「双击就能用、不用装 Python」的版本, 用打包好的安装包:" -ForegroundColor Cyan
Write-Host "  python packaging/build_installer.py --full   # -> dist\红杏-Setup-v<版本>-full.exe"
