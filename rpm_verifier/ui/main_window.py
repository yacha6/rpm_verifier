"""PySide6 界面层。

铁律:绝不在网络协程里直接操作 GUI 控件。网络跑在独立线程的 asyncio 事件
循环里,一切数据通过 Qt Signal 回到主线程。

视觉遵循 shadcn/ui 风格规范:白卡片浮在灰画布上,交互元素 18px 胶囊圆角、
容器 24px 圆角,全程单色系,#e7000b 只留给错误状态。全部色值与尺寸令牌
集中在 styles.py。
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QColor, QGuiApplication, QPalette
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..core.client import ApiClient, normalize_base_url
from ..core.modality import (
    MODALITY_ORDER,
    get_modality,
    plan_summary,
    planned_video_seconds,
)
from ..core.scheduler import RateScheduler
from ..core.source import SourceError, preview_source, resolve_source
from ..core.stats import (
    CODE_INCOMPLETE,
    StatsCollector,
    code_label,
    planned_count,
)
from . import styles as S

# 调度偏差超过这个比例就提示用户「实测值可能失真」(见 PRD 风险 R7)
SCHEDULE_DEVIATION_LIMIT = 0.02

# 视频类模态的计划输出秒数超过这个值,先弹一次确认。
#
# 视频按秒计费,一个误填的 RPM 可能产生几百上千个任务。内部测试也一样:
# 没人想在点「开始」之后才发现自己填了 600 RPM。阈值放在这里,方便调。
VIDEO_CONFIRM_SECONDS = 600

# 非敏感配置的落盘位置。API Key 默认不落盘。
CONFIG_PATH = Path(os.path.expanduser("~/.config/rpm_verifier/config.json"))

PLACEHOLDER = "—"


# ---------------------------------------------------------------------------
# 小部件工具
# ---------------------------------------------------------------------------

def _caption(text: str) -> QLabel:
    """小号说明文字。"""
    label = QLabel(text)
    label.setObjectName("Caption")
    return label


def _field_label(text: str) -> QLabel:
    """表单字段名。"""
    label = QLabel(text)
    label.setObjectName("FieldLabel")
    return label


def _card_title(text: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName("CardTitle")
    return label


def _style_combo_popup(combo: QComboBox) -> None:
    """让下拉弹出列表也走同一套纸张/发丝线风格。

    Qt 的弹出列表是一个独立的 QComboBoxPrivateContainer + QComboBoxListView,
    不在 QComboBox 的子树里;应用级 QSS 对这两个私有类不一定命中,所以
    直接在 view 上设 palette + 局部样式最稳。不这么做的话,前面是灰底胶囊,
    点开却弹出一个白底、黑色选中条的系统菜单,整套视觉就断在这里。
    """
    view = combo.view()
    palette = view.palette()
    for role, color in (
        (QPalette.Base, S.PAPER),
        (QPalette.Window, S.PAPER),
        (QPalette.Highlight, S.CANVAS),
        (QPalette.HighlightedText, S.INK),
        (QPalette.Text, S.INK),
    ):
        palette.setColor(role, QColor(color))
    view.setPalette(palette)

    # QComboBoxListView 是 QListView 的私有子类;用精确类名,别用 QListView。
    view.setStyleSheet(
        f"background: {S.PAPER}; border: none; padding: 4px; outline: none;"
        f" selection-background-color: {S.CANVAS};"
        f" selection-color: {S.INK};"
        f"QComboBoxListView::item {{ min-height: 28px; padding: 4px 10px;"
        f" border-radius: {S.RADIUS_SMALL}px; }}"
        f"QComboBoxListView::item:selected {{ background: {S.CANVAS};"
        f" color: {S.INK}; }}"
    )

    # 弹出容器本身(独立窗口)也补上纸张底色和发丝线。
    container = view.window()
    if container is not view:
        container.setStyleSheet(
            f"background: {S.PAPER}; border: 1px solid {S.HAIRLINE};"
            f" border-radius: {S.RADIUS_SMALL}px;"
        )


class Card(QFrame):
    """白底卡片容器:24px 圆角 + 1px 发丝线边框 + 20px 内边距。

    规范要求卡片必须保留 1px 边框 —— 层次靠「画布 #f5f5f5 → 纸张 #ffffff」
    的色调对比建立,而不是靠投影。
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("Card")
        self.body = QVBoxLayout(self)
        self.body.setContentsMargins(
            S.CARD_PADDING, S.CARD_PADDING, S.CARD_PADDING, S.CARD_PADDING
        )
        self.body.setSpacing(S.SPACE_12)


# ---------------------------------------------------------------------------
# 后台工作线程
# ---------------------------------------------------------------------------

class TestWorker(QThread):
    """在独立线程里跑 asyncio 事件循环。

    这样做的好处是网络 IO 完全不阻塞界面;代价是必须通过 Signal 与主线程
    通信,不能直接碰控件。
    """

    updated = Signal(object)          # StatsSnapshot
    completed = Signal(object)        # TestReport
    failed = Signal(str, str, str)    # 原因 / 服务器原话 / 实际请求地址

    def __init__(
        self,
        base_url,
        api_key,
        model,
        modality,
        params,
        target_rpm,
        duration,
        source="",
        source_summary="",
        parent=None,
    ):
        super().__init__(parent)
        self._base_url = base_url
        self._api_key = api_key
        self._model = model
        self._modality = modality
        self._params = dict(params)
        self._target_rpm = target_rpm
        self._duration = float(duration)
        # 已经由界面解析好的源素材:URL 或 Data URI。worker 不再读文件。
        self._source = source
        self._source_summary = source_summary
        self._scheduler = None

    def request_stop(self) -> None:
        """请求停止。可以从 GUI 线程安全调用。"""
        if self._scheduler is not None:
            self._scheduler.request_stop()

    def run(self) -> None:
        try:
            asyncio.run(self._main())
        except Exception as exc:  # 兜底:别让线程静默死掉,界面会一直卡在「运行中」
            self.failed.emit(f"测试异常终止:{exc}", "", "")

    async def _main(self) -> None:
        client = ApiClient(
            self._base_url,
            self._api_key,
            self._model,
            modality=self._modality,
            params=self._params,
            source=self._source,
            source_summary=self._source_summary,
        )
        try:
            # 探针请求有两个作用:提前暴露配置错误,以及预热连接池。
            # 少了预热,计时段最前面几个请求会被 TLS 握手开销拖慢。
            #
            # 只有「配置本身有问题」才中止(probe.fatal)。429 / 5xx / 超时
            # 一律放行 —— 它们说明地址和 key 都是对的,而 429 还是本工具要
            # 观测的对象,在门口就把它拦掉等于白跑一趟。
            probe = await client.probe()
            if probe.fatal:
                reason = probe.message or "请检查 Base URL、模态与模型名是否匹配"
                self.failed.emit(
                    f"探针请求失败({code_label(probe.code)}):{reason}",
                    probe.detail,
                    probe.endpoint,
                )
                return

            collector = StatsCollector(
                self._target_rpm, planned_count(self._target_rpm, self._duration)
            )
            scheduler = RateScheduler(
                client,
                collector,
                target_rpm=self._target_rpm,
                duration=self._duration,
                on_update=self.updated.emit,
                # 收尾等待由模态决定(见 modality.py):文本 8 秒,图像 20 秒。
                # 图像是同步生成,单请求就要十几秒,给得太短会白白丢掉最后
                # 一批状态码;但也不能跟着 120 秒的请求超时走,那界面看上去
                # 就跟卡死了一样。等不及的记「未返回」,账目仍然是平的。
                drain_grace=client.drain_grace,
            )
            self._scheduler = scheduler
            report = await scheduler.run()

            # 服务器抱怨过什么,跟着报告一起带出去。整张表都是 400 却不知道
            # 服务器在说什么,和探针直接失败一样让人抓瞎。
            if client.last_error_detail:
                report = replace(report, error_detail=client.last_error_detail)

            # 输入条件模态的 RPM 数字离开「用了什么素材」就没有意义。带上
            # 脱敏摘要,但绝不带完整签名 URL。
            if self._source_summary:
                report = replace(report, source_summary=self._source_summary)

            self.completed.emit(report)
        finally:
            await client.aclose()


