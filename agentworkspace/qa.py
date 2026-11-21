"""真窗口验收 —— 在**交互式桌面**里把外壳与原生程序真的跑一遍。

为什么需要这个文件：宿主经常是在 Session 0（服务会话）里被启动的，那里的
窗口站是 ``Service-0x0-3e7$``、``WSF_VISIBLE=0``。WebView2 在那种窗口站里
**能建窗口、永远不渲染** —— 于是「壳正常」和「壳白屏」看起来一模一样，
``webview.start()`` 甚至照样正常返回。

本模块不信任任何自我声明，判据全部来自操作系统：

    EnumWindows 数窗口 · GetWindowRect 看尺寸 · 截图看像素

它还能自己**走进交互式会话**：若当前是非交互窗口站且进程具备
``SeTcbPrivilege``（SYSTEM），就用 WTSQueryUserToken + CreateProcessAsUserW
把自己重投到活跃控制台会话里再跑。这不是绕过权限，是「服务进程要验桌面」
本来就该走的路。

用法::

    python -m agentworkspace.qa                 # 全部（desktop + native）
    python -m agentworkspace.qa desktop         # 只验桌面外壳
    python -m agentworkspace.qa native          # 只验原生程序窗口
    python -m agentworkspace.qa --no-relaunch   # 就地跑（调试用；Session 0 里会得到 blank）

退出码 0 = 全部通过。截图落在 ``.tmp/qa/``。
"""

from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wt
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / ".tmp" / "qa"

# ---------------------------------------------------------------- Win32 句柄
# DPI 感知：不声明的话 GetWindowRect 给的是逻辑坐标，而 ImageGrab 按物理像素裁，
# 截出来的图会整体偏移几十到上百像素 —— 偏移不会报错，只会让你看错东西。
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)      # per-monitor
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

u = ctypes.windll.user32
k32 = ctypes.windll.kernel32
advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
wtsapi32 = ctypes.WinDLL("wtsapi32", use_last_error=True)
userenv = ctypes.WinDLL("userenv", use_last_error=True)

WNDENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
CREATE_UNICODE_ENVIRONMENT = 0x00000400
SE_PRIVILEGE_ENABLED = 0x00000002
TOKEN_ADJUST_DEFAULT, TOKEN_QUERY = 0x0080, 0x0008
WM_CLOSE, SW_RESTORE = 0x0010, 9


class RECT(ctypes.Structure):
    _fields_ = [("left", wt.LONG), ("top", wt.LONG),
                ("right", wt.LONG), ("bottom", wt.LONG)]


class USEROBJECTFLAGS(ctypes.Structure):
    _fields_ = [("fInherit", wt.BOOL), ("fReserved", wt.BOOL), ("dwFlags", wt.DWORD)]


class LUID(ctypes.Structure):
    _fields_ = [("LowPart", wt.DWORD), ("HighPart", wt.LONG)]


class LUID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Luid", LUID), ("Attributes", wt.DWORD)]


class TOKEN_PRIVILEGES(ctypes.Structure):
    _fields_ = [("PrivilegeCount", wt.DWORD),
                ("Privileges", LUID_AND_ATTRIBUTES * 1)]


class STARTUPINFOW(ctypes.Structure):
    _fields_ = [("cb", wt.DWORD), ("lpReserved", wt.LPWSTR), ("lpDesktop", wt.LPWSTR),
                ("lpTitle", wt.LPWSTR), ("dwX", wt.DWORD), ("dwY", wt.DWORD),
                ("dwXSize", wt.DWORD), ("dwYSize", wt.DWORD),
                ("dwXCountChars", wt.DWORD), ("dwYCountChars", wt.DWORD),
                ("dwFillAttribute", wt.DWORD), ("dwFlags", wt.DWORD),
                ("wShowWindow", wt.WORD), ("cbReserved2", wt.WORD),
                ("lpReserved2", ctypes.POINTER(ctypes.c_byte)),
                ("hStdInput", wt.HANDLE), ("hStdOutput", wt.HANDLE),
                ("hStdError", wt.HANDLE)]


class PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [("hProcess", wt.HANDLE), ("hThread", wt.HANDLE),
                ("dwProcessId", wt.DWORD), ("dwThreadId", wt.DWORD)]


