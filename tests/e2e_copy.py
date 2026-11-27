"""「选中复制」的完整端到端：真窗口、真鼠标、真键盘、真剪贴板。

为什么不是浏览器里的 DOM 断言：``user-select:none`` 这类东西，用
``Selection.addRange()`` 程序化选中**照样能成功** —— 那样测出来的绿是假的。
只有走真实的输入管线（Win32 鼠标按下/移动/抬起）才测得到「拖不动」这件事。

为什么是两个进程：``webview.start()`` 一个进程只能调一次，而 ``text_select``
是**建窗参数**，进程内改不掉。所以 ``old``（修复前的写法）与 ``new``（现在的写法）
各起一个子进程；主控只汇总。

对照组不是多余的：如果旧写法也能复制，说明这套测试**根本没打在病上**，
那时 new 的绿不说明任何问题。

    python tests/e2e_copy.py            # 主控：跑两组，打印判据
    python tests/e2e_copy.py --mode new # 子进程内部使用
"""
from __future__ import annotations

import ctypes
import json
import sys
import tempfile
import threading
import time
from ctypes import wintypes
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

MARK = "SELECTME_ALPHA_123"          # 注入的一句话里必须有的 ASCII 主干
VK_CONTROL, VK_C = 0x11, 0x43
MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP = 0x0002, 0x0004
CF_UNICODETEXT = 13

u32 = ctypes.WinDLL("user32", use_last_error=True)
k32 = ctypes.WinDLL("kernel32", use_last_error=True)
u32.GetClipboardData.restype = ctypes.c_void_p
u32.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]


# ----------------------------------------------------------------- 剪贴板
def clip_clear() -> None:
    if u32.OpenClipboard(None):
        u32.EmptyClipboard()
        u32.CloseClipboard()


def clip_read() -> str:
    """读系统剪贴板的文本。读不到就是空串 —— 不猜。"""
    if not u32.OpenClipboard(None):
        return ""
    try:
        h = u32.GetClipboardData(CF_UNICODETEXT)
        if not h:
            return ""
        p = k32.GlobalLock(ctypes.c_void_p(h))
        if not p:
            return ""
        try:
            return ctypes.wstring_at(p)
        finally:
            k32.GlobalUnlock(ctypes.c_void_p(h))
    finally:
        u32.CloseClipboard()


# ----------------------------------------------------------------- 真实输入
def drag(x1: int, y1: int, x2: int, y2: int, steps: int = 12, pause: float = 0.02) -> None:
    """真实的按下 → 分步移动 → 抬起。分步是必须的：一步跳到终点，
    Chromium 未必把它当一次拖选（没有中间 mousemove）。"""
    u32.SetCursorPos(int(x1), int(y1))
    time.sleep(0.08)
    u32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
    time.sleep(0.08)
    for i in range(1, steps + 1):
        u32.SetCursorPos(int(x1 + (x2 - x1) * i / steps), int(y1 + (y2 - y1) * i / steps))
        time.sleep(pause)
    time.sleep(0.08)
    u32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
    time.sleep(0.2)


def press_ctrl_c() -> None:
    u32.keybd_event(VK_CONTROL, 0, 0, 0)
    u32.keybd_event(VK_C, 0, 0, 0)
    time.sleep(0.05)
    u32.keybd_event(VK_C, 0, 2, 0)
    u32.keybd_event(VK_CONTROL, 0, 2, 0)
    time.sleep(0.35)


