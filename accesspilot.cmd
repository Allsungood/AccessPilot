@echo off
rem AccessPilot Windows 启动器 —— 免安装直接运行
setlocal
set "ROOT=%~dp0"
set "PYTHONPATH=%ROOT%;%PYTHONPATH%"
where python >nul 2>nul
if errorlevel 1 (
  echo [x] 未找到 Python, 请先安装 Python 3.9+ 并加入 PATH
  exit /b 1
)
python -m accesspilot %*
exit /b %ERRORLEVEL%
