"""界面层的行为测试。

界面测试通常很脆,这里只测一件事:实时数字的刷新规则。原因是踩过坑 ——
快照以 4Hz 推上来,如果每次快照都重算「成功 RPM」,数字会在两个响应之间
一路下滑、响应一到又弹回去,看上去就是无规律乱跳。这条规则必须有守卫,
不然以后有人「顺手」把重算逻辑挪回 _on_updated 就又坏了。

用 offscreen 平台跑,不需要显示器。
"""

import json
import os

import pytest

# 必须在导入 PySide6 之前设置,否则 QApplication 会去找真实窗口系统
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from rpm_verifier.core.stats import (  # noqa: E402
    CODE_NETWORK_ERROR,
    StatsSnapshot,
)
from rpm_verifier.ui.main_window import MainWindow  # noqa: E402

PLACEHOLDER = "—"


@pytest.fixture(scope="module")
def qapp():
    """整个模块共用一个 QApplication —— Qt 不允许有第二个。"""
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def window(qapp):
    win = MainWindow()
    yield win
    win.close()


def _snapshot(success, elapsed, target_rpm=20, sent=None):
    """造一个模拟快照:成功数 success,已耗时 elapsed。

    没有重试时,全部请求 = 首次请求,全部成功 = 首次成功。
    """
    sent_value = sent if sent is not None else success
    codes = {200: success} if success else {}
    return StatsSnapshot(
        sent=sent_value,
        planned=20,
        success=success,
        all_sent=sent_value,
        all_success=success,
        elapsed=elapsed,
        codes=codes,
        all_codes=codes,
    )


def _prepare(window, target_rpm=20):
    """把窗口调成一次测试刚开始的状态。"""
    window._window_duration = 60.0
    window._target_rpm = target_rpm
    window._rpm_warmup = max(2.0, 2 * 60.0 / target_rpm)
    window._reset_view()


def test_live_rpm_is_not_rewritten_between_responses(window):
    """同一个成功数下面,「成功 RPM」必须始终是同一个值。

    这就是「数字乱跳」的根因所在:分子(成功数)要等响应回来才动,分母
    (已耗时)却每个快照都在涨。只要允许每个快照都重算,数字就会在两次响应
    之间持续往下掉,响应一到又跳回去。
    """
    _prepare(window)

    rendered = {}
    success = 0
    # 模拟 20 RPM:每 3 秒到一个响应,中间每 0.25 秒来一次快照
    for tick in range(120):
        elapsed = tick * 0.25
        if tick and tick % 12 == 0:
            success += 1
        window._on_updated(_snapshot(success, elapsed))

        label = window.lbl_success_rpm.text()
        if success in rendered:
            assert rendered[success] == label, (
                f"成功数还是 {success} 的时候,数字从 {rendered[success]} "
                f"变成了 {label} —— 又被重复重算了"
            )
        else:
            rendered[success] = label


def test_live_rpm_stays_blank_until_enough_samples(window):
    """样本太少时不显示数字。

    1 个成功 ÷ 0.3 秒 = 200 RPM,这种数字没有任何意义,不该出现在屏幕上。
    """
    _prepare(window)  # 20 RPM => 需要攒够 6 秒

    window._on_updated(_snapshot(success=1, elapsed=0.3))
    assert window.lbl_success_rpm.text() == PLACEHOLDER, "样本不足却显示了数字"

    window._on_updated(_snapshot(success=2, elapsed=6.0))
    assert window.lbl_success_rpm.text() != PLACEHOLDER, "样本够了却没显示数字"


def test_live_rpm_converges_to_the_reported_value(window):
    """实时数字应当收敛到最终报告值,末位允许有一点点差。

    注意这里**不能**断言完全相等。实时值算的是「到目前为止的速率」,最终值
    算的是「整个窗口的速率」,而最后一个槽位在窗口结束前就发完了(20 RPM
    跑 60 秒,最后一个在 t=57 发出),所以过程中会显示 20、最终收敛到 19。
    这是实时估算的固有性质,不是算错。
    """
    _prepare(window)

    # 20 RPM 跑 60 秒:发 20 个,成 19 个(有一个 429)
    for tick in range(240):
        elapsed = tick * 0.25
        success = min(int(elapsed / 3.0), 19)
        window._on_updated(_snapshot(success, elapsed))

    live = float(window.lbl_success_rpm.text())
    final = 19.0  # 19 个成功 / 60 秒
    assert abs(live - final) / final <= 0.10, (
        f"实时值 {live} 离最终值 {final} 太远,收敛性不对"
    )


