"""源素材解析:把用户填的 URL / 本地文件变成请求里的一个字符串。

为什么单独一个模块:图生图、图生视频、视频生视频对素材的要求完全不同 ——
图生图接受公开 URL 或本地文件(编码成 Data URI),视频接口只接受服务端
可公开访问的 URL。这些规则如果散落在界面和 client 里,加一种模态就要改
一圈,而且很容易出现「界面能选、请求里却没带上」的脱节。

本模块属于 core 层,禁止 import 任何 Qt 模块。
"""

from __future__ import annotations

import base64
import ipaddress
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import ParseResult, urlparse

from .modality import ModalitySpec

# 工具侧的本地文件上限。官方文档没有给图生图的输入图片大小上限;5 MB 是
# 按「base64 后约 6.7 MB 请求体」倒推的 —— 再大,高 RPM 下本机上行带宽
# 和 JSON 序列化会先成为瓶颈,测出来的就不是 API 的限流了。
MAX_LOCAL_IMAGE_BYTES = 5 * 1024 * 1024

# 超过这个大小就给一条非阻塞提示:可以用,但压测建议用小图。
LARGE_IMAGE_WARN_BYTES = 1 * 1024 * 1024

# 先只放最常见的三种。GIF/BMP/TIFF 没有放进来:不是所有图像接口都认,
# 与其让用户发出去收一个 415,不如在本地拦下来。
_IMAGE_MIME = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}


class SourceError(ValueError):
    """源素材不合法。消息是给用户看的,可以直接弹出来。"""


@dataclass(frozen=True)
class SourcePreview:
    """轻量校验结果:只做 stat,不读文件内容。

    界面在用户输入时用它实时显示摘要;真正开始测试时再调 resolve_source()
    做 base64 编码。这样用户敲一个字符不会触发一次 5 MB 文件读取。
    """

    kind: str = ""            # "" / "url" / "data_uri"
    summary: str = ""
    warning: str = ""
    is_local: bool = False
    size_bytes: int = 0
    path: Path | None = None
    url: str = ""


@dataclass(frozen=True)
class SourceRef:
    """真正放进请求体的源素材。"""

    kind: str
    value: str
    summary: str
    size_bytes: int = 0
    warning: str = ""
    is_local: bool = False


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------

def _human_size(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / (1024 * 1024):.1f} MB"


def _is_remote_url(raw: str) -> bool:
    return raw.lower().startswith(("http://", "https://"))


def _looks_like_other_scheme(raw: str) -> bool:
    # ftp://、file://、s3://…… 这些不是我们支持的素材引用。不单独拦的话,
    # 它们会被当成「本地路径」,然后报一个误导人的「找不到文件」。
    return "://" in raw and not _is_remote_url(raw)


def _validate_url(raw: str) -> ParseResult:
    """校验 http(s) URL,返回 urlparse 结果。"""
    if any(char.isspace() for char in raw):
        raise SourceError("素材 URL 不能包含空白字符。")

    parsed = urlparse(raw)
    if parsed.scheme.lower() not in ("http", "https"):
        raise SourceError("素材地址必须以 http:// 或 https:// 开头。")
    if not parsed.netloc or not parsed.hostname:
        raise SourceError("素材 URL 缺少主机名,请检查地址是否完整。")

    try:
        # 访问 .port 会校验端口范围;非法端口(比如 :99999)会抛 ValueError。
        parsed.port
    except ValueError as exc:
        raise SourceError(f"素材 URL 的端口不合法:{exc}") from exc

    return parsed


def _mask_url(parsed: ParseResult) -> str:
    """生成脱敏摘要:去掉 query / fragment / 用户名密码,只留主机和路径。

    签名 URL 的 query 里通常带着临时凭据,不能进界面、报告或配置文件。
    """
    host = parsed.hostname or ""
    try:
        if parsed.port:
            host = f"{host}:{parsed.port}"
    except ValueError:
        pass

    path = parsed.path or ""
    if len(path) > 48:
        name = path.rsplit("/", 1)[-1]
        path = f"/…/{name}" if name else "/…"
    return f"{host}{path}"


def _private_host_warning(parsed: ParseResult) -> str:
    """内网/本机地址的提示。

    不直接拦:自建网关可能和素材服务在同一内网,服务端确实能访问。
    但绝大多数情况下,填 localhost 的用户是在自己的机器上起了一个文件服务,
    Agnes 的服务器是够不着的。
    """
    host = (parsed.hostname or "").strip("[]").lower()
    if not host:
        return ""

    private = host in ("localhost", "127.0.0.1", "::1") or host.endswith(".local")
    if not private:
        try:
            ip = ipaddress.ip_address(host)
            private = ip.is_private or ip.is_loopback or ip.is_link_local
        except ValueError:
            pass

    if not private:
        return ""
    return "服务端大概率无法访问本机/内网地址,除非网关能访问同一内网。"


