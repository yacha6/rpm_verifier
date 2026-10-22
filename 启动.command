#!/bin/bash
# ===================================================================
#  LLM API 速率验证工具 —— macOS 启动入口
#
#  双击即可。真正的活(建 venv、装依赖、起界面)全在 launcher.py 里,
#  这个文件只负责切到脚本所在目录、确认有 Python,然后交出去。
# ===================================================================

# 双击运行时的工作目录是用户主目录,必须先切到脚本自己所在的目录,
# 否则找不到 launcher.py。
cd "$(dirname "$0")" || exit 1

if ! command -v python3 >/dev/null 2>&1; then
    echo
    echo "  ✗ 没有找到 python3。"
    echo
    echo "  在终端里执行下面这行,按提示装完后再双击本文件:"
    echo
    echo "      xcode-select --install"
    echo
    echo "  或者用 Homebrew 装一个:"
    echo
    echo "      brew install python@3.12"
    echo
    read -r -p "  按回车键关闭……"
    exit 1
fi

exec python3 launcher.py "$@"
