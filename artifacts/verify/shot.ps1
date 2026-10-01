# 验收用截图工具: 先在进程内声明 per-monitor-v2 DPI 感知, 再抓**物理像素**整屏。
# (gui.exe shot 是 DPI-unaware 的, 只能拿到虚拟化的 1280x720, 且会被前台窗口盖住任务栏)
param([Parameter(Mandatory=$true)][string]$Out)

Add-Type -Namespace Ap -Name Dpi -MemberDefinition @'
[DllImport("user32.dll")] public static extern bool SetProcessDpiAwarenessContext(IntPtr ctx);
[DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
'@
try { [Ap.Dpi]::SetProcessDpiAwarenessContext([IntPtr](-4)) | Out-Null } catch { [Ap.Dpi]::SetProcessDPIAware() | Out-Null }

Add-Type -AssemblyName System.Drawing
Add-Type -AssemblyName System.Windows.Forms

$vs = [System.Windows.Forms.SystemInformation]::VirtualScreen
$bmp = New-Object System.Drawing.Bitmap($vs.Width, $vs.Height)
$g = [System.Drawing.Graphics]::FromImage($bmp)
$g.CopyFromScreen($vs.Left, $vs.Top, 0, 0, (New-Object System.Drawing.Size($vs.Width, $vs.Height)))
$g.Dispose()
$bmp.Save($Out, [System.Drawing.Imaging.ImageFormat]::Png)
$bmp.Dispose()
Write-Host "saved $Out  $($vs.Width)x$($vs.Height) (virtual screen $($vs.Left),$($vs.Top))"
