"""程序入口。

运行方式:
    python -m rpm_verifier.app
"""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

from rpm_verifier.ui import styles
from rpm_verifier.ui.main_window import MainWindow

# 各平台优先用哪种图标格式。.icns / .ico 里打包了多个尺寸,系统在 Dock、
# 任务栏、Alt-Tab 里会各取所需,比单张 PNG 缩出来的清晰。
_NATIVE_ICON = {
    "darwin": "icon.icns",
    "win32": "icon.ico",
}


def load_app_icon() -> QIcon | None:
    """加载 assets/ 下的应用图标。找不到就返回 None,不报错。

    刻意做成防御式的:图标只是门面,缺了它程序照样能用。为了让任务栏好看
    一点而让整个程序起不来,那是本末倒置。
    """
    assets = Path(__file__).resolve().parents[1] / "assets"

    candidates = [
        _NATIVE_ICON.get(sys.platform),  # 本平台原生格式
        "icon.png",                      # 兜底:PNG 哪都能读
        "icon.ico",
        "icon.icns",
    ]
    for name in candidates:
        if not name:
            continue
        path = assets / name
        if not path.is_file():
            continue
        icon = QIcon(str(path))
        if not icon.isNull():
            return icon
    return None


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("LLM API 速率验证工具")

    # 在 setStyle 之前设图标:图标属于窗口装饰,Fusion 样式不该有机会插手。
    icon = load_app_icon()
    if icon is not None:
        app.setWindowIcon(icon)

    # macOS 原生样式会自己绘制 QLineEdit / QPushButton 的底色,把 QSS 里的
    # background 吃掉。Fusion 把绘制权完全交给样式表 —— 我们既然在实现一套
    # 自定设计系统,就不该让系统样式插手。
    app.setStyle("Fusion")

    # 顺序不能反:QFontDatabase 必须在 QApplication 构造之后才能用,
    # 而样式表里的 font-family 又依赖于字体是否已加载。
    styles.load_bundled_fonts()
    app.setStyleSheet(styles.build_stylesheet())

    window = MainWindow()
    window.show()

    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
