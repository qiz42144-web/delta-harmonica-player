@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

echo ============================================
echo   三角洲口琴自动演奏器 - 打包成单文件 exe
echo ============================================
echo.

where python >nul 2>nul
if errorlevel 1 (
  echo [x] 找不到 python，请先安装 Python 3.9+ 并加入 PATH。
  pause
  exit /b 1
)

if not exist ".venv-build\Scripts\python.exe" (
  echo [1/3] 创建构建用虚拟环境 .venv-build ...
  python -m venv .venv-build
  if errorlevel 1 goto :err
) else (
  echo [1/3] 复用已有虚拟环境 .venv-build
)
set "PY=.venv-build\Scripts\python.exe"

echo [2/3] 安装 / 更新 PyInstaller ...
"%PY%" -m pip install --disable-pip-version-check --quiet --upgrade pip
"%PY%" -m pip install --disable-pip-version-check --quiet --upgrade pyinstaller
if errorlevel 1 goto :err

echo [3/3] 打包中（第一次会比较慢）...
rem --uac-admin = 每次启动都请求管理员权限。游戏若以管理员运行，没有它会收不到模拟按键。
rem 如果你只想自己测试、不想每次弹 UAC，把下面这行的 --uac-admin 删掉即可。
set "UAC_FLAG=--uac-admin"
"%PY%" -m PyInstaller --noconfirm --clean --onefile --noconsole %UAC_FLAG% ^
  --name "三角洲口琴自动演奏器" ^
  --hidden-import selftest ^
  main.py
if errorlevel 1 goto :err

echo.
echo [√] 打包完成： dist\三角洲口琴自动演奏器.exe
echo     双击即可运行（会请求管理员权限，这是为了能和游戏通信）。
echo     命令行自检： dist\三角洲口琴自动演奏器.exe --selftest
echo.
pause
exit /b 0

:err
echo.
echo [x] 构建失败，请把上面的报错发出来。
pause
exit /b 1
