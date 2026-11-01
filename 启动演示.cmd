@echo off
setlocal
chcp 65001 >nul
title 星河虚拟银行 Agent
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "PYLAUNCHER_ALLOW_INSTALL="
set "PYTHON_MANAGER_AUTOMATIC_INSTALL=false"
set "AGENT_EXIT_CODE=1"
echo 星河虚拟银行 Agent - 正在检查启动环境...
echo.
pushd "%~dp0"
if errorlevel 1 goto directory_error
if not exist "banking_agent\__main__.py" goto missing_files
if not exist "launch_demo.py" goto missing_files

set "AGENT_PYTHON_ARGS="
if exist "runtime\python.exe" (
    set "AGENT_PYTHON=%~dp0runtime\python.exe"
    goto run_demo
)
py -3.11 -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>&1
if not errorlevel 1 (
    set "AGENT_PYTHON=py"
    set "AGENT_PYTHON_ARGS=-3.11"
    goto run_demo
)
py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>&1
if not errorlevel 1 (
    set "AGENT_PYTHON=py"
    set "AGENT_PYTHON_ARGS=-3"
    goto run_demo
)
python -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>&1
if not errorlevel 1 (
    set "AGENT_PYTHON=python"
    goto run_demo
)
python3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>&1
if not errorlevel 1 (
    set "AGENT_PYTHON=python3"
    goto run_demo
)
echo [启动失败] 未找到可用的 Python 3.11 或更新版本。
echo 请使用自带 runtime 文件夹的免安装版，先全部解压缩，再运行本文件。
echo 源码版用户也可以安装 Python，并勾选 Add python.exe to PATH。
echo Python 官方下载：https://www.python.org/downloads/windows/
goto failed

:run_demo
echo 已选择：%AGENT_PYTHON% %AGENT_PYTHON_ARGS%
echo.
"%AGENT_PYTHON%" %AGENT_PYTHON_ARGS% -X utf8 -u "%~dp0launch_demo.py" %*
set "AGENT_EXIT_CODE=%errorlevel%"
if "%AGENT_EXIT_CODE%"=="0" goto finished
echo.
echo [启动失败] 程序退出码：%AGENT_EXIT_CODE%
echo 请把本窗口完整截图发给项目提供者。
goto finished

:missing_files
echo [启动失败] 当前目录缺少完整项目文件。
echo 如果你正在 ZIP 压缩包内浏览，请先点击“全部解压缩”。
echo 解压后进入同时包含本文件、launch_demo.py 和 banking_agent 文件夹的目录。
goto failed

:directory_error
echo [启动失败] 无法进入项目文件夹。请先将整个压缩包解压到本地可写目录。
goto end

:failed
set "AGENT_EXIT_CODE=1"

:finished
popd

:end
echo.
pause
exit /b %AGENT_EXIT_CODE%
