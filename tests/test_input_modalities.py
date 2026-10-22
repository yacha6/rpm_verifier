"""输入条件模态的单元测试:请求体、源素材解析、计划量、探针提示。

这些测试完全不依赖 PySide6 —— 素材解析和请求体构造都收在 core 里,
必须能脱离界面验证。
"""

from __future__ import annotations

import asyncio
import base64
from pathlib import Path

import httpx
import pytest

from rpm_verifier.core.client import ApiClient, ProbeResult
from rpm_verifier.core.modality import (
    MODALITY_ORDER,
    get_modality,
    plan_summary,
    planned_video_seconds,
)
from rpm_verifier.core.source import (
    SourceError,
    preview_source,
    resolve_source,
)


# ---------------------------------------------------------------------------
# 模态注册与请求体
# ---------------------------------------------------------------------------

def test_six_modalities_are_registered():
    """下拉框要有六项,而且顺序稳定:老三项在前,新三项追加在后。"""
    assert [spec.key for spec in MODALITY_ORDER] == [
        "text",
        "image",
        "video",
        "image_to_image",
        "image_to_video",
        "video_to_video",
    ]
    assert [spec.label for spec in MODALITY_ORDER] == [
        "文本",
        "图像",
        "视频",
        "图生图",
        "图生视频",
        "视频生视频",
    ]


def test_image_to_image_body_shape():
    """图生图:输入图必须嵌套在 extra_body.image 里,而且必须是数组。"""
    spec = get_modality("image_to_image")
    body = spec.build_body(
        "agnes-image-2.5-flash",
        {"size": "2K"},
        "https://cdn.example.com/cat.png?token=secret",
    )

    assert body["model"] == "agnes-image-2.5-flash"
    assert body["size"] == "2K"
    assert body["extra_body"]["image"] == [
        "https://cdn.example.com/cat.png?token=secret"
    ]
    assert body["extra_body"]["response_format"] == "url"
    # 顶层 image 和 tags 都不能有:官方明确说不要这两样。
    assert "image" not in body
    assert "tags" not in body


def test_image_to_video_body_shape():
    """图生视频:keyframe + first_frame,不能带 images/audios/videos。"""
    spec = get_modality("image_to_video")
    body = spec.build_body(
        "agnes-video-2.5",
        {"seconds": "8", "size": "1080P"},
        "https://cdn.example.com/cat.png",
    )

    assert body["mode"] == "keyframe"
    assert body["first_frame"] == "https://cdn.example.com/cat.png"
    assert body["seconds"] == "8"
    assert body["size"] == "1080P"
    assert not {"images", "audios", "videos"} & body.keys()


def test_video_to_video_body_shape():
    """视频生视频:reference + videos 数组,不能带 first_frame/images。"""
    spec = get_modality("video_to_video")
    body = spec.build_body(
        "agnes-video-2.5",
        {"seconds": "4", "size": "720P"},
        "https://cdn.example.com/ref.mp4",
    )

    assert body["mode"] == "reference"
    assert body["videos"] == [
        {
            "url": "https://cdn.example.com/ref.mp4",
            "start_seconds": 0,
            "require_audio": False,
        }
    ]
    assert "first_frame" not in body
    assert "images" not in body


def test_new_modalities_hit_the_shared_endpoints():
    """图生图和图像共用 /images/generations;两种视频模态和视频共用 /videos。"""
    expected = {
        "image_to_image": "https://example.com/v1/images/generations",
        "image_to_video": "https://example.com/v1/videos",
        "video_to_video": "https://example.com/v1/videos",
    }
    for modality, endpoint in expected.items():
        spec = get_modality(modality)
        source = (
            "https://cdn.example.com/ref.mp4"
            if spec.source_kind == "video"
            else "https://cdn.example.com/cat.png"
        )
        client = ApiClient(
            "https://example.com/v1",
            "sk-test",
            "m",
            modality=modality,
            source=source,
        )
        assert client._endpoint == endpoint


