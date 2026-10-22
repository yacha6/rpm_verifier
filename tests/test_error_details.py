"""原始报错回传:连接失败没有 HTTP 状态码,但必须把原始异常带出来。

这条测试守的是一个很容易被忽略的体验问题:用户看到「连接错误」四个字,
却不知道是 DNS、连接被拒、证书错误还是长连接被对端重置。只给一个 -2
等于什么都没说。
"""

from __future__ import annotations

import asyncio
import time

import httpx
import pytest

from rpm_verifier.core.client import (
    ApiClient,
    SendOutcome,
    format_exception_detail,
)
from rpm_verifier.core.scheduler import RateScheduler
from rpm_verifier.core.stats import (
    CODE_NETWORK_ERROR,
    StatsCollector,
)


def _run_client_once(handler, **kwargs):
    async def scenario():
        client = ApiClient("https://example.com/v1", "sk-test", "m", **kwargs)
        await client._client.aclose()
        client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            return await client._send_once()
        finally:
            await client.aclose()

    return asyncio.run(scenario())


def test_network_error_keeps_the_original_exception():
    """连接失败不能只剩一个 -2;原始异常类名和消息都要带出来。"""

    def handler(request):
        raise httpx.ConnectError("connection refused", request=request)

    result = _run_client_once(handler)

    assert result.code == CODE_NETWORK_ERROR
    assert "ConnectError" in result.detail
    assert "connection refused" in result.detail


def test_probe_carries_the_original_exception_and_says_no_http_code():
    """探针失败时,弹窗要能拿到原始异常,并明说没有 HTTP 响应。"""

    def handler(request):
        raise httpx.ConnectError("connection refused", request=request)

    async def scenario():
        client = ApiClient("https://example.com/v1", "sk-test", "m")
        await client._client.aclose()
        client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            return await client.probe()
        finally:
            await client.aclose()

    probe = asyncio.run(scenario())

    assert probe.code == CODE_NETWORK_ERROR
    assert "ConnectError" in probe.detail
    assert "未收到 HTTP 响应" in probe.message
    assert "没有 HTTP 状态码" in probe.message


def test_format_exception_detail_is_one_line_and_bounded():
    exc = RuntimeError("line one\nline two")
    detail = format_exception_detail(exc)
    assert "\n" not in detail
    assert detail.startswith("RuntimeError:")
    assert len(detail) <= 400


def test_stats_collector_keeps_the_last_non_empty_detail():
    """同一状态码出现多次时保留最近一次非空说明,空的不能把已有的覆盖掉。"""
    collector = StatsCollector(60, 2)
    collector.record_first(400, "bad size")
    collector.record_first(400)
    collector.record_first(CODE_NETWORK_ERROR, "ConnectError: refused")

    snapshot = collector.snapshot()
    assert snapshot.details[400] == "bad size"
    assert snapshot.details[CODE_NETWORK_ERROR] == "ConnectError: refused"

    report = collector.report(elapsed=1.0)
    assert report.details[400] == "bad size"
    assert report.details[CODE_NETWORK_ERROR] == "ConnectError: refused"


def test_scheduler_passes_the_detail_into_the_report():
    """调度器记录首次请求时,要把 SendOutcome 里的 detail 一起写进统计。"""

    class DetailClient:
        async def send_with_retry(self, deadline):
            return SendOutcome(first_code=400, first_detail="bad size")

    async def scenario():
        collector = StatsCollector(60, 1)
        scheduler = RateScheduler(
            DetailClient(), collector, target_rpm=60, duration=1.0
        )
        return await scheduler.run()

    report = asyncio.run(scenario())

    assert report.codes == {400: 1}
    assert report.details[400] == "bad size"


def test_retry_attempts_count_in_all_stats_and_rpm():
    """重试请求也是真实流量:全部请求、状态码和全部 RPM 都必须算上它。"""
    collector = StatsCollector(60, 1)
    collector.mark_sent()
    collector.record_first(503, "first 503")
    collector.record_retry(
        triggered=True,
        success=False,
        failed=True,
        codes=[503] * 20,
        details=["retry 503"] * 20,
    )

    report = collector.report(elapsed=60.0)

    assert report.codes == {503: 1}            # 首次
    assert report.retry_codes == {503: 20}     # 重试
    assert report.all_codes == {503: 21}       # 合计
    assert report.all_sent == 21
    assert report.all_success == 0
    assert report.send_rpm == 1                # 首次请求 RPM
    assert report.all_send_rpm == 21           # 全部请求 RPM(含重试)
    assert report.retry_details[503] == "retry 503"


def test_retry_success_counts_as_all_success_but_not_first_success():
    collector = StatsCollector(60, 1)
    collector.mark_sent()
    collector.record_first(503)
    collector.record_retry(
        triggered=True, success=True, failed=False, codes=[200]
    )

    report = collector.report(elapsed=60.0)

    assert report.success == 0                 # 首次没有成功
    assert report.all_success == 1             # 重试成功算全部成功
    assert report.success_rpm == 0
    assert report.all_success_rpm == 1
