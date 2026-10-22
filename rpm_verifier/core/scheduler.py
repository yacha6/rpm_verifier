"""速率调度器:按绝对时间锚点匀速发射请求。

这是整个产品的核心资产 —— 如果调度不准,测出来的所有数字都没有意义。

本模块属于 core 层,禁止 import 任何 Qt 模块。
"""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Callable, Optional

from .client import FATAL_CODES, ApiClient, SendOutcome, format_exception_detail
from .stats import (
    CODE_INCOMPLETE,
    CODE_NETWORK_ERROR,
    StatsCollector,
    StatsSnapshot,
    TestReport,
    planned_count,
)

# 在途请求上限。因为发射后不等待响应,在途数 ≈ 速率 × 平均延迟,
# 必须设上限,否则连接池和内存会被打爆。
DEFAULT_MAX_INFLIGHT = 200

# 连续多少次网络错误后熔断。单次抖动不该熔断,但一直连不上就没必要继续跑。
NETWORK_ERROR_LIMIT = 5

# 收尾等待上限:所有槽位发完后,最多再等这么久让在途请求返回。
#
# 这个值必须大于客户端的请求超时,否则会出现「客户端还愿意等,收尾却先把
# 它掐了」的情况 —— 请求被 cancel,状态码直接丢失。调用方一般应该传
# client.timeout + 余量,这里的默认值只服务于不关心收尾的单元测试。
#
# 注意 asyncio.wait 只要有请求返回就会提前结束,所以这个上限调大,正常情况
# 下并不会真的多等 —— 只有真卡住的请求才会拖满。
DRAIN_GRACE = 5.0


