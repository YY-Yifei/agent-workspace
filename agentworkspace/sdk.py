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
import urllib.parse
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

    # -- 一条 POST 通路：能力、状态、喊一声都走它 --------------------------
    def _post(self, path: str, body: dict | None = None, timeout: float = 30) -> dict:
        data = json.dumps(body or {}, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            self.base + path, data=data, method="POST",
            headers={"content-type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:             # 门只会 400/404/403
            try:
                return json.loads(exc.read().decode("utf-8"))
            except ValueError:
                return {"status": "error", "text": f"HTTP {exc.code}", "data": {}}
        except (OSError, ValueError) as exc:              # 宿主已经关了
            return {"status": "error", "text": f"宿主不在：{exc}", "data": {}}

    # -- 调能力（自己或别人）：落到同一个 dispatch -------------------------
    def call(self, pid: str, fn: str | None = None, args: dict | None = None,
             **kw) -> dict:
        """执行一个能力，返回 ``{"status","text","data"}``。

        ``ui.call("list")`` 调自己；``ui.call("dingtalk", "list", {...})`` 调别人。
        **不是一个新入口**：照样过 schema 校验、照样进调用栈 —— 所以环形调用
        在 ``dispatch`` 里一处就被挡（与后端 ``ctx.call`` 是同一条路）。
        """
        if fn is None:
            pid, fn = self.pid, pid
        body = dict(args or {})
        body.update(kw)
        return self._post(f"/api/p/{pid}/call/{fn}", body)

    # -- 状态：与后端 ctx.get/set 是同一份 store.json ----------------------
    def get(self, key: str, default=None):
        """读自己 store 里的一个键。读不到（含宿主不在）就给你 ``default``。"""
        k = urllib.parse.quote(str(key), safe="")
        try:
            with urllib.request.urlopen(
                    f"{self.base}/api/p/{self.pid}/store?key={k}", timeout=15) as r:
                j = json.loads(r.read().decode("utf-8"))
        except (OSError, ValueError):
            return default
        return j.get("value", default)

    def set(self, key: str, value) -> bool:
        """写自己 store 里的一个键。**落盘了才回 True** —— 别把失败当成功。"""
        out = self._post(f"/api/p/{self.pid}/store", {"key": str(key), "value": value})
        return bool(out.get("ok"))

    def emit(self, kind: str, data: dict | None = None) -> bool:
        """往事件流里喊一声（人看得见，别的订阅者也能收到）。"""
        out = self._post(f"/api/p/{self.pid}/emit",
                         {"kind": str(kind), "data": data or {}})
        return bool(out.get("ok"))

    # -- 外观：**宿主解析，插件只消费** -----------------------------------
    def theme(self) -> str:
        """现在该画哪一档（``dark`` / ``light``）—— 解析后的值，直接能用。

        别自己去看 ``prefers-color-scheme``：那是第二份判据，必然与宿主漂移
        （宿主被设成浅色时，你的窗口仍是深的 —— 一个身体两种颜色）。
        """
        return self._theme_now()["resolved"]

    def theme_pref(self) -> str:
        """人点的那一档（``auto`` / ``dark`` / ``light``）。别拿它去画。"""
        return self._theme_now()["pref"]

    def _theme_now(self) -> dict:
        try:
            with urllib.request.urlopen(self.base + "/api/theme", timeout=10) as r:
                j = json.loads(r.read().decode("utf-8"))
        except (OSError, ValueError):                     # 宿主不在：给个能画的
            return {"pref": "auto", "resolved": "dark"}
        return {"pref": j.get("pref") or "auto",
                "resolved": j.get("resolved") or "dark"}

    def on_theme(self, cb):
        """外观变了就叫一声。返回**停止函数**（与后端 ctx.on_theme 同一件事）。"""
        return self.on_event(cb, kinds=("ui.theme",)).stop

    def text(self, fn: str, **args) -> str:
        """只要那句给人看的话（``call`` 的 text 字段）。"""
        return self.call(fn, **args).get("text", "") or ""

    # -- 看全局：人、agent、别的插件都发生了什么 --------------------------
    def state(self) -> dict:
        """整份工作台状态：有哪些程序、各自的能力、道连上没有。"""
        with urllib.request.urlopen(self.base + "/api/state", timeout=10) as r:
            return json.loads(r.read().decode("utf-8"))

    def on_event(self, cb, kinds: tuple[str, ...] | None = None) -> threading.Thread:
        """订阅事件流。agent 或别的窗口做了什么，你的程序也能立刻反应。

        cb:    收到一条事件时调用，参数是 dict（含 type / pid / fn / source / data）
        kinds: 只关心这些类型（如 ``("call.ok",)``）；None = 全都要
        返回后台线程（daemon，不用自己 join）；``线程.stop()`` 取消订阅。

        **停止只是停止回调**：那条 HTTP 连接已经阻塞在读上，要等宿主发下一条
        事件或退回时才会真的断开。所以取消之后别再碰 cb 之外的东西就行。
        """
        stop = threading.Event()

        def loop() -> None:
            try:
                with urllib.request.urlopen(self.base + "/api/events", timeout=None) as r:
                    for raw in r:
                        if stop.is_set():
                            return
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
        t.stop = stop.set                          # type: ignore[attr-defined]
        t.start()
        return t

    # -- 我自己的目录 ----------------------------------------------------
    def data_dir(self) -> Path:
        """这个程序的 ``data/``（用户攒下的东西都在这儿）。"""
        return self.dir / "data"
