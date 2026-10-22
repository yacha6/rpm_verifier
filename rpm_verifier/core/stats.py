"""统计收集器:只负责数数,不关心请求是怎么发出去的。

本模块属于 core 层,禁止 import 任何 Qt 模块。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# 没有 HTTP 状态码的失败,用负数表示,避免与真实状态码混淆
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# 这些是负数常量,不是 HTTP 状态码。
#
# 区分它们很重要:503 / 504 是服务器「回了话」,带了状态码;而下面这些是
# 压根没拿到响应。把两者混为一谈,用户就没法判断到底是 API 在限流,还是
# 网络/本机出了问题。
#
# -1 ~ -3 是「请求发出去了,但没等到响应」;细分下去诊断意义不同:
#   连不上   -> 网络或地址问题,与限流无关
#   响应超时 -> 服务器接下了但处理不过来,反而可能是限流的信号
#   池耗尽   -> 本机并发连接不够用,是我们自己的锅,绝不能算到 API 头上
# ---------------------------------------------------------------------------
CODE_TIMEOUT = -1          # 响应超时:连上了,但服务器没在超时时间内回话
CODE_NETWORK_ERROR = -2    # 连接错误:TCP 层就没通(域名解析失败、拒绝连接等)
CODE_INCOMPLETE = -3       # 未返回:窗口结束收尾时仍未回来,被取消
CODE_CONNECT_TIMEOUT = -4  # 连接超时:连 TCP 都超时了
CODE_POOL_TIMEOUT = -5     # 连接池耗尽:本机并发连接不够,与 API 无关

_CODE_LABELS = {
    CODE_TIMEOUT: "响应超时",
    CODE_NETWORK_ERROR: "连接错误",
    CODE_INCOMPLETE: "未返回",
    CODE_CONNECT_TIMEOUT: "连接超时",
    CODE_POOL_TIMEOUT: "连接池耗尽",
}

# 这些行不是 HTTP 状态码,界面上需要额外说明
NON_HTTP_CODES = frozenset(_CODE_LABELS)


def code_label(code: int) -> str:
    """把状态码转成界面上展示的文本。"""
    return _CODE_LABELS.get(code, str(code))


def is_success(code: int) -> bool:
    """这个状态码算不算「服务器收下了」。

    不能只认 200:视频创建任务这类接口返回 201 / 202 都是可能的,只认 200
    的话「成功数」会一直是 0,用户会以为请求全挂了 —— 而实际上服务器收下了。

    这里判的是「请求被接受」,不是「内容生成好了」。异步接口只负责创建任务,
    生成在服务端后台跑,那是另一回事。
    """
    return 200 <= code < 300


def planned_count(target_rpm: int, duration: float) -> int:
    """按目标速率与时长算出计划发送的请求数。

    60 RPM 跑 60 秒 => 60 个请求。这是验收标准 AC1 的计算依据。
    """
    return int(duration * target_rpm / 60.0)


@dataclass(frozen=True)
class StatsSnapshot:
    """测试过程中的实时快照,用于跨线程刷新界面。"""

    sent: int = 0
    planned: int = 0
    success: int = 0
    elapsed: float = 0.0
    codes: dict = field(default_factory=dict)
    # 每个状态码最近一次的原始报错:HTTP 错误是服务器原话,负数行是原始异常。
    # 只用于展示,不参与任何统计口径。
    details: dict = field(default_factory=dict)
    dropped: int = 0
    retry_triggered: int = 0
    # 重试请求也是真实打到 API 的流量,必须和首次请求一起统计。
    # retry_codes / retry_details 供界面拆出「重试」列;all_codes 是两者合并。
    retry_codes: dict = field(default_factory=dict)
    retry_details: dict = field(default_factory=dict)
    all_codes: dict = field(default_factory=dict)
    all_sent: int = 0
    all_success: int = 0
    circuit_broken: bool = False
    circuit_reason: str = ""

    @property
    def responded(self) -> int:
        """已经有结果的首次请求数。sent 减去它就是还在途中的数量。"""
        return sum(self.codes.values())

    @property
    def send_rpm(self) -> float:
        """首次请求的发送速率(调度槽位速率)。"""
        if self.elapsed <= 0:
            return 0.0
        return self.sent / self.elapsed * 60.0

    @property
    def success_rpm(self) -> float:
        """按当前已耗时长折算的首次请求成功速率。"""
        if self.elapsed <= 0:
            return 0.0
        return self.success / self.elapsed * 60.0

    @property
    def all_send_rpm(self) -> float:
        """全部请求(首次 + 重试)的实际发送速率。"""
        if self.elapsed <= 0:
            return 0.0
        return self.all_sent / self.elapsed * 60.0

    @property
    def all_success_rpm(self) -> float:
        """全部请求(含重试)中 2xx 的速率。"""
        if self.elapsed <= 0:
            return 0.0
        return self.all_success / self.elapsed * 60.0


@dataclass(frozen=True)
class TestReport:
    """一次测试结束后的最终报告。

    注意:这里没有任何「达标 / 未达标」字段。产品原则是只呈现数据,
    不替用户下判断。
    """

    target_rpm: int
    planned: int
    sent: int
    success: int
    elapsed: float
    codes: dict = field(default_factory=dict)
    # 每个状态码最近一次的原始报错。HTTP 错误带服务器原话,负数行带原始异常
    # (例如 ConnectError: Connection refused)—— 后者没有 HTTP 状态码,
    # 不把异常摆出来,用户就只能看到一个没有诊断价值的「连接错误」。
    details: dict = field(default_factory=dict)
    dropped: int = 0
    retry_triggered: int = 0
    retry_success: int = 0
    retry_failed: int = 0
    retry_codes: dict = field(default_factory=dict)
    retry_details: dict = field(default_factory=dict)
    # 全部请求(首次 + 重试)的合并状态码表。retry_codes 单独保留,是为了
    # 在界面上把「首次 / 重试 / 合计」三列拆开,而不是把重试藏起来。
    all_codes: dict = field(default_factory=dict)
    all_sent: int = 0
    all_success: int = 0
    finished_early: bool = False
    circuit_reason: str = ""
    # 服务器在错误响应里给的原始说明。整张表都是 400 却不知道服务器在抱怨
    # 什么,和探针直接失败一样让人抓瞎 —— 这条信息必须跟着报告一起出来。
    error_detail: str = ""
    # 本次测试用的源素材摘要(脱敏)。输入条件模态的 RPM 数字离开「用了什么
    # 素材」就没有意义;但签名 URL 的 query 不能带出来,所以这里只存摘要。
    # 文本/图像/视频模态为空字符串。
    source_summary: str = ""

    @property
    def responded(self) -> int:
        """已经有结果的首次请求数。

        正常情况下它必须等于 sent —— 每个发出去的请求都要有归宿。收尾阶段
        被取消的请求会被记成「未返回」,就是为了保住这条不变式。
        """
        return sum(self.codes.values())

    @property
    def send_rpm(self) -> float:
        """实际发出的首次请求速率,用于衡量工具自身的调度精度。"""
        if self.elapsed <= 0:
            return 0.0
        return self.sent / self.elapsed * 60.0

    @property
    def success_rpm(self) -> float:
        """成功(2xx)的**首次请求**速率。

        注意:这不是全部请求的成功速率。重试成功也算真实成功,见
        all_success_rpm。保留这个值,是为了区分「首次就成功」和「靠重试
        才成功」——前者才是限流诊断的关键。
        """
        if self.elapsed <= 0:
            return 0.0
        return self.success / self.elapsed * 60.0

    @property
    def all_send_rpm(self) -> float:
        """全部请求(首次 + 重试)的实际发送速率。

        这个值可能高于目标 RPM:重试不占调度槽位,但确实打到了 API。
        把它摆出来,用户才能看到真实的请求量。
        """
        if self.elapsed <= 0:
            return 0.0
        return self.all_sent / self.elapsed * 60.0

    @property
    def all_success_rpm(self) -> float:
        """全部请求(含重试)中 2xx 的速率。"""
        if self.elapsed <= 0:
            return 0.0
        return self.all_success / self.elapsed * 60.0


class StatsCollector:
    """线程安全的计数器。

    网络线程写入,GUI 线程读取,所以所有读写都要持锁。
    """

    def __init__(self, target_rpm: int, planned: int) -> None:
        self._lock = threading.Lock()
        self.target_rpm = target_rpm
        self.planned = planned
        self._sent = 0
        self._success = 0
        self._codes: dict = {}
        self._details: dict = {}
        self._dropped = 0
        self._retry_triggered = 0
        self._retry_success = 0
        self._retry_failed = 0
        self._retry_codes: dict = {}
        self._retry_details: dict = {}
        self._retry_attempts = 0
        self._circuit_broken = False
        self._circuit_reason = ""
        self._started = time.perf_counter()

    # -- 写入(网络线程) --------------------------------------------------

    def mark_sent(self) -> None:
        """记录一个首次请求已经发出。"""
        with self._lock:
            self._sent += 1

    def record_first(self, code: int, detail: str = "") -> None:
        """记录一个首次请求的结果。所有首次请求都进主统计表。

        detail 是该状态码的原始报错:HTTP 错误是服务器响应体里的说明,
        网络错误是 httpx 抛出的异常。同一个状态码出现多次时保留**最近一次**
        的非空说明 —— 用户要看的是「这个码到底在报什么」,不是历史全集。
        """
        with self._lock:
            self._codes[code] = self._codes.get(code, 0) + 1
            if detail:
                self._details[code] = detail
            if is_success(code):
                self._success += 1

    def record_dropped(self) -> None:
        """记录一个因在途请求过多而被丢弃的槽位。

        不记录的话,用户会以为工具按目标速率发满了,实际却漏发了。
        """
        with self._lock:
            self._dropped += 1

    def record_retry(
        self,
        triggered: bool,
        success: bool,
        failed: bool,
        codes: list,
        details: list | None = None,
    ) -> None:
        """记录重试情况。

        ⚠️ 重试请求**同样是真实打到 API 的流量**,会计入「全部请求」的
        状态码表、全部请求 RPM 和全部 2xx RPM。`retry_codes` 单独保留,
        只是为了在界面上拆出「首次 / 重试 / 合计」三列,而不是把重试藏起来。

        这个口径是对 v1.0「重试不计入主统计」的修正:验证 RPM 时,503 重试
        也消耗账号额度,不能被排除在统计之外。
        """
        with self._lock:
            if triggered:
                self._retry_triggered += 1
            if success:
                self._retry_success += 1
            if failed:
                self._retry_failed += 1

            self._retry_attempts += len(codes)
            for index, code in enumerate(codes):
                self._retry_codes[code] = self._retry_codes.get(code, 0) + 1
                detail = details[index] if details and index < len(details) else ""
                if detail:
                    self._retry_details[code] = detail

    def _merged_codes(self) -> dict:
        """首次 + 重试的合并状态码表。调用方必须已持锁。"""
        merged = dict(self._codes)
        for code, count in self._retry_codes.items():
            merged[code] = merged.get(code, 0) + count
        return merged

    @staticmethod
    def _success_count(codes: dict) -> int:
        return sum(count for code, count in codes.items() if is_success(code))

    def break_circuit(self, reason: str) -> None:
        """熔断:遇到不可恢复的错误,立即终止测试。只记录第一次的原因。"""
        with self._lock:
            if not self._circuit_broken:
                self._circuit_broken = True
                self._circuit_reason = reason

    # -- 读取(GUI 线程) --------------------------------------------------

    @property
    def circuit_broken(self) -> bool:
        with self._lock:
            return self._circuit_broken

    def snapshot(self) -> StatsSnapshot:
        with self._lock:
            all_codes = self._merged_codes()
            return StatsSnapshot(
                sent=self._sent,
                planned=self.planned,
                success=self._success,
                elapsed=time.perf_counter() - self._started,
                codes=dict(self._codes),
                details=dict(self._details),
                dropped=self._dropped,
                retry_triggered=self._retry_triggered,
                retry_codes=dict(self._retry_codes),
                retry_details=dict(self._retry_details),
                all_codes=all_codes,
                all_sent=self._sent + self._retry_attempts,
                all_success=self._success_count(all_codes),
                circuit_broken=self._circuit_broken,
                circuit_reason=self._circuit_reason,
            )

    def report(self, elapsed: float, finished_early: bool = False) -> TestReport:
        with self._lock:
            all_codes = self._merged_codes()
            return TestReport(
                target_rpm=self.target_rpm,
                planned=self.planned,
                sent=self._sent,
                success=self._success,
                elapsed=elapsed,
                codes=dict(self._codes),
                details=dict(self._details),
                dropped=self._dropped,
                retry_triggered=self._retry_triggered,
                retry_success=self._retry_success,
                retry_failed=self._retry_failed,
                retry_codes=dict(self._retry_codes),
                retry_details=dict(self._retry_details),
                all_codes=all_codes,
                all_sent=self._sent + self._retry_attempts,
                all_success=self._success_count(all_codes),
                finished_early=finished_early,
                circuit_reason=self._circuit_reason,
            )
