"""窗口图标 —— **自己画的那一个**，不是 python.exe 的标。

为什么要有它：pywebview 在 Windows 上会把 ``Form.Icon`` 设成从 ``sys.executable``
里抠出来的图标（见 ``webview/platforms/winforms.py``），于是窗口左上角、任务栏、
Alt-Tab 全是 Python 的蛇。一个产品不该顶着解释器的标。

这个文件只用标准库（zlib + struct）画图并打包成 ICO：

    16/24/32/48/64   32 位 BMP（DIB）条目 —— 老外壳与新窗口管理器都认
    128/256          PNG 条目（Vista 起支持，体积小得多）

图形语言和界面一致：左边一条亮色的「栏」，右边两张「卡」——
就是这个窗口自己的样子（左窄栏 + 舞台），缩到 16px 也还认得出来。

用法::

    python -m agentworkspace.assets.make_icon                      # 写 agentworkspace/assets/icon.ico
    python -m agentworkspace.assets.make_icon --preview .tmp/i.png # 额外出一张放大对照图
"""

from __future__ import annotations

import argparse
import math
import struct
import zlib
from pathlib import Path

DEFAULT_OUT = Path(__file__).resolve().parent / "icon.ico"

# 尺寸表：<=64 用 DIB，更大的用 PNG
DIB_SIZES = (16, 24, 32, 48, 64)
PNG_SIZES = (128, 256)
SIZES = DIB_SIZES + PNG_SIZES

TOP = (0x74, 0xB0, 0xFF)        # 渐变上端（偏亮的强调色）
BOTTOM = (0x2F, 0x6F, 0xE0)     # 渐变下端
RADIUS = 0.24                   # 圆角半径（归一化）
SS = 4                          # 每个像素 4x4 超采样

# 图形（归一化坐标）：左栏 + 两张卡
RAIL = (0.150, 0.180, 0.320, 0.820, 0.060)
CARD1 = (0.395, 0.265, 0.850, 0.435, 0.070)
CARD2 = (0.395, 0.565, 0.850, 0.735, 0.070)


def _inside_round_rect(px: float, py: float, x0: float, y0: float,
                       x1: float, y1: float, r: float) -> bool:
    """点是否落在圆角矩形里。四个角各判一次圆，其余按矩形判。"""
    if px < x0 or px > x1 or py < y0 or py > y1:
        return False
    cx = min(max(px, x0 + r), x1 - r)
    cy = min(max(py, y0 + r), y1 - r)
    if cx == px and cy == py:          # 在四条直边上，不是角
        return True
    dx, dy = px - cx, py - cy
    return dx * dx + dy * dy <= r * r


def render(size: int) -> list[list[tuple[int, int, int, int]]]:
    """画一张 ``size × size`` 的 RGBA 图（超采样求平均，边缘才不会锯齿）。"""
    px = []
    n = SS * SS
    for y in range(size):
        row = []
        for x in range(size):
            ar = ag = ab = aa = 0.0
            for sy in range(SS):
                ny = (y + (sy + 0.5) / SS) / size
                for sx in range(SS):
                    nx = (x + (sx + 0.5) / SS) / size
                    if not _inside_round_rect(nx, ny, 0.0, 0.0, 1.0, 1.0, RADIUS):
                        continue
                    t = ny                                     # 纵向渐变
                    r = TOP[0] + (BOTTOM[0] - TOP[0]) * t
                    g = TOP[1] + (BOTTOM[1] - TOP[1]) * t
                    b = TOP[2] + (BOTTOM[2] - TOP[2]) * t
                    if (_inside_round_rect(nx, ny, *RAIL)
                            or _inside_round_rect(nx, ny, *CARD1)
                            or _inside_round_rect(nx, ny, *CARD2)):
                        r, g, b = 255.0, 255.0, 255.0          # 白色图形
                    ar += r / n
                    ag += g / n
                    ab += b / n
                    aa += 1.0 / n
            a = int(round(aa * 255))
            if a == 0:
                row.append((0, 0, 0, 0))
            else:                       # 颜色按覆盖率归一，半透明边缘才不会发暗
                row.append((int(round(ar / aa)), int(round(ag / aa)),
                            int(round(ab / aa)), a))
        px.append(row)
    return px


