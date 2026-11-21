"""道这一侧的接线 —— **每个程序一条**，零智能。

本文件的全部职责是四件事，一件不多：

    1. 连上（connect），带上**这个程序**的 instructions
    2. 把**这个程序**当前的能力面声明出去（declareAgent）
    3. 把人的话送进去（sendInput），把事件流搬回来（event → 事件总线）
    4. 把模型点名的那个能力交给内核执行（invokeFunction → ``Runtime.dispatch``）

为什么一个程序一条而不是全局一条：**上下文就是「它知道自己能做什么」**。
全局一条，pomodoro 的上下文里就会长着「怎么造插件」—— 那是小助理的活。
一条声明里给什么能力，那个程序就会长出什么样的自我；这件事在内核的
``Runtime.capabilities_of`` 里判一次，这里只是把它送出去。

这里**没有**编排、没有重试策略、没有「如果模型调了 A 就接着做 B」、没有本地循环。
所有决定下一步的事情都在道那一侧。本文件写错方向的最典型症状，就是它自己开始
「想」—— 那意味着内核篡了道的位置。
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from pathlib import Path

import aaas
from aaas import FunctionDeclaration, FunctionResult, Part, Tool

from .settings import DEFAULT_URL as _DEFAULT_URL
from .kernel import Runtime

ASSISTANT = "assistant"

# 提示词文件缺席时的兜底：短，且**不撒谎**（不承诺它其实没有的能力）。
FALLBACK_ASSISTANT = "你是小助理。用户让你做什么，你就用你手上的能力做完它。"
# 占位符必须写成 {{...}}：替换那一侧（_plugin_prompt）只认双花括号。
# 写成单花括号时，一旦 prompts/ 下的文件缺失、走到这两个回退模板，
# 模型收到的就是字面的「{title}」「{tools}」。
FALLBACK_DEV = """你是「{{title}}」这个程序里的助手，**开发者模式开着**。

你除了自己那几项能力，还能用下面这些通用的手（它们属于小助理）：

{{tools}}

开发者模式是人亲手给你打开的：你现在可以读写工作区里的文件、跑命令、
也可以改你自己（{{dir}}）。改自己只走 plugin_write("{{pid}}", {...})，
先 plugin_verify 看一下，提交的是整份文件、不是补丁。别碰 data/。
"""

FALLBACK_PLUGIN = """你是「{{title}}」这个程序里的助手。

你只有下面这些能力，没有别的 —— 你不能造程序、不能改代码、不能看别的文件：

{{tools}}