def test_source_modalities_refuse_an_empty_source():
    """输入条件模态没素材就不该构造出请求,别等到服务端 400 才发现。"""
    for modality in ("image_to_image", "image_to_video", "video_to_video"):
        with pytest.raises(ValueError):
            ApiClient(
                "https://example.com/v1",
                "sk-test",
                "m",
                modality=modality,
            )


def test_payload_is_serialized_once_with_the_source():
    """请求体在客户端构造时就序列化好,内容里要带完整素材。"""
    client = ApiClient(
        "https://example.com/v1",
        "sk-test",
        "m",
        modality="image_to_image",
        source="https://cdn.example.com/cat.png",
    )
    payload = client._payload.decode("utf-8")
    assert "https://cdn.example.com/cat.png" in payload
    assert '"response_format":"url"' in payload


# ---------------------------------------------------------------------------
# 源素材:URL
# ---------------------------------------------------------------------------

def test_preview_masks_query_credentials_and_fragment():
    """摘要不能泄露签名参数,也不能泄露 URL 里的用户名密码。"""
    spec = get_modality("image_to_image")
    preview = preview_source(
        spec,
        "https://user:pass@cdn.example.com/a/b/cat.png?sig=secret#fragment",
    )

    assert preview.kind == "url"
    assert "cdn.example.com" in preview.summary
    assert "cat.png" in preview.summary
    for secret in ("user", "pass", "sig", "secret", "fragment"):
        assert secret not in preview.summary


def test_preview_warns_on_localhost_and_private_hosts():
    """本机/内网地址给非阻塞提示,但不直接拦 —— 自建网关可能真的能访问。"""
    spec = get_modality("video_to_video")
    for url in (
        "http://localhost/ref.mp4",
        "http://127.0.0.1/ref.mp4",
        "http://192.168.1.10/ref.mp4",
    ):
        preview = preview_source(spec, url)
        assert preview.warning, f"{url} 没有给出内网提示"


def test_url_validation_rejects_bad_addresses():
    spec = get_modality("image_to_image")
    for raw in (
        "ftp://example.com/cat.png",
        "https://",
        "https://exa mple.com/cat.png",
        "https://example.com:99999/cat.png",
    ):
        with pytest.raises(SourceError):
            preview_source(spec, raw)


def test_empty_source_is_rejected():
    spec = get_modality("image_to_image")
    with pytest.raises(SourceError):
        preview_source(spec, "   ")


def test_video_modalities_reject_local_files(tmp_path):
    """视频接口由服务端拉素材,本地文件必须被拦下来并说清楚原因。"""
    path = tmp_path / "cat.png"
    path.write_bytes(b"\x89PNG\r\n\x1a\n")

    for modality in ("image_to_video", "video_to_video"):
        with pytest.raises(SourceError) as excinfo:
            preview_source(get_modality(modality), str(path))
        assert "公开" in str(excinfo.value) or "本地文件" in str(excinfo.value)


# ---------------------------------------------------------------------------
# 源素材:本地图片
# ---------------------------------------------------------------------------

def test_local_image_becomes_a_data_uri(tmp_path):
    spec = get_modality("image_to_image")
    path = tmp_path / "cat.png"
    data = b"\x89PNG\r\n\x1a\n" + b"hello"
    path.write_bytes(data)

    ref = resolve_source(spec, str(path))

    assert ref.kind == "data_uri"
    assert ref.is_local is True
    assert ref.value.startswith("data:image/png;base64,")
    assert base64.b64decode(ref.value.split(",", 1)[1]) == data
    assert ref.summary.startswith("本地文件 cat.png")
    assert "Data URI" in ref.summary


def test_local_file_is_read_exactly_once(tmp_path, monkeypatch):
    """编码只做一次。每个请求都重读文件的话,高 RPM 下工具自己先成为瓶颈。"""
    spec = get_modality("image_to_image")
    path = tmp_path / "cat.png"
    path.write_bytes(b"\x89PNG\r\n\x1a\n")

    calls = []
    original = Path.read_bytes

    def counting(self):
        calls.append(self)
        return original(self)

    monkeypatch.setattr(Path, "read_bytes", counting)

    resolve_source(spec, str(path))
    assert len(calls) == 1, f"文件被读了 {len(calls)} 次,应该只读 1 次"


