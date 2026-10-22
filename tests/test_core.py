"""core 层的单元测试。

这些测试完全不依赖 PySide6 —— 这正是 core / ui 分层的意义所在:
调度精度是产品的核心资产,必须能被自动化验证。
"""

import asyncio
import time

import httpx
import pytest

from rpm_verifier.core.client import (
    ApiClient,
    ProbeResult,
    SendOutcome,
    normalize_base_url,
)
from rpm_verifier.core.modality import MODALITIES, get_modality
from rpm_verifier.core.scheduler import RateScheduler
from rpm_verifier.core.stats import (
    CODE_CONNECT_TIMEOUT,
    CODE_INCOMPLETE,
    CODE_NETWORK_ERROR,
    CODE_POOL_TIMEOUT,
    CODE_TIMEOUT,
    StatsCollector,
    TestReport,
    code_label,
    is_success,
    planned_count,
)


# ---------------------------------------------------------------------------
# 测试替身
# ---------------------------------------------------------------------------

class FakeClient:
    """假的 API 客户端,用来在没有真实网络的情况下验证调度精度。"""

    def __init__(self, code=200, delay=0.0):
        self.code = code
        self.delay = delay
        self.emit_times = []

    async def send_with_retry(self, deadline):
        self.emit_times.append(time.perf_counter())
        if self.delay:
            await asyncio.sleep(self.delay)
        return SendOutcome(first_code=self.code)


class RetryFakeClient:
    """模拟「首次 503,重试后成功」的客户端。"""

    async def send_with_retry(self, deadline):
        return SendOutcome(
            first_code=503,
            retry_triggered=True,
            retry_success=True,
            retry_codes=[200],
        )


def run(coro):
    """在同步测试里跑协程,避免引入 pytest-asyncio 依赖。"""
    return asyncio.run(coro)


async def _run_scheduler(client, target_rpm, duration):
    collector = StatsCollector(target_rpm, planned_count(target_rpm, duration))
    scheduler = RateScheduler(client, collector, target_rpm, duration)
    return await scheduler.run()


# ---------------------------------------------------------------------------
# 纯函数
# ---------------------------------------------------------------------------

def test_planned_count():
    """AC1 的计算依据:60 RPM 跑 60 秒应该正好是 60 个请求。"""
    assert planned_count(60, 60) == 60
    assert planned_count(100, 60) == 100
    assert planned_count(600, 30) == 300
    assert planned_count(1, 10) == 0  # 10 秒内发不出 1 个,取整为 0


def test_normalize_base_url():
    """Base URL 归一化:补全 /v1,但不猜测已有版本段的路径。"""
    assert normalize_base_url("https://api.openai.com") == "https://api.openai.com/v1"
    assert normalize_base_url("https://api.openai.com/") == "https://api.openai.com/v1"
    assert normalize_base_url("https://api.deepseek.com/v1") == "https://api.deepseek.com/v1"
    # 中转站常见的嵌套路径,原样保留
    assert normalize_base_url("https://proxy.example.com/openai/v1") == (
        "https://proxy.example.com/openai/v1"
    )


def test_code_label():
    """没有 HTTP 状态码的失败要显示成人看得懂的文本。

    注意「响应超时」不能简写成「超时」—— 用户看到它会以为是 504 之类的
    状态码,而实际上这些请求一个字节都没收到。
    """
    assert code_label(200) == "200"
    assert code_label(429) == "429"
    assert code_label(CODE_TIMEOUT) == "响应超时"
    assert code_label(CODE_NETWORK_ERROR) == "连接错误"
    assert code_label(CODE_CONNECT_TIMEOUT) == "连接超时"
    assert code_label(CODE_POOL_TIMEOUT) == "连接池耗尽"
    assert code_label(CODE_INCOMPLETE) == "未返回"


def test_rpm_calculation():
    """PRD 附录 A 的数字流:目标 100,成功 87。"""
    report = TestReport(
        target_rpm=100, planned=100, sent=100, success=87, elapsed=60.0
    )
    assert round(report.send_rpm) == 100
    assert round(report.success_rpm) == 87


