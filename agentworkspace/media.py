"""图片的预检 —— 在**发之前**把坏图拦下来。

判据与服务端同一套（``aaas_service/media.py``）：字节上限、张数上限、魔数与
``mimeType`` 自洽、像素规模。方向相反的那一条是：``mimeType`` 由**字节**决定，
不采信浏览器自述 —— ``File.type`` 可能是空串，也可能自称 ``image/jpeg`` 而内容
其实是 png，而服务端会**按实际拒掉**（§7 第 8 条）。

为什么值得在本地再判一遍（服务端明明已经判了）：服务端对坏图回 ``-32602``，
而那是**轮级**失败 —— 整轮对话连同用户刚说的那句话一起死掉。预检的用途不是
省一次往返，是把「这一轮失败了」换成一句人话，并且让那一轮根本不用开。

本文件只依赖标准库：界面这一侧不该为了读一张图的宽高去装一个图像库。
"""

from __future__ import annotations

import base64
import hashlib
import struct
from dataclasses import dataclass

import aaas

#: 只收这四种 —— 与协议**同一份清单**（``aaas.FILE_MIME_TYPES``）。照抄一份
#: 字符串表迟早会漂，而漂的方向是「协议收了、这里不认识」。
IMAGE_MIME_TYPES: tuple[str, ...] = tuple(aaas.FILE_MIME_TYPES)

#: 单张图字节上限的兜底值。真正该用的那个来自连接自报的
#: ``connect_result.limits.maxFileBytes``（服务端可能配成别的数）——
#: 两处取不同的值时，这里放行的图会在服务端被判死，等于这道预检没做。
DEFAULT_MAX_FILE_BYTES = 4 << 20

#: 一轮最多几张图。与服务端 ``MAX_IMAGES_PER_TURN`` 同一个数：这是**成本闸**，
#: 每张图都进模型请求、都按图算 token。
MAX_IMAGES_PER_TURN = 8

#: 单张图的像素上限（4000 万，与服务端同一条）。超过它的图绝大多数视觉模型
#: 都会先自行缩图 —— 替它先拒，省下的是一次白跑的请求。
MAX_IMAGE_PIXELS = 40_000_000

#: history 里图片的两道账（张数 / 总字节），与服务端 ``MAX_HISTORY_IMAGES`` /
#: ``MAX_HISTORY_IMAGE_BYTES`` 同一个数。stateless 下 history 每轮重发全史，
#: 于是这是唯一一处「成本随轮数累积」的口子：超了不是变慢，是整轮被拒。
HISTORY_IMAGES = 16
HISTORY_BYTES = 16 << 20

#: 后缀只用于落盘文件名：mime 是协议里的真名，后缀是给人看的那份。
EXT = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp", "image/gif": "gif"}

_JPEG_SOF = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}


def sniff(raw: bytes) -> str | None:
    """按**魔数**判定真实类型；判不出返回 ``None``。

    只看头部几个字节就够：这不是内容审查，是「这份字节自称是什么」。
    """
    head = raw[:16]
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith(b"GIF87a") or head.startswith(b"GIF89a"):
        return "image/gif"
    if len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    return None


def size(raw: bytes, mime: str) -> tuple[int, int] | None:
    """从**头部**读出宽高；读不出返回 ``None``。

    为什么只读头部：面板这一侧不该为了一张 4 MB 的图把它整个解码一遍。
    四种格式的尺寸都在头部里，代价是几个字节的解析。
    """
    try:
        if mime == "image/png":
            if raw[12:16] != b"IHDR":
                return None
            return struct.unpack(">II", raw[16:24])
        if mime == "image/gif":
            return struct.unpack("<HH", raw[6:10])
        if mime == "image/jpeg":
            i = 2
            while i + 9 < len(raw):
                if raw[i] != 0xFF:
                    return None
                marker = raw[i + 1]
                if marker in _JPEG_SOF:
                    # SOF: FF Cx len(2) precision(1) height(2) width(2)
                    h, w = struct.unpack(">HH", raw[i + 5:i + 9])
                    return (w, h)
                seg = struct.unpack(">H", raw[i + 2:i + 4])[0]
                if seg < 2:            # 段长为 0/1 是坏帧，别陷进死循环
                    return None
                i += 2 + seg
            return None
        if mime == "image/webp":
            chunk = raw[12:16]
            if chunk == b"VP8X":
                w = int.from_bytes(raw[24:27], "little") + 1
                h = int.from_bytes(raw[27:30], "little") + 1
                return (w, h)
            if chunk == b"VP8 ":
                if raw[23:26] != b"\x9d\x01\x2a":
                    return None
                w = int.from_bytes(raw[26:28], "little") & 0x3FFF
                h = int.from_bytes(raw[28:30], "little") & 0x3FFF
                return (w, h)
            if chunk == b"VP8L":
                bits = int.from_bytes(raw[21:25], "little")
                return (1 + (bits & 0x3FFF), 1 + ((bits >> 14) & 0x3FFF))
            return None
    except (struct.error, IndexError, ValueError):
        # 头部短于解析所需 —— 这是「读不出尺寸」，不是崩溃。
        return None
    return None