def test_local_image_rejects_unsupported_suffix(tmp_path):
    spec = get_modality("image_to_image")
    path = tmp_path / "cat.gif"
    path.write_bytes(b"GIF89a")
    with pytest.raises(SourceError):
        preview_source(spec, str(path))


def test_local_image_rejects_empty_file(tmp_path):
    spec = get_modality("image_to_image")
    path = tmp_path / "cat.png"
    path.write_bytes(b"")
    with pytest.raises(SourceError):
        preview_source(spec, str(path))


def test_local_image_rejects_files_over_the_cap(tmp_path, monkeypatch):
    import rpm_verifier.core.source as source_module

    monkeypatch.setattr(source_module, "MAX_LOCAL_IMAGE_BYTES", 10)
    spec = get_modality("image_to_image")
    path = tmp_path / "cat.png"
    path.write_bytes(b"x" * 11)
    with pytest.raises(SourceError):
        preview_source(spec, str(path))


def test_large_local_image_gets_a_warning(tmp_path, monkeypatch):
    import rpm_verifier.core.source as source_module

    monkeypatch.setattr(source_module, "LARGE_IMAGE_WARN_BYTES", 10)
    monkeypatch.setattr(source_module, "MAX_LOCAL_IMAGE_BYTES", 100)
    spec = get_modality("image_to_image")
    path = tmp_path / "cat.png"
    path.write_bytes(b"x" * 20)

    preview = preview_source(spec, str(path))
    assert preview.warning


# ---------------------------------------------------------------------------
# 计划量与探针
# ---------------------------------------------------------------------------

def test_plan_summary_for_each_plan_unit():
    assert "个请求" in plan_summary(get_modality("text"), 60, {})
    assert "张图" in plan_summary(get_modality("image_to_image"), 30, {})
    assert "240 秒" in plan_summary(
        get_modality("video"), 60, {"seconds": "4"}
    )
    assert "输入视频时长另行计费" in plan_summary(
        get_modality("video_to_video"), 2, {"seconds": "4"}
    )


def test_planned_video_seconds_only_counts_video_modalities():
    assert planned_video_seconds(get_modality("video"), 6, {"seconds": "8"}) == 48
    assert planned_video_seconds(get_modality("image_to_image"), 60, {}) == 0
    assert planned_video_seconds(get_modality("text"), 60, {}) == 0


def test_payload_and_media_errors_abort_the_probe():
    """413/415 是配置错误,和 400/422 一样应该在探针阶段拦下来。"""
    for code in (400, 401, 403, 404, 413, 415, 422):
        assert ProbeResult(code).fatal, f"{code} 应该中止测试"
    for code in (200, 201, 429, 500, 503):
        assert not ProbeResult(code).fatal, f"{code} 不该中止测试"


def _probe_with_status(modality: str, source: str, code: int):
    def handler(request):
        return httpx.Response(code, json={"error": {"message": "image is required"}})

    async def scenario():
        client = ApiClient(
            "https://example.com/v1",
            "sk-test",
            "m",
            modality=modality,
            source=source,
        )
        await client._client.aclose()
        client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            return await client.probe()
        finally:
            await client.aclose()

    return asyncio.run(scenario())


def test_i2i_probe_hint_points_at_the_source_image():
    probe = _probe_with_status(
        "image_to_image", "https://cdn.example.com/cat.png", 400
    )
    assert "源图片" in probe.message
    assert probe.detail == "image is required"
    assert probe.endpoint.endswith("/images/generations")


def test_v2v_probe_hint_points_at_the_source_video():
    probe = _probe_with_status(
        "video_to_video", "https://cdn.example.com/ref.mp4", 400
    )
    assert "源视频" in probe.message
    assert probe.detail == "image is required"
    assert probe.endpoint.endswith("/videos")