# ---------------------------------------------------------------------------
# 调度精度(产品核心)
# ---------------------------------------------------------------------------

def test_scheduler_emits_exact_count():
    """AC1:按目标速率发出准确数量的请求。

    用 300 RPM 跑 2 秒等价验证 60 RPM 跑 60 秒(都是 10 个 / 300 个请求的
    同比例),但测试只需 2 秒。
    """
    report = run(_run_scheduler(FakeClient(), target_rpm=300, duration=2.0))

    assert report.planned == 10
    assert report.sent == 10
    assert report.success == 10
    assert report.codes == {200: 10}
    assert report.dropped == 0


def test_scheduler_interval_has_no_drift():
    """AC4:间隔必须均匀,且不随时间累积漂移。

    这是绝对时间锚点与 `await asyncio.sleep(interval)` 的分水岭 —— 后者会
    让最后一个请求明显迟到。
    """
    client = FakeClient()
    run(_run_scheduler(client, target_rpm=600, duration=1.0))

    times = client.emit_times
    assert len(times) == 10  # 600 RPM × 1 秒 / 60

    gaps = [b - a for a, b in zip(times, times[1:])]
    for gap in gaps:
        assert abs(gap - 0.1) < 0.03, f"间隔偏离 100ms 过多: {gap:.4f}s"

    # 9 个间隔 × 100ms = 0.9s。若有累积漂移,这个跨度会明显偏大。
    span = times[-1] - times[0]
    assert abs(span - 0.9) < 0.05, f"出现累积漂移,跨度 {span:.4f}s"


def test_send_rpm_matches_target():
    """AC4:目标 RPM 与实际发送 RPM 的偏差应小于 2%。"""
    report = run(_run_scheduler(FakeClient(), target_rpm=600, duration=1.0))
    assert abs(report.send_rpm - 600) / 600 < 0.02


# ---------------------------------------------------------------------------
# 重试与统计分离(PRD D6)
# ---------------------------------------------------------------------------

def test_retries_are_split_but_counted_in_all_stats():
    """重试请求必须计入全部请求统计,同时保留首次 / 重试两列。

    v1.0 曾把重试完全排除在主统计外。验证 RPM 时这个口径不对:503 重试
    同样打到了 API、消耗了额度,不能从状态码表和 RPM 里消失。现在
    `codes` 只保留首次,`retry_codes` 单独保留,`all_codes` 是两者合并。
    """
    report = run(_run_scheduler(RetryFakeClient(), target_rpm=60, duration=1.0))

    assert report.codes == {503: 1}         # 首次请求的 503
    assert report.retry_codes == {200: 1}   # 重试请求的 200
    assert report.all_codes == {503: 1, 200: 1}
    assert report.all_sent == 2
    assert report.all_success == 1
    assert report.retry_triggered == 1
    assert report.retry_success == 1
    assert report.retry_failed == 0


def test_no_retry_on_429():
    """PRD D3:429 是要观测的对象,绝不能被重试。

    这条测试守的是一个很容易被「顺手优化」掉的设计决策:如果哪天有人给
    重试逻辑加上了 429,主统计的 RPM 会虚高,用户就再也找不到真实上限了。
    """
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(429)

    async def scenario():
        client = ApiClient("https://example.com/v1", "sk-test", "gpt-3.5-turbo")
        await client._client.aclose()
        client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            return await client.send_with_retry(time.perf_counter() + 5)
        finally:
            await client.aclose()

    outcome = run(scenario())

    assert outcome.first_code == 429
    assert outcome.retry_triggered is False
    assert len(calls) == 1, "429 被重试了,这会污染速率测量"


def test_retry_on_503_then_success():
    """503 在窗口内自动重试,直到成功。"""
    codes = [503, 503, 200]

    def handler(request):
        return httpx.Response(codes.pop(0) if codes else 200)

    async def scenario():
        client = ApiClient("https://example.com/v1", "sk-test", "gpt-3.5-turbo")
        await client._client.aclose()
        client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            return await client.send_with_retry(time.perf_counter() + 5)
        finally:
            await client.aclose()

    outcome = run(scenario())

    assert outcome.first_code == 503
    assert outcome.retry_triggered is True
    assert outcome.retry_success is True
    assert outcome.retry_failed is False
    assert outcome.retry_codes == [503, 200]


