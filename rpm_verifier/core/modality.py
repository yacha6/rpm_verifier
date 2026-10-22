"""模态规格:文本 / 图像 / 视频 / 图生图 / 图生视频 / 视频生视频。

为什么要单独立一个模块:六种模态的**端点、请求体、超时、响应形态、源素材
要求**都不一样 —— 文本、图像、图生图是同步的(服务器要把活干完才回话),
视频三种是异步的(立刻返回一个排队中的任务对象)。这些差异如果散落在
client 和界面里,加一种模态就要改一圈;收在一处,加模态只动这个文件。

本模块属于 core 层,禁止 import 任何 Qt 模块。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

# 空跑用的提示词。图像按张、视频按秒计费,prompt 长短都不影响价格,
# 所以取一个最短且一定合法的值就行。
MINIMAL_PROMPT = "a cat"

# 图生图/图生视频/视频生视频的提示词。它们必须是「对输入素材做一件事」的
# 指令,而不是无意义的 "a cat" —— 有些服务端会校验提示词与输入素材是否匹配,
# 一个纯文生图的提示词配上输入图,更容易被 400 挡回来。
IMAGE_EDIT_PROMPT = "把画面改成雨夜霓虹风格,保持原始构图"
IMAGE_TO_VIDEO_PROMPT = "让首帧中的主体自然运动,镜头缓慢推进"
VIDEO_TO_VIDEO_PROMPT = "参考 <Video 1> 的主体动作与镜头节奏,保持时序连贯"


@dataclass(frozen=True)
class ParamSpec:
    """一个可配置的请求参数。

    界面按它渲染下拉框,client 按它拼请求体 —— 两边读的是同一份定义,
    不会出现「界面上能选、请求里却忘了带上」这种脱节。
    """

    name: str
    label: str
    options: tuple
    default: str


@dataclass(frozen=True)
class ModalitySpec:
    """一种模态的全部差异。

    source_* / plan_unit / success_label 是 v1.1 加进来的:它们让界面和报告
    不需要写一堆 `if 模态 == ...`,也保证「加一种输入条件模态」只改这个文件。
    """

    key: str
    label: str
    path: str
    timeout: float
    drain_grace: float
    build_body: Callable[[str, dict, str], dict]
    params: tuple = ()
    note: str = ""

    # -- 源素材 ---------------------------------------------------------
    # none / image / video。界面据此决定是否显示「源素材」行。
    source_kind: str = "none"
    source_label: str = ""
    source_hint: str = ""
    # 只有图生图支持本地文件:图像接口接受 Data URI;视频接口只接受公开 URL。
    accepts_local_file: bool = False

    # -- 展示与计划量 ---------------------------------------------------
    # 异步任务模态的「成功」只是任务创建成功,标签必须说清楚。
    success_label: str = "成功"
    # none / image / video。界面据此决定计划量文案(请求数 / 张数 / 秒数)。
    plan_unit: str = "none"


# ---------------------------------------------------------------------------
# 请求体
# ---------------------------------------------------------------------------

def _text_body(model: str, params: dict, source: str = "") -> dict:
    """文本空跑请求。

    max_tokens 取 1 是为了把 token 消耗压到最低,直接降低用户的压测成本。

    请求体必须保持合法:如果因为参数问题被拒(400),状态码统计会被污染,
    用户会误判成「限流」。这也是为什么正式测试前要先做一次探针请求。
    """
    return {
        "model": model,
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 1,
        "stream": False,
    }


def _image_body(model: str, params: dict, source: str = "") -> dict:
    """图像生成请求(文生图)。

    size 是必填项,没有「省略即默认」的余地,所以这里一定要给一个值。
    文档推荐的档位是 1K / 2K / 3K / 4K,默认取 1K —— 最小档,成本最低。
    """
    return {
        "model": model,
        "prompt": MINIMAL_PROMPT,
        "size": params.get("size") or "1K",
    }


def _video_body(model: str, params: dict, source: str = "") -> dict:
    """视频生成请求(文生视频)。

    ⚠️ `mode` 是**必填**项,漏了直接 400 —— 文档的参数表里标着「是」,
    骗过我好一阵子。这里固定用 `text`(纯文本生成视频):另外两种模式
    keyframe / reference 都要求用户提供可公开访问的素材 URL,由专门的
    图生视频 / 视频生视频模态处理。

    seconds 可选 "4" ~ "12",文档默认 "5";这里默认取 4 秒 —— 视频按秒
    计费,4 秒是价格最低的一档。size 默认 720P,同样是价格最低的一档。

    注意这个请求只是**创建任务**,不等待视频生成完。
    """
    return {
        "model": model,
        "prompt": MINIMAL_PROMPT,
        "mode": "text",
        "seconds": params.get("seconds") or "4",
        "size": params.get("size") or "720P",
    }


def _image_to_image_body(model: str, params: dict, source: str = "") -> dict:
    """图生图请求。

    ⚠️ 输入图放在 `extra_body.image` 里,不是顶层 `image`。这是 Agnes 官方
    raw curl 示例的写法;官方还明确说不要传 `tags: ["img2img"]`。

    `response_format: "url"` 必须带:不带的话响应可能是 base64,请求本身
    可能 2 秒完成,但几 MB 的图片数据会把「响应耗时」拖长,测出来的就不再
    是生成限流,而是下载带宽。

    `source` 可以是公开图片 URL,也可以是 `data:image/...;base64,...`。
    本地文件在测试开始前已经编码好,这里只负责放进请求体。
    """
    return {
        "model": model,
        "prompt": IMAGE_EDIT_PROMPT,
        "size": params.get("size") or "1K",
        "extra_body": {
            "image": [source],
            "response_format": "url",
        },
    }


def _image_to_video_body(model: str, params: dict, source: str = "") -> dict:
    """图生视频请求。

    固定用 `keyframe` + `first_frame`:把输入图当作成片的真实首帧。另一种
    `reference`(参考图)模式会把图当作风格/内容参考,生成结果可能重新构图;
    对「图生视频」这个语义来说,首帧最直接,请求也最简单。

    `keyframe` 模式下不能再带 `images` / `audios` / `videos`,否则服务端
    会拒绝。
    """
    return {
        "model": model,
        "prompt": IMAGE_TO_VIDEO_PROMPT,
        "mode": "keyframe",
        "seconds": params.get("seconds") or "4",
        "size": params.get("size") or "720P",
        "first_frame": source,
    }


def _video_to_video_body(model: str, params: dict, source: str = "") -> dict:
    """视频生视频请求。

    只能走 `reference` 模式,输入放在 `videos` 数组里。单视频也必须写成
    数组。`reference` 模式下不能再带 `first_frame` / `images` / `audios`。

    `start_seconds` 固定 0、`require_audio` 固定 False:压测只关心「创建
    任务」这一步,不需要控制从第几秒开始读、也不要求源视频必须有音轨。

    ⚠️ 不是所有模型都支持 `videos`。例如 `agnes-video-2.5-flash` 会返回
    400 `videos is not supported`;视频生视频必须选支持视频参考的模型。
    """
    return {
        "model": model,
        "prompt": VIDEO_TO_VIDEO_PROMPT,
        "mode": "reference",
        "seconds": params.get("seconds") or "4",
        "size": params.get("size") or "720P",
        "videos": [
            {
                "url": source,
                "start_seconds": 0,
                "require_audio": False,
            }
        ],
    }


# ---------------------------------------------------------------------------
# 六种模态
# ---------------------------------------------------------------------------

TEXT = ModalitySpec(
    key="text",
    label="文本",
    path="/chat/completions",
    # 文本也是同步的,但空跑只要 1 个 token,响应很快。
    timeout=30.0,
    drain_grace=8.0,
    build_body=_text_body,
)

IMAGE = ModalitySpec(
    key="image",
    label="图像",
    path="/images/generations",
    # 图像是**同步**的:服务器要把图真正生成完才回话,十几秒是常态。
    # 超时如果按文本的 30 秒给,会大面积误报「响应超时」—— 那是工具自己
    # 造出来的假数据,比没有数据更糟。
    timeout=120.0,
    # 收尾只等 20 秒,而不是跟着 120 秒的请求超时走。窗口结束时挂着的请求
    # 确实还要十几秒才回来,但让用户对着界面干等两分钟同样不可接受。
    # 等不及的会被记成「未返回」,账目不会丢(见 README 设计规则 6)。
    drain_grace=20.0,
    build_body=_image_body,
    params=(
        ParamSpec("size", "尺寸", ("1K", "2K", "3K", "4K"), "1K"),
    ),
    note="图像为同步接口:服务器生成完才返回,单个请求通常要数秒到数十秒。",
    plan_unit="image",
)

VIDEO = ModalitySpec(
    key="video",
    label="视频",
    path="/videos",
    # 视频是**异步**的:POST /videos 只负责创建任务,立刻返回一个
    # status=queued 的任务对象,真正的生成在服务端后台跑。请求本身很快。
    timeout=30.0,
    drain_grace=8.0,
    build_body=_video_body,
    params=(
        ParamSpec("seconds", "时长", ("4", "5", "6", "8", "10", "12"), "4"),
        ParamSpec("size", "分辨率", ("720P", "1080P", "1K", "2K"), "720P"),
    ),
    note=(
        "视频为异步接口:测的是「创建任务」这一步的限流,不是视频生成的吞吐。"
        "示例模型:agnes-video-2.5 / agnes-video-2.5-flash。"
        "503 重试会重新创建任务,可能产生额外计费。"
    ),
    success_label="成功(任务创建)",
    plan_unit="video",
)

IMAGE_TO_IMAGE = ModalitySpec(
    key="image_to_image",
    label="图生图",
    path="/images/generations",
    # 和图像一样是同步生成,超时/收尾都按图像的来。
    timeout=120.0,
    drain_grace=20.0,
    build_body=_image_to_image_body,
    params=(
        ParamSpec("size", "尺寸", ("1K", "2K", "3K", "4K"), "1K"),
    ),
    note=(
        "图生图为同步接口:服务器生成完才返回。"
        "源素材支持公开图片 URL 或本地图片文件,本地文件会编码为 Data URI。"
        "示例模型:agnes-image-2.5-flash。"
    ),
    source_kind="image",
    source_label="源图片",
    source_hint=(
        "支持公开图片 URL,或小于 5 MB 的本地 JPG/PNG/WebP;"
        "本地文件会编码为 Data URI 随请求发送。"
    ),
    accepts_local_file=True,
    plan_unit="image",
)

IMAGE_TO_VIDEO = ModalitySpec(
    key="image_to_video",
    label="图生视频",
    path="/videos",
    # 异步建任务,请求本身很快。
    timeout=30.0,
    drain_grace=8.0,
    build_body=_image_to_video_body,
    params=(
        ParamSpec("seconds", "时长", ("4", "5", "6", "8", "10", "12"), "4"),
        ParamSpec("size", "分辨率", ("720P", "1080P", "1K", "2K"), "720P"),
    ),
    note=(
        "图生视频为异步接口:测的是「创建任务」这一步的限流,不是视频生成的吞吐。"
        "源图片必须是服务端可公开访问的 URL。"
        "示例模型:agnes-video-2.5。"
        "503 重试会重新创建任务,可能产生额外计费。"
    ),
    source_kind="image",
    source_label="源图片 URL",
    source_hint=(
        "需要可公开访问的图片 URL(服务端会自行下载);"
        "单张 256–5760 px、小于 15 MB。本地文件无法用于该模态。"
    ),
    accepts_local_file=False,
    success_label="成功(任务创建)",
    plan_unit="video",
)

VIDEO_TO_VIDEO = ModalitySpec(
    key="video_to_video",
    label="视频生视频",
    path="/videos",
    timeout=30.0,
    drain_grace=8.0,
    build_body=_video_to_video_body,
    params=(
        ParamSpec("seconds", "时长", ("4", "5", "6", "8", "10", "12"), "4"),
        ParamSpec("size", "分辨率", ("720P", "1080P", "1K", "2K"), "720P"),
    ),
    note=(
        "视频生视频为异步接口:测的是「创建任务」这一步的限流,不是视频生成的吞吐。"
        "源视频必须是服务端可公开访问的 URL;模型需支持 videos 参考"
        "(如 agnes-video-2.5,2.5-flash 不支持)。输入视频时长也会计费。"
        "503 重试会重新创建任务,可能产生额外计费。"
    ),
    source_kind="video",
    source_label="源视频 URL",
    source_hint=(
        "需要可公开访问的视频 URL:2–12 秒、24–60 FPS、小于 50 MB。"
        "本地文件无法用于该模态。"
    ),
    accepts_local_file=False,
    success_label="成功(任务创建)",
    plan_unit="video",
)

# 界面上下拉框的展示顺序:先保留现有三项,再追加输入条件模态。
# 这样老的 config.json 里的 modality 取值仍然落在同一个位置附近,
# 用户也不会因为升级后顺序大变而点错。
MODALITY_ORDER = (
    TEXT,
    IMAGE,
    VIDEO,
    IMAGE_TO_IMAGE,
    IMAGE_TO_VIDEO,
    VIDEO_TO_VIDEO,
)

MODALITIES = {spec.key: spec for spec in MODALITY_ORDER}


def get_modality(key: str) -> ModalitySpec:
    """按 key 取模态规格。认不出来的当作文本处理,不让界面崩掉。"""
    return MODALITIES.get(key, TEXT)


def _seconds_param(params: dict) -> int:
    """从参数里取单次视频秒数,取不到就按默认 4 秒算。

    只用于计划量展示,不参与请求体构造 —— 所以这里宽容一点,别因为下拉框
    里出现脏值就让界面炸掉。
    """
    try:
        return max(1, int(str(params.get("seconds") or "4")))
    except (TypeError, ValueError):
        return 4


def plan_summary(spec: ModalitySpec, planned: int, params: dict) -> str:
    """生成配置卡上的「本次计划」文案。

    放在 core 而不是 ui,是因为它是模态元数据的一部分,而且能被纯 pytest
    覆盖。视频类必须把「任务数 × 秒数 = 输出秒数」摆出来 —— 这是用户判断
    这次测试大概要花多少钱的唯一依据。
    """
    if spec.plan_unit == "video":
        seconds = _seconds_param(params)
        total = planned * seconds
        text = (
            f"本次计划:{planned} 个任务 × {seconds} 秒 = {total} 秒输出视频"
            f"(不含探针 1 个任务)"
        )
        if spec.key == "video_to_video":
            text += ";输入视频时长另行计费"
        return text
    if spec.plan_unit == "image":
        return f"本次计划:{planned} 张图(不含探针 1 张)"
    return f"本次计划:{planned} 个请求(不含探针 1 次)"


def planned_video_seconds(spec: ModalitySpec, planned: int, params: dict) -> int:
    """视频类模态的计划输出秒数;非视频模态返回 0。

    成本确认框用它判断要不要拦一下。视频生视频的**输入**视频时长这里算不
    进去 —— 工具不知道源视频有多长,只能提示「输入时长另行计费」。
    """
    if spec.plan_unit != "video":
        return 0
    return planned * _seconds_param(params)