# ---------------------------------------------------------------------------
# 主窗口
# ---------------------------------------------------------------------------

class MainWindow(QMainWindow):
    # 窗口的偏好尺寸。860×840 是在 macOS 上调出来的,内容刚好一屏放得下。
    PREFERRED_SIZE = (860, 840)

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("LLM API 速率验证工具")
        self._apply_preferred_size()

        self._worker = None
        self._window_duration = 60.0
        self._retry_title = "重试统计"
        self._target_rpm = 60
        # 上一次已经把成功 RPM 画上屏幕的那个成功数。-1 表示还没画过。
        self._last_live_success = -1
        self._rpm_warmup = 2.0

        # 每个输入条件模态各自记住自己的源素材(只在本会话内存里)。
        # 切换模态再切回来时,用户不用重新填;重启后一律清空。
        self._sources: dict = {}
        self._active_modality = "text"
        # 最近一次真正跑过的模态 / 素材摘要。报告的重试提示要用到它们 ——
        # 测试结束后用户可能已经把下拉框切走了。
        self._last_modality = "text"
        self._last_source_summary = ""
        # 「成功」这个指标的名字要随模态变(异步视频是「成功(任务创建)」)。
        self._stat_captions: dict = {}

        self._build_ui()
        self._load_config()

    # -- 界面搭建 ----------------------------------------------------------

    def _apply_preferred_size(self) -> None:
        """按偏好尺寸开窗,但绝不超出屏幕可用区域。

        高 DPI 的 Windows 笔记本上这一条是必需的:一块 1920×1080 的屏在
        150% 缩放下,逻辑分辨率只有 1280×720,再扣掉任务栏,可用高度可能
        只有 680 左右。这时还硬开 840 高的窗口,底部会被顶到屏幕外面 ——
        「开始测试」按钮和状态码表都够不着,而窗口又拖不上来(标题栏已经
        贴在最上面了),同事会以为程序坏了。

        屏幕上放得下时,尺寸和原来一模一样(整个内容都在 QScrollArea 里,
        万一真的被压矮了,滚动条也能兜住,不会有内容丢失)。
        """
        width, height = self.PREFERRED_SIZE

        screen = QGuiApplication.primaryScreen()
        if screen is not None:
            available = screen.availableGeometry()
            width = min(width, max(available.width() - 40, 320))
            height = min(height, max(available.height() - 40, 240))

        self.resize(width, height)

    def _build_ui(self) -> None:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        # 注意:这里绝对不能用 setStyleSheet 给 viewport 上底色。Qt 的 widget 级
        # 样式表会向下级联到整棵子树,一条无选择器的 background 会把卡片、按钮
        # 的底色全部覆盖掉。必须回到 styles.py 用 ID 选择器精确指定。
        scroll.viewport().setObjectName("CanvasViewport")

        canvas = QWidget()
        canvas.setObjectName("Canvas")
        root = QVBoxLayout(canvas)
        root.setContentsMargins(S.SPACE_24, S.SPACE_24, S.SPACE_24, S.SPACE_24)
        root.setSpacing(S.SPACE_16)

        root.addWidget(self._build_header())
        root.addWidget(self._build_config_card())
        root.addWidget(self._build_metrics_card())
        root.addWidget(self._build_table_card())
        root.addWidget(self._build_retry_card())
        root.addStretch(1)

        scroll.setWidget(canvas)
        self.setCentralWidget(scroll)

        # 目标 RPM 在任何时候都是已知的,让标签跟着输入框走。必须等两张卡片都
        # 建好再连 —— 信号回调里要访问指标卡上的标签。
        # 测试期间输入框会被禁用,所以标签不会在测量中途被改掉。
        self.rpm_spin.valueChanged.connect(
            lambda value: self.lbl_target_rpm.setText(str(value))
        )

    def _build_header(self) -> QWidget:
        box = QWidget()
        layout = QVBoxLayout(box)
        layout.setContentsMargins(S.SPACE_4, 0, S.SPACE_4, S.SPACE_4)
        layout.setSpacing(S.SPACE_4)

        title = QLabel("LLM API 速率验证工具")
        title.setObjectName("Display")
        S.tighten(title, S.TEXT_HEADING)
        layout.addWidget(title)

        subtitle = QLabel("以指定速率探测账号的真实限流上限 · 只呈现数据,不做判定")
        subtitle.setObjectName("Muted")
        layout.addWidget(subtitle)

        return box

    def _build_config_card(self) -> Card:
        card = Card()
        card.body.addWidget(_card_title("配置"))

        grid = QGridLayout()
        grid.setHorizontalSpacing(S.SPACE_12)
        grid.setVerticalSpacing(S.SPACE_8)
        grid.setColumnStretch(1, 1)

        self.base_url_edit = QLineEdit()
        self.base_url_edit.setPlaceholderText("https://api.openai.com/v1")
        grid.addWidget(_field_label("Base URL"), 0, 0)
        grid.addWidget(self.base_url_edit, 0, 1, 1, 2)

        self.api_key_edit = QLineEdit()
        self.api_key_edit.setEchoMode(QLineEdit.Password)
        self.api_key_edit.setPlaceholderText("sk-…")
        self.reveal_btn = QToolButton()
        self.reveal_btn.setObjectName("Pill")
        self.reveal_btn.setText("显示")
        self.reveal_btn.setCheckable(True)
        self.reveal_btn.setCursor(Qt.PointingHandCursor)
        self.reveal_btn.setToolTip("显示 / 隐藏 API Key")
        self.reveal_btn.toggled.connect(self._on_reveal_toggled)
        grid.addWidget(_field_label("API Key"), 1, 0)
        grid.addWidget(self.api_key_edit, 1, 1)
        grid.addWidget(self.reveal_btn, 1, 2)

        self.model_edit = QLineEdit("gpt-3.5-turbo")
        grid.addWidget(_field_label("模型名"), 2, 0)
        grid.addWidget(self.model_edit, 2, 1, 1, 2)

        # -- 模态 ----------------------------------------------------------
        #
        # 模态决定了请求打到哪个端点、请求体长什么样、超时给多久(见
        # core/modality.py)。它是配置里最容易出错的一项 —— 用文本的端点
        # 去请求图像模型,会得到 404,而人第一反应总是去查模型名。
        self.modality_combo = QComboBox()
        for spec in MODALITY_ORDER:
            self.modality_combo.addItem(spec.label, spec.key)
        _style_combo_popup(self.modality_combo)
        # 先 addItem 再连信号:addItem 本身就会发 currentIndexChanged,
        # 那时候参数行还没建出来,回调会炸在 None 上。
        self.modality_combo.currentIndexChanged.connect(self._on_modality_changed)
        grid.addWidget(_field_label("模态"), 3, 0)
        grid.addWidget(self.modality_combo, 3, 1, 1, 2)

        # -- 源素材(仅输入条件模态) ----------------------------------------
        #
        # 图生图支持本地文件,编码成 Data URI;图生视频/视频生视频由服务端
        # 去拉素材,只接受公开 URL。这行只在需要素材的模态下显示。
        self.source_label = _field_label("源素材")
        self.source_edit = QLineEdit()
        self.source_edit.textChanged.connect(self._on_source_changed)

        self.source_browse_btn = QPushButton("选择文件…")
        self.source_browse_btn.setObjectName("Ghost")
        self.source_browse_btn.setCursor(Qt.PointingHandCursor)
        self.source_browse_btn.clicked.connect(self._on_browse_source)

        source_row = QHBoxLayout()
        source_row.setSpacing(S.SPACE_8)
        source_row.addWidget(self.source_edit, 1)
        source_row.addWidget(self.source_browse_btn)

        self.source_hint = _caption("")
        self.source_hint.setWordWrap(True)
        self.source_summary = _caption("")
        self.source_summary.setWordWrap(True)
        self.source_summary.setVisible(False)

        self.source_cell = QWidget()
        source_cell_layout = QVBoxLayout(self.source_cell)
        source_cell_layout.setContentsMargins(0, 0, 0, 0)
        source_cell_layout.setSpacing(2)
        source_cell_layout.addLayout(source_row)
        source_cell_layout.addWidget(self.source_hint)
        source_cell_layout.addWidget(self.source_summary)

        grid.addWidget(self.source_label, 4, 0)
        grid.addWidget(self.source_cell, 4, 1, 1, 2)

        # -- 模态参数 ------------------------------------------------------
        #
        # 参数不再包成「小标题 + 下拉」的子表单。每个参数占一行,左边是字段名,
        # 右边是下拉框 —— 和 Base URL / 模型名 / 模态走同一条标签列。这样
        # 眼睛只需要沿着一条竖线往下看,不会在「参数」格里再看到一套缩进的
        # 小标题。
        self.param_combos = {}
        self.param_labels = {}
        max_params = max((len(spec.params) for spec in MODALITY_ORDER), default=0)
        param_row_start = 5

        for spec in MODALITY_ORDER:
            for index, param in enumerate(spec.params):
                row = param_row_start + index
                label = _field_label(param.label)
                label.setVisible(False)
                combo = QComboBox()
                combo.addItems(param.options)
                combo.setCurrentText(param.default)
                _style_combo_popup(combo)
                combo.setVisible(False)
                grid.addWidget(label, row, 0)
                grid.addWidget(combo, row, 1, 1, 2)
                self.param_labels[(spec.key, param.name)] = label
                self.param_combos[(spec.key, param.name)] = combo

        self.rpm_spin = QSpinBox()
        self.rpm_spin.setRange(1, 6000)
        self.rpm_spin.setValue(60)
        self.rpm_spin.setSuffix(" RPM")

        self.duration_spin = QSpinBox()
        self.duration_spin.setRange(10, 3600)
        self.duration_spin.setValue(60)
        self.duration_spin.setSuffix(" 秒")

        rate_row = QHBoxLayout()
        rate_row.setSpacing(S.SPACE_8)
        rate_row.addWidget(self.rpm_spin, 1)
        rate_row.addWidget(self.duration_spin, 1)
        rate_row_index = param_row_start + max_params
        grid.addWidget(_field_label("目标速率"), rate_row_index, 0)
        grid.addLayout(rate_row, rate_row_index, 1, 1, 2)

        card.body.addLayout(grid)
        card.body.addSpacing(S.SPACE_4)

        # 端点预览:把最终会请求的完整地址摆出来。Base URL 填错(最常见的是
        # 把整条 /v1/images/generations 粘进 Base URL 框)时,这行字让人一眼
        # 看出问题,而不是等 404 弹窗再回头猜。
        self.endpoint_preview = _caption("")
        self.endpoint_preview.setWordWrap(True)
        self.endpoint_preview.setObjectName("EndpointPreview")
        self.base_url_edit.textChanged.connect(self._refresh_endpoint_preview)
        card.body.addWidget(self.endpoint_preview)

        # 计划量预览:视频类必须把「N 个任务 × S 秒 = 总秒数」摆出来。
        # 这是用户判断一次测试大概要花多少钱的唯一依据。
        self.plan_preview = _caption("")
        self.plan_preview.setWordWrap(True)
        card.body.addWidget(self.plan_preview)

        self.modality_note = _caption("")
        self.modality_note.setWordWrap(True)
        card.body.addWidget(self.modality_note)

        # 计划量跟着速率、时长和单次秒数走。这些连接必须放在 plan_preview
        # 建好之后 —— 信号回调里要写它。
        self.rpm_spin.valueChanged.connect(self._refresh_plan_preview)
        self.duration_spin.valueChanged.connect(self._refresh_plan_preview)
        for combo in self.param_combos.values():
            combo.currentTextChanged.connect(self._refresh_plan_preview)

        card.body.addSpacing(S.SPACE_4)

        self.start_btn = QPushButton("开始测试")
        self.start_btn.setObjectName("Primary")
        self.start_btn.setCursor(Qt.PointingHandCursor)
        self.start_btn.clicked.connect(self._on_start_clicked)
        card.body.addWidget(self.start_btn)

        # 建完就把初始模态的可见性刷一遍 —— 否则「参数」标签和模态说明
        # 会以默认可见的状态留在屏幕上。
        self._on_modality_changed()

        return card

    # -- 模态联动 ----------------------------------------------------------

    def _on_modality_changed(self) -> None:
        """切换模态:换参数页、源素材行、说明、端点与计划量。"""
        key = self.modality_combo.currentData()
        if not key:
            return

        # 先把上一个模态的源素材存起来,再切到新模态。只存在内存里 ——
        # 签名 URL 和本地路径都不该落盘。
        if self._active_modality != key:
            self._sources[self._active_modality] = self.source_edit.text()
        self._active_modality = key
        spec = get_modality(key)

        for (spec_key, _name), label in self.param_labels.items():
            label.setVisible(spec_key == key)
        for (spec_key, _name), combo in self.param_combos.items():
            combo.setVisible(spec_key == key)

        # -- 源素材行 --------------------------------------------------
        has_source = spec.source_kind != "none"
        self.source_label.setVisible(has_source)
        self.source_cell.setVisible(has_source)

        if has_source:
            self.source_label.setText(spec.source_label or "源素材")
            self.source_edit.setPlaceholderText(
                "公开 URL,或本地图片路径"
                if spec.accepts_local_file
                else "可公开访问的 URL"
            )
            # 图生视频/视频生视频由服务端拉素材,本地文件没有意义。
            self.source_browse_btn.setEnabled(spec.accepts_local_file)
            self.source_browse_btn.setToolTip(
                "选择本地图片"
                if spec.accepts_local_file
                else "该模态要求公开 URL:服务端需要自行下载素材,本地文件无法使用"
            )
            self.source_hint.setText(spec.source_hint)
            self.source_hint.setVisible(bool(spec.source_hint))

            # 恢复这个模态自己的素材;blockSignals 避免恢复过程中触发摘要刷新。
            self.source_edit.blockSignals(True)
            self.source_edit.setText(self._sources.get(key, ""))
            self.source_edit.blockSignals(False)
            self._refresh_source_summary()
        else:
            self.source_edit.blockSignals(True)
            self.source_edit.clear()
            self.source_edit.blockSignals(False)
            self.source_summary.clear()
            self.source_summary.setVisible(False)

        # 「成功」的标签随模态变。异步视频的 2xx 只是任务创建成功,
        # 不写清楚的话用户会把它当成「视频生成成功」。
        caption = self._stat_captions.get("success")
        if caption is not None:
            caption.setText(spec.success_label)

        # 说明文字随模态走。视频那条尤其重要:它测的是创建任务的限流,
        # 不是视频生成的吞吐 —— 不说清楚,用户会拿这个数字去推断错的东西。
        self.modality_note.setText(spec.note)
        self.modality_note.setVisible(bool(spec.note))

        self._refresh_endpoint_preview()
        self._refresh_plan_preview()

    # -- 源素材 ------------------------------------------------------------

    def _on_source_changed(self) -> None:
        """输入框变化时刷新摘要。

        只做 stat / URL 语法这种便宜的事;base64 编码留到点「开始测试」时。
        否则用户每敲一个字符都会触发一次 5 MB 文件读取。
        """
        self._refresh_source_summary()

    def _refresh_source_summary(self) -> None:
        spec = get_modality(self.modality_combo.currentData())
        raw = self.source_edit.text().strip()

        if spec.source_kind == "none" or not raw:
            self.source_summary.clear()
            self.source_summary.setVisible(False)
            return

        try:
            preview = preview_source(spec, raw)
        except SourceError as exc:
            # 输入到一半时的中间态也会走到这里(比如只敲了 "ht")。还没形成
            # 一个完整地址/路径时先不报错,免得用户每敲一个字符就看到一句
            # 「找不到文件」;真正的拦截在点「开始测试」时做。
            looks_complete = raw.lower().startswith(
                ("http://", "https://")
            ) or Path(raw).expanduser().exists()
            if not looks_complete:
                self.source_summary.clear()
                self.source_summary.setVisible(False)
                return
            self.source_summary.setText(str(exc))
            self.source_summary.setVisible(True)
            return

        text = f"当前:{preview.summary}"
        if preview.warning:
            text += f" {preview.warning}"
        self.source_summary.setText(text)
        self.source_summary.setVisible(True)

    def _on_browse_source(self) -> None:
        """选一个本地图片。只有图生图能点这个按钮。"""
        path, _ = QFileDialog.getOpenFileName(
            self,
            "选择源图片",
            "",
            "图片 (*.png *.jpg *.jpeg *.webp)",
        )
        if path:
            self.source_edit.setText(path)

    def _refresh_plan_preview(self) -> None:
        """按当前模态/速率/时长/单次秒数刷新计划量文案。"""
        if not hasattr(self, "plan_preview"):
            return
        spec = get_modality(self.modality_combo.currentData())
        planned = planned_count(self.rpm_spin.value(), self.duration_spin.value())
        self.plan_preview.setText(plan_summary(spec, planned, self._current_params()))

    def _refresh_endpoint_preview(self) -> None:
        """实时显示最终请求地址。"""
        spec = get_modality(self.modality_combo.currentData())
        base = normalize_base_url(self.base_url_edit.text().strip())
        if not base:
            self.endpoint_preview.setText(f"请求地址:POST {spec.path}")
            return
        self.endpoint_preview.setText(f"请求地址:POST {base}{spec.path}")

    def _current_params(self) -> dict:
        """收集当前模态下用户选的参数。"""
        key = self.modality_combo.currentData()
        spec = get_modality(key)
        return {
            param.name: self.param_combos[(key, param.name)].currentText()
            for param in spec.params
        }

    def _build_metrics_card(self) -> Card:
        card = Card()

        # --- 主指标:用户最关心的那个数字,独占一行 ---
        # v1.1.2 起口径改成「全部请求(含重试)中 2xx 的速率」。重试也是真实
        # 打到 API 的流量,503 重试不能被排除在 RPM 之外。
        card.body.addWidget(_caption("成功 RPM(含重试)"))

        self.lbl_success_rpm = QLabel(PLACEHOLDER)
        self.lbl_success_rpm.setObjectName("StatValue")
        S.tighten(self.lbl_success_rpm, S.TEXT_HEADING_LG)
        card.body.addWidget(self.lbl_success_rpm)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setFixedHeight(8)
        self.progress.setTextVisible(False)
        card.body.addWidget(self.progress)

        status_row = QHBoxLayout()
        status_row.setSpacing(S.SPACE_8)
        self.lbl_progress_text = QLabel("就绪")
        self.lbl_progress_text.setObjectName("Muted")
        status_row.addWidget(self.lbl_progress_text)
        status_row.addStretch(1)
        card.body.addLayout(status_row)

        # 异常提示。平时隐藏;只有熔断 / 丢弃 / 调度失准时才出现,
        # 并且用 #e7000b —— 规范里这个红只服务于错误状态。
        self.lbl_notice = QLabel("")
        self.lbl_notice.setWordWrap(True)
        self.lbl_notice.setVisible(False)
        card.body.addWidget(self.lbl_notice)

        # --- 次要指标 ---
        stats_row = QHBoxLayout()
        stats_row.setSpacing(S.SPACE_24)
        self.lbl_target_rpm = self._add_stat(stats_row, "目标 RPM", key="target")
        self.lbl_sent = self._add_stat(stats_row, "首次请求", key="sent")
        self.lbl_all_sent = self._add_stat(stats_row, "全部请求", key="all_sent")
        self.lbl_success = self._add_stat(stats_row, "成功", key="success")
        card.body.addLayout(stats_row)

        # 首次 / 全部请求的 RPM 单独摆一行。目标 RPM 是调度槽位,全部请求
        # RPM 才是实际打到 API 的速率 —— 重试不占槽位,但它确实算流量。
        self.lbl_all_rpm = _caption("")
        self.lbl_all_rpm.setWordWrap(True)
        card.body.addWidget(self.lbl_all_rpm)

        return card

    def _add_stat(self, row: QHBoxLayout, caption: str, key: str = "") -> QLabel:
        box = QWidget()
        layout = QVBoxLayout(box)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        caption_label = _caption(caption)
        layout.addWidget(caption_label)

        value = QLabel(PLACEHOLDER)
        value.setObjectName("StatValueSmall")
        S.tighten(value, S.TEXT_HEADING)
        layout.addWidget(value)

        if key:
            # 模态切换时要改「成功」这个名字(异步视频是「成功(任务创建)」)。
            self._stat_captions[key] = caption_label

        row.addWidget(box, 1)
        return value

    def _build_table_card(self) -> Card:
        card = Card()

        head = QHBoxLayout()
        head.addWidget(_card_title("状态码统计"))
        head.addStretch(1)
        # 重试请求同样是真实流量,所以这里统计的是「全部请求」,不是「仅首次」。
        head.addWidget(_caption("全部请求(含重试)"))
        card.body.addLayout(head)

        self.code_table = QTableWidget(0, 5)
        self.code_table.setHorizontalHeaderLabels(
            ["状态码", "首次", "重试", "合计", "占比"]
        )
        self.code_table.verticalHeader().setVisible(False)
        self.code_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.code_table.setSelectionMode(QAbstractItemView.NoSelection)
        self.code_table.setShowGrid(False)
        # 高度跟着行数走(见 _fit_table_height)。给个固定高度会让只有两三行
        # 的时候,卡片底下拖着一大块空白。
        self.code_table.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.code_table.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)

        header = self.code_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        for column in range(1, 5):
            header.setSectionResizeMode(column, QHeaderView.ResizeToContents)
        header.setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)

        card.body.addWidget(self.code_table)

        # 原始报错。HTTP 错误显示服务器原话;「连接错误」这类没有 HTTP 响应
        # 的行显示 httpx 的原始异常。只写一个「连接错误」等于什么都没说。
        self.raw_error = QLabel("")
        self.raw_error.setObjectName("RawError")
        self.raw_error.setWordWrap(True)
        self.raw_error.setTextFormat(Qt.PlainText)
        self.raw_error.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.raw_error.setVisible(False)
        card.body.addWidget(self.raw_error)

        # 本次输入。输入条件模态的 RPM 数字离开「用了什么素材」就没有意义;
        # 但只显示脱敏摘要,签名 URL 的 query 不能进报告。
        self.report_source = _caption("")
        self.report_source.setWordWrap(True)
        self.report_source.setVisible(False)
        card.body.addWidget(self.report_source)

        # 「超时」「连接错误」这些不是 HTTP 状态码 —— 那些请求根本没拿到响应。
        # 不说明的话,用户会以为收到了一个叫「超时」的状态码。
        footnote = _caption(
            "「合计」= 首次 + 重试,即本次实际打到 API 的全部请求;重试请求"
            "同样计入 RPM,不再被排除。上表中带括号的负数行不是 HTTP 状态码"
            " —— 这些请求没有收到任何 HTTP 响应,原始异常见上方「原始错误」。"
        )
        footnote.setWordWrap(True)
        card.body.addWidget(footnote)

        return card

    def _build_retry_card(self) -> Card:
        card = Card()
        card.body.setSpacing(S.SPACE_8)

        self.retry_toggle = QToolButton()
        self.retry_toggle.setObjectName("Disclosure")
        self.retry_toggle.setText("▸ 重试统计")
        self.retry_toggle.setCheckable(True)
        self.retry_toggle.setCursor(Qt.PointingHandCursor)
        self.retry_toggle.toggled.connect(self._on_retry_toggled)
        card.body.addWidget(self.retry_toggle)

        self.retry_body = QLabel("本次测试没有发生 503 重试。")
        self.retry_body.setObjectName("RetryBody")
        self.retry_body.setWordWrap(True)
        self.retry_body.setVisible(False)
        card.body.addWidget(self.retry_body)

        return card

    # -- 交互 --------------------------------------------------------------

    def _on_reveal_toggled(self, shown: bool) -> None:
        self.api_key_edit.setEchoMode(QLineEdit.Normal if shown else QLineEdit.Password)
        self.reveal_btn.setText("隐藏" if shown else "显示")

    def _on_retry_toggled(self, expanded: bool) -> None:
        self.retry_body.setVisible(expanded)
        self.retry_toggle.setText(("▾ " if expanded else "▸ ") + self._retry_title)

    def _on_start_clicked(self) -> None:
        if self._worker is not None and self._worker.isRunning():
            self._worker.request_stop()
            self.start_btn.setEnabled(False)
            self.start_btn.setText("正在停止…")
            return

        base_url = self.base_url_edit.text().strip()
        api_key = self.api_key_edit.text().strip()
        model = self.model_edit.text().strip()

        if not base_url:
            QMessageBox.warning(self, "配置不完整", "请填写 Base URL。")
            return
        if not base_url.startswith(("http://", "https://")):
            QMessageBox.warning(
                self, "Base URL 格式错误", "Base URL 需要以 http:// 或 https:// 开头。"
            )
            return
        if not api_key:
            QMessageBox.warning(self, "配置不完整", "请填写 API Key。")
            return
        if not model:
            QMessageBox.warning(self, "配置不完整", "请填写模型名。")
            return

        spec = get_modality(self.modality_combo.currentData())

        # 源素材在启动前解析:URL 只做语法校验,本地图片读一次、编码一次。
        # 失败就不启动,别等探针 400 再回头猜。
        resolved_source = None
        if spec.source_kind != "none":
            try:
                resolved_source = resolve_source(spec, self.source_edit.text().strip())
            except SourceError as exc:
                QMessageBox.warning(self, "源素材有问题", str(exc))
                return

        # 视频按秒计费,计划量太大时先确认一次。这里不算美元金额 ——
        # 价格随供应商/档位/活动变化,只摆计划量,让用户自己换算。
        if spec.plan_unit == "video":
            planned = planned_count(
                self.rpm_spin.value(), self.duration_spin.value()
            )
            total_seconds = planned_video_seconds(
                spec, planned, self._current_params()
            )
            if total_seconds > VIDEO_CONFIRM_SECONDS:
                seconds = total_seconds // planned if planned else 0
                lines = [
                    f"本次计划创建 {planned} 个视频任务,单次 {seconds} 秒,"
                    f"合计 {total_seconds} 秒输出视频。",
                    "视频通常按输出秒数计费;视频生视频的输入视频时长也会计费。"
                    if spec.key == "video_to_video"
                    else "视频通常按输出秒数计费。",
                    "另有 1 次探针请求(不计入统计,也可能计费)。",
                    "",
                    "确认按这个计划开始?",
                ]
                answer = QMessageBox.question(
                    self,
                    "确认视频计划",
                    "\n".join(lines),
                    QMessageBox.Yes | QMessageBox.No,
                    QMessageBox.No,
                )
                if answer != QMessageBox.Yes:
                    return

        self._window_duration = float(self.duration_spin.value())
        self._target_rpm = self.rpm_spin.value()
        # 至少要攒够两个调度间隔的样本,折算出来的速率才有意义
        self._rpm_warmup = max(2.0, 2 * 60.0 / self._target_rpm)
        self._last_modality = spec.key
        self._last_source_summary = resolved_source.summary if resolved_source else ""
        self._reset_view()
        self._save_config()

        self._worker = TestWorker(
            base_url,
            api_key,
            model,
            self.modality_combo.currentData(),
            self._current_params(),
            self.rpm_spin.value(),
            self.duration_spin.value(),
            source=resolved_source.value if resolved_source else "",
            source_summary=resolved_source.summary if resolved_source else "",
            parent=self,
        )
        self._worker.updated.connect(self._on_updated)
        self._worker.completed.connect(self._on_completed)
        self._worker.failed.connect(self._on_failed)
        self._worker.finished.connect(self._on_worker_finished)

        self._set_inputs_enabled(False)
        self.start_btn.setText("停止")
        self._worker.start()

    def _on_worker_finished(self) -> None:
        self._set_inputs_enabled(True)
        self.start_btn.setEnabled(True)
        self.start_btn.setText("开始测试")

    # -- 数据刷新 ----------------------------------------------------------

    def _on_updated(self, snapshot) -> None:
        elapsed = snapshot.elapsed
        ratio = min(elapsed / self._window_duration, 1.0) if self._window_duration else 0
        self.progress.setValue(int(ratio * 100))

        inflight = snapshot.sent - snapshot.responded
        if snapshot.planned and snapshot.sent >= snapshot.planned and inflight > 0:
            # 槽位已经发完,但还有请求没回来。收尾阶段可能停留数秒,不说清楚
            # 用户会以为卡死了。
            self.lbl_progress_text.setText(f"等待 {inflight} 个请求返回…")
        else:
            self.lbl_progress_text.setText(
                f"{elapsed:.0f}s / {self._window_duration:.0f}s"
            )

        self.lbl_sent.setText(str(snapshot.sent))
        self.lbl_all_sent.setText(str(snapshot.all_sent))
        self.lbl_success.setText(str(snapshot.all_success))
        self.lbl_all_rpm.setText(
            f"全部请求 RPM(含重试):{snapshot.all_send_rpm:.0f} · "
            f"首次请求 RPM:{snapshot.send_rpm:.0f}"
        )
        self._render_live_rpm(snapshot)
        self._refresh_code_table(
            snapshot.codes,
            snapshot.retry_codes,
            snapshot.details,
            snapshot.retry_details,
        )

    def _render_live_rpm(self, snapshot) -> None:
        """刷新实时「成功 RPM」。

        只在成功数变化时才重写这个数字。快照是 4Hz 推上来的,但分子(成功数)
        要等响应回来才动,分母(已耗时)却每个快照都在涨 —— 如果每次都重算,
        数字就会在两个响应之间一路下滑、响应一到又弹回去,看上去就是无规律
        乱跳。它本质上只在新数据到达时才真的变了。

        另外,样本太少时这个比值毫无意义(1 个成功 ÷ 0.3 秒 = 200 RPM),
        所以先空着,等攒够两个调度间隔再显示。
        """
        if snapshot.all_success == self._last_live_success:
            return
        self._last_live_success = snapshot.all_success

        if snapshot.elapsed < self._rpm_warmup:
            return

        # 含重试的 2xx 速率。重试成功也是真实成功,不能把这个数字做小。
        self.lbl_success_rpm.setText(f"{snapshot.all_success_rpm:.0f}")

    def _on_completed(self, report) -> None:
        self.progress.setValue(100)
        self.lbl_progress_text.setText(
            "提前结束" if report.finished_early else "完成"
        )

        self.lbl_sent.setText(str(report.sent))
        self.lbl_all_sent.setText(str(report.all_sent))
        self.lbl_success.setText(str(report.all_success))
        self.lbl_success_rpm.setText(f"{report.all_success_rpm:.0f}")
        self.lbl_all_rpm.setText(
            f"全部请求 RPM(含重试):{report.all_send_rpm:.0f} · "
            f"首次请求 RPM:{report.send_rpm:.0f}"
        )
        self._refresh_code_table(
            report.codes,
            report.retry_codes,
            report.details,
            report.retry_details,
        )

        # 输入条件模态的 RPM 数字离开「用了什么素材」就没有意义。这里只显示
        # 脱敏摘要,完整 URL(尤其带签名的)不进报告。
        source_summary = getattr(report, "source_summary", "") or self._last_source_summary
        if source_summary:
            self.report_source.setText(f"本次输入:{source_summary}")
            self.report_source.setVisible(True)
        else:
            self.report_source.setVisible(False)

        self._update_retry_section(report)
        self._show_notes(report)

    def _on_failed(self, message: str, detail: str = "", endpoint: str = "") -> None:
        """探针失败:把三样东西一起摆出来。

        只给一句「服务器拒绝了请求参数」是不够的 —— 用户知道被拒了,却不知道
        为什么,只能一个个参数去试。所以服务器返回的**原话**和**实际请求的
        地址**都要显示,这三样凑齐,一次就能定位问题。
        """
        self.progress.setValue(0)
        self.lbl_progress_text.setText("失败")

        parts = [message]
        if detail:
            # 可能是服务器原话,也可能是 httpx 的原始异常(连接失败时没有
            # HTTP 响应)。统一叫「原始报错」,不硬说成服务器返回。
            parts.append(f"原始报错:{detail}")
        if endpoint:
            parts.append(f"请求地址:{endpoint}")
        QMessageBox.critical(self, "测试失败", "\n\n".join(parts))

    def _refresh_code_table(
        self,
        codes: dict,
        retry_codes: dict | None = None,
        details: dict | None = None,
        retry_details: dict | None = None,
    ) -> None:
        """渲染状态码表:首次 / 重试 / 合计 / 占比。

        ⚠️ 合计是**全部请求**,不是仅首次。重试请求同样打到了 API、消耗了
        账号额度,必须算进 RPM;这里把首次和重试拆成两列,既不隐藏重试,
        也不让用户失去「首次成功率」这个诊断信号。
        """
        retry_codes = retry_codes or {}
        details = details or {}

        all_codes: dict = dict(codes)
        for code, count in retry_codes.items():
            all_codes[code] = all_codes.get(code, 0) + count

        total = sum(all_codes.values())
        # 按合计次数降序;次数相同时按状态码升序,保证 200 稳定排在最上面
        rows = sorted(all_codes.items(), key=lambda kv: (-kv[1], kv[0]))

        self.code_table.setRowCount(len(rows))
        for i, (code, total_count) in enumerate(rows):
            share = f"{total_count / total * 100:.1f}%" if total else PLACEHOLDER
            # 负数行把原始值也带上:「连接错误(-2)」比光秃秃的「连接错误」
            # 更能说明它不是 HTTP 状态码。
            label = code_label(code) if code >= 0 else f"{code_label(code)}({code})"
            cells = (
                label,
                str(codes.get(code, 0)),
                str(retry_codes.get(code, 0)),
                str(total_count),
                share,
            )
            for col, text in enumerate(cells):
                item = QTableWidgetItem(text)
                item.setTextAlignment(Qt.AlignLeft | Qt.AlignVCenter)
                self.code_table.setItem(i, col, item)

        self._fit_table_height(len(rows))
        self._refresh_raw_errors(rows, details, retry_details or {})

    def _refresh_raw_errors(
        self,
        rows,
        details: dict,
        retry_details: dict | None = None,
    ) -> None:
        """把每个状态码的原始报错摆到表格下面。

        这是本次修复的重点:以前连接失败只显示一个「连接错误」,原始异常被
        吞掉;HTTP 错误也只留最后一条服务器原话。现在按状态码逐条列出,
        用户可以直接看到「HTTP 400 到底说了什么」以及「连接失败到底是
        ConnectError、SSLError 还是 RemoteProtocolError」。

        首次和重试的原始报错不同时,两条都显示 —— 比如首次 503 的说明和
        重试 503 的说明可能不一样。
        """
        retry_details = retry_details or {}
        lines = []

        for code, _total in rows:
            first_detail = details.get(code, "")
            retry_detail = retry_details.get(code, "")

            parts = []
            if first_detail:
                parts.append(first_detail)
            if retry_detail and retry_detail != first_detail:
                parts.append(f"重试:{retry_detail}")
            if not parts:
                continue

            body = "\n".join(parts)
            if code < 0:
                lines.append(
                    f"{code_label(code)}({code}) · 未收到 HTTP 响应:\n{body}"
                )
            else:
                lines.append(f"HTTP {code} · 服务器原话:\n{body}")

        if not lines:
            self.raw_error.clear()
            self.raw_error.setVisible(False)
            return

        self.raw_error.setText("原始错误:\n\n" + "\n\n".join(lines))
        self.raw_error.setVisible(True)

    def _fit_table_height(self, row_count: int) -> None:
        """把表格高度收到刚好装下行数。

        要让卡片贴合内容,而不是拖一块空白。至少留一行的高度,免得空表塌成
        一根线。
        """
        header_h = self.code_table.horizontalHeader().height()
        row_h = self.code_table.verticalHeader().defaultSectionSize()
        visible = max(row_count, 1)
        # +2 是给最后一行下面的发丝线留位置
        self.code_table.setFixedHeight(header_h + row_h * visible + 2)

    def _update_retry_section(self, report) -> None:
        if report.retry_triggered == 0:
            self._retry_title = "重试统计"
            self.retry_body.setText("本次测试没有发生 503 重试。")
        else:
            lines = [
                f"503 触发 {report.retry_triggered} 次 · "
                f"重试成功 {report.retry_success} 次 · "
                f"最终失败 {report.retry_failed} 次"
            ]
            if report.retry_codes:
                detail = "、".join(
                    f"{code_label(code)} × {n}"
                    for code, n in sorted(report.retry_codes.items())
                )
                lines.append(f"重试请求的状态码:{detail}")
            retry_attempts = sum(report.retry_codes.values())
            lines.append(
                f"重试请求共 {retry_attempts} 次,已计入上方状态码表的"
                f"「重试」列和全部请求 RPM。"
            )
            # 异步视频的 503 重试会重新创建任务;这是 D18 保留 10 次重试后
            # 必须让用户看到的代价。
            if get_modality(self._last_modality).plan_unit == "video":
                lines.append("视频任务的重试会重新创建任务,可能产生额外计费。")
            self._retry_title = f"重试统计({report.retry_triggered})"
            self.retry_body.setText("\n".join(lines))

        self.retry_toggle.setText(
            ("▾ " if self.retry_toggle.isChecked() else "▸ ") + self._retry_title
        )

    def _show_notes(self, report) -> None:
        """把需要用户知道的情况讲清楚。

        注意措辞:这里不给「达标 / 未达标」的结论,只陈述发生了什么。
        """
        notes = []
        severe = False

        if report.circuit_reason:
            notes.append(report.circuit_reason)
            severe = True

        if report.dropped:
            notes.append(
                f"有 {report.dropped} 个槽位因在途请求过多被丢弃,实际发送数少于计划数。"
            )

        if report.finished_early and not report.circuit_reason:
            notes.append("测试被手动提前停止,数据按实际运行时长折算。")

        # 服务器对请求的抱怨。没有这条,用户看到一整列 400 / 422 只能靠猜。
        if report.error_detail:
            notes.append(f"服务器对请求的说明:{report.error_detail}")
            severe = True

        incomplete = report.codes.get(CODE_INCOMPLETE, 0)
        if incomplete:
            notes.append(
                f"有 {incomplete} 个请求直到收尾等待结束都没返回,已计入「未返回」。"
                f"这说明该接口的响应时间超过了收尾等待上限,可以调小目标速率重测。"
            )

        # 调度精度自检:工具自身失准的话,数字没有参考价值,必须说清楚
        if not report.finished_early and report.target_rpm > 0:
            deviation = abs(report.send_rpm - report.target_rpm) / report.target_rpm
            if deviation > SCHEDULE_DEVIATION_LIMIT:
                notes.append(
                    f"目标速率与实际发送速率偏差 {deviation * 100:.1f}%,超过 2%,"
                    f"本次实测值可能失真。"
                )

        if not notes:
            self.lbl_notice.setVisible(False)
            return

        color = S.EMBER if severe else S.MID_GRAY
        self.lbl_notice.setStyleSheet(f"color: {color}; font-size: {S.TEXT_BODY}px;")
        self.lbl_notice.setText(" ".join(notes))
        self.lbl_notice.setVisible(True)

    # -- 辅助 --------------------------------------------------------------

    def _reset_view(self) -> None:
        self.progress.setValue(0)
        self.lbl_progress_text.setText("准备中…")
        self.lbl_target_rpm.setText(str(self.rpm_spin.value()))
        self.lbl_success_rpm.setText(PLACEHOLDER)
        self._last_live_success = -1
        self.lbl_sent.setText(PLACEHOLDER)
        self.lbl_all_sent.setText(PLACEHOLDER)
        self.lbl_success.setText(PLACEHOLDER)
        self.lbl_all_rpm.setText("全部请求 RPM(含重试):— · 首次请求 RPM:—")
        self.code_table.setRowCount(0)
        self._fit_table_height(0)
        self.lbl_notice.setVisible(False)
        self.raw_error.clear()
        self.raw_error.setVisible(False)
        self.report_source.clear()
        self.report_source.setVisible(False)
        self._retry_title = "重试统计"
        self.retry_body.setText("本次测试没有发生 503 重试。")
        self.retry_toggle.setText(
            ("▾ " if self.retry_toggle.isChecked() else "▸ ") + self._retry_title
        )
        self._refresh_plan_preview()

    def _set_inputs_enabled(self, enabled: bool) -> None:
        for widget in (
            self.base_url_edit,
            self.api_key_edit,
            self.model_edit,
            self.modality_combo,
            self.source_edit,
            self.source_label,
            self.source_hint,
            self.source_summary,
            *self.param_labels.values(),
            *self.param_combos.values(),
            self.rpm_spin,
            self.duration_spin,
            self.reveal_btn,
        ):
            widget.setEnabled(enabled)

        # 浏览按钮的可用性还要看模态:图生视频/视频生视频只接受公开 URL。
        # 不能把它塞进上面的循环 —— 测完恢复时会把不该点的按钮重新点亮。
        spec = get_modality(self.modality_combo.currentData())
        self.source_browse_btn.setEnabled(enabled and spec.accepts_local_file)

    # -- 配置持久化 --------------------------------------------------------
    #
    # API Key 默认不落盘。这里只保存非敏感字段,避免明文凭据留在磁盘上。

    def _save_config(self) -> None:
        data = {
            "base_url": self.base_url_edit.text().strip(),
            "model": self.model_edit.text().strip(),
            "modality": self.modality_combo.currentData(),
            "params": self._current_params(),
            "target_rpm": self.rpm_spin.value(),
            "duration": self.duration_spin.value(),
        }
        try:
            CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
            CONFIG_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except OSError:
            # 配置存不下来不影响主流程,静默忽略
            pass

    def _load_config(self) -> None:
        try:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return

        self.base_url_edit.setText(data.get("base_url", ""))
        self.model_edit.setText(data.get("model", "gpt-3.5-turbo"))

        # 模态要在参数之前恢复 —— 参数是按 (模态, 参数名) 存的
        modality = data.get("modality", "text")
        index = self.modality_combo.findData(modality)
        if index >= 0:
            self.modality_combo.setCurrentIndex(index)
        else:
            modality = "text"

        # 存进去的值如果已经不在候选里了(比如程序升级后改了档位),就保持
        # 默认值,别把非法值塞进下拉框。
        for name, value in (data.get("params") or {}).items():
            combo = self.param_combos.get((modality, name))
            if combo is not None and combo.findText(str(value)) >= 0:
                combo.setCurrentText(str(value))

        self.rpm_spin.setValue(int(data.get("target_rpm", 60)))
        self.duration_spin.setValue(int(data.get("duration", 60)))

        # 目标 RPM 任何时候都是已知的(它由用户填写),不必等测试开始才显示
        self.lbl_target_rpm.setText(str(self.rpm_spin.value()))

    def closeEvent(self, event) -> None:
        if self._worker is not None and self._worker.isRunning():
            self._worker.request_stop()
            self._worker.wait(3000)
        event.accept()