def _mime_for_suffix(suffix: str) -> str:
    return _IMAGE_MIME.get(suffix.lower(), "")


# ---------------------------------------------------------------------------
# 对外接口
# ---------------------------------------------------------------------------

def preview_source(spec: ModalitySpec, raw: str) -> SourcePreview:
    """只做本地能确定的校验,不读文件内容。

    失败抛 SourceError,消息可以直接展示。成功返回的 preview 供界面实时
    显示摘要;真正发请求前还要再走一次 resolve_source()。
    """
    raw = (raw or "").strip()

    # 不带素材的模态(文本/图像/视频)不该有源素材行;传进来也当没有。
    if spec.source_kind == "none":
        return SourcePreview()

    if not raw:
        if spec.accepts_local_file:
            raise SourceError(
                f"{spec.label}需要源图片:请填写公开图片 URL,或选择一个本地图片文件。"
            )
        raise SourceError(
            f"{spec.label}需要{spec.source_label}:请填写可公开访问的地址。"
        )

    if _is_remote_url(raw):
        parsed = _validate_url(raw)
        label = "图片 URL" if spec.source_kind == "image" else "视频 URL"
        return SourcePreview(
            kind="url",
            summary=f"{label}({_mask_url(parsed)})",
            warning=_private_host_warning(parsed),
            url=raw,
        )

    if _looks_like_other_scheme(raw):
        raise SourceError("素材地址只支持 http:// 或 https://。")

    # -- 本地文件 ------------------------------------------------------
    if not spec.accepts_local_file:
        raise SourceError(
            f"{spec.label}不支持本地文件。{spec.source_hint}"
        )

    path = Path(raw).expanduser()
    if not path.is_file():
        raise SourceError(f"找不到源素材文件:{path.name or raw}")

    suffix = path.suffix.lower()
    if suffix not in _IMAGE_MIME:
        raise SourceError("源图片格式不支持:请使用 JPG、PNG 或 WebP。")

    try:
        size = path.stat().st_size
    except OSError as exc:
        raise SourceError(f"无法读取源素材文件:{exc}") from exc

    if size <= 0:
        raise SourceError("源素材文件是空的。")
    if size > MAX_LOCAL_IMAGE_BYTES:
        raise SourceError(
            f"源图片有 {_human_size(size)},超过 5 MB 上限,请换一张小图。"
        )

    warning = ""
    if size > LARGE_IMAGE_WARN_BYTES:
        warning = (
            "源图片大于 1 MB,请求体会变大;高 RPM 下可能先撞上本机上行带宽/CPU,"
            "建议压测用小于 1 MB 的图。"
        )

    return SourcePreview(
        kind="data_uri",
        summary=f"本地文件 {path.name}({_human_size(size)},将编码为 Data URI)",
        warning=warning,
        is_local=True,
        size_bytes=size,
        path=path,
    )


def resolve_source(spec: ModalitySpec, raw: str) -> SourceRef:
    """把源素材解析成最终要放进请求体的字符串。

    URL 原样返回;本地图片读一次、base64 编码一次,拼成 Data URI。所有请求
    复用同一个字符串 —— 绝不能在每个请求里重读文件。
    """
    preview = preview_source(spec, raw)

    if preview.kind == "":
        return SourceRef(kind="", value="", summary="")

    if preview.kind == "url":
        return SourceRef(
            kind="url",
            value=preview.url,
            summary=preview.summary,
            warning=preview.warning,
        )

    # -- 本地文件:只在真正开始测试时读一次 ------------------------------
    assert preview.path is not None  # preview_source 保证 data_uri 一定带 path
    try:
        data = preview.path.read_bytes()
    except OSError as exc:
        raise SourceError(f"无法读取源素材文件:{exc}") from exc

    mime = _mime_for_suffix(preview.path.suffix)
    if not mime:  # 理论上 preview_source 已经拦过,防御性兜底
        raise SourceError("源图片格式不支持:请使用 JPG、PNG 或 WebP。")

    encoded = base64.b64encode(data).decode("ascii")
    value = f"data:{mime};base64,{encoded}"
    summary = f"本地文件 {preview.path.name}({_human_size(len(data))},Data URI)"

    return SourceRef(
        kind="data_uri",
        value=value,
        summary=summary,
        size_bytes=len(data),
        warning=preview.warning,
        is_local=True,
    )
