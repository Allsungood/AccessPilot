# AccessPilot 一键安装脚本 (Windows PowerShell)
#
#   powershell -ExecutionPolicy Bypass -File scripts\install.ps1
#
# 作用: 检查 Python 环境 -> 下载 mihomo 内核与 TUN 驱动 -> 创建桌面快捷方式

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

Write-Host ""
Write-Host "验证安装:  accesspilot --version" -ForegroundColor Cyan
Write-Host ""
Write-Host "安装完成。下一步:" -ForegroundColor Cyan
Write-Host "  accesspilot free auto                     # 零成本: 抓公开免费节点并测速"
Write-Host "  accesspilot start --no-sysproxy           # 先只启动内核(不动系统代理)"
Write-Host "  accesspilot test                          # 确认平台可用"
Write-Host "  accesspilot proxy on                      # 确认没问题再开系统代理"