def test_retry_stops_at_deadline():
    """窗口结束即停止重试 —— deadline 是重试的硬边界。"""

    def handler(request):
        return httpx.Response(503)

    async def scenario():
        client = ApiClient("https://example.com/v1", "sk-test", "gpt-3.5-turbo")
        await client._client.aclose()
        client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            # deadline 设成已经过去的时刻
            return await client.send_with_retry(time.perf_counter() - 1)
        finally:
            await client.aclose()

    outcome = run(scenario())

    assert outcome.first_code == 503
    assert outcome.retry_triggered is True
    assert outcome.retry_codes == [], "窗口已结束,不应该再发出重试请求"
    assert outcome.retry_failed is True


# ---------------------------------------------------------------------------
# 熔断
# ---------------------------------------------------------------------------

def test_circuit_break_on_401():
    """AC3:填入无效 Key 后应立即熔断,不跑满整个测试时长。"""
    report = run(_run_scheduler(FakeClient(code=401), target_rpm=600, duration=10.0))

    assert report.finished_early is True
    assert "401" in report.circuit_reason
    assert report.sent < report.planned
    assert report.sent <= 3, f"熔断太慢,已经发出 {report.sent} 个请求"


def test_circuit_break_on_repeated_network_errors():
    """连续网络错误达到阈值后熔断,单次抖动则不会。"""
    report = run(
        _run_scheduler(FakeClient(code=-2), target_rpm=600, duration=10.0)
    )

    assert report.finished_early is True
    assert "网络错误" in report.circuit_reason


def test_no_circuit_break_on_429():
    """429 绝不能触发熔断 —— 它是本工具要观测的正常信号。"""
    report = run(_run_scheduler(FakeClient(code=429), target_rpm=600, duration=1.0))

    assert report.finished_early is False
    assert report.circuit_reason == ""
    assert report.sent == 10
    assert report.codes == {429: 10}


# ---------------------------------------------------------------------------
# 提前停止
# ---------------------------------------------------------------------------

def test_manual_stop_finishes_early():
    """用户中途点停止,应按实际时长折算 RPM。"""

    async def scenario():
        client = FakeClient(delay=0.05)
        collector = StatsCollector(600, planned_count(600, 10.0))
        scheduler = RateScheduler(client, collector, target_rpm=600, duration=10.0)

        async def stop_soon():
            await asyncio.sleep(0.5)
            scheduler.request_stop()

        asyncio.create_task(stop_soon())
        return await scheduler.run()

    report = run(scenario())

    assert report.finished_early is True
    assert report.sent < report.planned
    assert report.elapsed < 10.0


# ---------------------------------------------------------------------------
# 在途请求上限
# ---------------------------------------------------------------------------

def test_dropped_slots_are_recorded():
    """在途请求超过上限时丢弃槽位,并且必须记录下来。

    不记录的话,用户会以为工具按目标速率发满了,实际却漏发了。
    """

    async def scenario():
        client = FakeClient(delay=1.0)  # 响应很慢,在途数会堆积
        collector = StatsCollector(600, planned_count(600, 1.0))
        scheduler = RateScheduler(
            client, collector, target_rpm=600, duration=1.0, max_inflight=3
        )
        return await scheduler.run()

    report = run(scenario())

    assert report.dropped > 0, "在途上限没有生效"
    assert report.sent + report.dropped == report.planned


# ---------------------------------------------------------------------------
# 异常分类
# ---------------------------------------------------------------------------

def test_timeout_types_are_classified_separately():
    """三种超时必须分得开。

    它们都是 TimeoutException 的子类,判断顺序写反就会被吞成同一个
    「响应超时」。那样用户会把「本机连接池不够用」和「网络连不上」统统
    怪到 API 头上 —— 而这正是这个工具最不该犯的错。
    """
    from rpm_verifier.core.client import classify_error

    request = httpx.Request("POST", "https://example.com/v1/chat/completions")

    assert classify_error(httpx.PoolTimeout("pool", request=request)) == CODE_POOL_TIMEOUT
    assert classify_error(
        httpx.ConnectTimeout("connect", request=request)
    ) == CODE_CONNECT_TIMEOUT
    assert classify_error(httpx.ReadTimeout("read", request=request)) == CODE_TIMEOUT
    assert classify_error(
        httpx.ConnectError("refused", request=request)
    ) == CODE_NETWORK_ERROR