class RateScheduler:
    """按目标 RPM 匀速发射请求,并在窗口结束时汇总报告。"""

    def __init__(
        self,
        client: ApiClient,
        collector: StatsCollector,
        target_rpm: int,
        duration: float,
        max_inflight: int = DEFAULT_MAX_INFLIGHT,
        on_update: Optional[Callable[[StatsSnapshot], None]] = None,
        update_interval: float = 0.25,
        drain_grace: float = DRAIN_GRACE,
    ) -> None:
        self.client = client
        self.collector = collector
        self.target_rpm = target_rpm
        self.duration = duration
        self.max_inflight = max_inflight
        self.drain_grace = drain_grace
        self._on_update = on_update
        self._update_interval = update_interval

        self._stop_flag = threading.Event()
        self._inflight: set = set()
        self._network_errors = 0
        self._window_elapsed = duration
        self._finished_early = False

    def request_stop(self) -> None:
        """请求停止测试。可以从 GUI 线程安全调用。"""
        self._stop_flag.set()

    @property
    def planned(self) -> int:
        """计划发送的首次请求总数。"""
        return planned_count(self.target_rpm, self.duration)

    # -- 主流程 ------------------------------------------------------------

    async def run(self) -> TestReport:
        collector = self.collector
        t0 = time.perf_counter()
        deadline = t0 + self.duration
        interval = 60.0 / self.target_rpm
        planned = self.planned

        ticker = asyncio.create_task(self._tick())
        try:
            for n in range(planned):
                if self._should_stop():
                    break

                # 绝对时间锚点:第 n 个请求在 t0 + n × interval 发射。
                #
                # 这里绝对不能写成 `await asyncio.sleep(interval)` —— 那样每次
                # 循环的额外开销都会累积进来,实际速率将系统性低于目标值,
                # 而且测试时间越长偏差越大。
                delay = (t0 + n * interval) - time.perf_counter()
                if delay > 0:
                    await asyncio.sleep(delay)

                if self._should_stop():
                    break

                self._emit(deadline)
        finally:
            # 窗口时长:正常跑完就取设定时长;提前结束(手动停止或熔断)则取
            # 实际跑了多久。RPM 折算用的是这个值。
            if self._finished_early:
                self._window_elapsed = time.perf_counter() - t0
            else:
                self._window_elapsed = self.duration

            await self._drain()

            ticker.cancel()
            try:
                await ticker
            except asyncio.CancelledError:
                pass

        return collector.report(self._window_elapsed, self._finished_early)

    def _should_stop(self) -> bool:
        if self._stop_flag.is_set() or self.collector.circuit_broken:
            self._finished_early = True
            return True
        return False

    def _emit(self, deadline: float) -> None:
        """发射一个请求。发射后立即返回,不等待响应。"""
        if len(self._inflight) >= self.max_inflight:
            # 在途请求太多,丢弃这个槽位。必须记录下来,否则用户会以为
            # 工具按目标速率发满了,实际却漏发了。
            self.collector.record_dropped()
            return

        self.collector.mark_sent()
        task = asyncio.create_task(self._one(deadline))
        self._inflight.add(task)
        task.add_done_callback(self._inflight.discard)

    async def _one(self, deadline: float) -> None:
        """发一个请求并记录结果。"""
        try:
            outcome = await self.client.send_with_retry(deadline)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # 任何没预料到的异常都不能让整个测试崩掉,统一记成网络错误;
            # 但异常本身必须带出去,不能只留一个 -2。
            outcome = SendOutcome(
                first_code=CODE_NETWORK_ERROR,
                first_detail=format_exception_detail(exc),
            )
        self._record(outcome)

    def _record(self, outcome: SendOutcome) -> None:
        collector = self.collector
        collector.record_first(outcome.first_code, outcome.first_detail)

        if outcome.retry_triggered:
            collector.record_retry(
                triggered=True,
                success=outcome.retry_success,
                failed=outcome.retry_failed,
                codes=outcome.retry_codes,
                details=outcome.retry_details,
            )

        self._check_circuit(outcome.first_code)

    def _check_circuit(self, code: int) -> None:
        """熔断判定。

        401 / 403 立即熔断:key 无效或余额不足时,继续跑满 60 秒 × 100 RPM
        = 6000 次无效请求,纯粹是浪费时间,还可能触发平台的异常行为风控。
        """
        if code in FATAL_CODES:
            self.collector.break_circuit(f"收到 {code},API Key 无效或余额不足,已立即停止测试")
            self._finished_early = True
            return

        if code == CODE_NETWORK_ERROR:
            self._network_errors += 1
            if self._network_errors >= NETWORK_ERROR_LIMIT:
                self.collector.break_circuit(
                    f"连续 {NETWORK_ERROR_LIMIT} 次网络错误,已停止测试"
                )
                self._finished_early = True
        else:
            self._network_errors = 0

    async def _drain(self) -> None:
        """等待在途请求收尾,最多等 drain_grace 秒。

        没有这一步的话,测试窗口末尾发出的请求会被直接丢掉,最后几秒的
        数据就白采了。

        **发出去的请求必须每一个都有归宿。** 等不及的那些会被取消,取消意味着
        状态码永远拿不到了 —— 所以必须在这里补记成「未返回」。少了这一步,
        界面就会出现「已发送 20、状态码只有 19」这种配不平的情况,而这恰恰
        会让用户怀疑整个工具的数字。观测工具的数,必须自己对得上账。
        """
        if not self._inflight:
            return
        pending = list(self._inflight)
        _, still_pending = await asyncio.wait(pending, timeout=self.drain_grace)
        for task in still_pending:
            # cancel() 返回 False,说明任务在我们动手之前就已经跑完并记过账了,
            # 此时补记会重复计数。只有真正被我们取消掉的才需要补。
            if task.cancel():
                self.collector.record_first(
                    CODE_INCOMPLETE, "收尾等待结束仍未返回(请求被取消)"
                )
        if still_pending:
            await asyncio.gather(*still_pending, return_exceptions=True)

    async def _tick(self) -> None:
        """定期把实时快照推给界面。

        刷新频率默认 4Hz。再高会和网络协程争抢 GIL,反而拖累调度精度。
        """
        if self._on_update is None:
            return
        try:
            while True:
                await asyncio.sleep(self._update_interval)
                self._on_update(self.collector.snapshot())
        except asyncio.CancelledError:
            pass