class SECURITY_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("nLength", wt.DWORD), ("lpSecurityDescriptor", wt.LPVOID),
                ("bInheritHandle", wt.BOOL)]


def _text(hwnd) -> str:
    n = u.GetWindowTextLengthW(hwnd)
    b = ctypes.create_unicode_buffer(n + 1)
    u.GetWindowTextW(hwnd, b, n + 1)
    return b.value


def find_windows(substr: str) -> list[dict]:
    """标题含 ``substr`` 的**可见**顶层窗口。这就是「窗口真的在那儿」的判据。"""
    out: list[dict] = []

    def cb(hwnd, _):
        if substr in _text(hwnd) and u.IsWindowVisible(hwnd):
            r = RECT()
            u.GetWindowRect(hwnd, ctypes.byref(r))
            cls = ctypes.create_unicode_buffer(256)
            u.GetClassNameW(hwnd, cls, 256)
            out.append({"hwnd": int(hwnd), "title": _text(hwnd), "class": cls.value,
                        "rect": [r.left, r.top, r.right - r.left, r.bottom - r.top]})
        return True

    u.EnumWindows(WNDENUMPROC(cb), 0)
    return out


def station() -> tuple[str, bool]:
    """当前窗口站的名字，以及它是否可见（可见 = 页面才有机会渲染）。"""
    hws = u.GetProcessWindowStation()
    buf = ctypes.create_unicode_buffer(256)
    need = wt.DWORD()
    u.GetUserObjectInformationW(hws, 2, buf, 512, ctypes.byref(need))
    flags = USEROBJECTFLAGS()
    u.GetUserObjectInformationW(hws, 1, ctypes.byref(flags),
                                ctypes.sizeof(flags), ctypes.byref(need))
    return buf.value, bool(flags.dwFlags & 0x0001)


def _enable(name: str) -> None:
    h = wt.HANDLE()
    if not advapi32.OpenProcessToken(k32.GetCurrentProcess(),
                                     TOKEN_ADJUST_DEFAULT | TOKEN_QUERY, ctypes.byref(h)):
        return
    luid = LUID()
    if not advapi32.LookupPrivilegeValueW(None, name, ctypes.byref(luid)):
        return
    tp = TOKEN_PRIVILEGES()
    tp.PrivilegeCount = 1
    tp.Privileges[0].Luid = luid
    tp.Privileges[0].Attributes = SE_PRIVILEGE_ENABLED
    advapi32.AdjustTokenPrivileges(h, False, ctypes.byref(tp), 0, None, None)


def relaunch_in_active_session(argv: list[str]) -> int | None:
    """把自己重投到活跃控制台会话。返回子进程退出码；不需要/做不到则 None。"""
    name, visible = station()
    if visible:
        return None
    for priv in ("SeTcbPrivilege", "SeAssignPrimaryTokenPrivilege",
                 "SeIncreaseQuotaPrivilege"):
        _enable(priv)
    session = k32.WTSGetActiveConsoleSessionId()
    if session in (0xFFFFFFFF, 0):
        return None
    token = wt.HANDLE()
    if not wtsapi32.WTSQueryUserToken(session, ctypes.byref(token)):
        return None                        # 不是 SYSTEM：只能就地跑
    env = ctypes.c_void_p()
    userenv.CreateEnvironmentBlock(ctypes.byref(env), token, False)

    OUT.mkdir(parents=True, exist_ok=True)
    log = OUT / "in-session.log"
    sa = SECURITY_ATTRIBUTES()
    sa.nLength = ctypes.sizeof(sa)
    sa.bInheritHandle = True
    fh = k32.CreateFileW(str(log), 0x40000000, 3, ctypes.byref(sa), 2, 0x80, None)
    si = STARTUPINFOW()
    si.cb = ctypes.sizeof(si)
    si.lpDesktop = "winsta0\\default"
    si.dwFlags = 0x00000100 | 0x00000001
    si.wShowWindow = 1
    si.hStdOutput = fh
    si.hStdError = fh
    si.hStdInput = k32.GetStdHandle(-10)
    pi = PROCESS_INFORMATION()
    cmd = ctypes.create_unicode_buffer(
        " ".join('"%s"' % a if " " in a else a for a in argv))
    ok = advapi32.CreateProcessAsUserW(
        token, None, cmd, None, None, True, CREATE_UNICODE_ENVIRONMENT,
        env, str(ROOT), ctypes.byref(si), ctypes.byref(pi))
    if not ok:
        return None
    print(f"[qa] 当前窗口站 {name}（不可见）→ 已重投到会话 {session}，pid={pi.dwProcessId}")
    k32.WaitForSingleObject(pi.hProcess, 0xFFFFFFFF)
    code = wt.DWORD()
    k32.GetExitCodeProcess(pi.hProcess, ctypes.byref(code))
    k32.CloseHandle(pi.hProcess)
    k32.CloseHandle(pi.hThread)
    k32.CloseHandle(fh)
    print(f"[qa] 会话内验收结束，退出码 {code.value}；日志 {log}")
    return int(code.value)


