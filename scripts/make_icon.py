"""生成应用图标:一个深蓝渐变底 + 白色仪表盘弧线 + 琥珀色指针。

画的是「速率表」,正好对应这个工具在干的事。

输出到 assets/,一次生成三种格式:

    icon.png    单张 512×512,给 Linux / 兜底用
    icon.ico    Windows:16/24/32/48/64/128/256 多尺寸打包
    icon.icns   macOS:Dock 与 Finder

🔁 可重复运行:每次都是先重新画、再覆盖 assets/ 下的旧文件,没有中间状态。
需要改图标长相时,只改 `make_icon()` 里的绘制代码,三种格式会自动跟着变。

为什么要手写 ICO 打包(Qt 明明能写 ico):实测 Qt 的 ICO writer 只写**单个**
尺寸(文件头里 count=1),而且是未压缩的 BGRA —— 一张 256×256 就 270KB。
Windows 任务栏要 16px、资源管理器要 32/48、属性面板要 256,全靠系统从一张
图上缩,小尺寸会糊。这里按 ICO 规范把多个尺寸塞进同一个文件,每张用 PNG
压缩(Vista 以后的 ICO 支持 PNG 载荷),Windows 各取所需,文件反而更小。
"""

from __future__ import annotations

import math
import os
import struct
from pathlib import Path

# 必须在 import QtGui 之前设置:这是个纯离线的生成脚本,不该要求有显示器。
# 不设的话,在没装 X11 的 Linux CI 上会直接起不来。
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QBuffer, QByteArray, QPointF, QRectF, Qt  # noqa: E402
from PySide6.QtGui import (  # noqa: E402
    QColor,
    QGuiApplication,
    QImage,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
)

# 输出目录固定为项目根的 assets/,和 styles.py 找字体的地方是同一个。
# ⚠️ 这里原先写死的是 /tmp/rpm.iconset —— 那是给 macOS 的 iconutil 用的。
# /tmp 在 Windows 上根本不存在,同事跑这个脚本会直接失败。
OUT_DIR = Path(__file__).resolve().parents[1] / "assets"

# macOS 图标的圆角半径约为边长的 22.5%
CORNER_RATIO = 0.225
# 四周留白,避免图标顶满画布
MARGIN_RATIO = 0.075

# ICO 里打包的尺寸。Windows 会在不同场合各取所需:
# 16 任务栏 / 24 小图标视图 / 32 桌面快捷方式 / 48 中等图标 /
# 256 大图标与属性面板。
ICO_SIZES = (16, 24, 32, 48, 64, 128, 256)

# icon.png 的尺寸。512 足够任何场合,再大只是徒增体积。
PNG_SIZE = 512

# icns 的尺寸。Qt 的 icns writer 会自行挑选可用的尺寸,多给几个没坏处。
ICNS_SIZE = 512


def make_icon(size: int) -> QImage:
    """画出指定边长的图标。

    全部尺寸都用矢量方式重画,而不是画一张大的再缩 —— 16px 下缩放会把
    指针和弧线糊成一团,重画才能在小尺寸下保持清晰。
    """
    image = QImage(size, size, QImage.Format_ARGB32)
    image.fill(Qt.transparent)

    painter = QPainter(image)
    painter.setRenderHint(QPainter.Antialiasing, True)

    # --- 圆角矩形底 ---
    margin = size * MARGIN_RATIO
    rect = QRectF(margin, margin, size - 2 * margin, size - 2 * margin)
    radius = rect.width() * CORNER_RATIO

    path = QPainterPath()
    path.addRoundedRect(rect, radius, radius)

    gradient = QLinearGradient(rect.topLeft(), rect.bottomRight())
    gradient.setColorAt(0.0, QColor("#3B82F6"))
    gradient.setColorAt(1.0, QColor("#1E3A8A"))
    painter.fillPath(path, gradient)

    # --- 仪表盘弧线:从 210° 顺时针扫到 -30°,共 240° ---
    center = QPointF(size / 2, size * 0.60)
    arc_radius = size * 0.27
    arc_rect = QRectF(
        center.x() - arc_radius,
        center.y() - arc_radius,
        arc_radius * 2,
        arc_radius * 2,
    )

    painter.setPen(
        QPen(QColor(255, 255, 255, 95), size * 0.055, Qt.SolidLine, Qt.RoundCap)
    )
    painter.drawArc(arc_rect, 210 * 16, -240 * 16)

    # --- 指针:指向约 65% 的位置 ---
    angle_deg = 210 - 240 * 0.65
    angle = math.radians(angle_deg)
    tip = QPointF(
        center.x() + arc_radius * 0.82 * math.cos(angle),
        center.y() - arc_radius * 0.82 * math.sin(angle),
    )
    painter.setPen(
        QPen(QColor("#FBBF24"), size * 0.05, Qt.SolidLine, Qt.RoundCap)
    )
    painter.drawLine(center, tip)

    # --- 轴心圆点 ---
    hub = size * 0.045
    painter.setPen(Qt.NoPen)
    painter.setBrush(QColor("#FBBF24"))
    painter.drawEllipse(center, hub, hub)

    painter.end()
    return image