def test_timeout_is_not_an_http_status_code():
    """负数常量不能和真实状态码撞号,否则表格会混乱。"""
    from rpm_verifier.core.stats import NON_HTTP_CODES

    for code in NON_HTTP_CODES:
        assert code < 0, f"{code} 是负数常量区,不该出现非负值"
    # 每个常量的标签都得有,而且互不相同
    labels = [code_label(c) for c in NON_HTTP_CODES]
    assert len(labels) == len(set(labels)), "有负数常量共用了同一个标签"
    assert "" not in labels


# ---------------------------------------------------------------------------
# 账目配平
# ---------------------------------------------------------------------------

class HangingClient:
    """一个响应永远不返回的客户端,用来逼出收尾超时。"""

    def __init__(self):
        self.started = 0

    async def send_with_retry(self, deadline):
        self.started += 1
        await asyncio.sleep(3600)  # 等到天荒地老
        return SendOutcome(first_code=200)


def test_unfinished_requests_are_counted_as_incomplete():
    """收尾等不及的请求必须记成「未返回」,不能让状态码凭空消失。

    这是真实踩过的坑:客户端超时 30 秒,收尾却只等 5 秒,于是耗时 5~30 秒
    的请求被 cancel,CancelledError 一路抛出去,谁也没记账。用户看到的是
    「已发送 20,状态码只有 19」—— 一个自己都对不上账的观测工具,数字就没
    人敢信了。
    """

    async def scenario():
        client = HangingClient()
        collector = StatsCollector(60, planned_count(60, 1.0))
        scheduler = RateScheduler(
            client, collector, target_rpm=60, duration=1.0, drain_grace=0.3
        )
        return await scheduler.run()

    report = run(scenario())

    assert report.codes.get(CODE_INCOMPLETE, 0) > 0, "卡住的请求没有被记为「未返回」"


def test_every_sent_request_lands_in_exactly_one_bucket():
    """不变式:发出去的每一个请求,都必须恰好落进一个状态码桶。

    这条一旦破了,主统计表就会和「已发送」对不上,而用户正是拿这两个数
    互相印证的。
    """
    cases = [
        # (状态码, 响应耗时, 收尾等待上限)
        (200, 0.0, 5.0),    # 正常
        (429, 0.02, 5.0),   # 撞限流
        (503, 0.05, 5.0),   # 触发重试
        (200, 0.4, 5.0),    # 响应偏慢
        (200, 1.0, 0.1),    # 收尾等不及,必须有请求被记成「未返回」
    ]
    for code, delay, grace in cases:
        async def scenario(code=code, delay=delay, grace=grace):
            client = FakeClient(code=code, delay=delay)
            collector = StatsCollector(120, planned_count(120, 1.0))
            scheduler = RateScheduler(
                client, collector, target_rpm=120, duration=1.0, drain_grace=grace
            )
            return await scheduler.run()

        report = run(scenario())
        assert report.responded == report.sent, (
            f"code={code} delay={delay} grace={grace}:已发送 {report.sent} "
            f"但只记录了 {report.responded} 个状态码"
        )


# ---------------------------------------------------------------------------
# 多模态:文本 / 图像 / 视频
# ---------------------------------------------------------------------------

def test_each_modality_hits_its_own_endpoint():
    """三种模态各自打自己的端点。

    端点写死成 /chat/completions 的年代,图像模型会被拼成
    `.../v1/images/generations/chat/completions`,然后 404 —— 而错误提示
    还在说「请检查模型名是否正确」。
    """
    expected = {
        "text": "https://example.com/v1/chat/completions",
        "image": "https://example.com/v1/images/generations",
        "video": "https://example.com/v1/videos",
    }
    for modality, endpoint in expected.items():
        client = ApiClient("https://example.com/v1", "sk-test", "m", modality=modality)
        assert client._endpoint == endpoint