# ---------------------------------------------------------------- 被验对象
def _get(port: int, path: str, timeout: float = 4) -> dict:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=timeout) as r:
        return json.loads(r.read().decode())


def have(port: int, pid: str) -> bool:
    """工作区里现在有没有这个程序。

    为什么需要它：验收脚本里写死了样例程序的名字（memo / mood）。那些程序是**用户数据**
    —— 被删掉之后，硬跑只会得到一条看着像「功能坏了」的红线。缺数据该说「跳过」，
    那不是通过，也不该伪装成失败。
    """
    try:
        st = _get(port, "/api/state", 3)
    except Exception:
        return False
    return any(p.get("id") == pid for p in (st.get("programs") or []))


def _post(port: int, path: str, body: dict | None = None, timeout: float = 15) -> dict:
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", method="POST",
        data=json.dumps(body or {}).encode(),
        headers={"content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def _shot(path: Path, hwnd: int | None = None) -> bool:
    """截图。带 hwnd 时：先置前、**确认它真是前台窗口**，再只抓它的矩形。

    为什么这么啰嗦：``ImageGrab`` 抓的是屏幕。窗口被别的程序挡住时，抓到的
    就是别人的画面，而测试照样报 ok —— 这种「撒谎的截图」比没有截图更坏：
    它会让人拿着一张微信截图说「界面验收通过」。
    """
    try:
        from PIL import ImageGrab

        rect = None
        if hwnd:
            u.ShowWindow(hwnd, SW_RESTORE)
            try:
                u.SetForegroundWindow(hwnd)
            except Exception:
                pass
            time.sleep(0.8)
            if int(u.GetForegroundWindow()) != int(hwnd):
                return False                     # 没置前：如实失败，不去拍别人的窗口
            r = RECT()
            u.GetWindowRect(hwnd, ctypes.byref(r))
            rect = (r.left, r.top, r.right, r.bottom)
        img = ImageGrab.grab(bbox=rect) if rect else ImageGrab.grab()
        img.save(path)
        return True
    except Exception:
        return False


def top_window_at(x: int, y: int) -> dict:
    """屏幕某点最上层的可见窗口 —— 用来验证「截到的确实是它」。"""
    hwnd = int(u.WindowFromPoint(ctypes.wintypes.POINT(x, y)))
    root = int(u.GetAncestor(hwnd, 2)) or hwnd          # GA_ROOT
    return {"hwnd": root, "title": _text(root)}


class Host:
    """起一个真宿主，等 HTTP + 等窗口，收尸。"""

    def __init__(self, extra: list[str] | None = None) -> None:
        OUT.mkdir(parents=True, exist_ok=True)
        self.log = OUT / "host.log"
        self.fh = open(self.log, "w", encoding="utf-8", buffering=1)
        argv = [sys.executable, "-X", "utf8", "-m", "agentworkspace.main", "--port", "0",
                "--no-browser"] + list(extra or [])
        self.proc = subprocess.Popen(
            argv,
            cwd=str(ROOT), stdout=self.fh, stderr=subprocess.STDOUT,
            env=dict(os.environ, PYTHONIOENCODING="utf-8"))
        self.port = self._wait_port()

    def _wait_port(self, timeout: float = 60) -> int:
        t0 = time.time()
        while time.time() - t0 < timeout:
            txt = self.log.read_text(encoding="utf-8", errors="replace")
            m = re.search(r"\[门\] http://127\.0\.0\.1:(\d+)/", txt)
            if m:
                port = int(m.group(1))
                try:
                    _get(port, "/api/state", 1.5)
                    return port
                except Exception:
                    pass
            if self.proc.poll() is not None:
                raise RuntimeError("宿主退出了：" + txt[-800:])
            time.sleep(0.3)
        raise RuntimeError("宿主 60s 内没有就绪：" + self.log.read_text(encoding="utf-8")[-800:])

    def wait_window(self, substr: str = "我的工作区", timeout: float = 40) -> list[dict]:
        t0 = time.time()
        while time.time() - t0 < timeout:
            w = find_windows(substr)
            if w:
                return w
            time.sleep(0.5)
        return []

    def close(self) -> int:
        wins = find_windows("我的工作区")
        for w in wins:
            u.PostMessageW(w["hwnd"], WM_CLOSE, 0, 0)
        try:
            self.proc.wait(timeout=25)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        self.fh.close()
        return self.proc.returncode


def qa_desktop() -> dict:
    """壳：页面必须**渲染出来**，窗口必须是真窗口。"""
    name, visible = station()
    res: dict = {"station": name, "visible": visible, "checks": []}

    host = Host()
    res["port"] = host.port
    wins = host.wait_window()
    res["windows"] = wins
    if wins:
        res["shot"] = _shot(OUT / "desktop.png", wins[0]["hwnd"])
    log = host.log.read_text(encoding="utf-8", errors="replace")
    res["shell"] = ("ok" if "桌面窗口已关闭" in log else
                    "blank" if "没渲染" in log else
                    "unavailable" if "不可用" in log else "unknown")
    res["exit"] = host.close()

    # 关窗之后壳应该已经打印结论；再读一次（close 前可能还没写完）
    log = host.log.read_text(encoding="utf-8", errors="replace")
    if "桌面窗口已关闭" in log:
        res["shell"] = "ok"

    w0 = wins[0]["rect"] if wins else None
    res["checks"] = [
        {"name": "窗口站在可见桌面", "ok": visible, "detail": name},
        {"name": "找到了宿主窗口", "ok": bool(wins), "detail": str(w0)},
        {"name": "窗口是真窗口（有尺寸）", "ok": bool(w0) and w0[2] > 400 and w0[3] > 300,
         "detail": str(w0)},
        {"name": "壳的结局是 ok（页面渲染了）", "ok": res["shell"] == "ok",
         "detail": res["shell"]},
    ]
    return res


def qa_native() -> dict:
    """原生程序：必须是**独立的真 OS 窗口**，开→1，关→0。"""
    name, visible = station()
    res: dict = {"station": name, "visible": visible, "checks": []}
    host = Host()
    res["port"] = host.port
    host.wait_window()
    if not have(host.port, "memo"):
        host.close()
        res["checks"] = [{"name": "跳过：工作区里没有 memo（原生窗口样例）——"
                                  "「独立的真 OS 窗口」这件事本轮没有验到", "ok": True,
                          "skip": True}]
        return res
    before = len(find_windows("便签"))
    res["opened"] = _post(host.port, "/api/p/memo/open")
    opened: list[dict] = []
    t0 = time.time()
    while time.time() - t0 < 25:
        opened = find_windows("便签")
        if opened:
            break
        time.sleep(0.5)
    if opened:
        res["shot"] = _shot(OUT / "native.png", opened[0]["hwnd"])
    res["closed"] = _post(host.port, "/api/p/memo/close")
    time.sleep(2.0)
    after = len(find_windows("便签"))
    res["exit"] = host.close()
    res["checks"] = [
        {"name": "打开前没有便签窗口", "ok": before == 0, "detail": str(before)},
        {"name": "打开后正好 1 个真窗口", "ok": len(opened) == 1,
         "detail": str(opened[0]["rect"]) if opened else "0"},
        {"name": "关掉后窗口消失", "ok": after == 0, "detail": str(after)},
    ]
    return res


SEED = {"assistant": "用一句话介绍你自己。",
        "mood": "记一条：今天有点累，3 分"}


def qa_ui(chat: bool = False) -> dict:
    """界面：小助理只有一张嘴；有页面的程序是两栏（左页面 / 右对话）。

    两条都要在**真窗口**里截到 —— 逻辑成立不等于长对了。
    ``chat=True`` 时先真说一句再截：截图里要有真东西，不能只是一张空壳。
    """
    name, visible = station()
    res: dict = {"station": name, "visible": visible, "checks": []}
    for pid, fname in (("assistant", "ui-assistant.png"), ("mood", "ui-mood.png")):
        host = Host(["--open", pid])
        res["port"] = host.port
        wins = host.wait_window()
        if not have(host.port, pid):
            host.close()
            res["checks"].append({"name": f"跳过：工作区里没有 {pid}（没有页面样例可截）",
                                  "ok": True, "skip": True})
            continue
        seed = None
        hist = 0
        if chat and pid in SEED:
            seed = _post(host.port, f"/api/p/{pid}/chat", {"text": SEED[pid]}, timeout=60)
            t0 = time.time()
            while time.time() - t0 < 120:
                st = _get(host.port, "/api/state", 3).get("agents", {}).get(pid) or {}
                hist = len(st.get("history") or [])
                if st.get("status") == "ready" and not st.get("busy"):
                    break
                time.sleep(1.0)
            time.sleep(1.5)                      # 让最后一帧画完
        shot_ok = bool(wins) and _shot(OUT / fname, wins[0]["hwnd"])
        rect = wins[0]["rect"] if wins else None
        front = None
        if wins:
            cx, cy = wins[0]["rect"][0] + wins[0]["rect"][2] // 2, \
                     wins[0]["rect"][1] + wins[0]["rect"][3] // 2
            front = top_window_at(cx, cy)
        res[f"{pid}Exit"] = host.close()
        res["checks"].append({"name": f"{pid} 的界面在真窗口里截到了", "ok": bool(shot_ok),
                              "detail": f"{fname} rect={rect}"})
        res["checks"].append({
            "name": f"{pid}：截图中心点最上层的就是它自己（没被别的程序挡住）",
            "ok": bool(wins) and front is not None and front["hwnd"] == wins[0]["hwnd"],
            "detail": str(front),
        })
        if chat and pid in SEED:
            res["checks"].append({
                "name": f"{pid}：截图前真说过一句话（历史 {hist} 条）",
                "ok": hist >= 2, "detail": f"seed={str(seed)[:80]}",
            })
    return res


def _report(res: dict) -> bool:
    ok = True
    print(f"\n  窗口站 {res.get('station')}  可见={res.get('visible')}")
    for c in res.get("checks", []):
        if c.get("skip"):
            print(f"  --  {c['name']}")
            continue
        mark = "ok  " if c["ok"] else "!!  "
        ok = ok and c["ok"]
        print(f"  {mark}{c['name']}  {c['detail']}")
    return ok


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="agentworkspace.qa", description="真窗口验收")
    ap.add_argument("part", nargs="?", default="all",
                    choices=["all", "desktop", "native", "ui"])
    ap.add_argument("--with-chat", action="store_true",
                    help="截图前先真说一句（慢，但截图里有真东西）")
    ap.add_argument("--no-relaunch", action="store_true",
                    help="就在当前会话跑（Session 0 里会如实得到 blank）")
    a = ap.parse_args(argv)

    if not a.no_relaunch:
        # 原文只带 part，其余开关（--with-chat 之类）会被吞掉：跑的是另一份配置，
        # 却照样打印「全绿」。这里把原始参数原样带过去。
        args = [x for x in sys.argv[1:] if x != "--no-relaunch"]
        code = relaunch_in_active_session(
            [sys.executable, "-X", "utf8", "-m", "agentworkspace.qa", *args, "--no-relaunch"])
        if code is not None:
            return code

    OUT.mkdir(parents=True, exist_ok=True)
    passed = True
    results: dict = {}
    if a.part in ("all", "desktop"):
        print("[qa] 桌面外壳")
        results["desktop"] = qa_desktop()
        passed = _report(results["desktop"]) and passed
    if a.part in ("all", "ui"):
        print("[qa] 界面形态（小助理 / 两栏程序）")
        results["ui"] = qa_ui(chat=a.with_chat)
        passed = _report(results["ui"]) and passed
    if a.part in ("all", "native"):
        print("[qa] 原生程序窗口")
        results["native"] = qa_native()
        passed = _report(results["native"]) and passed

    (OUT / "report.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n{'全绿' if passed else '有失败'}；报告 {OUT / 'report.json'}")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
