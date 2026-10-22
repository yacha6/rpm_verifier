"""设计系统令牌与样式表。

把 shadcn/ui 风格规范翻译成 Qt 能理解的形式。

Qt QSS 相比 CSS 有三个硬性缺口,这里都做了降级处理:

1. **不支持 box-shadow。** 规范里的卡片阴影是「1px hairline + 1px + 2px」的
   叠加,Qt 画不出来。好在规范本身强调 "Do not skip the 1px hairline border
   on cards —— the shadow alone does not define the card edge",所以保留 1px
   边框、靠 画布(#f5f5f5)→ 卡片(#ffffff)的色调对比建立层次即可。
2. **不支持 letter-spacing。** 只能通过 QFont.setLetterSpacing 在代码里设置,
   见 apply_tracking()。
3. **不支持 oklab()。** 阴影色值需要换算。

规范原文里的三条原则在本文件中的体现:
- achromatic by default —— 所有色值都是无彩色,唯一的有彩色 #e7000b 只用于错误。
- radius defines hierarchy —— 交互元素 18px,容器 24px,中间值一律不用。
- elevation is whisper-quiet —— 不追求戏剧化的投影。
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtGui import QFont, QFontDatabase

# ---------------------------------------------------------------------------
# 颜色令牌
# ---------------------------------------------------------------------------
CANVAS = "#f5f5f5"       # 画布:页面背景、次要表面、输入框静息态
PAPER = "#ffffff"        # 纸张:卡片表面
SURFACE_ALT = "#fafafa"  # 次级表面:侧栏、次级卡片
INK = "#0a0a0a"          # 墨:主要文字
INK_SOFT = "#171717"     # 柔墨:填充按钮背景
MID_GRAY = "#737373"     # 中灰:次要文字、占位符
HAIRLINE = "#e5e5e5"     # 发丝线:边框
EMBER = "#e7000b"        # 余烬:仅用于错误与破坏性状态,绝不做装饰

# ---------------------------------------------------------------------------
# 字号令牌
# ---------------------------------------------------------------------------
TEXT_CAPTION = 12
TEXT_BODY = 14
TEXT_BODY_LG = 16
TEXT_SUBHEADING = 18
TEXT_HEADING_SM = 24
TEXT_HEADING = 30
TEXT_HEADING_LG = 36
TEXT_DISPLAY = 48

# ---------------------------------------------------------------------------
# 间距令牌(基准单位 4px)
# ---------------------------------------------------------------------------
SPACE_4 = 4
SPACE_8 = 8
SPACE_12 = 12
SPACE_16 = 16
SPACE_20 = 20
SPACE_24 = 24
SPACE_48 = 48

# ---------------------------------------------------------------------------
# 圆角令牌 —— 只有两档:交互 18px,容器 24px
# ---------------------------------------------------------------------------
RADIUS_INTERACTIVE = 18
RADIUS_CONTAINER = 24
# 第三档是破例的:端点预览是一个小代码块,不是卡片,套 24px 会显得圆过头。
# 规范里的圆角阶梯本来就允许「代码块」用更小的一档。
RADIUS_SMALL = 8

CARD_PADDING = 20

# ---------------------------------------------------------------------------
# 字体
# ---------------------------------------------------------------------------
_FONT_DIR = Path(__file__).resolve().parents[2] / "assets" / "fonts"

# 下拉框的自绘箭头。Fusion 默认那个小黑三角太重,和这套轻量表单不搭;
# 用一张 12×8 的 SVG 细线 chevron,颜色取 MID_GRAY。
_CHEVRON_PATH = (
    Path(__file__).resolve().parents[2] / "assets" / "chevron-down.svg"
).as_posix()

# 规范指定 Geist,替代方案 Inter。macOS 上 SF Pro / Helvetica Neue 同属
# 中性几何无衬线,观感接近。
#
# ⚠️ 这个栈是**跨平台**的,别只留 macOS 的字体名。原先只有 SF Pro /
# Helvetica Neue,到了 Windows 上会一路落到 Arial —— 拉丁字母勉强能看,
# 中文直接掉进系统兜底字体,和这个文件精心挑的排版完全不是一回事。
# Segoe UI 是 Windows 的系统 UI 字体(对应 macOS 的 SF Pro),
# Microsoft YaHei 负责中文字形。这两个名字在 macOS 上不存在,会被跳过,
# 所以加上它们对你自己的观感没有任何影响。
_FALLBACK_STACK = (
    "'Geist', 'Inter', "
    "'SF Pro Text', 'Helvetica Neue', "   # macOS
    "'Segoe UI', 'Microsoft YaHei', "     # Windows
    "'Noto Sans CJK SC', 'Noto Sans SC', 'WenQuanYi Micro Hei', "  # Linux
    "Helvetica, Arial, sans-serif"
)

# 等宽栈:只给端点预览这类「要逐字符比对」的地方用。URL 里的 / 和 l、
# 1 和 I 在比例字体下几乎分不出来。Menlo 是 macOS 的,Consolas 是 Windows 的,
# DejaVu Sans Mono / Liberation Mono 是大多数 Linux 桌面自带的。
MONO_STACK = (
    "'SF Mono', 'Geist Mono', Menlo, Consolas, "
    "'DejaVu Sans Mono', 'Liberation Mono', 'Courier New', monospace"
)

_loaded_families: list = []


def load_bundled_fonts() -> list:
    """加载 assets/fonts 下的字体文件,返回加载成功的字体族名。

    字体随应用走,不要求用户往系统里装字体。把 Geist 的 .ttf 丢进
    assets/fonts/ 就会被自动启用。

    必须在 QApplication 构造之后调用。
    """
    global _loaded_families
    if _loaded_families:
        return _loaded_families
    if not _FONT_DIR.is_dir():
        return []

    families: list = []
    for pattern in ("*.ttf", "*.otf"):
        for path in sorted(_FONT_DIR.glob(pattern)):
            font_id = QFontDatabase.addApplicationFont(str(path))
            if font_id != -1:
                families.extend(QFontDatabase.applicationFontFamilies(font_id))

    _loaded_families = families
    return families


def _font_stack() -> str:
    families = load_bundled_fonts()
    if not families:
        return _FALLBACK_STACK
    quoted = ", ".join(f"'{name}'" for name in families)
    return f"{quoted}, {_FALLBACK_STACK}"


def apply_tracking(label, pixels: float) -> None:
    """给控件设置字距。

    QSS 没有 letter-spacing,只能落到 QFont 上。规范的规则是:大字号收紧,
    小号大写标签略放开。
    """
    font = label.font()
    font.setLetterSpacing(QFont.AbsoluteSpacing, pixels)
    label.setFont(font)


def tighten(label, font_size: int, ratio: float = -0.02) -> None:
    """按字号比例收紧字距。

    字号越大越需要收紧,否则数字会显得松散。em 换算成 px 只能在这里算 ——
    QFont 不认 em。
    """
    apply_tracking(label, font_size * ratio)


# ---------------------------------------------------------------------------
# 样式表
# ---------------------------------------------------------------------------

def build_stylesheet() -> str:
    return f"""
    QWidget {{
        font-family: {_font_stack()};
        font-size: {TEXT_BODY}px;
        color: {INK};
    }}

    QMainWindow, QWidget#Canvas {{
        background: {CANVAS};
    }}

    /* ---- 卡片:24px 圆角 + 1px 发丝线。规范要求边框不可省略 ---- */
    QFrame#Card {{
        background: {PAPER};
        border: 1px solid {HAIRLINE};
        border-radius: {RADIUS_CONTAINER}px;
    }}

    /* ---- 文字层级 ---- */
    QLabel#Display {{
        font-size: {TEXT_HEADING}px;
        font-weight: 600;
        color: {INK};
    }}
    QLabel#CardTitle {{
        font-size: {TEXT_BODY_LG}px;
        font-weight: 600;
        color: {INK};
    }}
    QLabel#Caption {{
        font-size: {TEXT_CAPTION}px;
        font-weight: 500;
        color: {MID_GRAY};
    }}
    /* 端点预览。用等宽字体 —— URL 里的 / 和 l、1 和 I 在比例字体下很难分辨,
       而这一行存在的全部意义就是让人一眼看出地址写错在哪。 */
    QLabel#EndpointPreview {{
        font-family: {MONO_STACK};
        font-size: {TEXT_CAPTION}px;
        font-weight: 500;
        color: {INK_SOFT};
        background: {SURFACE_ALT};
        border: 1px solid {HAIRLINE};
        border-radius: {RADIUS_SMALL}px;
        padding: 8px 10px;
    }}
    /* 原始错误。和端点预览同一套代码块语言:等宽、浅底、发丝线。
       用等宽字体是因为里面会出现 ConnectError、HTTP 400、JSON 片段,
       比例字体下这些字符很容易看混。 */
    QLabel#RawError {{
        font-family: {MONO_STACK};
        font-size: {TEXT_CAPTION}px;
        color: {INK_SOFT};
        background: {SURFACE_ALT};
        border: 1px solid {HAIRLINE};
        border-radius: {RADIUS_SMALL}px;
        padding: 8px 10px;
    }}
    QLabel#StatValue {{
        font-size: {TEXT_HEADING_LG}px;
        font-weight: 600;
        color: {INK};
    }}
    QLabel#StatValueSmall {{
        font-size: {TEXT_HEADING}px;
        font-weight: 600;
        color: {INK};
    }}
    QLabel#Muted {{
        font-size: {TEXT_BODY}px;
        color: {MID_GRAY};
    }}
    QLabel#FieldLabel {{
        font-size: {TEXT_BODY}px;
        font-weight: 500;
        color: {INK};
    }}
    QLabel#Danger {{
        font-size: {TEXT_BODY}px;
        color: {EMBER};
    }}
    QLabel#RetryBody {{
        font-size: {TEXT_BODY}px;
        color: {MID_GRAY};
    }}

    /* ---- 主按钮:深色填充是系统里唯一的「反转」交互 ---- */
    QPushButton#Primary {{
        background: {INK};
        color: {SURFACE_ALT};
        border: none;
        border-radius: {RADIUS_INTERACTIVE}px;
        font-size: {TEXT_BODY}px;
        font-weight: 500;
        padding: 9px 18px;
    }}
    QPushButton#Primary:hover {{
        background: {INK_SOFT};
    }}
    QPushButton#Primary:disabled {{
        background: {HAIRLINE};
        color: {MID_GRAY};
    }}

    /* ---- 幽灵按钮:与主按钮同形状同字号,只差明度 ---- */
    QPushButton#Ghost {{
        background: {CANVAS};
        color: {INK};
        border: none;
        border-radius: {RADIUS_INTERACTIVE}px;
        font-size: {TEXT_BODY}px;
        font-weight: 500;
        padding: 9px 18px;
    }}
    QPushButton#Ghost:hover {{
        background: #ebebeb;
    }}
    QPushButton#Ghost:disabled {{
        /* 置灰时连底色一起去掉:图生视频/视频生视频的「选择文件…」按钮
           必须一眼看出点不了,而不是只把字调浅。 */
        background: transparent;
        color: {MID_GRAY};
    }}

    /* ---- 输入:静息态用灰填充,聚焦时换成 1px 描边 ---- */
    QLineEdit, QSpinBox {{
        background: {CANVAS};
        border: 1px solid transparent;
        border-radius: {RADIUS_INTERACTIVE}px;
        padding: 8px 12px;
        font-size: {TEXT_BODY}px;
        color: {INK};
        selection-background-color: {INK};
        selection-color: {SURFACE_ALT};
        placeholder-text-color: {MID_GRAY};
    }}
    QLineEdit:focus, QSpinBox:focus {{
        border: 1px solid {HAIRLINE};
    }}
    QLineEdit:disabled, QSpinBox:disabled {{
        background: {SURFACE_ALT};
        color: {MID_GRAY};
    }}

    /* ---- 下拉框:和输入框同一套灰底胶囊,只多一个自绘箭头 ----
       ⚠️ 不要把它并进上面的 QLineEdit/QSpinBox 规则。那条规则把
       selection-background-color 设成 INK(深色),弹出列表会继承这个
       选中色,表现为一条刺眼的黑色选中条。这里必须用浅灰。 */
    QComboBox {{
        background: {CANVAS};
        border: 1px solid transparent;
        border-radius: {RADIUS_INTERACTIVE}px;
        padding: 8px 32px 8px 12px;
        font-size: {TEXT_BODY}px;
        color: {INK};
        selection-background-color: {CANVAS};
        selection-color: {INK};
    }}
    QComboBox:focus {{
        border: 1px solid {HAIRLINE};
    }}
    QComboBox:disabled {{
        background: {SURFACE_ALT};
        color: {MID_GRAY};
    }}
    QComboBox::drop-down {{
        subcontrol-origin: padding;
        subcontrol-position: center right;
        width: 28px;
        border: none;
        background: transparent;
    }}
    QComboBox::down-arrow {{
        image: url("{_CHEVRON_PATH}");
        width: 10px;
        height: 6px;
    }}
    QComboBox::down-arrow:disabled {{
        image: url("{_CHEVRON_PATH}");
    }}
    /* 弹出列表也用纸张 + 发丝线,不要系统默认的白底方角菜单。 */
    QComboBox QAbstractItemView {{
        background: {PAPER};
        border: 1px solid {HAIRLINE};
        border-radius: {RADIUS_SMALL}px;
        padding: 4px;
        outline: none;
        selection-background-color: {CANVAS};
        selection-color: {INK};
    }}
    QComboBox QAbstractItemView::item {{
        min-height: 28px;
        padding: 4px 10px;
        border-radius: {RADIUS_SMALL}px;
    }}
    QComboBox QAbstractItemView::item:selected {{
        background: {CANVAS};
        color: {INK};
    }}
    /* 弹出列表是一个独立窗口,不在 QComboBox 的子树里,所以上面的后代
       选择器不一定命中。再按它的真实类和 QListView 兜一层 —— 否则会看到
       系统默认的白底 + 黑色选中条,和整套界面格格不入。 */
    QComboBoxPrivateContainer {{
        background: {PAPER};
        border: 1px solid {HAIRLINE};
        border-radius: {RADIUS_SMALL}px;
    }}
    QListView {{
        background: {PAPER};
        border: none;
        padding: 4px;
        outline: none;
        selection-background-color: {CANVAS};
        selection-color: {INK};
    }}
    QListView::item {{
        min-height: 28px;
        padding: 4px 10px;
        border-radius: {RADIUS_SMALL}px;
    }}
    QListView::item:selected {{
        background: {CANVAS};
        color: {INK};
    }}

    QSpinBox::up-button, QSpinBox::down-button {{
        width: 16px;
        background: transparent;
        border: none;
    }}
    QSpinBox::up-arrow, QSpinBox::down-arrow {{
        width: 7px;
        height: 7px;
    }}

    /* ---- 胶囊按钮(API Key 的显示/隐藏) ---- */
    QToolButton#Pill {{
        background: {CANVAS};
        border: none;
        border-radius: {RADIUS_INTERACTIVE}px;
        padding: 6px 10px;
        font-size: {TEXT_CAPTION}px;
        color: {INK};
    }}
    QToolButton#Pill:hover {{
        background: #ebebeb;
    }}
    QToolButton#Pill:checked {{
        background: {INK_SOFT};
        color: {SURFACE_ALT};
    }}

    /* ---- 折叠开关:纯文字,无边框 ---- */
    QToolButton#Disclosure {{
        background: transparent;
        border: none;
        color: {INK};
        font-size: {TEXT_BODY}px;
        font-weight: 500;
        padding: 4px 0;
        text-align: left;
    }}

    /* ---- 进度条:细胶囊,填充用墨色 ---- */
    QProgressBar {{
        background: {CANVAS};
        border: none;
        border-radius: 4px;
        text-align: center;
        color: transparent;
    }}
    QProgressBar::chunk {{
        background: {INK};
        border-radius: 4px;
    }}

    /* ---- 表格:去掉网格线,只留发丝分隔 ---- */
    QTableWidget {{
        background: transparent;
        border: none;
        gridline-color: transparent;
        font-size: {TEXT_BODY}px;
        outline: none;
    }}
    QTableWidget::item {{
        padding: 9px 8px;
        border-bottom: 1px solid #f0f0f0;
        color: {INK};
    }}
    /* 表头整体透明。只写 ::section 不够 —— Fusion 会给表头自己的视口刷一层
       灰底,看上去像一条多余的色带。 */
    QHeaderView {{
        background: transparent;
        border: none;
    }}
    QHeaderView::section {{
        background: transparent;
        border: none;
        border-bottom: 1px solid {HAIRLINE};
        padding: 6px 8px;
        color: {MID_GRAY};
        font-size: {TEXT_CAPTION}px;
        font-weight: 500;
    }}
    QTableCornerButton::section {{
        background: transparent;
        border: none;
    }}

    /* ---- 滚动条:细,不抢眼 ---- */
    QScrollBar:vertical {{
        background: transparent;
        width: 8px;
        margin: 0;
    }}
    QScrollBar::handle:vertical {{
        background: #d4d4d4;
        border-radius: 4px;
        min-height: 24px;
    }}
    QScrollBar::handle:vertical:hover {{
        background: {MID_GRAY};
    }}
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical,
    QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{
        background: transparent;
        height: 0;
    }}
    QScrollArea {{
        background: transparent;
        border: none;
    }}
    /* 滚动区视口。用 ID 选择器而不是 setStyleSheet,因为 widget 级样式表会
       级联到整棵子树,把卡片和按钮的底色一起冲掉。 */
    QWidget#CanvasViewport {{
        background: {CANVAS};
    }}

    /* ---- 弹窗:沿用卡片语言 ---- */
    QMessageBox {{
        background: {PAPER};
    }}
    QMessageBox QLabel {{
        color: {INK};
        font-size: {TEXT_BODY}px;
    }}
    QMessageBox QPushButton {{
        background: {CANVAS};
        color: {INK};
        border: none;
        border-radius: {RADIUS_INTERACTIVE}px;
        font-size: {TEXT_BODY}px;
        font-weight: 500;
        padding: 7px 16px;
        min-width: 64px;
    }}
    QMessageBox QPushButton:hover {{
        background: #ebebeb;
    }}
    """
