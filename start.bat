@echo off
title WiFiWeb 无线网络管理

rem ===== 检查管理员权限，无则自动请求提升（扫描旁路与部分连接操作需要） =====
net session >nul 2>nul
if %errorlevel% neq 0 (
    echo 需要管理员权限，正在请求提升...
    powershell -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)

cd /d "%~dp0"

rem ===== WLAN 位置策略旁路：允许跳过 Win11 24H2 的扫描位置限制 =====
rem 详见 doc/位置策略研究报告.md；删除该注册表键可恢复系统默认策略
reg add "HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\Capabilities\wlanLocationBypass" /v RequireWindowsCert /t REG_DWORD /d 0 /f >nul 2>nul

rem ===== 检查 Python（直接使用系统现有的 Python） =====
where python >nul 2>nul
if %errorlevel% neq 0 (
    echo [错误] 未找到 Python，请先安装 Python 3.9+ 并勾选 "Add to PATH"
    pause
    exit /b 1
)

rem ===== 依赖检查：缺失时自动安装 fastapi / uvicorn =====
python -c "import fastapi, uvicorn" >nul 2>nul
if %errorlevel% neq 0 (
    echo 首次运行，正在安装依赖 fastapi / uvicorn ...
    python -m pip install -q -r requirements.txt || (echo [错误] 依赖安装失败，请检查网络 & pause & exit /b 1)
)

echo.
echo  WiFiWeb 启动中... 浏览器将自动打开 http://127.0.0.1:8086
echo  局域网内其他设备请访问 http://(本机IP):8086
echo  关闭本窗口即停止服务
echo.
python -m uvicorn server.main:app --host 0.0.0.0 --port 8086
REM pause
