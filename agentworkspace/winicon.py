"""窗口图标读回 —— 「左上角那个标，到底是谁的」。

为什么需要它：**设置 `icon=` 不等于图标换上了。** pywebview 的文档写着
「icon 只在 GTK/QT 生效」，而 winforms 实现里确有一段 `self.Icon = Icon(path)`；
文档与实现哪个是真的，只有读回窗口上的那个 HICON 才算数。

做法（全部是 Windows 的公开 API，不猜）：

    WM_GETICON / GetClassLongPtr   拿到窗口此刻挂着的大/小图标句柄
    GetIconInfo + GetDIBits        把那个句柄拆成像素
    和 agentworkspace/assets/icon.ico 的 32px 帧、以及 python.exe 的图标逐像素比

结论三选一：``ours``（是我们的标）/ ``python``（还是解释器的标）/ ``other``。

用法::

    python -m agentworkspace.winicon                       # 找「我的工作区」那个窗口
    python -m agentworkspace.winicon --title 界面行为验收
    python -m agentworkspace.winicon --out .tmp/qa/icons   # 把三张图都存下来看
"""

from __future__ import annotations

import argparse
import ctypes
import struct
import sys
import zlib
from ctypes import wintypes
from pathlib import Path

u = ctypes.windll.user32
g = ctypes.windll.gdi32

WM_GETICON = 0x007F
ICON_SMALL, ICON_BIG, ICON_SMALL2 = 0, 1, 2
GCLP_HICON, GCLP_HICONSM = -14, -34
DIB_RGB_COLORS = 0


class ICONINFO(ctypes.Structure):
    _fields_ = [("fIcon", wintypes.BOOL), ("xHotspot", wintypes.DWORD),
                ("yHotspot", wintypes.DWORD), ("hbmMask", wintypes.HBITMAP),
                ("hbmColor", wintypes.HBITMAP)]


class BITMAP(ctypes.Structure):
    _fields_ = [("bmType", ctypes.c_long), ("bmWidth", ctypes.c_long),
                ("bmHeight", ctypes.c_long), ("bmWidthBytes", ctypes.c_long),
                ("bmPlanes", ctypes.c_ushort), ("bmBitsPixel", ctypes.c_ushort),
                ("bmBits", ctypes.c_void_p)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", ctypes.c_uint32), ("biWidth", ctypes.c_int32),
                ("biHeight", ctypes.c_int32), ("biPlanes", ctypes.c_uint16),
                ("biBitCount", ctypes.c_uint16), ("biCompression", ctypes.c_uint32),
                ("biSizeImage", ctypes.c_uint32), ("biXPelsPerMeter", ctypes.c_int32),
                ("biYPelsPerMeter", ctypes.c_int32), ("biClrUsed", ctypes.c_uint32),
                ("biClrImportant", ctypes.c_uint32)]


WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)


def _bind() -> None:
    """显式声明参数类型。

    不声明时 ctypes 把 HGDIOBJ/HICON 当 32 位 int，句柄一大就
    ``OverflowError: int too long to convert`` —— 在 64 位上几乎是必然。
    这类错**只在真的拿到句柄时才出现**，所以极易漏到"跑起来才发现"。
    """
    L = ctypes.c_ssize_t
    g.GetObjectW.argtypes = [wintypes.HGDIOBJ, ctypes.c_int, ctypes.c_void_p]
    g.GetObjectW.restype = ctypes.c_int
    g.DeleteObject.argtypes = [wintypes.HGDIOBJ]
    g.DeleteObject.restype = wintypes.BOOL
    g.GetDIBits.argtypes = [wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT,
                            ctypes.c_void_p, ctypes.POINTER(BITMAPINFOHEADER), wintypes.UINT]
    g.GetDIBits.restype = ctypes.c_int
    u.GetIconInfo.argtypes = [wintypes.HICON, ctypes.POINTER(ICONINFO)]
    u.GetIconInfo.restype = wintypes.BOOL
    u.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    u.SendMessageW.restype = L
    u.GetClassLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
    u.GetClassLongPtrW.restype = L
    u.GetDC.argtypes = [wintypes.HWND]
    u.GetDC.restype = wintypes.HDC
    u.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
    u.ReleaseDC.restype = ctypes.c_int
    # 下面这几个也在拿句柄：不声明 argtypes，ctypes 会把 hwnd 当 32 位 int 转，
    # 64 位上门柄一大就 ValueError —— 而且只在真的找到窗口时才出现
    u.EnumWindows.argtypes = [WNDENUMPROC, wintypes.LPARAM]
    u.EnumWindows.restype = wintypes.BOOL
    u.IsWindowVisible.argtypes = [wintypes.HWND]
    u.IsWindowVisible.restype = wintypes.BOOL
    u.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    u.GetWindowTextLengthW.restype = ctypes.c_int
    u.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    u.GetWindowTextW.restype = ctypes.c_int