def to_png_bytes(image: QImage) -> bytes:
    """把 QImage 编码成 PNG 字节串。"""
    # ⚠️ QBuffer 必须留在 Python 变量里。写成 QImage.save(QBuffer(ba), "PNG")
    # 的话,临时的 QBuffer 会在这一行结束时被 GC 回收,而 Qt 那边还握着它的
    # 指针 —— 表现是段错误(退出码 139),不是异常,堆栈里什么都看不到。
    # 这个坑在原型阶段踩过一次,别再把 buffer 写成临时的。
    buffer = QBuffer()
    buffer.open(QBuffer.WriteOnly)
    image.save(buffer, "PNG")
    return bytes(buffer.data())


def pack_ico(images: list[tuple[int, QImage]]) -> bytes:
    """把多个尺寸打包成一个多尺寸 .ico。

    ICO 文件结构(全是小端):

        ICONDIR      6 字节   保留位(0) + 类型(1=图标) + 图像数量
        ICONDIRENTRY 16 字节×N 每张图的宽高/色深/数据大小/数据偏移
        <图像数据>             这里每张都是 PNG 字节流

    宽高字段是 1 字节,所以 256 只能用 0 表示 —— 这是规范里的约定,不是笔误。
    """
    payloads = [to_png_bytes(image) for _, image in images]

    header = struct.pack("<HHH", 0, 1, len(images))

    offset = len(header) + 16 * len(images)
    entries = bytearray()
    for (size, _), payload in zip(images, payloads):
        entries += struct.pack(
            "<BBBBHHII",
            size if size < 256 else 0,  # 宽
            size if size < 256 else 0,  # 高
            0,                          # 调色板颜色数(真彩色填 0)
            0,                          # 保留位
            1,                          # 颜色平面数
            32,                         # 每像素位数
            len(payload),               # 该图像数据的字节数
            offset,                     # 该图像数据的起始偏移
        )
        offset += len(payload)

    return bytes(header + bytes(entries) + b"".join(payloads))


def write_png(path: Path, image: QImage) -> None:
    with open(path, "wb") as handle:
        handle.write(to_png_bytes(image))


def write_icns(path: Path, image: QImage) -> None:
    """写 .icns。Qt 自带 icns writer,不需要 macOS 专有的 iconutil。

    这一条很关键:原来生成 macOS 图标要调 `iconutil`,那是 macOS 独有的
    命令行工具 —— 意味着图标只能在 Mac 上生成。改用 Qt 之后三个平台都能
    重画图标。
    """
    # QImage.save 按扩展名选格式,所以这里必须用真实路径而不是 QBuffer。
    if not image.save(str(path), "ICNS"):
        raise RuntimeError(f"写入 {path.name} 失败")


def main() -> None:
    app = QGuiApplication([])  # noqa: F841  QPainter 需要一个 QGuiApplication

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    # --- icon.png ---
    write_png(OUT_DIR / "icon.png", make_icon(PNG_SIZE))
    print(f"  icon.png    {PNG_SIZE}×{PNG_SIZE}")

    # --- icon.ico(多尺寸) ---
    ico_path = OUT_DIR / "icon.ico"
    with open(ico_path, "wb") as handle:
        handle.write(pack_ico([(size, make_icon(size)) for size in ICO_SIZES]))
    sizes_text = " / ".join(str(size) for size in ICO_SIZES)
    print(f"  icon.ico    {sizes_text}")

    # --- icon.icns ---
    write_icns(OUT_DIR / "icon.icns", make_icon(ICNS_SIZE))
    print(f"  icon.icns   {ICNS_SIZE}×{ICNS_SIZE}")

    print(f"\n已生成 3 个图标文件 -> {OUT_DIR}")


if __name__ == "__main__":
    main()
