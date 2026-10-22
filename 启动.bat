@echo off
rem ===================================================================
rem  LLM API 速率验证工具 —— Windows 启动入口
rem
rem  这个文件只做三件事:切到脚本所在目录、找到一个可用的 Python、
rem  把它交给 launcher.py。真正的活(建 venv、装依赖、起界面)全在
rem  launcher.py 里,那份代码 macOS 也在用。
rem
rem  chcp 65001 必须在任何中文输出**之前**执行 —— cmd.exe 是按当前
rem  代码页逐行解析批处理文件的,顺序反了的话下面的中文会变成乱码。
rem ===================================================================

chcp 65001 >nul
cd /d "%~dp0"
title LLM API 速率验证工具

set "PY="

rem 优先挑版本合适的解释器。PySide6 只支持 3.10 ~ 3.14,
rem 所以先按具体版本号试,最后才退到「随便哪个 Python 3」。
for %%V in (3.12 3.13 3.11 3.10 3.14) do (
    if not defined PY (
        py -%%V --version >nul 2>nul && set "PY=py -%%V"
    )
)

if not defined PY (
    py -3 --version >nul 2>nul && set "PY=py -3"
)

rem python 在 Windows 上可能是个「应用执行别名」——一个指向微软商店的
rem 空壳,执行时返回 9009 并弹商店。下面用 --version 试探,正好能把它滤掉。
if not defined PY (
    python --version >nul 2>nul && set "PY=python"
)
if not defined PY (
    python3 --version >nul 2>nul && set "PY=python3"
)

if not defined PY goto nopython

%PY% launcher.py %*
set "CODE=%ERRORLEVEL%"

rem 出错时留住窗口,否则双击运行的话窗口一闪就没了,什么都看不到。
if not "%CODE%"=="0" (
    echo.
    echo 程序以退出码 %CODE% 结束。按任意键关闭窗口。
    pause >nul
)
exit /b %CODE%

:nopython
echo.
echo   ✗ 没有找到 Python。
echo.
echo   这个工具需要 Python 3.10 ~ 3.14 才能运行。
echo.
echo   安装步骤:
echo     1. 打开 https://www.python.org/downloads/
echo     2. 下载 Python 3.12 并运行安装程序
echo     3. 安装第一个界面下方务必勾选
echo        "Add python.exe to PATH"
echo        ^(漏勾的话命令行里找不到 python,只能卸载重装^)
echo     4. 装完后重新双击本文件
echo.
echo   如果已经装过 Python 却还是看到这段话,说明安装时漏勾了
echo   上面那个选项,或者需要重启一次电脑让 PATH 生效。
echo.
pause
exit /b 1