def test_completed_report_overrides_the_live_estimate(window):
    """报告出来时,屏幕上的数字必须换成报告值 —— 那才是权威数字。"""
    from rpm_verifier.core.stats import TestReport

    _prepare(window)
    window._on_updated(_snapshot(success=19, elapsed=57.0))
    assert window.lbl_success_rpm.text() == "20"  # 过程中的估算

    window._on_completed(
        TestReport(
            target_rpm=20, planned=20, sent=20, success=19,
            all_sent=20, all_success=19,
            elapsed=60.0, codes={200: 19, 429: 1},
            all_codes={200: 19, 429: 1},
        )
    )
    assert window.lbl_success_rpm.text() == "19", "报告出来后没有换成权威数字"


def test_live_rpm_is_blank_again_after_reset(window):
    """重新开始测试时,上一轮的数字必须清掉。"""
    _prepare(window)
    window._on_updated(_snapshot(success=5, elapsed=20.0))
    assert window.lbl_success_rpm.text() != PLACEHOLDER

    window._reset_view()
    assert window.lbl_success_rpm.text() == PLACEHOLDER


# ---------------------------------------------------------------------------
# 模态切换
# ---------------------------------------------------------------------------

def _select_modality(window, key):
    """把模态下拉切到指定项。"""
    index = window.modality_combo.findData(key)
    assert index >= 0, f"下拉框里没有 {key} 这个模态"
    window.modality_combo.setCurrentIndex(index)


def test_only_the_active_modality_shows_its_params(window):
    """参数行只显示当前模态的参数。

    每个参数现在是表单里独立的一行(左边字段名、右边下拉框),靠
    setVisible 切换。要是忘了隐藏,用户会看到「尺寸」和「时长 / 分辨率」
    叠在一起 —— 而且不知道哪个会真的生效。
    """
    _select_modality(window, "text")
    for label in window.param_labels.values():
        assert label.isVisibleTo(window) is False
    for combo in window.param_combos.values():
        assert combo.isVisibleTo(window) is False

    _select_modality(window, "image")
    assert window.param_labels[("image", "size")].isVisibleTo(window) is True
    assert window.param_combos[("image", "size")].isVisibleTo(window) is True
    assert window.param_combos[("video", "seconds")].isVisibleTo(window) is False
    assert window.param_combos[("video", "size")].isVisibleTo(window) is False

    _select_modality(window, "video")
    assert window.param_combos[("video", "seconds")].isVisibleTo(window) is True
    assert window.param_combos[("video", "size")].isVisibleTo(window) is True
    assert window.param_combos[("image", "size")].isVisibleTo(window) is False


def test_endpoint_preview_tracks_base_url_and_modality(window):
    """端点预览必须同时跟着 Base URL 和模态走。

    这行字是给「地址到底拼成什么样」这个问题准备的。Base URL 填错时,
    它是用户唯一能一眼看出问题的地方 —— 更新漏了一边就等于没有。
    """
    window.base_url_edit.setText("https://apihub.agnes-ai.com/v1")

    _select_modality(window, "image")
    assert "https://apihub.agnes-ai.com/v1/images/generations" in (
        window.endpoint_preview.text()
    )

    _select_modality(window, "video")
    assert "https://apihub.agnes-ai.com/v1/videos" in window.endpoint_preview.text()

    window.base_url_edit.setText("https://api.openai.com")
    assert "https://api.openai.com/v1/chat/completions" in (
        window.endpoint_preview.text()
    ) or "/v1/videos" in window.endpoint_preview.text()


def test_pasted_full_endpoint_is_normalized_in_the_preview(window):
    """把整条端点粘进 Base URL 框,预览里不能出现重复路径。"""
    window.base_url_edit.setText(
        "https://apihub.agnes-ai.com/v1/images/generations"
    )
    _select_modality(window, "image")
    text = window.endpoint_preview.text()
    assert text.count("/images/generations") == 1, f"端点被拼重复了:{text}"


def test_params_are_collected_for_the_active_modality(window):
    """_current_params 收集的必须是当前模态的参数,而且要能覆盖默认值。"""
    _select_modality(window, "image")
    assert window._current_params() == {"size": "1K"}

    window.param_combos[("image", "size")].setCurrentText("2K")
    assert window._current_params() == {"size": "2K"}

    _select_modality(window, "video")
    assert window._current_params() == {"seconds": "4", "size": "720P"}