def test_full_endpoint_pasted_as_base_url_still_works():
    """把整条端点 URL 粘进 Base URL 框,也必须能work。

    这是用户真实踩过的坑:从文档的 curl 示例里复制了
    `https://apihub.agnes-ai.com/v1/images/generations` 填进 Base URL,
    得到的却是 404,而提示还把人往「模型名写错了」的方向带。
    """
    client = ApiClient(
        "https://apihub.agnes-ai.com/v1/images/generations",
        "sk-test",
        "agnes-image-2.5-flash",
        modality="image",
    )
    assert client._endpoint == "https://apihub.agnes-ai.com/v1/images/generations"


def test_normalize_strips_every_known_endpoint():
    """已知的端点后缀都要能剥掉,带尾斜杠的也要。"""
    for suffix in ("/chat/completions", "/images/generations", "/videos"):
        url = f"https://api.example.com/v1{suffix}"
        assert normalize_base_url(url) == "https://api.example.com/v1"
        assert normalize_base_url(url + "/") == "https://api.example.com/v1"

    # 带任务 ID 的路径不在已知后缀里,不要自作聪明去猜
    assert normalize_base_url("https://api.example.com/v1/videos/task_123") == (
        "https://api.example.com/v1/videos/task_123"
    )


def test_text_body_is_unchanged_when_modality_is_omitted():
    """不指定模态时行为必须和以前一模一样 —— 老调用点不该受影响。"""
    client = ApiClient("https://example.com/v1", "sk-test", "gpt-3.5-turbo")
    assert client._body() == {
        "model": "gpt-3.5-turbo",
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 1,
        "stream": False,
    }


def test_image_and_video_bodies_default_to_the_cheapest_tier():
    """请求参数默认取最低成本档 —— 这是用户明确敲定的。

    图像按张、视频按秒计费,默认值选错会让一次压测烧掉几十美元。
    """
    image = ApiClient("https://example.com/v1", "sk-test", "img", modality="image")
    body = image._body()
    assert body["size"] == "1K"
    assert body["prompt"], "prompt 是必填项,不能空着"
    assert "messages" not in body, "图像端点不认 messages"

    video = ApiClient("https://example.com/v1", "sk-test", "vid", modality="video")
    body = video._body()
    assert body["seconds"] == "4"
    assert body["size"] == "720P"


def test_video_body_includes_the_required_mode():
    """视频的 mode 是必填项,漏了直接 400。

    这条是真金白银换来的:文档参数表里 mode 标着「必填」,我漏了,用户拿到
    一个没有任何说明的 400。测试守住它,免得以后有人觉得这个字段多余。
    """
    client = ApiClient("https://example.com/v1", "k", "vid", modality="video")
    body = client._body()
    assert body["mode"] == "text"
    assert body["prompt"], "prompt 也是必填"
    assert body["model"]


def test_modality_params_override_the_defaults():
    """界面选的值要真的进到请求体里。"""
    client = ApiClient(
        "https://example.com/v1",
        "sk-test",
        "vid",
        modality="video",
        params={"seconds": "8", "size": "1080P"},
    )
    body = client._body()
    assert body["seconds"] == "8"
    assert body["size"] == "1080P"


def test_every_modality_declares_a_working_body_builder():
    """每个模态的 build_body 都得能跑通,而且必须带 model。

    加模态时最容易漏的就是这个 —— 声明里写了,构造请求体时忘了带模型名,
    于是整个模态一跑就是 400。
    """
    for key, spec in MODALITIES.items():
        body = spec.build_body("test-model", {})
        assert body["model"] == "test-model", f"{key} 的请求体没带模型名"
        assert spec.path.startswith("/"), f"{key} 的端点路径要以斜杠开头"
        assert spec.timeout > 0 and spec.drain_grace > 0


