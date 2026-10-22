#!/usr/bin/env python3
"""一键启动器:建环境 → 装依赖 → 启动界面。

同事那边只需要「有 Python」这一件事,其余的(建 venv、装 PySide6、路径差异)
全部由这个文件搞定。双击 `启动.bat`(Windows)或 `启动.command`(macOS)即可。

为什么用 Python 写而不是 shell / bat:

1. **两个平台共用一份代码。** venv 的解释器路径两边不一样
   (Windows 是 `.venv\\Scripts\\python.exe`,macOS/Linux 是 `.venv/bin/python`),
   写成两个脚本就要维护两份逻辑,而且是很容易写错的那种。
2. **编码。** 中文 Windows 的控制台默认是 GBK,shell 脚本里 echo 中文
   (更别说 ✓ 这类符号)会直接抛 UnicodeEncodeError。Python 能自己把
   输出流重新配成 UTF-8,见 `force_utf8_output()`。

这个脚本本身只依赖标准库,所以能用**系统 Python** 跑起来 —— 它做的事就是
准备好一个装了依赖的独立环境,再去跑真正的程序。
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# 路径
# ---------------------------------------------------------------------------
# 全部相对于这个文件所在的目录,所以整个包可以放在任何位置、随便改名。
PACKAGE_DIR = Path(__file__).resolve().parent
VENV_DIR = PACKAGE_DIR / ".venv"
REQUIREMENTS = PACKAGE_DIR / "requirements.txt"
# 依赖装好之后留一个戳。内容没变就不重复跑 pip —— pip 每次启动都要
# 好几秒,而且会刷一屏输出,对「双击就能用」的体验是致命的。
STAMP_FILE = VENV_DIR / ".deps-stamp"

# ---------------------------------------------------------------------------
# Python 版本窗口
# ---------------------------------------------------------------------------
# 这个区间不是拍脑袋定的,是 PySide6 元数据里写死的:
#     Requires-Python: <3.15,>=3.10
# 低于下限和高于上限**都装不上**。只拦下限的话,同事机器上是 3.15 时会
# 一头撞进 pip 的依赖解析报错里,那堆输出没人看得懂,也看不出是版本问题。
MIN_PYTHON = (3, 10)
MAX_PYTHON = (3, 15)  # 开区间,不含 3.15

EXIT_OK = 0
EXIT_ENV_ERROR = 1

# 前两次安装都失败时才会用到的备用源。国内直连 pypi.org 经常很慢甚至超时,
# 而界面库的 wheel 有 110MB —— 卡在这里的同事会以为是程序坏了。
MIRROR_INDEX = "https://pypi.tuna.tsinghua.edu.cn/simple"


def force_utf8_output() -> None:
    """把标准输出/错误流强制成 UTF-8。

    这条在 Windows 上是必需的。控制台直连时 Python 走 PEP 528,中文没问题;
    但输出一旦被**重定向**(写进文件、管道、某些终端模拟器),Python 会退回
    系统的首选编码 —— 中文 Windows 上是 GBK,打印「✓」会当场抛
    UnicodeEncodeError 把程序打死。这不是推测,是实测复现过的。

    errors="replace" 是第二道保险:就算某个字符在当前终端真的渲染不了,
    也只会显示成问号,而不是让启动器崩在半路。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            # 流已经关了,或者不支持重配 —— 都无关紧要,不该因此中断启动
            pass


def say(message: str = "") -> None:
    """打印一行提示。统一走这里,方便以后加前缀或写日志。"""
    print(message, flush=True)


def venv_python() -> Path:
    """当前平台上 venv 里解释器的路径。

    Windows 放在 Scripts\\,其余平台放在 bin/ —— 这是 venv 模块自己的约定,
    写死任何一边都会在另一边挂掉。
    """
    if os.name == "nt":
        return VENV_DIR / "Scripts" / "python.exe"
    return VENV_DIR / "bin" / "python"