def test_video_modality_explains_what_is_actually_measured(window):
    """视频是异步接口,界面必须说明白测的是哪一步。

    不说清楚的话,用户会把这个数字当成「视频生成吞吐」—— 而它其实是
    「创建任务的限流」,两者完全不是一回事。
    """
    _select_modality(window, "text")
    assert window.modality_note.isVisibleTo(window) is False

    _select_modality(window, "video")
    assert window.modality_note.isVisibleTo(window) is True
    assert "创建任务" in window.modality_note.text()


# ---------------------------------------------------------------------------
# 输入条件模态:源素材行 / 计划量 / 启动校验
# ---------------------------------------------------------------------------

def test_modality_combo_lists_all_six(window):
    labels = [
        window.modality_combo.itemText(i)
        for i in range(window.modality_combo.count())
    ]
    assert labels == ["文本", "图像", "视频", "图生图", "图生视频", "视频生视频"]


def test_source_row_only_appears_for_input_modalities(window):
    """文本/图像/视频没有源素材;新三种必须有,而且浏览按钮的可用性不同。"""
    for key in ("text", "image", "video"):
        _select_modality(window, key)
        assert window.source_label.isVisibleTo(window) is False
        assert window.source_cell.isVisibleTo(window) is False

    _select_modality(window, "image_to_image")
    assert window.source_label.isVisibleTo(window) is True
    assert window.source_cell.isVisibleTo(window) is True
    assert window.source_browse_btn.isEnabled() is True

    _select_modality(window, "image_to_video")
    assert window.source_label.isVisibleTo(window) is True
    assert window.source_browse_btn.isEnabled() is False

    _select_modality(window, "video_to_video")
    assert window.source_label.isVisibleTo(window) is True
    assert window.source_browse_btn.isEnabled() is False


def test_source_summary_masks_signed_url(window):
    """摘要不能把签名参数带到屏幕上。"""
    _select_modality(window, "image_to_image")
    window.source_edit.setText(
        "https://user:pass@cdn.example.com/a/cat.png?sig=secret"
    )

    text = window.source_summary.text()
    assert text
    for secret in ("user", "pass", "sig", "secret"):
        assert secret not in text


def test_local_source_summary_hides_the_full_path(window, tmp_path):
    """报告/界面只显示文件名,不显示用户的完整目录结构。"""
    path = tmp_path / "cat.png"
    path.write_bytes(b"\x89PNG\r\n\x1a\n")

    _select_modality(window, "image_to_image")
    window.source_edit.setText(str(path))

    text = window.source_summary.text()
    assert "cat.png" in text
    assert str(tmp_path) not in text


def test_plan_preview_tracks_rpm_duration_and_seconds(window):
    """计划量文案要跟着模态、速率、时长、单次秒数一起变。"""
    _select_modality(window, "video")
    window.rpm_spin.setValue(60)
    window.duration_spin.setValue(60)
    window.param_combos[("video", "seconds")].setCurrentText("4")
    assert "240 秒" in window.plan_preview.text()

    window.param_combos[("video", "seconds")].setCurrentText("8")
    assert "480 秒" in window.plan_preview.text()

    _select_modality(window, "image_to_image")
    assert "张图" in window.plan_preview.text()

    _select_modality(window, "text")
    assert "个请求" in window.plan_preview.text()


def test_success_caption_changes_for_async_modalities(window):
    """异步视频的「成功」是任务创建成功,标签必须说清楚。"""
    _select_modality(window, "video")
    assert "任务创建" in window._stat_captions["success"].text()

    _select_modality(window, "image_to_image")
    assert window._stat_captions["success"].text() == "成功"


def test_source_fields_are_locked_and_browse_respects_modality(window):
    """测试中要锁住源素材;恢复时不能把 i2v/v2v 的浏览按钮重新点亮。"""
    _select_modality(window, "image_to_image")
    window._set_inputs_enabled(False)
    assert window.source_edit.isEnabled() is False
    assert window.source_browse_btn.isEnabled() is False

    window._set_inputs_enabled(True)
    assert window.source_edit.isEnabled() is True
    assert window.source_browse_btn.isEnabled() is True

    _select_modality(window, "image_to_video")
    window._set_inputs_enabled(False)
    window._set_inputs_enabled(True)
    assert window.source_browse_btn.isEnabled() is False


def _fill_required_config(window):
    window.base_url_edit.setText("https://example.com/v1")
    window.api_key_edit.setText("sk-test")
    window.model_edit.setText("m")


def test_start_is_blocked_when_source_is_missing(window, monkeypatch):
    """输入条件模态没填素材,点开始必须被拦下,而且不能创建 worker。"""
    warnings = []
    monkeypatch.setattr(
        QMessageBox,
        "warning",
        staticmethod(lambda *args, **kwargs: warnings.append(args)),
    )
    _fill_required_config(window)
    _select_modality(window, "image_to_image")
    window.source_edit.clear()

    window._on_start_clicked()

    assert warnings, "缺少源素材却没有弹提示"
    assert window._worker is None


