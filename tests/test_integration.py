"""端到端集成测试:用真实的本地 HTTP 服务器验证完整链路。

前面的单元测试用测试替身验证了调度逻辑;这里补上真实网络的部分 ——
连接池复用、状态码解析、重试时序,这些只有在真实网络栈上才验证得了。
"""

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from rpm_verifier.core.client import ApiClient
from rpm_verifier.core.modality import get_modality
from rpm_verifier.core.scheduler import RateScheduler
from rpm_verifier.core.source import resolve_source
from rpm_verifier.core.stats import StatsCollector, planned_count


class MockHandler(BaseHTTPRequestHandler):
    """一个可以按剧本返回状态码的假 API 服务器。"""

    script = []      # 依次返回的状态码;用完后一律返回 200
    received = []    # 收到的请求路径
    auth_headers = []
    bodies = []      # 收到的原始请求体,用来验证新模态的 JSON 结构

    def do_POST(self):
        type(self).received.append(self.path)
        type(self).auth_headers.append(self.headers.get("Authorization", ""))
        # 请求体必须读掉,否则客户端可能收到连接重置
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        type(self).bodies.append(body)

        code = type(self).script.pop(0) if type(self).script else 200
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"choices": []}')

    def log_message(self, *args):
        pass  # 别把服务器日志刷到测试输出里


