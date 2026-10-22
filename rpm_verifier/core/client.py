"""HTTP 客户端:构造空跑请求、发送、处理 503 重试。

只负责「怎么发、发失败了怎么办」,不做任何统计。
本模块属于 core 层,禁止 import 任何 Qt 模块。
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from urllib.parse import urlparse

import httpx

from .modality import get_modality
from .stats import (
    CODE_CONNECT_TIMEOUT,
    CODE_NETWORK_ERROR,
    CODE_POOL_TIMEOUT,
    CODE_TIMEOUT,
    is_success,
)

# ---------------------------------------------------------------------------
# 503 重试参数(见 PRD FR-4)
# ---------------------------------------------------------------------------
MAX_RETRIES = 10   # 单请求重试上限。防御性设置,防止窗口内疯狂重试
RETRY_DELAY = 0.2  # 固定间隔 200ms。窗口只有 60 秒,指数退避会浪费重试机会

# 需要立即熔断的状态码。这些错误重试或继续跑都没有意义。
FATAL_CODES = {401, 403}

# 探针遇到这些状态码,说明**配置本身**有问题,继续跑只会白烧时间和额度。
# 其余一律放行:429 和 5xx 恰恰证明地址与 key 都是对的,而 429 正是本工具
# 要观测的对象 —— 在探针阶段就中止的话,用户永远测不到限流。
#
# 413 / 415 是 v1.1 加的:图生图的 Data URI 太大、或者本地素材格式不被服务端
# 接受,都是配置问题。放行的话,正式测试会制造一整片相同的失败,毫无信息量。
ABORT_CODES = frozenset({400, 401, 403, 404, 413, 415, 422})

# 用户经常把整条端点 URL 直接粘进 Base URL 框(从文档的 curl 示例里复制是
# 最常见的操作)。认出来就剥掉。不剥的话会拼成
# `.../v1/images/generations/chat/completions` 这种四不像,然后 404 ——
# 而错误提示还会把人往「模型名写错了」的方向带。
KNOWN_ENDPOINT_SUFFIXES = (
    "/chat/completions",
    "/images/generations",
    "/videos",
)

# 服务器原话最多带这么多字符。再长对话框就装不下了,而且错误说明通常
# 一两句话就说完。
DETAIL_LIMIT = 400


def extract_error_detail(response: httpx.Response) -> str:
    """把服务器返回的错误说明抠出来。

    这是本工具最该做好的事情之一。400 只报一句「服务器拒绝了请求参数」
    等于什么都没说:用户知道被拒了,但不知道为什么,只能一个个参数去试。
    而服务器的响应体里通常写得清清楚楚 —— `{"error": {"message": "mode
    is required"}}` 一眼就能定位。把原话带出来,省掉的就是这一轮瞎猜。

    各家 OpenAI 兼容实现的错误体大同小异,这里按常见形态依次尝试;都抠不到
    就退回原始文本(截断),总比什么都不给强。
    """

    def clean(text) -> str:
        # 压掉换行和连续空白,免得对话框里出现一大块乱糟糟的排版
        return " ".join(str(text).split())[:DETAIL_LIMIT]

    try:
        payload = response.json()
    except ValueError:
        # 不是 JSON(HTML 错误页、纯文本……),直接用原文
        return clean(response.text)

    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict) and error.get("message"):
            return clean(error["message"])
        if isinstance(error, str) and error:
            return clean(error)
        for key in ("message", "detail", "msg", "error_message"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                return clean(value)

    return clean(response.text)


def normalize_base_url(url: str) -> str:
    """归一化 Base URL:剥掉误粘进来的端点路径,去尾斜杠,补 /v1。

    这样下面三种写法都能用:
      https://api.openai.com
      https://api.openai.com/v1
      https://api.openai.com/v1/chat/completions   ← 从文档里整条粘来的

    路径里已经有别的版本段(比如某些中转站的 `/openai/v1`)则原样保留,
    不做猜测。
    """
    url = url.strip().rstrip("/")
    if not url:
        return url

    for suffix in KNOWN_ENDPOINT_SUFFIXES:
        if url.endswith(suffix):
            url = url[: -len(suffix)].rstrip("/")
            break

    if urlparse(url).path in ("", "/"):
        return url + "/v1"
    return url


def classify_error(exc: Exception) -> int:
    """把 httpx 抛出的异常翻译成统计用的负数常量。

    ⚠️ 判断顺序绝对不能反。PoolTimeout 和 ConnectTimeout **都是
    TimeoutException 的子类**,先判父类的话,这两种会被一网打尽记成同一个
    「响应超时」—— 用户就会把「本机连接池不够用」和「网络连不上」统统
    怪到 API 头上,而这恰恰是这个工具最不该犯的错。
    """
    if isinstance(exc, httpx.PoolTimeout):
        return CODE_POOL_TIMEOUT
    if isinstance(exc, httpx.ConnectTimeout):
        return CODE_CONNECT_TIMEOUT
    if isinstance(exc, httpx.TimeoutException):
        return CODE_TIMEOUT
    return CODE_NETWORK_ERROR


def format_exception_detail(exc: Exception) -> str:
    """把网络层异常压成一行,作为「原始报错」显示。

    这类错误**没有 HTTP 状态码**:服务器还没回响应,TCP/TLS/代理层就断了。
    以前这里只返回一个 -2,原始异常被吞掉,用户看到「连接错误」却不知道是
    DNS 失败、连接被拒、证书错误,还是长连接被服务端重置。

    把异常类名和消息带出来,才能让用户直接判断该怪谁:
      ConnectError: [Errno 61] Connection refused   -> 地址/端口/网络
      ConnectTimeout: ...                             -> 网络或防火墙
      RemoteProtocolError: Server disconnected ...     -> 长连接被对端重置
      SSLError: ...                                    -> 证书/TLS/代理
    """
    message = " ".join(str(exc).split())
    detail = f"{type(exc).__name__}: {message}" if message else type(exc).__name__
    return detail[:DETAIL_LIMIT]


@dataclass(frozen=True)
class SendResult:
    """一次请求的原始结果:状态码,外加服务器给的错误说明(如果有)。"""

    code: int
    detail: str = ""


@dataclass
class SendOutcome:
    """一次「首次请求 + 可能的 503 重试」的完整结果。"""

    first_code: int
    first_detail: str = ""
    retry_triggered: bool = False
    retry_success: bool = False
    retry_failed: bool = False
    retry_codes: list = field(default_factory=list)
    # 每次重试请求的原始报错,和 retry_codes 一一对应。重试请求同样是真实
    # 打到 API 的流量,不能只记状态码、把原始原因丢掉。
    retry_details: list = field(default_factory=list)


@dataclass(frozen=True)
class ProbeResult:
    """探针请求的结果。

    区分「配置错了」和「服务器暂时不痛快」是这里的全部意义:前者必须拦下来,
    后者恰恰是本工具要观测的东西,拦下来就等于把用户想看的现象挡在门外。

    detail 是服务器的**原话**,endpoint 是**实际请求的地址**。这两样缺一不可:
    没有原话,用户知道被拒却不知道为什么;没有地址,用户不知道请求到底打到了
    哪里 —— 而这两条正是排查一次 400 / 404 所需要的全部信息。
    """

    code: int
    message: str = ""
    detail: str = ""
    endpoint: str = ""

    @property
    def fatal(self) -> bool:
        """要不要因为这次探针而中止测试。"""
        return self.code in ABORT_CODES


class ApiClient:
    """封装 httpx.AsyncClient,负责发空跑请求。

    关键点:必须复用连接池。如果每个请求都新建 TCP + TLS 连接,握手开销会
    显著拉低可达 RPM,测出来的数字就变成「工具的能力上限」,而不是「API 的
    限流上限」了。
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        timeout: float | None = None,
        modality: str = "text",
        params: dict | None = None,
        source: str = "",
        source_summary: str = "",
    ) -> None:
        self.spec = get_modality(modality)
        self.model = model
        self.params = dict(params or {})
        # 源素材已经由 core/source.py 解析成最终字符串(URL 或 Data URI)。
        # client 不再读文件,只负责把它放进请求体。
        self.source = source
        self.source_summary = source_summary
        if self.spec.source_kind != "none" and not source:
            raise ValueError(f"{self.spec.label}需要源素材,不能为空")
        # 超时按模态走:文本 30 秒,图像 120 秒(同步生成,慢是正常的)。
        # 调用方显式传了就用调用方的。
        self.timeout = self.spec.timeout if timeout is None else timeout
        # 收尾等待由模态决定,**不**跟着 timeout 走。图像超时 120 秒,但收尾
        # 也等 120 秒的话,界面看上去就是卡死了。等不及的请求会被记成
        # 「未返回」,账目仍然是平的。
        self.drain_grace = self.spec.drain_grace
        self._endpoint = normalize_base_url(base_url) + self.spec.path
        # 最近一次服务器给的错误说明。探针只跑一次,而正式测试里可能整张表
        # 都是 400 —— 那时候用户同样需要知道服务器到底在抱怨什么。
        self.last_error_detail = ""

        # 请求体在构造时**只序列化一次**。图生图的 Data URI 可能有几 MB,
        # 每个请求都 json.dumps 一遍的话,高 RPM 下工具自己先成为瓶颈。
        # 请求体在测试期间完全不变(素材固定、参数固定),所以复用同一份
        # bytes 是安全的。
        self._payload = json.dumps(
            self._body(), ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")

        self._client = httpx.AsyncClient(
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            # 连接池要足够宽:压测 6000 RPM 时在途请求可能上百
            limits=httpx.Limits(max_connections=256, max_keepalive_connections=64),
            # 三种超时分开给。连接和取连接 10 秒就该有结论 —— 让它们跟着
            # 120 秒的读超时走,「连接池耗尽」这种本机问题要拖两分钟才报,
            # 毫无诊断价值。读超时才需要跟着模态走。
            timeout=httpx.Timeout(self.timeout, connect=10.0, pool=10.0),
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    def _body(self) -> dict:
        """请求体。具体长什么样完全由模态决定,见 core/modality.py。"""
        return self.spec.build_body(self.model, self.params, self.source)

    async def _send_once(self) -> SendResult:
        """发一次请求,返回状态码;没有状态码的失败返回负数常量。

        非 2xx 时顺便把服务器的话抠出来 —— 只有状态码的话,用户看到一片 400
        只知道「被拒了」,不知道「为什么被拒」,只能一个个参数去试。
        """
        try:
            # content= 而不是 json=:请求体已经序列化好,每个请求复用同一份
            # bytes,不再重复 dumps。Content-Type 在构造时的 headers 里。
            response = await self._client.post(self._endpoint, content=self._payload)
        except httpx.HTTPError as exc:
            # 没有 HTTP 响应,也就没有状态码。但原始异常必须带出去,
            # 否则用户只知道「连接错误」,不知道到底卡在哪一层。
            return SendResult(
                code=classify_error(exc),
                detail=format_exception_detail(exc),
            )

        if 200 <= response.status_code < 300:
            return SendResult(code=response.status_code)

        detail = extract_error_detail(response)
        if detail:
            self.last_error_detail = detail
        return SendResult(code=response.status_code, detail=detail)

    async def probe(self) -> ProbeResult:
        """探针请求:校验配置是否可用,同时预热连接池。

        两个作用缺一不可:
        1. 提前暴露 401 / 模型名错误 / Base URL 写错,避免跑满 60 秒才发现;
        2. 让正式计时段内的连接已经是热的,否则前几个请求的 TLS 握手开销
           会拉低整体数字。

        只有配置本身有问题才算失败(见 ABORT_CODES)。别的失败照样放行。
        """
        result = await self._send_once()
        return ProbeResult(
            code=result.code,
            message=self._probe_hint(result.code),
            detail=result.detail,
            endpoint=self._endpoint,
        )

    def _probe_hint(self, code: int) -> str:
        """探针失败时,告诉用户到底卡在哪一环。

        这几种失败的表现都是「没反应」,但原因完全不同,提示必须分开写,
        否则用户只能靠猜。

        注意这里只说**该怎么办**。服务器的原话和实际请求地址由 ProbeResult
        的 detail / endpoint 字段承载,界面会把三样一起摆出来 —— 原先这里
        写死「请检查模型名是否正确」,于是把整条 `/v1/images/generations`
        粘进 Base URL 框的用户会去反复核对模型名,查不出问题,因为问题根本
        不在那儿。
        """
        if code == 404:
            return "接口地址不存在:请检查 Base URL,并确认「模态」与所选模型匹配"
        if code == 400:
            return self._bad_request_hint()
        if code == 413:
            return "请求体过大:源素材太大,请换用小图或改用公开 URL"
        if code == 415:
            return "媒体类型不被支持:请检查源文件格式(图片用 JPG/PNG/WebP)"
        if code == 422:
            return "请求参数校验不通过:请对照官方文档检查必填字段"
        if code in (401, 403):
            return "API Key 无效,或账号余额不足"
        if code == 429:
            return "探针就撞上了限流 —— 说明配置是对的,可以直接开测"
        if code == CODE_CONNECT_TIMEOUT:
            return (
                "连接超时(未收到 HTTP 响应):无法与服务器建立连接,"
                "请检查 Base URL 与网络;原始异常见下"
            )
        if code == CODE_TIMEOUT:
            return (
                f"响应超时(未收到完整 HTTP 响应):服务器在 {self.timeout:.0f} 秒内"
                f"没有返回任何内容。地址可能是通的,但服务没有响应;原始异常见下"
            )
        if code == CODE_POOL_TIMEOUT:
            return "本机连接池耗尽(不是 API 的问题),请调小目标速率后重试"
        if code == CODE_NETWORK_ERROR:
            return (
                "连接失败(未收到 HTTP 响应,所以没有 HTTP 状态码):"
                "请检查 Base URL 与网络;原始异常见下"
            )
        return ""

    def _bad_request_hint(self) -> str:
        """400 的提示按模态分开写。

        图生图/图生视频/视频生视频的 400 大概率不是「请求体字段少了一个」,
        而是「源素材服务端拿不到 / 模型不支持这个模式 / 视频超限」。只说
        「服务器拒绝了请求参数」会把人往完全错误的方向带。
        """
        if self.spec.key == "image_to_image":
            return (
                "图生图请求被拒:请检查源图片是否可公开访问(或本地文件是否有效)、"
                "模型是否支持图生图"
            )
        if self.spec.key == "image_to_video":
            return (
                "图生视频请求被拒:请检查源图片 URL 是否可公开访问、"
                "模型是否支持 keyframe 模式"
            )
        if self.spec.key == "video_to_video":
            return (
                "视频生视频请求被拒:请检查源视频 URL(2–12 秒、24–60 FPS、< 50 MB),"
                "以及模型是否支持 videos 参考"
            )
        return "服务器拒绝了请求参数:请求体里有字段不符合它的要求"

    async def send_with_retry(self, deadline: float) -> SendOutcome:
        """发出一个首次请求;若返回 503,则在 deadline 之前不断重试。

        deadline 是整个测试窗口的结束时刻,它是重试的硬边界 —— 窗口结束就
        不再重试,没成功的请求就记为失败。

        ⚠️ 这里故意不重试 429:429 是本工具要**观测的对象**,不是要绕过的
        障碍。重试 429 会让工具不断把撞墙的请求怼回去,既污染速率测量,
        也可能触发平台风控。请勿「顺手优化」掉下面这个判断。
        """
        result = await self._send_once()
        code = result.code
        outcome = SendOutcome(first_code=code, first_detail=result.detail)

        if code != 503:
            return outcome

        outcome.retry_triggered = True
        while len(outcome.retry_codes) < MAX_RETRIES:
            if time.perf_counter() >= deadline:
                break
            await asyncio.sleep(RETRY_DELAY)
            if time.perf_counter() >= deadline:
                break

            retry_result = await self._send_once()
            outcome.retry_codes.append(retry_result.code)
            outcome.retry_details.append(retry_result.detail)
            retry_code = retry_result.code
            if is_success(retry_code):
                outcome.retry_success = True
                return outcome
            if retry_code != 503:
                # 错误类型变了(比如变成了 429),继续重试没有意义
                break

        outcome.retry_failed = True
        return outcome