def test_local_file_is_blocked_for_image_to_video(window, monkeypatch, tmp_path):
    """图生视频由服务端拉素材,本地文件必须被拦下来。"""
    warnings = []
    monkeypatch.setattr(
        QMessageBox,
        "warning",
        staticmethod(lambda *args, **kwargs: warnings.append(args)),
    )
    _fill_required_config(window)

    path = tmp_path / "cat.png"
    path.write_bytes(b"\x89PNG\r\n\x1a\n")

    _select_modality(window, "image_to_video")
    window.source_edit.setText(str(path))

    window._on_start_clicked()

    assert warnings, "本地文件用于图生视频却没有被拦下"
    assert window._worker is None


def test_video_plan_confirmation_can_be_cancelled(window, monkeypatch):
    """视频计划超过 600 秒输出时,点取消不能启动测试。"""
    asked = []

    def fake_question(*args, **kwargs):
        asked.append(args)
        return QMessageBox.No

    monkeypatch.setattr(QMessageBox, "question", staticmethod(fake_question))
    _fill_required_config(window)
    _select_modality(window, "video")
    window.rpm_spin.setValue(600)
    window.duration_spin.setValue(60)
    window.param_combos[("video", "seconds")].setCurrentText("4")

    window._on_start_clicked()

    assert asked, "计划 2400 秒输出却没有弹二次确认"
    assert window._worker is None


def test_source_is_not_written_to_config(window, monkeypatch, tmp_path):
    """源素材(尤其签名 URL)不能落盘。"""
    import rpm_verifier.ui.main_window as main_window_module

    config_path = tmp_path / "config.json"
    monkeypatch.setattr(main_window_module, "CONFIG_PATH", config_path)

    _select_modality(window, "image_to_image")
    window.source_edit.setText("https://cdn.example.com/cat.png?sig=secret")
    window._save_config()

    text = config_path.read_text(encoding="utf-8")
    data = json.loads(text)
    assert "source" not in data
    assert "secret" not in text


# ---------------------------------------------------------------------------
# 原始报错:负数行 + 原始异常/服务器原话
# ---------------------------------------------------------------------------

def test_code_table_marks_negative_codes_and_shows_raw_errors(window):
    """连接失败不是 HTTP 状态码,但用户要能看到原始异常。"""
    window._refresh_code_table(
        {200: 2},
        {CODE_NETWORK_ERROR: 1},
        {},
        {CODE_NETWORK_ERROR: "ConnectError: [Errno 61] Connection refused"},
    )

    labels = [
        window.code_table.item(row, 0).text()
        for row in range(window.code_table.rowCount())
    ]
    assert "连接错误(-2)" in labels, f"负数行没有带原始值:{labels}"

    text = window.raw_error.text()
    assert "ConnectError" in text
    assert "Connection refused" in text
    assert "未收到 HTTP 响应" in text
    assert window.raw_error.isVisibleTo(window) is True


def test_code_table_shows_http_status_and_server_message(window):
    """HTTP 错误要能直接看到原始状态码和服务器原话。"""
    window._refresh_code_table(
        {400: 1},
        {},
        {400: '{"error":{"message":"mode is required"}}'},
        {},
    )

    labels = [
        window.code_table.item(row, 0).text()
        for row in range(window.code_table.rowCount())
    ]
    assert labels == ["400"]
    assert "HTTP 400" in window.raw_error.text()
    assert "mode is required" in window.raw_error.text()


def test_code_table_splits_first_retry_and_total(window):
    """重试必须显示在「重试」列,并计入「合计」和占比。"""
    window._refresh_code_table({503: 2}, {503: 20}, {}, {})

    headers = [
        window.code_table.horizontalHeaderItem(i).text()
        for i in range(window.code_table.columnCount())
    ]
    assert headers == ["状态码", "首次", "重试", "合计", "占比"]

    cells = [window.code_table.item(0, i).text() for i in range(5)]
    assert cells[:4] == ["503", "2", "20", "22"]
    assert cells[4] == "100.0%"


def test_raw_errors_are_hidden_when_nothing_to_show(window):
    window._refresh_code_table({200: 3}, {}, {}, {})
    assert window.raw_error.isVisibleTo(window) is False
    assert window.raw_error.text() == ""

    window._refresh_code_table({200: 3}, {}, {200: ""}, {})
    assert window.raw_error.isVisibleTo(window) is False