@dataclass(frozen=True)
class Image:
    """一张**已经验过**的图。字节是解码后的，落盘与发送都用它。"""

    name: str
    mime: str
    raw: bytes

    @property
    def size(self) -> int:
        return len(self.raw)

    @property
    def ext(self) -> str:
        return EXT.get(self.mime, "bin")

    @property
    def sha(self) -> str:
        """16 位十六进制。两个用途：落盘文件名（同一张图发两次只存一份）、
        界面上认出「这是同一张」。"""
        return hashlib.sha256(self.raw).hexdigest()[:16]


def _mb(n: int) -> str:
    """给人看的字节数。「4194304 字节」读不出规模，而判错的正是规模。"""
    if n >= 1 << 20:
        return f"{n / (1 << 20):.1f} MB"
    return f"{n / 1024:.0f} KB"


def _renamed(name: str, ext: str) -> str:
    """把自称的后缀换成**实际**的那个，别让文件名继续撒谎。"""
    head, dot, _tail = name.rpartition(".")
    return f"{head}.{ext}" if dot and head else f"{name}.{ext}"


def _one(index: int, item: dict, *, max_bytes: int) -> Image | str:
    """验一张。通过返回 :class:`Image`，不通过返回一句人话。

    为什么返回而不是抛：调用方手上可能有一批图，而「第 3 张不行」应该是
    **一句解释**，不该是一条把整次发送打死的异常。
    """
    label = f"第 {index + 1} 张"
    if not isinstance(item, dict):
        return f"{label}的格式不对（要 {{name, mime, data}}）"
    name = str(item.get("name") or "").strip()[:120] or f"图{index + 1}.png"
    data = item.get("data")
    if not isinstance(data, str) or not data.strip():
        return f"「{name}」是空文件"
    if data.startswith("data:"):
        # 界面给了整段 data URL：base64 在逗号之后。静默切掉那一半比留一条
        # 「不是合法 base64」更好 —— 那句话会让人去查编码，而问题在分隔符。
        data = data.split(",", 1)[1] if "," in data else ""
    data = data.replace("\n", "").replace("\r", "")
    try:
        # validate=True：非规范 base64 当场拒。服务端对「长度与内容不自洽」的帧
        # 回 -32602，而那同样是轮级失败。
        raw = base64.b64decode(data, validate=True)
    except (ValueError, TypeError):
        return f"「{name}」的图片数据不是合法 base64"
    if not raw:
        return f"「{name}」是空文件"
    if len(raw) > max_bytes:
        return (f"「{name}」有 {_mb(len(raw))}，超过这张连接的上限 {_mb(max_bytes)}"
                " —— 先缩小或压缩再发")
    mime = sniff(raw)
    if mime is None:
        kinds = " / ".join(m.split("/")[1] for m in IMAGE_MIME_TYPES)
        return f"「{name}」不是图片（按字节判不出类型）—— 只收 {kinds}"
    # 按**实际**定 mime，不采信自述（见文件头）。服务端只认「魔数与自述一致」，
    # 而这里把自述换成实际的那一刻，一致性就成立 —— 我们发出去的帧永远自洽。
    declared = str(item.get("mime") or "")
    if declared and declared != mime:
        name = _renamed(name, EXT.get(mime, "bin"))
    wh = size(raw, mime)
    if wh is None:
        return f"「{name}」的图片头部读不出尺寸 —— 文件可能没写完"
    if wh[0] * wh[1] > MAX_IMAGE_PIXELS:
        return (f"「{name}」是 {wh[0]}×{wh[1]}（{wh[0] * wh[1] / 1e6:.0f} 万像素），"
                f"上限 {MAX_IMAGE_PIXELS // 1_000_000} 万 —— 先缩小再发")
    return Image(name=name, mime=mime, raw=raw)


def prepare(
    images: list[dict],
    *,
    max_bytes: int = DEFAULT_MAX_FILE_BYTES,
    max_items: int = MAX_IMAGES_PER_TURN,
) -> tuple[list[Image], str]:
    """一次发送的全部图 —— 返回 ``（通过的那些, 第一句人话）``。

    判据是**一次判到底**：只要有一张不行就整批不发。不做「先发能发的那几张」：
    那会让用户以为整批都到了（而模型只看见一部分），而「漏掉的是哪张」
    只能从沉默里猜。
    """
    images = list(images or ())
    if len(images) > max_items:
        return ([], f"一次最多 {max_items} 张图，这次有 {len(images)} 张")
    out: list[Image] = []
    for index, item in enumerate(images):
        got = _one(index, item, max_bytes=max_bytes)
        if isinstance(got, str):
            return ([], got)
        out.append(got)
    return (out, "")


def message(text: str, images: list[Image]):
    """把一句话与几张图拼成一条 ``aaas.Message``。

    空的文本块**不要**：``parts`` 只放真的存在的东西。只有图、没有字是合法的
    一句话（「这张怎么样」的省略式），协议不要求文本块。
    """
    parts = []
    if text.strip():
        parts.append(aaas.Part.t(text))
    for im in images:
        parts.append(aaas.Part.image(im.raw, im.mime, im.name))
    return aaas.Message(role="user", parts=tuple(parts))