def _png(size: int, img) -> bytes:
    """最小的 PNG 编码器：一张 RGBA 图，一个 IDAT。"""
    raw = bytearray()
    for row in img:
        raw.append(0)                                   # filter: none
        for r, g, b, a in row:
            raw += bytes((r, g, b, a))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(bytes(raw), 9)) + chunk(b"IEND", b""))


def _dib(size: int, img) -> bytes:
    """32 位 BMP（DIB）条目：BITMAPINFOHEADER + 自下而上的 BGRA + AND 掩码。"""
    header = struct.pack("<IiiHHIIiiII", 40, size, size * 2, 1, 32, 0,
                         size * size * 4, 0, 0, 0, 0)
    xor = bytearray()
    for row in reversed(img):                           # BMP 是自下而上
        for r, g, b, a in row:
            xor += bytes((b, g, r, a))
    stride = ((size + 31) // 32) * 4                    # AND 掩码按 32 位对齐
    and_mask = bytearray()
    for row in reversed(img):
        bits = bytearray(stride)
        for x, (_r, _g, _b, a) in enumerate(row):
            if a < 128:                                  # 1 = 透明
                bits[x // 8] |= 0x80 >> (x % 8)
        and_mask += bits
    return header + bytes(xor) + bytes(and_mask)


def build(out: Path = DEFAULT_OUT, preview: Path | None = None) -> Path:
    """画出全部尺寸并打包成 ICO。返回写出的路径。"""
    frames: list[tuple[int, bytes, bool]] = []           # (size, data, is_png)
    for size in SIZES:
        img = render(size)
        if size in PNG_SIZES:
            frames.append((size, _png(size, img), True))
        else:
            frames.append((size, _dib(size, img), False))

    count = len(frames)
    offset = 6 + 16 * count
    entries, blobs = b"", b""
    for size, data, _png_frame in frames:
        entries += struct.pack("<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32,
                               len(data), offset)
        blobs += data
        offset += len(data)
    payload = struct.pack("<HHH", 0, 1, count) + entries + blobs

    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(payload)

    if preview is not None:
        _sheet(frames, Path(preview))
    return out


def _sheet(frames: list[tuple[int, bytes, bool]], out: Path) -> None:
    """对照图：每个尺寸按整数倍放大，排成一行 —— 16px 认不认得出，看这张。"""
    tiles = []
    for size, _data, _png_frame in frames:
        # 小尺寸放大到 128，大尺寸保持原样（1:1）—— 目的是看清 16px 认不认得出
        scale = max(1, 128 // size)
        big = size * scale
        tiles.append((big, render(size)))
    width = sum(big + 16 for big, _img in tiles) + 8
    height = max(big for big, _img in tiles) + 32
    canvas = [[(24, 27, 31, 255) for _ in range(width)] for _ in range(height)]
    x0 = 8
    for big, img in tiles:
        size = len(img)
        scale = big // size
        oy = (height - big) // 2
        for y in range(big):
            for x in range(big):
                r, g, b, a = img[y // scale][x // scale]
                if a:
                    canvas[oy + y][x0 + x] = (r, g, b, 255)
        x0 += big + 16
    # 预览图不需要 alpha：直接转成不透明的 PNG
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_bytes(_png_flat(width, height, canvas))


def _png_flat(w: int, h: int, canvas) -> bytes:
    raw = bytearray()
    for row in canvas:
        raw.append(0)
        for r, g, b, _a in row:
            raw += bytes((r, g, b))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(bytes(raw), 9)) + chunk(b"IEND", b""))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="agentworkspace.assets.make_icon", description="画窗口图标")
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--preview", default=None, help="额外写一张放大对照图（PNG）")
    a = ap.parse_args(argv)
    path = build(Path(a.out), Path(a.preview) if a.preview else None)
    print(f"[图标] 已写出 {path}（{path.stat().st_size} 字节，{len(SIZES)} 个尺寸）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
