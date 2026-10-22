"""构建分发包:把源码打成一个同事解压就能用的 zip。

用法(在项目根目录):
    .venv/bin/python packaging/build_release.py

做四件事:

1. 从当前 .venv 读出**实际装着的**版本,生成锁定版 requirements.txt
2. 按白名单把源码拷进 dist/rpm-verifier-<版本>/
3. 生成给同事看的《使用说明.md》
4. 打成 dist/rpm-verifier-<版本>.zip

为什么是白名单而不是「拷贝全部再排除」:排除法只要漏掉一条规则
(.venv、__pycache__、.DS_Store……),就会把不该带的东西带出去。这个项目的
.venv 是 1.2G 的 macOS 二进制,里面还有整个 QtWebEngine —— 混进分发包的话,
同事拿到的是个装不上的东西,而且是几百兆的垃圾。白名单漏了顶多少带一个文件,
代价小得多。

只依赖标准库。`requires-python` 之类的花活不在这里做,那是启动器的事。
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DIST_DIR = PROJECT_ROOT / "dist"

# 分发包的根目录名,也是 zip 里的顶层目录
PACKAGE_NAME = "rpm-verifier"

# ---------------------------------------------------------------------------
# 要打进去的东西(白名单)
# ---------------------------------------------------------------------------
# 每一项是相对项目根的路径。目录会被递归拷贝,但下面 EXCLUDE_NAMES 里的
# 名字在任何层级都会被跳过。
INCLUDE = (
    "rpm_verifier",     # 程序本体
    "assets",           # 图标与(可选)字体
    "tests",            # 保留,同事可以用 launcher.py --selftest 自检
    "scripts",          # 重新生成图标用
    "launcher.py",      # 启动器
    "启动.bat",          # Windows 入口
    "启动.command",      # macOS 入口
    "README.md",        # 开发者文档
    # requirements.txt 和 使用说明.md 是**生成**的,不在这里列
)

# 这些名字出现在任何层级都跳过
EXCLUDE_NAMES = frozenset({
    "__pycache__",
    ".pytest_cache",
    ".DS_Store",
    ".venv",
    "venv",
    ".git",
    ".idea",
    ".vscode",
})

EXCLUDE_SUFFIXES = (".pyc", ".pyo", ".pyd")

# 依赖里要锁定的包。写的是**分发包实际安装**的名字 ——
# 注意这里是 PySide6-Essentials 而不是 PySide6:
# 元包 PySide6 会把 QtWebEngine 之类的 Addons 一起拖下来(仅 QtWebEngineCore
# 一个就有 589MB,整个 .venv 因此到 1.2G),而本程序只用到 QtCore / QtGui /
# QtWidgets —— 全都在 Essentials 里。实测只装 Essentials 是 357MB,
# 相差三倍多,对一个「同事自己下载安装」的分发包来说这个差别很实在。
LOCKED_PACKAGES = ("PySide6-Essentials", "httpx")


def read_installed_version(package: str) -> str:
    """从当前环境读出某个包实际装着的版本。

    读的是**当前环境**,不是 requirements.txt 里写的范围 —— 范围是 `>=6.6`
    这种,照着写进分发包等于没锁,同事装到的版本可能和验证过的完全不同。
    """
    try:
        completed = subprocess.run(
            [sys.executable, "-m", "pip", "show", package],
            capture_output=True, text=True, check=True,
        )
    except (subprocess.CalledProcessError, OSError) as exc:
        raise SystemExit(f"✗ 读不出 {package} 的版本,请确认它在当前环境里:{exc}")

    for line in completed.stdout.splitlines():
        if line.startswith("Version:"):
            return line.split(":", 1)[1].strip()

    raise SystemExit(f"✗ {package} 的输出里没有 Version 字段")


def read_version() -> str:
    """从 rpm_verifier/__init__.py 读出 __version__。

    故意用最笨的办法(找一行、切字符串)而不是 import:import 会真的把包
    加载起来,连带把 PySide6 也拖进来 —— 一个纯粹打包的脚本不该有这种依赖。
    """
    init_file = PROJECT_ROOT / "rpm_verifier" / "__init__.py"
    for line in init_file.read_text(encoding="utf-8").splitlines():
        if line.startswith("__version__"):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise SystemExit("✗ 在 rpm_verifier/__init__.py 里找不到 __version__")


def should_skip(path: Path) -> bool:
    if any(part in EXCLUDE_NAMES for part in path.parts):
        return True
    return path.suffix in EXCLUDE_SUFFIXES


def copy_item(source: Path, target: Path) -> int:
    """把一个文件或目录拷进目标位置,返回拷了几个文件。"""
    if source.is_dir():
        count = 0
        for item in sorted(source.rglob("*")):
            if should_skip(item.relative_to(source)):
                continue
            destination = target / item.relative_to(source)
            if item.is_dir():
                destination.mkdir(parents=True, exist_ok=True)
            else:
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(item, destination)
                count += 1
        return count

    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    return 1


def build_requirements(package_dir: Path) -> str:
    """生成锁定版 requirements.txt,返回内容。"""
    lines = [
        "# 本文件由 packaging/build_release.py 自动生成,请勿手工修改。",
        "#",
        "# 版本是构建时从开发机上**实际安装的**环境里读出来的,写成 == 精确锁死。",
        "# 不用 >= 是因为:同事装到的版本必须和我们验证过的一模一样,",
        "# 否则「在我这儿好好的」就会变成一句空话。",
        "#",
        "# 用 PySide6-Essentials 而不是 PySide6:后者会把 QtWebEngine 等",
        "# Addons 一并拖下来(QtWebEngineCore 单个 589MB),而本程序只用到",
        "# QtCore / QtGui / QtWidgets,全在 Essentials 里。",
        "",
    ]
    for package in LOCKED_PACKAGES:
        lines.append(f"{package}=={read_installed_version(package)}")

    content = "\n".join(lines) + "\n"
    (package_dir / "requirements.txt").write_text(content, encoding="utf-8")
    return content


def build_readme(package_dir: Path, version: str) -> None:
    """生成给同事看的《使用说明.md》。"""
    content = f"""# LLM API 速率验证工具 v{version}