说话要短、要具体。用户想要的东西不在这份清单里，就直说做不了。
"""


# 协议上限：instructions >= 4096 字符时服务端在 2ms 内判 PARAMS_INVALID
# （实测 4000 可以、4096 不行）。留点余量，别贴着红线走。
MAX_INSTRUCTIONS = 4000


def clip_instructions(text: str) -> tuple[str, bool]:
    """把提示词截到协议允许的长度。返回（文本，是否截过）。

    这件事必须有人做，否则症状是「发出去就没回音」：整轮在 2ms 内失败，
    而用户看不见任何解释。截断也不是静默的 —— 会留下标记，并且发一条事件。
    """
    if len(text) <= MAX_INSTRUCTIONS:
        return text, False
    mark = f"\n\n[提示词超长，已截到 {MAX_INSTRUCTIONS} 字符：精简 workspace/prompts/]"
    return text[: MAX_INSTRUCTIONS - len(mark)] + mark, True


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


class Link:
    """一条通往道的连接 —— 属于**某一个程序**。生命周期：易失。"""

    def __init__(self, runtime: Runtime, root: Path, cfg: dict, pid: str = ASSISTANT) -> None:
        self.rt = runtime
        self.root = Path(root)
        self.cfg = cfg or {}
        self.pid = pid
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._caller: aaas.Caller | None = None
        self._turn: aaas.Turn | None = None
        # 「已受理」闸门：在 send() 里**同步**置位，不等协程跑到 _turn 赋值。
        # 否则第二句会在那个窗口里穿过去，两轮并发 —— 不报错，只是行为错。
        self._busy = False
        self._send_lock = threading.Lock()
        self._turn_task: asyncio.Task | None = None
        self._declared_sig: tuple | None = None
        self._declare_fails = 0        # 同一份坏声明连续失败几次（用来停手）
        self._declare_sig_tried: tuple | None = None   # 上面的计数属于哪一份声明
        self.status = "idle"            # idle | connecting | ready | error | closed
        self.error: str | None = None
        self.session_id: str | None = None
        self.auto_continue = True       # 本轮内自己的能力面变了 → 自动接力一轮
        self.MAX_CONTINUE = 3           # 防死循环：最多连续接力几轮
        self._continues = 0
        self._turn_caps: set[str] = set()
        self.history: list[dict] = []   # stateless 模式下服务端不留，我们只留给人看
        self._clip_warned = False       # 提示词被截断，只提醒一次（别每轮刷屏）

    # ------------------------------------------------------------------ 生命周期
    def start(self) -> str:
        """在后台线程里跑一条 asyncio 循环。不阻塞调用方（界面要立刻能开）。"""
        if self._thread and self._thread.is_alive():
            return "already started"
        self._thread = threading.Thread(target=self._run_loop, name=f"aaas-{self.pid}", daemon=True)
        self._thread.start()
        return "starting"

    def _run_loop(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._main())
        finally:
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
            finally:
                loop.close()

    async def _main(self) -> None:
        url = self.cfg.get("url") or _DEFAULT_URL
        token = self.cfg.get("token")
        self.status = "connecting"
        self._emit("link.status", {"status": "connecting", "url": url})
        try:
            self._caller = aaas.Caller(url, token=token)
            await self._caller.connect()
        except Exception as exc:      # 连不上就是连不上：如实报，不做假聊天降级
            self.status = "error"
            self.error = f"{type(exc).__name__}: {exc}"
            self._emit("link.status", {"status": "error", "error": self.error,
                                       "hint": "检查 workspace/config.json 里的 aaas.url，"
                                               "或先启动 aaas-service"})
            return

        cr = self._caller.connect_result
        self.session_id = getattr(cr, "session_id", None)
        # **先声明，再标 ready。** 反过来的话，第一句话可能在 declare 还在路上时就被
        # 调度出去 —— 那一轮里模型看不到这个程序的任何能力（工具还没到）。
        self._mark_ready(await self._declare())
        # 连接活着的期间：**这个程序**的能力有变化就重新声明。
        # 判据是能力面的差异，不是 rt.version —— 版本号是全工作区的，
        # 别的插件变了也跟着跳，那会让每个程序无谓地重声明。
        while True:
            await asyncio.sleep(0.5)
            if self.status == "closed":
                return
            if self._sig() != self._declared_sig:
                self._mark_ready(await self._declare())

    def _emit(self, kind: str, data: dict, source: str = "system") -> None:
        self.rt.publish(kind, pid=self.pid, source=source, data=data)

    def _mark_ready(self, ok: bool) -> None:
        """声明成功才算 ready；没成功就留在 connecting —— send() 会如实说「还没连上」，
        而不是把人送进一轮「模型看不到任何能力」的对话。"""
        if not ok or self.status in ("ready", "closed"):
            # closed 是终态：stop() 可能在 declare 的往返途中把链路关掉，
            # 那时再把 status 改回 ready，等于让一个已经关掉的 Link 声称「已连上」
            return
        self.status = "ready"
        self._emit("link.status", {"status": "ready", "sessionId": self.session_id,
                                   "historyMode": getattr(self._caller.connect_result,
                                                          "history_mode", None)})

    # ------------------------------------------------------------------ 声明
    def instructions(self) -> str:
        """这个程序这一次带着什么自我认知。协议规定它不进历史、每轮临时注入 ——
        于是「改提示词」= 改它的自我认知，且不污染历史。"""
        raw = (_read(self.root / "prompts" / "assistant.md") or FALLBACK_ASSISTANT
               if self.pid == ASSISTANT else self._plugin_prompt())
        text, clipped = clip_instructions(raw)
        if clipped and not self._clip_warned:
            self._clip_warned = True
            self._emit("prompt.clipped", {"chars": len(raw), "limit": MAX_INSTRUCTIONS})
        return text

    def _plugin_prompt(self) -> str:
        prog = self.rt.program(self.pid)
        meta = getattr(prog, "manifest", None) or getattr(prog, "meta", None) or {}
        title = (meta.get("title") or self.pid) if meta else self.pid
        dev = self.rt.dev_mode(self.pid)
        caps = self.rt.capabilities_of(self.pid)
        tools = "\n".join(f"- `{c.wire}`：{c.description}" for c in caps.values()) \
            or "（这个程序还没有任何能力）"
        # 开发者模式换的是**自我认知**，不只是换一堆工具：能改自己的人，
        # 必须先知道自己能改、能改到哪儿、改坏了会怎样。
        tpl = (_read(self.root / "prompts" / ("plugin-dev.md" if dev else "plugin.md"))
               or (FALLBACK_DEV if dev else FALLBACK_PLUGIN))
        text = (tpl.replace("{{title}}", title)
                   .replace("{{pid}}", self.pid)
                   .replace("{{description}}", meta.get("description", "") if meta else "")
                   .replace("{{dir}}", f"workspace/plugins/{self.pid}")
                   .replace("{{tools}}", tools))
        # 程序自带的补充说明（造它的时候一起写的），只属于它自己
        own = _read(self.root / "plugins" / self.pid / "agent.md")
        return f"{text}\n\n{own}" if own else text

    def _sig(self) -> tuple:
        """这个程序的能力面指纹。变了才需要重声明。

        必须包含**声明相关的一切**：只看 wire 名字的话，改了 description、
        参数 schema 或 timeout（改函数签名就会变）时指纹不动 —— 模型会一直看到
        旧声明，而界面显示「已是最新」。
        """
        rows = []
        for wire, cap in sorted(self.rt.capabilities_of(self.pid).items()):
            rows.append((wire, cap.description or "",
                         json.dumps(cap.parameters, sort_keys=True,
                                    ensure_ascii=False, default=str),
                         getattr(cap, "timeout_ms", None)))
        return tuple(rows)

    def _tools(self) -> list[Tool]:
        tools: list[Tool] = []
        for cap in self.rt.capabilities_of(self.pid).values():
            tools.append(Tool(
                declaration=FunctionDeclaration(
                    name=cap.wire,
                    description=cap.description,
                    parameters=cap.parameters,
                    timeout_ms=cap.timeout_ms,
                    metadata={"plugin": cap.pid, "fn": cap.fn},
                ),
                fn=self._handler(cap.pid, cap.fn),
            ))
        return tools

    def _handler(self, pid: str, fn: str):
        """一个能力的执行体：**只做一次转发**，落进内核的唯一入口。"""
        def run(**kwargs):
            # caller=self.pid：这道手可能属于别人（开发者模式借来的），
            # 但动手的是这个程序 —— 内核要能分清这两件事。
            res = self.rt.dispatch(pid, fn, kwargs, source="agent", caller=self.pid)
            return FunctionResult(status=res.status,
                                  parts=(Part.t(res.text or ""),),
                                  reason=res.reason)
        return run

    async def _declare(self) -> bool:
        """声明一次能力面。返回是否成功（成功才配叫 ready）。"""
        caller = self._caller
        if caller is None:
            return False
        sig = self._sig()
        tools = self._tools()
        definition = aaas.AgentDefinition(
            revision=f"{self.cfg.get('revision', 'workspace-v1')}-{self.pid}",
            instructions=self.instructions(),
            functions=tuple(t.declaration for t in tools),
        )
        try:
            rev, names = await caller.declare(definition, tools=tools)
        except Exception as exc:
            self.error = f"declare 失败：{type(exc).__name__}: {exc}"
            self._emit("link.status", {"status": "error", "error": self.error,
                                       "scope": "declare"})
            # **不**把 sig 记成「已声明」：网络抖动是暂时的，得有机会重试；
            # 原来一失败就记上，这个程序就永远没有自己的函数了（而 status 还是 ready）。
            # 但同一份坏声明白重试 3 次就停手，等能力面再变。
            # 计数必须按「声明内容」分开算：否则同一份坏声明攒到 3 次之后，
            # 能力面变了、新声明第一次失败就直接 >= 3 被记成「已声明」——
            # 新能力从此再也声明不上去（而且 status 可能永远停在 connecting）。
            if sig != self._declare_sig_tried:
                self._declare_sig_tried = sig
                self._declare_fails = 0
            self._declare_fails += 1
            if self._declare_fails >= 3:
                self._declared_sig = sig
            return False
        self._declare_fails = 0
        self._declare_sig_tried = sig
        self._declared_sig = sig
        self.error = None
        self._emit("link.declared", {"revision": rev, "functions": list(names),
                                     "version": self.rt.version})
        return True

    def redeclare(self) -> None:
        """能力变了：下一轮生效。（协议本来就这样：declare 只影响后续轮次。）"""
        self.rt.publish("link.wakeup", pid=self.pid, source="system",
                        data={"version": self.rt.version})

    # ------------------------------------------------------------------ 一轮
    def send(self, text: str, images: list[str] | None = None, wait: float = 20.0) -> str:
        """把人的一句话送进去。立刻返回，事件经总线流回前端。

        **首句不能丢。** 一个程序第一次被打开时连接才建起来（connecting），
        而这恰好是用户最可能立刻开口的时刻。原来的实现直接回「还没连上」——
        那句话就消失了：不报错、只是没人理，最坏的一种失败形态。
        所以这里等一小会儿（默认 20 秒）；等不到才算失败，且如实说。
        """
        if self.status in ("idle", "connecting") and wait > 0:
            deadline = time.time() + wait
            while time.time() < deadline and self.status in ("idle", "connecting"):
                time.sleep(0.05)
        if self.status != "ready" or self._loop is None:
            return self.error or "还没连上 aaas-service"
        with self._send_lock:                      # 「检查 + 置位」必须是一步
            # aaas.Turn.done 是 **property**（返回 final is not None），不是方法。
            # 写成 .done() 会 TypeError: 'bool' object is not callable。
            if self._busy or (self._turn is not None and not self._turn.done):
                return "上一轮还没结束"
            self._busy = True
            self._continues = 0
            self._turn_caps = set(self.rt.capabilities_of(self.pid))
            msg = text
            try:
                self._turn_task = asyncio.run_coroutine_threadsafe(  # type: ignore[assignment]
                    self._open_turn(msg), self._loop)
                # 后台任务炸了也得有人知道：没有这个回调，异常只留在 Future 里，
                # 界面永远停在「运行中」，也没有任何 turn.failed。
                self._turn_task.add_done_callback(self._turn_done)
            except Exception as exc:               # 调度失败不能把闸门留在关着的位置
                self._busy = False
                self.error = f"{type(exc).__name__}: {exc}"
                return self.error
        return "sent"

    def _turn_done(self, fut) -> None:
        """run_coroutine_threadsafe 回来的 Future：异常要落地成一条 turn.failed。"""
        try:
            exc = fut.exception()
        except Exception as inner:            # CancelledError 之类的
            exc = inner
        if exc is None:
            return
        # **不能在这里拿 _send_lock**：add_done_callback 遇到已经完成的 future
        # 会在注册线程里同步执行回调，而注册那段正在 with self._send_lock 里 ——
        # 非重入锁 → 那个线程（HTTP handler）永久阻塞。bool 赋值本身就够用了。
        self._busy = False
        self.error = f"{type(exc).__name__}: {exc}"
        self._emit("turn.failed", {"error": self.error})

    def _new_caps(self) -> list[str]:
        """本轮内**这个程序**新出现的能力。

        判据是能力面本身的差异，不是版本号：后台声明循环可能已经把版本追平了，
        用版本号比会永远为假 —— 那正是这里曾经错过的东西。
        """
        return [w for w in self.rt.capabilities_of(self.pid) if w not in self._turn_caps]

    async def _open_turn(self, text: str) -> None:
        """跑一轮；若本轮内能力面变过，就**接力**跑下一轮。

        典型场景：小助理改写了某个程序的 backend.py —— 那个程序自己的能力变了。
        对小助理自己，手里那套「手」不会因此变，所以它不会白跑接力轮。
        """
        try:
            final = await self._one_turn(text)
            while (self.auto_continue
                   and getattr(final, "status", None) == "completed"
                   and self._new_caps()
                   and self._continues < self.MAX_CONTINUE):
                self._continues += 1
                added = self._new_caps()
                await self._declare()
                self._turn_caps = set(self.rt.capabilities_of(self.pid))
                handoff = ("[系统] 能力面已更新并在本轮生效。新增能力："
                           + ("、".join(added) or "无")
                           + "。继续把任务做完：新能力现在就可以调用。如果已经完成，"
                           "用一句话告诉用户你交付了什么、他可以怎么用。")
                self._emit("turn.handoff", {"added": added, "round": self._continues})
                final = await self._one_turn(handoff)
        finally:
            # 闸门必须开回去：否则聊天会永远回「上一轮还没结束」，
            # 而那看起来像「功能正常，只是不理人」—— 最坏的一种故障形态。
            with self._send_lock:
                self._busy = False

    async def _one_turn(self, text: str):
        try:
            turn = await self._caller.send(text, instructions=self.instructions())
        except Exception as exc:
            self._emit("turn.failed", {"error": f"{type(exc).__name__}: {exc}"})
            return None
        self._turn = turn
        self.history.append({"role": "user", "text": text, "ts": time.time()})
        self._emit("turn.started", {"turnId": turn.turn_id})
        parts: list[str] = []
        try:
            async for ev in turn:
                kind = ev.kind
                if kind == "message.delta":
                    parts.append(ev.text or "")
                    self._emit("message.delta", {"text": ev.text}, source="agent")
                elif kind == "reasoning.delta":
                    self._emit("reasoning.delta", {"text": ev.text}, source="agent")
                elif kind == "functionCall.started":
                    self._emit("call.started", {"name": ev.name, "callId": ev.call_id},
                               source="agent")
                elif kind == "functionCall.ready":
                    self._emit("call.ready", {"name": ev.name, "args": ev.arguments,
                                              "callId": ev.call_id}, source="agent")
                elif kind == "functionResult":
                    self._emit("call.result", {"status": ev.status, "callId": ev.call_id,
                                               "text": "".join(p.text or "" for p in (ev.parts or ()))},
                               source="agent")
                elif kind == "message.completed":
                    pass
        except Exception as exc:
            self._emit("turn.failed", {"error": f"{type(exc).__name__}: {exc}"})
        final = await turn.end()
        text_out = "".join(parts) or turn.text
        self.history.append({"role": "assistant", "text": text_out, "ts": time.time()})
        self._emit("turn.completed", {
            "status": final.status, "finishReason": final.finish_reason,
            "usage": (final.usage.to_wire() if getattr(final, "usage", None) else None),
            "text": text_out,
            "error": (final.error.to_wire() if getattr(final, "error", None) else None),
        }, source="agent")
        return final

    def cancel(self) -> str:
        if self._loop is None or self._caller is None or self._turn is None:
            return "没有可取消的轮次"
        asyncio.run_coroutine_threadsafe(
            self._caller.cancel(self._turn.turn_id, reason="用户取消"), self._loop)
        return "已请求取消"

    # ------------------------------------------------------------------ 收尾
    def stop(self) -> None:
        self.status = "closed"
        if self._loop and self._caller:
            try:
                asyncio.run_coroutine_threadsafe(self._caller.close(), self._loop).result(timeout=3)
            except Exception:
                pass

    def as_dict(self) -> dict:
        return {"pid": self.pid, "status": self.status, "error": self.error,
                "sessionId": self.session_id, "busy": self._busy,
                "history": self.history[-60:]}


class Hub:
    """程序的智能体池：一个 pid 一条 Link，**用到才连**。

    为什么懒：工作区里可以有几十个程序，而一次对话只属于其中一个。
    启动就把每个程序都连上，开机时间会被程序数量拖住，且大部分连接永远是闲的。
    """

    def __init__(self, runtime: Runtime, root: Path, cfg: dict | None = None) -> None:
        self.rt = runtime
        self.root = Path(root)
        self.cfg = cfg or {}
        self._links: dict[str, Link] = {}
        self._lock = threading.Lock()

    def link(self, pid: str, create: bool = False) -> Link | None:
        with self._lock:
            l = self._links.get(pid)
            if l is not None and l.status in ("error", "closed") and create:
                l.stop()
                l = None                    # 连不上的旧连接不再复用，重开一条
            if l is None and create:
                if self.rt.program(pid) is None:
                    return None
                l = Link(self.rt, self.root, self.cfg, pid)
                self._links[pid] = l
                l.start()
            return l

    def ensure(self, pid: str) -> dict:
        """界面打开一个程序时调它：把这个程序的智能体叫起来（幂等）。"""
        l = self.link(pid, create=True)
        if l is None:
            return {"pid": pid, "status": "missing", "error": f"没有程序 {pid}"}
        return l.as_dict()

    def send(self, pid: str, text: str) -> str:
        l = self.link(pid, create=True)
        if l is None:
            return f"没有程序 {pid}"
        return l.send(text)

    def cancel(self, pid: str) -> str:
        l = self.link(pid)
        return "没有在跑的程序" if l is None else l.cancel()

    # -- 换地址 ---------------------------------------------------------
    @property
    def url(self) -> str:
        """现在用的是哪个 aaas-service。界面上那个设置项读的就是它。"""
        return str(self.cfg.get("url") or "")

    def rebind(self, cfg: dict) -> list[str]:
        """换一份配置（含地址）：停掉旧连接，按新配置把**原来开着的程序**重连。

        为什么不只改字段了事：每条 Link 的地址在 ``_main()`` 里读一次就建了
        ``aaas.Caller``。换字段不会让已有连接改道，只会让「设置生效了」变成一句谎。
        所以这里真的重建 —— 代价是正在跑的那一轮会被中断，这一点必须告诉人。

        返回被重连的 pid 列表（顺序无关，用来在回执里说清「动了谁」）。
        """
        with self._lock:
            pids = list(self._links)
            old = list(self._links.values())
            self._links = {}
            self.cfg = dict(cfg or {})
        for l in old:
            try:
                l.stop()
            except Exception:          # 一条停不掉的连接不该挡住别的重连
                pass
        for pid in pids:
            self.link(pid, create=True)
        return pids

    def status(self) -> dict:
        with self._lock:
            return {pid: l.as_dict() for pid, l in self._links.items()}

    def stop(self) -> None:
        with self._lock:
            links = list(self._links.values())
        for l in links:
            l.stop()