@pytest.fixture
def mock_server():
    MockHandler.script = []
    MockHandler.received = []
    MockHandler.auth_headers = []
    MockHandler.bodies = []

    server = ThreadingHTTPServer(("127.0.0.1", 0), MockHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    host, port = server.server_address
    yield f"http://{host}:{port}/v1"

    server.shutdown()
    server.server_close()


def _run(base_url, target_rpm, duration):
    async def scenario():
        client = ApiClient(base_url, "sk-test", "gpt-3.5-turbo")
        try:
            collector = StatsCollector(target_rpm, planned_count(target_rpm, duration))
            scheduler = RateScheduler(client, collector, target_rpm, duration)
            return await scheduler.run()
        finally:
            await client.aclose()

    return asyncio.run(scenario())


def _run_client(client, target_rpm, duration):
    """用给定的 ApiClient 跑一小段测试,结束后关闭连接池。"""
    async def scenario():
        try:
            collector = StatsCollector(target_rpm, planned_count(target_rpm, duration))
            scheduler = RateScheduler(client, collector, target_rpm, duration)
            return await scheduler.run()
        finally:
            await client.aclose()

    return asyncio.run(scenario())


def test_end_to_end_status_code_stats(mock_server):
    """真实 HTTP 链路上,状态码统计要准确。"""
    MockHandler.script = [200] * 5 + [429] * 3 + [200] * 10

    report = _run(mock_server, target_rpm=300, duration=2.0)

    assert report.sent == 10           # 300 RPM × 2 秒 / 60
    assert report.codes == {200: 7, 429: 3}
    assert report.success == 7
    assert len(MockHandler.received) == 10
    assert all(p == "/v1/chat/completions" for p in MockHandler.received)


def test_end_to_end_sends_auth_header(mock_server):
    """鉴权头必须带上,否则用户会拿到一堆 401 却不知道为什么。"""
    _run(mock_server, target_rpm=60, duration=1.0)

    assert MockHandler.auth_headers
    assert MockHandler.auth_headers[0] == "Bearer sk-test"


def test_end_to_end_503_retry(mock_server):
    """503 在真实链路上会被重试;首次和重试分列,但都计入全部统计。"""
    MockHandler.script = [503, 503, 200]

    report = _run(mock_server, target_rpm=60, duration=1.0)

    assert report.sent == 1                       # 只发了 1 个首次请求
    assert report.codes == {503: 1}               # 首次请求的 503
    assert report.retry_codes == {503: 1, 200: 1} # 两次重试
    assert report.all_codes == {503: 2, 200: 1}   # 全部请求合并
    assert report.all_sent == 3                   # 1 首次 + 2 重试
    assert report.all_success == 1                # 重试成功的 200 也算成功
    assert report.retry_triggered == 1
    assert report.retry_success == 1
    # 服务器确实收到了 1 个首次 + 2 个重试
    assert len(MockHandler.received) == 3


def test_end_to_end_429_not_retried(mock_server):
    """端到端确认:429 不会被重试。

    这是产品最容易被误改的规则 —— 一旦重试,主统计的 RPM 就会虚高,
    用户再也找不到真实上限。
    """
    MockHandler.script = [429] * 20

    report = _run(mock_server, target_rpm=300, duration=2.0)

    assert report.retry_triggered == 0
    assert report.retry_codes == {}
    assert report.codes == {429: 10}
    # 一次请求对应一次服务器调用,没有任何额外的重试请求
    assert len(MockHandler.received) == 10


def test_end_to_end_401_circuit_break(mock_server):
    """401 立即熔断,不会把 60 秒跑满。"""
    MockHandler.script = [401] * 200

    report = _run(mock_server, target_rpm=600, duration=10.0)

    assert report.finished_early is True
    assert "401" in report.circuit_reason
    assert report.sent <= 3
    assert len(MockHandler.received) <= 3


# ---------------------------------------------------------------------------
# 输入条件模态:真实链路上的请求体
# ---------------------------------------------------------------------------

def test_end_to_end_image_to_image_body(mock_server):
    """图生图的输入图必须在 extra_body.image 里,响应格式要 URL。"""
    client = ApiClient(
        mock_server,
        "sk-test",
        "agnes-image-2.5-flash",
        modality="image_to_image",
        source="https://cdn.example.com/cat.png?token=secret",
    )

    report = _run_client(client, target_rpm=60, duration=1.0)

    assert report.sent == 1
    assert MockHandler.received == ["/v1/images/generations"]
    body = json.loads(MockHandler.bodies[0])
    assert body["model"] == "agnes-image-2.5-flash"
    assert body["extra_body"]["image"] == [
        "https://cdn.example.com/cat.png?token=secret"
    ]
    assert body["extra_body"]["response_format"] == "url"
    assert "image" not in body


def test_end_to_end_image_to_video_body(mock_server):
    """图生视频:keyframe + first_frame,不能带 images/audios/videos。"""
    client = ApiClient(
        mock_server,
        "sk-test",
        "agnes-video-2.5",
        modality="image_to_video",
        params={"seconds": "8", "size": "1080P"},
        source="https://cdn.example.com/cat.png",
    )

    report = _run_client(client, target_rpm=60, duration=1.0)

    assert report.sent == 1
    assert MockHandler.received == ["/v1/videos"]
    body = json.loads(MockHandler.bodies[0])
    assert body["mode"] == "keyframe"
    assert body["first_frame"] == "https://cdn.example.com/cat.png"
    assert body["seconds"] == "8"
    assert body["size"] == "1080P"
    assert not {"images", "audios", "videos"} & body.keys()


def test_end_to_end_video_to_video_body(mock_server):
    """视频生视频:reference + videos 数组。"""
    client = ApiClient(
        mock_server,
        "sk-test",
        "agnes-video-2.5",
        modality="video_to_video",
        source="https://cdn.example.com/ref.mp4",
    )

    report = _run_client(client, target_rpm=60, duration=1.0)

    assert report.sent == 1
    assert MockHandler.received == ["/v1/videos"]
    body = json.loads(MockHandler.bodies[0])
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


def test_end_to_end_local_image_becomes_data_uri(mock_server, tmp_path):
    """本地图片在真实链路上必须变成 Data URI,而不是被当成 URL 发出去。"""
    path = tmp_path / "cat.png"
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"hello")
    ref = resolve_source(get_modality("image_to_image"), str(path))

    client = ApiClient(
        mock_server,
        "sk-test",
        "agnes-image-2.5-flash",
        modality="image_to_image",
        source=ref.value,
    )

    _run_client(client, target_rpm=60, duration=1.0)

    body = json.loads(MockHandler.bodies[0])
    images = body["extra_body"]["image"]
    assert len(images) == 1
    assert images[0].startswith("data:image/png;base64,")