一个本地小工具:按你指定的速率(RPM)向 OpenAI 兼容 API 发空跑请求,
测出实际能跑到的速率,并统计各个 HTTP 状态码出现的次数。

产品定位是**验证器,不是探测器** —— 你填入一个假设值(「我这个 key 应该
能跑 100 RPM」),工具按这个值跑一遍,然后如实报告发生了什么。
它不自动调速,也不给「达标 / 未达标」的结论,判断权在你手上。

## 怎么用

### Windows

双击 **`启动.bat`**。

第一次运行会自动建环境、下载依赖,需要几分钟(界面库有 200MB 左右),
之后再启动就是几秒钟的事。装依赖需要能访问外网。

### macOS

双击 **`启动.command`**。

如果双击没反应,或者提示「无法打开,因为它来自身份不明的开发者」,
在终端里执行一次(把路径换成你解压的位置):

```bash
cd <解压出来的目录>
xattr -d com.apple.quarantine 启动.command
```

还是不行的话,直接在终端里跑:

```bash
python3 launcher.py
```

### 前置条件

只需要机器上有一个 **Python 3.10 ~ 3.14**。其余的东西(虚拟环境、依赖)
启动器会自己搞定,不会污染系统里的 Python 环境。

Windows 上装 Python 时,**务必勾选安装界面下方的 "Add python.exe to PATH"**,
漏勾的话命令行里找不到 python,只能卸载重装。

## 界面怎么填

1. **Base URL** —— API 地址。缺 `/v1` 会自动补全
2. **API Key**
3. **模型名**
4. **模态** —— 文本 / 图像 / 视频 / 图生图 / 图生视频 / 视频生视频,端点和参数都不一样
5. **源素材**(仅图生图 / 图生视频 / 视频生视频)—— 图生图支持公开图片 URL
   或本地图片;图生视频 / 视频生视频只接受公开 URL
6. **目标 RPM** 和**测试时长**(默认 60 秒)
7. 点「开始测试」

测试期间所有配置项会锁住,结束后展示实际 RPM 和状态码统计表。

配置卡片下方的**「请求地址」**会实时显示最终拼出来的完整端点 ——
地址填错的时候,这是最快能看出问题的地方。

## 六种模态的区别

| 模态 | 端点 | 响应形态 | 默认参数 |
|---|---|---|---|
| 文本 | `/chat/completions` | 同步 | `max_tokens=1` |
| 图像 | `/images/generations` | **同步**(生成完才返回) | `size=1K` |
| 视频 | `/videos` | **异步**(立刻返回排队任务) | `seconds=4`, `size=720P` |
| 图生图 | `/images/generations` | **同步** | `size=1K` + 输入图 |
| 图生视频 | `/videos` | **异步** | `seconds=4`, `size=720P` + 首帧 URL |
| 视频生视频 | `/videos` | **异步** | `seconds=4`, `size=720P` + 参考视频 URL |

⚠️ **默认参数一律取最低成本档。** 图像按张、视频按秒计费,把 60 秒的测试
按高分辨率跑满,账单会很难看。界面上可以调,但默认值是保守的。

⚠️ **视频类测的是「创建任务」这一步的限流**,不是视频生成的吞吐。异步接口
只负责接单,真正的生成在服务端后台跑 —— 这是两件完全不同的事。

⚠️ **图生视频 / 视频生视频只接受可公开访问的素材 URL。** 服务端要自己下载
素材,本地文件用不了;图生图可以用本地图片,因为它能编码成 Data URI。
源素材不落盘,报告只显示脱敏摘要。

## 状态码表怎么读

负数**不是** HTTP 状态码,是「压根没收到响应」的几种情况:

| 值 | 显示 | 含义 | 该怪谁 |
|---|---|---|---|
| -1 | 响应超时 | 连上了,但服务器没在超时时间内回话 | 可能是 API 过载(限流信号) |
| -2 | 连接错误 | TCP 层就没通(DNS 失败、拒绝连接) | 网络 / Base URL |
| -3 | 未返回 | 窗口结束时仍未回来,被取消 | 接口响应太慢 |
| -4 | 连接超时 | 连 TCP 都超时了 | 网络 / Base URL |
| -5 | 连接池耗尽 | 本机并发连接不够用 | **本机**,与 API 无关 |

区分它们是有意义的:把「连不上」记成 5xx,就会误以为是被限流了。

## 出问题了怎么办

先跑一次自检:

```
Windows:      启动.bat --selftest
macOS:        python3 launcher.py --selftest
```

它会装好测试依赖并跑一遍测试套件。这一步能一刀切开两种情况:

- **测试全过** → 环境没问题,是你填的 API 地址 / key / 模型名 的问题
- **测试失败** → 环境装坏了,把输出完整发出来

### 几个常见现象

**依赖下载很慢或卡住** —— 界面库有 110MB,国内直连 pypi.org 可能很慢。
启动器在两次失败后会自动改用清华镜像重试,不用你管。如果还是不行,
手工执行(在包目录下):

```
macOS:    .venv/bin/python -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
Windows:  .venv\\Scripts\\python.exe -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
```

装完再重新双击启动文件即可。

**「探针请求失败(400 / 404)」** —— 弹窗里会带上**服务器原话**和**实际请求
地址**。先看请求地址对不对,再看服务器怎么说。

**测试跑起来整片 400** —— 请求体里有字段不符合对方要求。报告里会附上服务器
对请求的说明。

**429 特别多** —— 这不是故障,这正是本工具要观测的东西。429 越多,说明越
接近真实上限。

**连接池耗尽(-5)** —— 目标速率定太高,本机端口/连接不够用了。调小一点。

## 想自己看看代码

```bash
# 跑测试(需要先在包目录下执行过至少一次启动,建好 .venv)
python3 launcher.py --selftest
```

项目结构、设计规则、以及一堆踩过的坑,都写在 `README.md` 里。

## 这个包不含什么

- 不带 `.venv` —— 那是在你自己的机器上现建的,跨平台搬不动
- 不带任何 API Key,配置只存在本机(`~/.config/rpm_verifier/config.json`)
- 不上传任何数据,所有请求直接从你的机器发往你填的那个 API 地址
"""
    (package_dir / "使用说明.md").write_text(content, encoding="utf-8")


def write_zip(package_dir: Path, zip_path: Path) -> None:
    """打包成 zip。

    ⚠️ 必须手工设置 启动.command 的可执行位。zipfile 默认不写 Unix 权限,
    打出来的 .command 在 macOS 上会变成「没有执行权限」——双击毫无反应,
    而且不报任何错,非常难查。
    """
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for item in sorted(package_dir.rglob("*")):
            if item.is_dir():
                continue

            relative = item.relative_to(package_dir.parent)
            info = zipfile.ZipInfo(str(relative).replace("\\", "/"))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16

            # 入口脚本要可执行:macOS 的 .command 靠这个位才能双击运行
            if item.suffix == ".command" or item.name == "launcher.py":
                info.external_attr = 0o100755 << 16

            archive.writestr(info, item.read_bytes())


def main() -> int:
    version = read_version()
    package_dir = DIST_DIR / f"{PACKAGE_NAME}-{version}"

    print(f"构建 rpm-verifier v{version}")
    print(f"  源码目录: {PROJECT_ROOT}")

    # 每次从零开始,避免上一次构建的残留文件混进来
    if package_dir.exists():
        shutil.rmtree(package_dir)
    package_dir.mkdir(parents=True, exist_ok=True)

    # --- 拷源码 ---
    total = 0
    missing = []
    for name in INCLUDE:
        source = PROJECT_ROOT / name
        if not source.exists():
            missing.append(name)
            continue
        count = copy_item(source, package_dir / name)
        total += count
        print(f"  + {name:<16} {count:>3} 个文件")

    if missing:
        print(f"\n⚠️ 以下条目不存在,已跳过: {', '.join(missing)}")

    # --- 生成锁定依赖 ---
    requirements = build_requirements(package_dir)
    locked = [line for line in requirements.splitlines() if "==" in line]
    print(f"  + requirements.txt  {' / '.join(locked)}")

    # --- 生成使用说明 ---
    build_readme(package_dir, version)
    print("  + 使用说明.md")

    # --- 打包 ---
    zip_path = DIST_DIR / f"{PACKAGE_NAME}-{version}.zip"
    write_zip(package_dir, zip_path)

    size_mb = zip_path.stat().st_size / (1024 * 1024)
    print(f"\n✓ 共 {total} 个源文件")
    print(f"✓ 目录: {package_dir}")
    print(f"✓ 压缩包: {zip_path}  ({size_mb:.1f} MB)")
    print("\n把这个 zip 发给同事即可。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