_bind()


# ---------------------------------------------------------------- 找窗口
def find_window(title: str) -> int | None:
    """标题含 ``title`` 的可见顶层窗口。"""
    found: list[int] = []

    @WNDENUMPROC
    def cb(hwnd, _l):
        if not u.IsWindowVisible(hwnd):
            return True
        n = u.GetWindowTextLengthW(hwnd)
        if n <= 0:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        u.GetWindowTextW(hwnd, buf, n + 1)
        if title in buf.value:
            found.append(int(hwnd))
        return True

    u.EnumWindows(cb, 0)
    return found[0] if found else None


def window_icons(hwnd: int) -> dict[str, int]:
    """这个窗口此刻挂着哪些图标句柄（大 / 小 / 类图标）。"""
    out: dict[str, int] = {}
    for name, kind in (("big", ICON_BIG), ("small", ICON_SMALL)):
        h = int(u.SendMessageW(hwnd, WM_GETICON, kind, 0))
        if h:
            out[name] = h
    # 窗口自己没设时会落到类图标上（那正是 python.exe 那个）
    for name, idx in (("class_big", GCLP_HICON), ("class_small", GCLP_HICONSM)):
        try:
            h = int(u.GetClassLongPtrW(hwnd, idx))
        except Exception:
            h = 0
        if h:
            out[name] = h
    return out


# ---------------------------------------------------------------- 拆图标
def hicon_rgba(hicon: int) -> tuple[int, int, bytes]:
    """HICON → (宽, 高, 自上而下的 RGBA 字节)。"""
    ii = ICONINFO()
    if not u.GetIconInfo(wintypes.HICON(hicon), ctypes.byref(ii)):
        raise OSError("GetIconInfo 失败")
    try:
        bm = BITMAP()
        if not g.GetObjectW(ii.hbmColor, ctypes.sizeof(BITMAP), ctypes.byref(bm)):
            raise OSError("GetObject 失败")
        w, h = int(bm.bmWidth), int(bm.bmHeight)
        hdc = u.GetDC(None)
        try:
            bi = BITMAPINFOHEADER()
            bi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
            bi.biWidth = w
            bi.biHeight = -h                  # 负数 = 自上而下，省一次翻转
            bi.biPlanes = 1
            bi.biBitCount = 32
            bi.biCompression = 0              # BI_RGB
            buf = ctypes.create_string_buffer(w * h * 4)
            n = g.GetDIBits(hdc, ii.hbmColor, 0, h, buf, ctypes.byref(bi), DIB_RGB_COLORS)
            if n == 0:
                raise OSError("GetDIBits 失败")
            bgra = buf.raw
        finally:
            u.ReleaseDC(None, hdc)
    finally:
        for hbm in (ii.hbmColor, ii.hbmMask):
            if hbm:
                g.DeleteObject(hbm)
    out = bytearray(len(bgra))
    for i in range(0, len(bgra), 4):           # BGRA → RGBA
        out[i] = bgra[i + 2]
        out[i + 1] = bgra[i + 1]
        out[i + 2] = bgra[i]
        out[i + 3] = bgra[i + 3]
    return w, h, bytes(out)


def exe_icon(exe: str) -> int:
    """从 exe 里抠一个大图标 —— 「默认那个标」的基准。"""
    big, small = wintypes.HICON(), wintypes.HICON()
    n = ctypes.windll.shell32.ExtractIconExW(str(exe), 0, ctypes.byref(big),
                                            ctypes.byref(small), 1)
    if n <= 0 or not big.value:
        raise OSError(f"从 {exe} 抠不出图标")
    return int(big.value)


