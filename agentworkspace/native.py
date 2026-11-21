"""原生程序宿主 —— 把 ``ui/app.py`` 拉起成它自己的窗口。

为什么必须是独立进程：tkinter / PyQt 的窗口塞不进 webview，两套事件循环也不能共处
一个线程。所以宿主只做三件事：**拉起它、记账（进程/日志/退出）、退出时收尸。**

**它不改执行路径。** 原生程序拿到的只是同一个本地 HTTP 地址，它的每次点击照样是
``POST /api/p/<pid>/call/<fn>`` → 同一个 ``Runtime.dispatch(..., source="human")``。
于是「人的门」有三种壳 —— 浏览器 / pywebview 窗口 / 原生窗口 —— 里面是同一条路。

判据（与 kernel 同一条）：本文件里没有一行在问「接下来做什么」。
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

KEEP_LOG = 4000          # 启动失败时，只把日志尾巴交回去
BOOT_GRACE = 2.0         # 窗口活过这么久才算「起来了」
ROOT = Path(__file__).resolve().parent.parent

# 为什么不直接 ``python ui/app.py``：Python 会把脚本所在目录插到 sys.path[0]，
# 于是**一个叫 app.py 的入口会把自己变成 ``app`` 模块**，``from agentworkspace.sdk import``
# 立刻炸成「'app' is not a package」—— 而这句话读起来完全不像人话。
# 所以统一走这个 bootstrap：项目根排最前，脚本目录排最后。
_BOOT = r"""
import pathlib, runpy, sys
entry = sys.argv[1]
sys.path.append(str(pathlib.Path(entry).resolve().parent))
runpy.run_path(entry, run_name="__main__")
"""


class NativeHost:
    """线程安全的进程账本。一个插件同时只有一个原生窗口。"""

    def __init__(self, runtime, base: str) -> None:
        self.rt = runtime
        self.base = base
        self.root = Path(runtime.root)
        self.log_dir = self.root / "logs"
        self.procs: dict[str, dict] = {}
        self.python = _pythonw()

    # -- 查 -------------------------------------------------------------
    def info(self) -> dict:
        """{pid: {"running": bool, "code": int|None, "log": 相对路径}} —— 给界面看。"""
        out: dict[str, dict] = {}
        for pid, rec in list(self.procs.items()):
            proc: subprocess.Popen = rec["proc"]
            code = proc.poll()
            if code is not None and rec.get("running"):
                rec["running"] = False
                rec["code"] = code
            out[pid] = {"running": bool(rec.get("running")), "code": code,
                        "log": str(rec["log"].relative_to(self.root)) if rec.get("log") else ""}
        return out

    def running(self, pid: str) -> bool:
        rec = self.procs.get(pid)
        return bool(rec and rec.get("running"))

    # -- 开 -------------------------------------------------------------
    def open(self, pid: str) -> dict:
        """拉起一个原生窗口。已在跑就只报告，不重复开。"""
        plugin = self.rt.plugins.get(pid)
        if plugin is None:
            return {"status": "error", "message": f"没有插件 {pid}"}
        if plugin.ui_kind != "native":
            return {"status": "error", "message": f"{pid} 不是原生程序（它的界面是网页）"}
        if self.running(pid):
            return {"status": "ok", "message": "已经在自己的窗口里开着", "running": True}

        entry = plugin.path / str(plugin.manifest.get("ui"))
        if not entry.is_file():
            return {"status": "error", "message": f"没有入口 {entry.name}"}

        self.log_dir.mkdir(parents=True, exist_ok=True)
        log = self.log_dir / f"native-{pid}.log"
        env = os.environ.copy()
        env.update({
            "AW_BASE": self.base, "AW_PID": plugin.pid,
            "AW_PLUGIN_DIR": str(plugin.path),
            "PYTHONPATH": str(ROOT)
                          + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""),
            "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1",
        })
        try:
            # 子进程拿到的是自己那一份句柄，父进程这份 Popen 一返回就能关：
            # 否则每次开程序都漏一个句柄（Windows 上还一直攥着日志文件）
            with log.open("w", encoding="utf-8") as fh:
                proc = subprocess.Popen(
                    [self.python, "-c", _BOOT, str(entry)], cwd=str(plugin.path), env=env,
                    stdout=fh, stderr=subprocess.STDOUT,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except OSError as exc:
            return {"status": "error", "message": f"拉不起来：{exc}"}

        self.procs[pid] = {"proc": proc, "log": log, "running": True, "code": None,
                           "started": time.time()}
        self.rt.publish("native.opened", pid=pid, data={"log": log.name}, source="human")

        # 立刻死掉的，要如实说为什么 —— 否则用户只看到「点了没反应」
        time.sleep(0.35)
        code = proc.poll()
        if code not in (None, 0):
            tail = _tail(log)
            self.procs[pid].update(running=False, code=code)
            self.rt.publish("native.failed", pid=pid, data={"code": code, "log": tail},
                            source="human")
            return {"status": "error", "message": f"窗口起不来（退出码 {code}）", "log": tail}
        return {"status": "ok", "message": "已在自己的窗口里打开", "running": True,
                "log": log.name}

    # -- 关 -------------------------------------------------------------
    def close(self, pid: str) -> dict:
        rec = self.procs.get(pid)
        if not rec or not rec.get("running"):
            return {"status": "ok", "message": "本来就没开着"}
        try:
            rec["proc"].terminate()
        except OSError:
            pass
        rec["running"] = False
        self.rt.publish("native.closed", pid=pid, source="human")
        return {"status": "ok", "message": "已关闭"}

    def shutdown(self) -> None:
        """宿主退出时收尸：不留孤儿窗口。"""
        for pid in list(self.procs):
            self.close(pid)


def _pythonw() -> str:
    """优先用没有黑框的解释器（Windows 上 python.exe 会先弹一个控制台）。"""
    exe = Path(sys.executable)
    quiet = exe.with_name("pythonw.exe")
    return str(quiet if quiet.is_file() else exe)


def _tail(log: Path) -> str:
    try:
        return log.read_text(encoding="utf-8", errors="replace")[-KEEP_LOG:].strip()
    except OSError:
        return ""