def check_python_version() -> bool:
    """检查当前解释器的版本是否落在可用区间内。"""
    current = sys.version_info[:2]
    if MIN_PYTHON <= current < MAX_PYTHON:
        return True

    minimum = ".".join(str(part) for part in MIN_PYTHON)
    maximum = ".".join(str(part) for part in MAX_PYTHON)
    actual = ".".join(str(part) for part in sys.version_info[:3])

    say()
    say(f"✗ Python 版本不合适:当前是 {actual},需要 {minimum} ~ {maximum}。")
    say()
    say("  界面库 PySide6 的安装要求写死了 >=3.10,<3.15,低于或高于这个")
    say("  区间都装不上。与其让你看到一堆 pip 报错,不如在这里直接讲清楚。")
    say()
    say("  Windows:去 https://www.python.org/downloads/ 装一个 3.12。")
    say('           安装第一步务必勾选 "Add python.exe to PATH",')
    say("           漏勾的话命令行里找不到 python,还得重装一遍。")
    say()
    say("  macOS:  brew install python@3.12")
    say()
    return False


def ensure_venv() -> Path | None:
    """准备好 venv,返回它的解释器路径;失败返回 None。"""
    python = venv_python()

    if python.exists():
        return python

    # venv 目录在,但解释器不在 —— 最可能的原因是整个包被从别的系统
    # 拷了过来(macOS 上建的 .venv 里是 Mach-O 二进制,在 Windows 上
    # 根本执行不了)。这种情况必须说清楚,否则同事会对着一个
    # 「找不到文件」的报错查半天。
    if VENV_DIR.exists():
        say()
        say(f"✗ {VENV_DIR.name} 目录存在,但里面的 Python 解释器不见了:")
        say(f"    {python}")
        say()
        say("  多半是从别的电脑(或别的系统)整个拷贝过来的 —— venv 不能跨")
        say("  平台搬,它里面是平台相关的二进制文件。")
        say()
        say(f"  解决办法:把 {VENV_DIR.name} 整个删掉,重新运行本程序即可。")
        say()
        return None

    say("首次运行,正在创建独立的运行环境(只需一次)……")
    try:
        # 用 venv 模块建,而不是手工造目录结构:里面的路径、激活脚本、
        # python 配置都是它按当前平台生成的,自己拼容易漏东西。
        subprocess.run(
            [sys.executable, "-m", "venv", str(VENV_DIR)],
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        say()
        say(f"✗ 创建虚拟环境失败(退出码 {exc.returncode})。")
        say()
        say("  常见原因:系统 Python 没装 venv 模块。")
        say("  在 Ubuntu / Debian 上执行: sudo apt install python3-venv")
        say()
        return None
    except OSError as exc:
        say()
        say(f"✗ 创建虚拟环境失败:{exc}")
        say()
        return None

    if not python.exists():
        say(f"✗ 虚拟环境建好了,但没找到解释器:{python}")
        return None
    return python


def requirements_hash() -> str:
    """算出 requirements.txt 的指纹,用来判断依赖要不要重装。"""
    try:
        content = REQUIREMENTS.read_bytes()
    except OSError:
        return ""
    return hashlib.sha256(content).hexdigest()


def deps_look_ready() -> bool:
    """依赖是否已经装好且是最新的。

    指纹里带上解释器版本:万一有人换了 Python 重建环境,旧戳不该继续生效。
    """
    if not STAMP_FILE.exists():
        return False
    expected = f"{requirements_hash()}\n{sys.version_info[:2]}\n"
    try:
        return STAMP_FILE.read_text(encoding="utf-8") == expected
    except OSError:
        return False


def write_stamp() -> None:
    try:
        STAMP_FILE.write_text(
            f"{requirements_hash()}\n{sys.version_info[:2]}\n", encoding="utf-8"
        )
    except OSError:
        # 写不了就算了,大不了下次重启再装一遍,不值得为此中断启动
        pass


def install_deps(python: Path, extra: tuple = ()) -> bool:
    """把依赖装进 venv。已经是最新的就跳过。"""
    if not extra and deps_look_ready():
        say("依赖已就绪,跳过安装。")
        return True

    if not REQUIREMENTS.is_file():
        say(f"✗ 找不到依赖清单:{REQUIREMENTS}")
        return False

    say("正在安装依赖,第一次需要几分钟(要下载界面库,约 110MB)……")
    command = [
        str(python), "-m", "pip", "install",
        "--disable-pip-version-check",
        "-r", str(REQUIREMENTS),
        *extra,
    ]

    if _run_pip(command):
        write_stamp()
        return True

    # 第一步失败最常见的原因是 pip 太老,看不懂新版本 wheel 的标签。
    # 升级 pip 再试一次,比直接甩一段红字给同事强。
    say()
    say("安装失败,正在升级 pip 后重试……")
    if _run_pip([str(python), "-m", "pip", "install", "--upgrade", "pip"]):
        if _run_pip(command):
            write_stamp()
            return True

    # 还是不行,多半是网络到 pypi.org 不通或者太慢。换国内镜像再试一次 ——
    # 这一步只在前两次都失败之后才走,不影响网络正常的机器。
    say()
    say("仍未成功,正在改用国内镜像源重试……")
    if _run_pip(command + ["-i", MIRROR_INDEX]):
        write_stamp()
        return True

    say()
    say("✗ 依赖安装失败。请检查网络连接,或手工执行下面这条命令看看具体报错:")
    say()
    say(f"  {python} -m pip install -r {REQUIREMENTS.name}")
    say()
    say("  如果公司网络需要走代理,要先配置 pip 的代理设置。")
    say()
    return False


def _run_pip(command: list) -> bool:
    """跑一条 pip 命令,返回是否成功。输出直接透传给用户。"""
    try:
        subprocess.run(command, check=True)
        return True
    except subprocess.CalledProcessError:
        return False
    except OSError as exc:
        say(f"✗ 无法执行 pip:{exc}")
        return False


def launch_app(python: Path) -> int:
    """启动界面程序,把它的退出码原样传回去。"""
    say()
    say("正在启动界面……")
    say()
    try:
        completed = subprocess.run(
            [str(python), "-m", "rpm_verifier.app"],
            cwd=str(PACKAGE_DIR),
        )
        return completed.returncode
    except KeyboardInterrupt:
        return EXIT_OK
    except OSError as exc:
        say(f"✗ 启动失败:{exc}")
        return EXIT_ENV_ERROR


def run_selftest(python: Path) -> int:
    """自检:装 pytest 并跑一遍测试。

    同事遇到问题时报的这一条命令,能一刀切开「环境没装好」和「代码有 bug」
    —— 这两种情况的处理方式完全不同,不先分清就只能瞎猜。
    """
    say("=" * 56)
    say("自检模式:会额外安装 pytest 并运行测试套件")
    say("=" * 56)
    say()

    # tests/ 只在开发机本地保留,不进 GitHub。从仓库 clone 的目录、
    # 或在不带 tests/ 的机器上构建的包,都没有这个目录。直接跑 pytest
    # 会得到一句 pytest 的 "file or directory not found",对同事毫无帮助。
    if not (PACKAGE_DIR / "tests").is_dir():
        say("✗ 当前目录不含 tests/,无法自检。")
        say("  tests/ 只在开发机本地保留;请在有 tests/ 的开发机上运行测试。")
        return EXIT_ENV_ERROR

    if not install_deps(python, extra=("pytest",)):
        return EXIT_ENV_ERROR

    say()
    say("正在运行测试……")
    say()
    try:
        completed = subprocess.run(
            [str(python), "-m", "pytest", "tests/", "-q"],
            cwd=str(PACKAGE_DIR),
        )
    except OSError as exc:
        say(f"✗ 无法运行测试:{exc}")
        return EXIT_ENV_ERROR

    say()
    if completed.returncode == 0:
        say("✓ 自检通过:环境正常,代码正常。")
    else:
        say("✗ 自检失败。请把上面的输出完整发出来。")
    return completed.returncode


def main(argv: list) -> int:
    # 顺序很重要:编码要在**任何输出之前**配好,否则第一句中文就可能炸。
    force_utf8_output()

    if not check_python_version():
        return EXIT_ENV_ERROR

    python = ensure_venv()
    if python is None:
        return EXIT_ENV_ERROR

    if "--selftest" in argv:
        return run_selftest(python)

    if not install_deps(python):
        return EXIT_ENV_ERROR

    return launch_app(python)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
