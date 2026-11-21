"""原生程序的最小客户端 —— 让 tkinter / PyQt 写的东西也能是一个插件。

谁用它：插件里的 ``ui/app.py``（原生窗口），由宿主 ``agentworkspace/native.py`` 拉起。
它只做一件事：把你的程序接回**同一个门** —— ``POST /api/p/<pid>/call/<fn>``。

为什么是同一个门：原生程序不是第二套执行路径，它只是「人的门」的第三种壳
（浏览器 / pywebview 窗口 / 原生窗口）。里面走的是同一条 ``Runtime.dispatch``，
所以你在原生窗口里点的每一次，事件里都带着 ``source="human"``，
和网页、和 agent 的动作在审计上完全一样。

用法::

    from agentworkspace.sdk import Native

    ui = Native()                      # 从宿主注入的环境变量里读 base / pid
    print(ui.call("list")["text"])     # 调自己的一个能力
    ui.on_event(lambda ev: ...)        # 订阅：agent 的动作你也能看见

环境变量（宿主注入，你不用自己设）：
    AW_BASE         http://127.0.0.1:<port>
    AW_PID          这个插件的 id
    AW_PLUGIN_DIR   插件目录的绝对路径
"""

from __future__ import annotations

import json
import os
import threading
import urllib.error
import urllib.request
from pathlib import Path


class Native:
    """一个原生窗口与它的插件之间的那根线。零智能：只转发，不解释。"""

    def __init__(self, base: str | None = None, pid: str | None = None,
                 watch_host: bool = True) -> None:
        self.base = (base or os.environ.get("AW_BASE") or "http://127.0.0.1:8777").rstrip("/")
        self.pid = pid or os.environ.get("AW_PID") or ""
        self.dir = Path(os.environ.get("AW_PLUGIN_DIR") or ".")
        if watch_host:
            self._watch_host()

    # -- 宿主死了，我也该走 ------------------------------------------------
    def _watch_host(self) -> None:
        """后台盯着宿主：两次连不上就自己退出。

        为什么需要这条：宿主被强杀（关控制台、任务管理器结束进程）时，
        子进程不会收到任何通知，窗口会变成**孤儿**飘在屏幕上 —— 用户点它，
        每一次操作都失败。宁可自己退，也不要留一个点不动的窗口。
        """

        def loop() -> None:
            misses = 0
            while True:
                threading.Event().wait(3.0)
                try:
                    with urllib.request.urlopen(self.base + "/api/state", timeout=2.5) as r:
                        r.read(1)
                    misses = 0
                except Exception:
                    misses += 1
                    if misses >= 2:
                        os._exit(0)        # 宿主不在了：安静退场
        threading.Thread(target=loop, name="aw-host-watch", daemon=True).start()

    # -- 调自己的能力：落到同一个 dispatch --------------------------------
    def call(self, fn: str, **args) -> dict:
        """执行自己插件的一个能力，返回 ``{"status","text","data"}``。

        fn: 插件 backend.py 里的公开函数名（不带 pid 前缀）
        """
        body = json.dumps(args or {}, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base}/api/p/{self.pid}/call/{fn}", data=body, method="POST",
            headers={"content-type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:             # 门只会 400/404/403
            try:
                return json.loads(exc.read().decode("utf-8"))
            except ValueError:
                return {"status": "error", "text": f"HTTP {exc.code}", "data": {}}
        except OSError as exc:                            # 宿主已经关了
            return {"status": "error", "text": f"宿主不在：{exc}", "data": {}}

    def text(self, fn: str, **args) -> str:
        """只要那句给人看的话（``call`` 的 text 字段）。"""
        return self.call(fn, **args).get("text", "") or ""

    # -- 看全局：人、agent、别的插件都发生了什么 --------------------------
    def state(self) -> dict:
        """整份工作区状态：有哪些程序、各自的能力、道连上没有。"""
        with urllib.request.urlopen(self.base + "/api/state", timeout=10) as r:
            return json.loads(r.read().decode("utf-8"))

    def on_event(self, cb, kinds: tuple[str, ...] | None = None) -> threading.Thread:
        """订阅事件流。agent 或别的窗口做了什么，你的程序也能立刻反应。

        cb:    收到一条事件时调用，参数是 dict（含 type / pid / fn / source / data）
        kinds: 只关心这些类型（如 ``("call.ok",)``）；None = 全都要
        返回后台线程（daemon，不用自己 join）
        """
        def loop() -> None:
            try:
                with urllib.request.urlopen(self.base + "/api/events", timeout=None) as r:
                    for raw in r:
                        if not raw.startswith(b"data: "):
                            continue
                        try:
                            ev = json.loads(raw[6:].decode("utf-8"))
                        except ValueError:
                            continue
                        if kinds and ev.get("type") not in kinds:
                            continue
                        try:
                            cb(ev)
                        except Exception:                 # 你的回调炸了别把订阅带走
                            pass
            except Exception:                             # 连不上/宿主关门：静默退出
                pass

        t = threading.Thread(target=loop, name=f"aw-events-{self.pid}", daemon=True)
        t.start()
        return t

    # -- 我自己的目录 ----------------------------------------------------
    def data_dir(self) -> Path:
        """这个程序的 ``data/``（用户攒下的东西都在这儿）。"""
        return self.dir / "data"