def test_timeout_and_drain_grace_follow_the_modality():
    """图像是同步生成,超时和收尾等待都必须比文本宽。

    给图像 30 秒超时,会大面积误报「响应超时」—— 那是工具自己造出来的
    假数据,比没有数据更糟。
    """
    text = ApiClient("https://example.com/v1", "k", "m", modality="text")
    image = ApiClient("https://example.com/v1", "k", "m", modality="image")

    assert image.timeout > text.timeout
    assert image.drain_grace > text.drain_grace

    # 收尾等待不能跟着请求超时走:图像超时 120 秒,收尾也等 120 秒的话,
    # 界面看上去就跟卡死了一样。
    assert image.drain_grace < image.timeout


def test_probe_aborts_only_on_configuration_errors():
    """探针只拦「配置错了」,不拦「服务器不痛快」。

    429 尤其不能拦 —— 那正是本工具要观测的东西。在门口把它挡掉,用户就
    永远测不到限流了。
    """
    for code in (400, 401, 403, 404, 422):
        assert ProbeResult(code).fatal, f"{code} 是配置错误,应该拦下来"

    for code in (200, 201, 429, 500, 503, CODE_TIMEOUT, CODE_NETWORK_ERROR):
        assert not ProbeResult(code).fatal, f"{code} 不该中止测试"


def test_probe_brings_back_the_endpoint_and_the_servers_own_words():
    """探针必须把「服务器原话」和「实际请求地址」都带回来。

    这是用户明确要求的:光甩一句「服务器拒绝了请求参数」,他没法知道下一步
    该改什么。状态码 + 服务器原话 + 请求地址,三样凑齐才够一次定位问题。
    """

    def handler(request):
        return httpx.Response(400, json={"error": {"message": "mode is required"}})

    async def scenario():
        client = ApiClient(
            "https://example.com/v1", "sk-test", "vid", modality="video"
        )
        await client._client.aclose()
        client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            return await client.probe()
        finally:
            await client.aclose()

    probe = run(scenario())

    assert probe.code == 400
    assert probe.fatal is True
    assert probe.detail == "mode is required", "服务器原话没带回来"
    assert probe.endpoint == "https://example.com/v1/videos"


def test_error_detail_extraction_handles_common_shapes():
    """各家 OpenAI 兼容实现的错误体长得不一样,常见的几种都要能抠出来。

    抠不出来就等于没有 —— 用户还是会对着一个光秃秃的 400 发呆。
    """
    from rpm_verifier.core.client import extract_error_detail

    for payload in (
        {"error": {"message": "bad size"}},
        {"error": "bad size"},
        {"message": "bad size"},
        {"detail": "bad size"},
    ):
        assert extract_error_detail(httpx.Response(400, json=payload)) == "bad size"

    # 不是 JSON 就退回原文(HTML 错误页、纯文本……)
    assert extract_error_detail(httpx.Response(500, text="upstream exploded")) == (
        "upstream exploded"
    )

    # 超长内容要截断,别把整个响应体塞进对话框
    long_text = "x" * 5000
    assert len(extract_error_detail(httpx.Response(400, text=long_text))) <= 400


def test_last_error_detail_survives_into_the_report():
    """正式测试里服务器抱怨了什么,也要能带到界面上。

    探针只跑一次。探针侥幸过了、正式跑起来才整片 400 的情况一样存在,
    那时候用户同样需要知道服务器在抱怨什么。
    """

    def handler(request):
        return httpx.Response(400, json={"error": {"message": "mode is required"}})

    async def scenario():
        client = ApiClient(
            "https://example.com/v1", "sk-test", "vid", modality="video"
        )
        await client._client.aclose()
        client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            await client.send_with_retry(time.perf_counter() + 5)
            return client.last_error_detail
        finally:
            await client.aclose()

    assert run(scenario()) == "mode is required"


def test_any_2xx_counts_as_success():
    """201 / 202 也算成功。

    只认 200 的话,视频这类返回 201 的接口成功数会一直是 0,用户会以为
    请求全挂了 —— 而服务器其实收下了。
    """
    assert is_success(200)
    assert is_success(201)
    assert is_success(204)
    assert not is_success(301)
    assert not is_success(429)
    assert not is_success(CODE_TIMEOUT)


def test_unknown_modality_falls_back_to_text():
    """配置里存了个认不出来的模态名,不能让界面崩。"""
    assert get_modality("nonsense").key == "text"
    assert get_modality("").key == "text"