# ---------------------------------------------------------------- 比像素
def png(w: int, h: int, rgba: bytes) -> bytes:
    raw = bytearray()
    for y in range(h):
        raw.append(0)
        raw += rgba[y * w * 4:(y + 1) * w * 4]

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(raw), 6)) + chunk(b"IEND", b""))


def _at(w: int, h: int, rgba: bytes, x: int, y: int) -> tuple[int, int, int, int]:
    i = (y * w + x) * 4
    return rgba[i], rgba[i + 1], rgba[i + 2], rgba[i + 3]


def similar(a: tuple[int, int, int, int], b: tuple[int, int, int, int], tol: int = 12) -> bool:
    return all(abs(a[i] - b[i]) <= tol for i in range(3))


def match_ratio(wa: int, ha: int, pa: bytes, wb: int, hb: int, pb: bytes) -> float:
    """两张图按比例重采样后，颜色接近的像素占比（透明像素不算）。"""
    if wa == 0 or ha == 0:
        return 0.0
    hit = total = 0
    for y in range(32):
        for x in range(32):
            a = _at(wa, ha, pa, min(wa - 1, x * wa // 32), min(ha - 1, y * ha // 32))
            b = _at(wb, hb, pb, min(wb - 1, x * wb // 32), min(hb - 1, y * hb // 32))
            if a[3] < 32 and b[3] < 32:
                continue
            total += 1
            if a[3] >= 32 and b[3] >= 32 and similar(a, b):
                hit += 1
    return hit / total if total else 0.0


def ico_frame(path: Path, size: int = 32) -> tuple[int, int, bytes]:
    """读我们自己那张 ico 的某一帧（用 Pillow；没有就退化成「读不到」）。"""
    from PIL import Image

    im = Image.open(path)
    im.size = (size, size)
    im = im.convert("RGBA")
    return im.width, im.height, im.tobytes()


# ---------------------------------------------------------------- 命令行
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="agentworkspace.winicon", description="窗口图标读回")
    ap.add_argument("--title", default="我的工作区", help="窗口标题里的片段")
    ap.add_argument("--icon", default=str(Path(__file__).parent / "assets" / "icon.ico"))
    ap.add_argument("--exe", default=sys.executable, help="基准：哪个 exe 的图标算「默认」")
    ap.add_argument("--out", default=None, help="把三张图存到这里（目录）")
    a = ap.parse_args(argv)

    hwnd = find_window(a.title)
    if not hwnd:
        print(f"[图标] 没找到标题含「{a.title}」的窗口 —— 没有窗口就无从谈起")
        return 2
    icons = window_icons(hwnd)
    print(f"[图标] 窗口 0x{hwnd:X}「{a.title}」；句柄 {icons}")
    if not icons:
        print("[图标] 这个窗口一个图标句柄都没有（连类图标也没有）")
        return 3

    got = icons.get("big") or icons.get("class_big") or next(iter(icons.values()))
    w, h, pix = hicon_rgba(got)
    print(f"[图标] 窗口大图标 {w}×{h}")

    ours = ico_frame(Path(a.icon), 32)
    ratio_ours = match_ratio(w, h, pix, *ours)
    print(f"[图标] 与我们的标（icon.ico 的 32px 帧）相似度 {ratio_ours:.0%}")

    ratio_py = -1.0
    try:
        py = hicon_rgba(exe_icon(a.exe))
        ratio_py = match_ratio(w, h, pix, *py)
        print(f"[图标] 与 {Path(a.exe).name} 的图标相似度 {ratio_py:.0%}")
    except OSError as exc:
        print(f"[图标] 基准图标取不到（{exc}）")

    if a.out:
        out = Path(a.out)
        out.mkdir(parents=True, exist_ok=True)
        (out / "window.png").write_bytes(png(w, h, pix))
        (out / "ours32.png").write_bytes(png(*ours))
        if ratio_py >= 0:
            (out / "baseline.png").write_bytes(png(*py))
        print(f"[图标] 图已存到 {out}")

    verdict = "ours" if ratio_ours >= 0.85 and ratio_ours > ratio_py else (
        "python" if ratio_py >= 0.85 else "other")
    print(f"[图标] 结论：{verdict}")
    return 0 if verdict == "ours" else 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