# ----------------------------------------------------------------- 服务
def start_server() -> tuple[str, object]:
    """起一个**自己的**工作台 + 真门（不碰用户的数据）。"""
    from agentworkspace import assistant, kernel
    from agentworkspace.selftest import _FakeHub
    from agentworkspace.server import serve

    ws = Path(tempfile.mkdtemp(prefix="e2e-copy-"))
    rt = kernel.Runtime(ws)
    assistant.install(rt)
    rt.scan()
    web = Path(__file__).resolve().parents[1] / "agentworkspace" / "web"
    httpd = serve(rt, _FakeHub(ws, rt, "pomodoro"), web, port=0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{httpd.server_address[1]}/", httpd


# ----------------------------------------------------------------- 单组
def one(mode: str) -> dict:
    import webview
    from agentworkspace import shell as sh

    base, httpd = start_server()
    out: dict = {"mode": mode, "port": int(base.rsplit(":", 1)[1].rstrip("/"))}
    kwargs = dict(width=1100, height=760,
                  min_size=(760, 520), background_color="#f7f6f4")
    try:
        if mode == "old":
            # 修复前的写法：**不**传 text_select，也**不**调 open_copy_doors
            win = webview.create_window("e2e-old", base, **kwargs)
        else:
            win = webview.create_window("e2e-new", base, text_select=True, **kwargs)
    except TypeError as exc:                 # 老版本 pywebview 没有这个参数
        out["fatal"] = f"pywebview 不支持 text_select：{exc}"
        return out

    def work() -> None:
        try:
            for _ in range(160):             # 最多等 16 秒页面画出来
                if win.evaluate_js("!!document.querySelector('#thread') && "
                                   "!!document.querySelector('#composer')"):
                    break
                time.sleep(0.1)
            else:
                out["fatal"] = "页面 16 秒内没画出 thread/composer"
                return

            if mode == "new":
                out["doors"] = sh.open_copy_doors(win, debug=False)

            # 注入一句已知文本：走产品自己的 push()，不自己造 DOM
            win.evaluate_js(
                "push(S.pid,{role:'it',text:%s,tools:[]});paintThread();1"
                % json.dumps(f"{MARK} 这是要被选中的一句话。")
            )
            time.sleep(0.4)
            info = win.evaluate_js(
                "(() => { const els=[...document.querySelectorAll('#thread .msg .body')];"
                "  const el=els.find(e=>e.textContent.indexOf(%s)>=0);"
                "  if(!el) return null; const r=el.getBoundingClientRect();"
                "  return {x:r.x,y:r.y,w:r.width,h:r.height,dpr:window.devicePixelRatio||1}; })()"
                % json.dumps(MARK)
            )
            out["box"] = info
            if not info:
                out["fatal"] = "注入的消息没出现在 DOM 里"
                return

            hwnd = int(win.native.Handle)
            out["hwnd"] = hwnd
            u32.ShowWindow(hwnd, 9)               # SW_RESTORE
            u32.SetForegroundWindow(hwnd)
            time.sleep(0.5)
            out["foreground"] = int(u32.GetForegroundWindow()) == hwnd

            pt = wintypes.POINT(0, 0)
            u32.ClientToScreen(hwnd, ctypes.byref(pt))
            dpr = float(info["dpr"])
            y = pt.y + (info["y"] + info["h"] / 2) * dpr
            x1 = pt.x + (info["x"] + 4) * dpr
            x2 = pt.x + (info["x"] + info["w"] - 4) * dpr
            out["dpr"] = dpr

            clip_clear()
            drag(x1, y, x2, y)
            out["selected"] = str(win.evaluate_js("String(window.getSelection())") or "")
            out["bodySelect"] = str(win.evaluate_js(
                "getComputedStyle(document.body).webkitUserSelect") or "")

            clip_clear()
            press_ctrl_c()
            out["clip"] = clip_read()

            try:
                s = win.native.webview.CoreWebView2.Settings
                out["ctxMenu"] = bool(s.AreDefaultContextMenusEnabled)
                out["accel"] = bool(s.AreBrowserAcceleratorKeysEnabled)
                out["devtools"] = bool(s.AreDevToolsEnabled)
            except Exception as exc:
                out["settingsError"] = f"{type(exc).__name__}: {exc}"
        except Exception as exc:
            out["fatal"] = f"{type(exc).__name__}: {exc}"
        finally:
            try:
                win.destroy()
            except Exception:
                pass
            try:
                httpd.shutdown()
            except Exception:
                pass

    def on_loaded() -> None:
        threading.Thread(target=work, daemon=True).start()

    def watchdog() -> None:
        # WebView2 在无桌面会话里**起不来也不抛异常**：loaded 永远不触发，
        # webview.start() 就一直阻塞。没有这道闸，脚本会挂到外层超时，
        # 而挂死不给原因 —— 那比失败更糟。
        if not win.events.loaded.wait(12):
            out.setdefault("fatal", "12 秒内 loaded 没触发：WebView2 控制器大概没建起来"
                                    "（本机若无交互桌面，这组测试跑不了）")
            try:
                win.destroy()
            except Exception:
                pass

    threading.Thread(target=watchdog, daemon=True).start()
    win.events.loaded += on_loaded
    webview.start()                    # 关掉窗口才返回
    return out


# ----------------------------------------------------------------- 主控
def judge(old: dict, new: dict) -> list[tuple[str, bool, str]]:
    rows: list[tuple[str, bool, str]] = []

    def add(name: str, cond: bool, detail: str = "") -> None:
        rows.append((name, bool(cond), detail))

    add("两组都真的跑完了（没有致命错误）",
        not old.get("fatal") and not new.get("fatal"),
        f"old={old.get('fatal')!r} new={new.get('fatal')!r}")
    add("两组窗口都拿到前台（否则输入的落点不可信）",
        old.get("foreground") and new.get("foreground"),
        f"old={old.get('foreground')} new={new.get('foreground')}")

    # 对照组：证明这套测试真的打在病上
    add("【对照】修复前 body 是 user-select:none（病真的存在）",
        old.get("bodySelect") == "none", f"bodySelect={old.get('bodySelect')!r}")
    add("【对照】修复前拖选选不中",
        MARK not in (old.get("selected") or ""), f"selected={old.get('selected')!r}")
    add("【对照】修复前 Ctrl+C 没进剪贴板",
        MARK not in (old.get("clip") or ""), f"clip={old.get('clip')!r}")

    # 修复后
    add("修复后 body 是 user-select:text",
        new.get("bodySelect") == "text", f"bodySelect={new.get('bodySelect')!r}")
    add("修复后拖选真的选中了那句",
        MARK in (new.get("selected") or ""), f"selected={new.get('selected')!r}")
    add("修复后 Ctrl+C 真的进了剪贴板",
        MARK in (new.get("clip") or ""), f"clip={new.get('clip')!r}")
    add("选中文字与剪贴板一致（没有多复制/少复制）",
        (new.get("selected") or "").strip() == (new.get("clip") or "").strip(),
        f"sel={new.get('selected')!r} clip={new.get('clip')!r}")
    add("右键菜单可用（AreDefaultContextMenusEnabled）",
        new.get("ctxMenu") is True, str(new.get("ctxMenu")))
    add("快捷键可用（AreBrowserAcceleratorKeysEnabled）",
        new.get("accel") is True, str(new.get("accel")))
    add("DevTools 仍然关着（不因为要复制就把 F12 送出去）",
        new.get("devtools") is False, str(new.get("devtools")))
    return rows


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--mode" in argv:
        mode = argv[argv.index("--mode") + 1]
        print("RESULT " + json.dumps(one(mode), ensure_ascii=False))
        return 0

    import subprocess

    got: dict[str, dict] = {}
    for mode in ("old", "new"):
        r = subprocess.run([sys.executable, __file__, "--mode", mode],
                           capture_output=True, text=True, encoding="utf-8", timeout=120)
        line = next((l for l in r.stdout.splitlines() if l.startswith("RESULT ")), None)
        if line is None:
            print(f"[{mode}] 子进程没有交回结果\nSTDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}")
            return 2
        got[mode] = json.loads(line[7:])

    rows = judge(got["old"], got["new"])
    bad = 0
    for name, ok, detail in rows:
        print(f"{'OK  ' if ok else 'FAIL'} {name}" + (f"   [{detail}]" if not ok and detail else ""))
        bad += 0 if ok else 1
    print(f"\n{len(rows) - bad}/{len(rows)} 通过")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
