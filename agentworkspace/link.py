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
import concurrent.futures
import json
import queue
import threading
import time
from pathlib import Path

import aaas
from aaas import FunctionDeclaration, FunctionResult, Part, Tool

from . import media, sessions
from .settings import DEFAULT_URL as _DEFAULT_URL
from .kernel import Runtime


def _image_parts(images) -> tuple:
    """把内核 Result 上的二进制产物翻成协议里的 file part。

    为什么这一层才翻：``Result`` 属于内核，它不该知道协议长什么样；
    ``FunctionResult`` 才是协议。图到不了 agent 眼前，唯一的缺口就在这里 ——
    在补上之前，``dev_page_shot`` 拍得再好，agent 也只收到一句「已截图」。
    """
    out = []
    for im in images or []:
        if isinstance(im, dict) and im.get("raw"):
            out.append(Part.image(im["raw"], im.get("mime") or "image/png",
                                  im.get("name") or None))
        elif isinstance(im, (bytes, bytearray)):
            out.append(Part.image(bytes(im), "image/png"))
    return tuple(out)

ASSISTANT = "assistant"

# 提示词文件缺席时的兜底：短，且**不撒谎**（不承诺它其实没有的能力）。
#
# 为什么不能是空串：服务端 ``validate_definition`` 拒空 / 纯空白 instructions
# （``INSTRUCTIONS_INVALID``），整轮在 2ms 内失败，而用户看不见任何解释 ——
# 兜底非空是**协议要求**，不是文风偏好。所以这里只能在「非空」里挑最短的：
#   * 「你是小助理」删掉：工具名全是 ``assistant_*``，界面标题也是它，
#     prompt 里再说一遍不改变任何行为；
#   * 「用你手上的能力」删掉：工具列表本身已经说完了（与 9483517 同一判据）；
#   * 留下的是唯一会改变行为的部分 —— 任务来源（用户）+ 完成度（做出来，
#     不是给个方案）。
FALLBACK_ASSISTANT = "用户要什么，就做出来。"
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


# 重连退避的封顶：不再翻倍，也不再无限膨胀（秒）。
RECONNECT_CAP = 30.0
# 造 Caller 的**唯一**出口，同时是测试注入点：selftest [23] 用假 transport 跑真 Link，
# 断言「掉线之后自己回来」。注入点必须在模块上 —— 放在 Link 里就只有第一次连接能被替身。
CALLER_CLASS: type | None = None


def _make_caller(url: str, token):
    """建一条 Caller。重连会反复经过这里，所以只此一处 new。"""
    cls = CALLER_CLASS or aaas.Caller
    return cls(url, token=token)


def _backoff(attempt: int) -> float:
    """第 attempt 次连续失败后睡多久：1,2,4,8,16,30,30…（秒）。

    抽成纯函数是为了**能被断言**（selftest [23] A7）：序列写在形式里，
    而不是藏在 while 里的某个 ``* 2`` 里 —— 那种东西没人能验。
    """
    n = max(1, int(attempt))
    return float(min(RECONNECT_CAP, 2.0 ** (n - 1)))


class _SerialExecutor:
    """一条 daemon 线程 + 一条队列：把**同步**执行体挪出事件循环，且保持串行。

    为什么必须挪出去：aaas 的 ``Tool.invoke`` 就在事件循环线程里直接调执行体
    （``aaas/function.py:270``）。一个跑 30 秒的 shell 冻住的不是一次调用，是
    **整条链路** —— 心跳发不出去（websockets ``ping_interval=20``）、取消帧
    收不到、连 aaas 自己的 ``wait_for(deadline*0.9)`` 也永远等不到超时点
    （超时得靠循环转起来才触发）。症状是「模型调了工具之后，那个程序彻底不说话」。

    为什么不用 ``asyncio.to_thread``：它用循环的默认执行器，那些线程是**非 daemon**
    的，解释器退出时会 join 它们 —— 一个卡住的 30 秒 shell 会把「关窗口」也拖住
    30 秒。今天执行体跑在 daemon 线程上，退出是立刻的，这个性质不能丢。

    为什么不是多线程池：插件从来是串行执行的（一次 dispatch 做完才轮到下一次），
    并发会改变它们的语义（读-改-写）。一个程序一条队列，顺序与今天逐字节一致。
    """

    def __init__(self, name: str) -> None:
        self._q: queue.Queue = queue.Queue()
        self._closed = False
        self._thread = threading.Thread(target=self._run, name=name, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while True:
            item = self._q.get()
            if item is None:                  # 收工信号
                return
            fut, fn = item
            if not fut.set_running_or_notify_cancel():
                continue                      # 还没开跑就被取消：那就别跑
            try:
                value = fn()
            except BaseException as exc:      # noqa: BLE001 - 异常必须落到 Future 上
                try:
                    fut.set_exception(exc)
                except Exception:             # 等待者已经走开
                    pass
            else:
                try:
                    fut.set_result(value)
                except Exception:             # 同上：等待者已经走开，结果无人要
                    pass

    def submit(self, fn):
        if self._closed:
            raise RuntimeError("执行器已关闭")
        fut: concurrent.futures.Future = concurrent.futures.Future()
        self._q.put((fut, fn))
        return fut

    def shutdown(self) -> None:
        self._closed = True
        self._q.put(None)


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
        # 闸门放下的时刻。用来区分两种「忙」：真的在跑，和「刚跑完、还差最后一步」。
        self._idle_at = 0.0
        self._declared_sig: tuple | None = None
        self._declare_fails = 0        # 同一份坏声明连续失败几次（用来停手）
        self._declare_sig_tried: tuple | None = None   # 上面的计数属于哪一份声明
        # idle | connecting | ready | reconnecting | error | closed
        # reconnecting 是**新的真话**：以前断线不改状态，于是它一直是 ready，
        # 而下一句会撞进一个已经不存在的连接。
        self.status = "idle"
        self._reconnects = 0            # 这一条 Link 掉线过几次（as_dict 报给界面）
        self._lost_at: float | None = None
        self.error: str | None = None
        self.session_id: str | None = None
        self.auto_continue = True       # 本轮内自己的能力面变了 → 自动接力一轮
        self.MAX_CONTINUE = 3           # 防死循环：最多连续接力几轮
        self._continues = 0
        self._turn_caps: set[str] = set()
        # 对话的**唯一真相**在磁盘上（sessions.py）。self.history 只是它的展示镜像：
        # 切换会话时整份换掉，切完模型看到的就是这份 —— 没有第二份在别处长着。
        self.store = sessions.Store(self.root)
        # 工具执行的一端：同步、串行、在循环之外（见 _SerialExecutor）
        self._exec = _SerialExecutor(f"tool-{pid}")
        self.sid: str = self.store.latest(pid) or self.store.create(pid)
        self.history: list[dict] = self.store.messages(pid, self.sid)
        # 轮数与 token 累计：在内存里跟着长，避免 as_dict 每次都重读整份文件
        # （/api/state 会为每个程序调一次 as_dict）。
        self._stat: dict = self.store.stats(pid, self.sid)
        # 标题缓存在内存里：as_dict() 会被 /api/state 为**每个程序**调一次，
        # 每次都去重读整份会话文件，会话越长越慢，而它只需要一个字符串。
        self.title: str = self.store.meta(pid, self.sid).get("title", "")
        self._clip_warned = False       # 提示词被截断，只提醒一次（别每轮刷屏）
        #: 我们**受理过**停止的那些轮。撤回的判据要用它：服务端自己取消（超时 /
        #: 上游关闭）不算人的意志，不该替人做「这一轮不算数」这个决定。
        #: 跨线程读写（cancel 在 HTTP 线程、_one_turn 在循环线程）—— set 的
        #: add/in 在 GIL 下是原子的，且**不能**在这里拿 _send_lock（见 _turn_done）。
        self._stop_asked: set[str] = set()
        #: 撤回时若那一轮真执行过工具，就给下一轮塞一句一次性提示（用完即弃）。
        #: 世界没有回退，而模型看不见自己做过什么 —— 这是唯一一处会伤人的地方。
        self._retract_note = ""

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
        fails = 0                        # 连续失败几次 —— 退避的指数
        while self.status != "closed":
            if fails:
                delay = _backoff(fails)
                self._emit("link.retry", {"attempt": fails, "delay": delay,
                                          "error": self.error, "url": url})
                if not await self._sleep(delay):
                    return               # stop() 在睡眠里把状态改成了 closed
            if not await self._connect(url, token):
                fails += 1
                continue
            fails = 0
            why = None
            # 连接活着的期间：等它坏，或者**这个程序**的能力面变了要重声明。
            # 判据是能力面的差异，不是 rt.version —— 版本号是全工作区的，
            # 别的插件变了也跟着跳，那会让每个程序无谓地重声明。
            while self.status != "closed":
                if not await self._sleep(0.5):
                    return
                why = self._lost()
                if why:
                    break
                if self._sig() != self._declared_sig:
                    self._mark_ready(await self._declare())
            if self.status == "closed":
                return
            # 掉线：先把这条连接收干净（在飞那一轮要收到终态，闸门才开得回来），
            # 再说实话（reconnecting），然后从 1 秒开始退避重连。
            await self._drop_caller(why)
            fails = 1

    async def _connect(self, url: str, token) -> bool:
        """建一条连接并声明。返回是否成功（失败时 error 已写好、状态已说实话）。"""
        try:
            self._caller = _make_caller(url, token)
            await self._caller.connect()
        except Exception as exc:      # 连不上就是连不上：如实报，不做假聊天降级
            self._caller = None       # 半成品不带进下一轮
            self.error = f"{type(exc).__name__}: {exc}"
            self.status = "reconnecting"
            self._emit("link.status", {"status": "reconnecting", "url": url,
                                       "error": self.error,
                                       "attempt": self._reconnects + 1,
                                       "hint": "检查 workspace/config.json 里的 aaas.url，"
                                               "或先启动 aaas-service"})
            return False
        cr = self._caller.connect_result
        self.session_id = getattr(cr, "session_id", None)
        self._turn = None            # 上一轮的终态属于上一条连接
        # **先声明，再标 ready。** 反过来的话，第一句话可能在 declare 还在路上时就被
        # 调度出去 —— 那一轮里模型看不到这个程序的任何能力（工具还没到）。
        ok = await self._declare()
        if ok:
            self.error = None
            self._lost_at = None
        else:
            self.status = "reconnecting"
        self._mark_ready(ok)
        return ok

    def _lost(self) -> str | None:
        """这条连接还活着吗？返回「为什么算掉线」，或 None。

        aaas 没有公开的「断开」回调，所以读它的**实际状态**，判据写在明处：
          * ``_reader.done()``：读循环退出 —— 远端消失时最先出现的信号
          * ``_ws.close_code``：websockets 已判定关闭（1006 = 连关闭帧都没收到）
          * ``_closed``：Caller 自己被关过
        三个都不看「我们有没有在发请求」，所以闲置的连接也查得出来。
        """
        caller = self._caller
        if caller is None:
            return "no caller"
        reader = getattr(caller, "_reader", None)
        if reader is not None and getattr(reader, "done", lambda: False)():
            return "read loop ended"
        ws = getattr(caller, "_ws", None)
        if ws is None:
            return "socket gone"
        code = getattr(ws, "close_code", None)
        if code is not None:
            return f"socket closed ({code})"
        if getattr(caller, "_closed", False):
            return "caller closed"
        return None

    async def _drop_caller(self, why: str | None) -> None:
        """收掉断掉的那条连接，并把状态说成真话（reconnecting）。

        为什么必须 close：``Caller.close()`` 会给在飞的轮发 ``connection_lost`` 终态
        （abrupt 掉线时 ``_read_loop`` 的 finally 也会做；close 顺带收掉 socket 与读
        任务）。轮拿到终态，``_open_turn`` 的 finally 才开得回闸门 —— 否则闸门永久
        关着，表现是「这个程序从此只回你一句『上一轮还没结束』」。
        """
        caller, self._caller = self._caller, None
        self._turn = None
        self._reconnects += 1
        self._lost_at = time.time()
        self.status = "reconnecting"
        # 状态说「在重连」，error 必须说「为什么」—— 否则界面上一句没有原因的重连中，
        # 人只能自己猜是网络、是服务端重启、还是地址写错了。
        self.error = f"连接断开：{why}" if why else "连接断开"
        self._emit("link.status", {"status": "reconnecting", "why": why,
                                   "error": self.error, "attempt": self._reconnects,
                                   "lossAt": self._lost_at})
        if caller is None:
            return
        try:
            await asyncio.wait_for(caller.close(), timeout=3.0)
        except Exception:            # 拆除路径上的异常不该把重连卡住
            pass

    async def _sleep(self, secs: float) -> bool:
        """短步睡眠：``stop()`` 一声令下就回来。返回 False 表示已关闭。

        为什么不 ``await asyncio.sleep(secs)`` 一把睡过去：退避封顶 30 秒，一觉睡完
        才看状态的话，关窗口也要陪着等 30 秒 —— 那正是「关不掉」的来源。
        """
        end = time.time() + max(0.0, float(secs))
        while True:
            if self.status == "closed":
                return False
            left = end - time.time()
            if left <= 0:
                return self.status != "closed"
            await asyncio.sleep(min(0.2, left))

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

    def _instructions_now(self) -> str:
        """这一次真正发出去的自我认知 = 提示词 + （至多一次）撤回的后果提示。

        注记**不进历史**：它不是谁说的话 —— 塞进 history 就只能塞成 user/agent，
        两条都是假发言。用完即弃，所以它在下一轮之后就消失了。
        """
        ins = self.instructions()
        note, self._retract_note = self._retract_note, ""
        if not note:
            return ins
        # 给注记留位置：截断发生在尾巴上，直接拼接会把**刚加的这句**截掉 ——
        # 那正好是「加了等于没加」，而且不报错。
        room = max(200, MAX_INSTRUCTIONS - len(note) - 2)
        return (ins[:room] + "\n\n" + note)[:MAX_INSTRUCTIONS]

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
        """一个能力的执行体：**只做一次转发**，落进内核的唯一入口。

        ``run`` 是协程，dispatch 仍然是同步的 —— 这一层是**边界**：
        同步执行体在这个循环线程里跑，就会把心跳、取消、deadline 一起冻住
        （见 ``_SerialExecutor`` 的说明）。所以这里只做两件事：
        把 dispatch 交给那条串行队列，再把结果翻回协议要的形状。
        """
        async def run(**kwargs):
            loop = asyncio.get_running_loop()
            # caller=self.pid：这道手可能属于别人（开发者模式借来的），
            # 但动手的是这个程序 —— 内核要能分清这两件事。
            fut = self._exec.submit(
                lambda: self.rt.dispatch(pid, fn, kwargs, source="agent", caller=self.pid))
            res = await asyncio.wrap_future(fut, loop=loop)
            return FunctionResult(status=res.status,
                                  parts=self._out_parts(res.text, res.images),
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
        """把人的一句话送进去（可以带图）。立刻返回，事件经总线流回前端。

        **首句不能丢。** 一个程序第一次被打开时连接才建起来（connecting），
        而这恰好是用户最可能立刻开口的时刻。原来的实现直接回「还没连上」——
        那句话就消失了：不报错、只是没人理，最坏的一种失败形态。
        所以这里等一小会儿（默认 20 秒）；等不到才算失败，且如实说。
        """
        # reconnecting 也要等：掉线后 1 秒就开始重连了，这时人开口的那句话
        # 不该被立刻拒掉（「首句不能丢」的通则，断线重连是它的另一半）。
        waiting = ("idle", "connecting", "reconnecting")
        if self.status in waiting and wait > 0:
            deadline = time.time() + wait
            while time.time() < deadline and self.status in waiting:
                time.sleep(0.05)
        if self.status != "ready" or self._loop is None:
            if self.status == "reconnecting":
                return (f"连接断了，正在重连（第 {self._reconnects} 次）："
                        f"{self.error or '原因未知'}")
            return self.error or f"还没连上 aaas-service（当前 {self.status}）"
        with self._send_lock:                      # 「检查 + 置位」必须是一步
            # aaas.Turn.done 是 **property**（返回 final is not None），不是方法。
            # 写成 .done() 会 TypeError: 'bool' object is not callable。
            if self._busy or (self._turn is not None and not self._turn.done):
                return "上一轮还没结束"
            # 图在**开这一轮之前**判完、落完盘：
            #   * 服务端对坏图回 -32602，那是**轮级**失败 —— 整轮连同这句话一起死，
            #     所以坏图根本不该让那一轮开起来（闸门也就没动过）；
            #   * 落盘放在这里，是为了「历史里那一行」与「真的发出去的字节」
            #     出自同一次判定 —— 分两处判就会出现「记下的图模型没见过」。
            pics: list[media.Image] = []
            files: list[dict] = []
            if images:
                why = self._no_vision_reason()
                if why:
                    return why
                pics, why = media.prepare(images, max_bytes=self._max_file_bytes())
                if why:
                    return why
                try:
                    files = [self.store.save_image(self.pid, self.sid, im) for im in pics]
                except OSError as exc:
                    return f"图没能存下来：{type(exc).__name__}: {exc}（这一轮没发出去）"
            self._busy = True
            self._continues = 0
            self._turn_caps = set(self.rt.capabilities_of(self.pid))
            try:
                self._turn_task = asyncio.run_coroutine_threadsafe(  # type: ignore[assignment]
                    self._open_turn(text, pics=pics, files=files), self._loop)
                # 后台任务炸了也得有人知道：没有这个回调，异常只留在 Future 里，
                # 界面永远停在「运行中」，也没有任何 turn.failed。
                self._turn_task.add_done_callback(self._turn_done)
            except Exception as exc:               # 调度失败不能把闸门留在关着的位置
                self._busy = False
                self.error = f"{type(exc).__name__}: {exc}"
                return self.error
        return "sent"

    def _out_parts(self, text: str, images) -> tuple:
        """工具结果 → 协议 parts。**图要过和 send() 一样的两道闸。**

        为什么不能直接 ``Part.image``：服务端对坏图回 ``-32602`` 是**轮级**
        失败 —— 一张截图能把整轮连同这句话一起弄死。所以先问「这条连接收不收图」
        （:meth:`_no_vision_reason`），再问「这张图合不合规」（``media.prepare``）。

        被闸下来时把原因写进**文字**：工具结果没有「这一轮」可死，
        但 agent 必须知道图没到它眼前 —— 否则它会对着一张自己没看见的图下判断。
        """
        parts: list = [Part.t(text or "")]
        if not images:
            return tuple(parts)
        why = self._no_vision_reason()
        pics: list[media.Image] = []
        if not why:
            pics, why = media.prepare(images, max_bytes=self._max_file_bytes())
        if why:
            parts[0] = Part.t(f"{text or ''}\n\n（图没能发给你：{why}）")
        else:
            parts.extend(Part.image(im.raw, im.mime, im.name) for im in pics)
        return tuple(parts)

    def _no_vision_reason(self) -> str:
        """这条连接能不能收图；不能就返回一句人话（空串 = 能）。

        为什么要在这里拦而不是等 SDK：``Caller.send`` 确实会预检并抛 -32009，
        但那是一条**异常**，落在协程里 —— 用户看到的会是「这一轮失败了」，
        而真相是「这张图从来没发出去过」。两句话的区别是：后者能让人换个做法。
        """
        cr = getattr(self._caller, "connect_result", None)
        if cr is None:
            return "还没连上 aaas-service，图没发出去"
        if not cr.has("part.file"):
            return ("这条连接不收图（aaas-service 没有协商 part.file）—— 图没有发出去。"
                    "把图里的内容用文字说一遍，或者换一个能看图的模型再试")
        return ""

    def _max_file_bytes(self) -> int:
        """这张连接自报的单张图上床。``0`` 是「不收图」的那种读数（见 aaas
        ``build_server``：未宣告 ``part.file`` 时它把 maxFileBytes 报成 0）——
        这里退回兜底值，真正能不能发由 :meth:`_no_vision_reason` 判。
        """
        cr = getattr(self._caller, "connect_result", None)
        try:
            n = int((getattr(cr, "limits", None) or {}).get("maxFileBytes") or 0)
        except (TypeError, ValueError):
            n = 0
        return n if n > 0 else media.DEFAULT_MAX_FILE_BYTES

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

    async def _open_turn(self, text: str, *, pics=(), files=()) -> None:
        """跑一轮；若本轮内能力面变过，就**接力**跑下一轮。

        典型场景：小助理改写了某个程序的 backend.py —— 那个程序自己的能力变了。
        对小助理自己，手里那套「手」不会因此变，所以它不会白跑接力轮。
        """
        try:
            final = await self._one_turn(text, pics=pics, files=files)
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
                # sys=True：这句不是人说的。它照样进模型上下文（当时确实是 user 身份
                # 被看见的），但界面上必须能与人说的分开 —— 否则一旦落盘，
                # 它就成了永远擦不掉的假用户发言。
                final = await self._one_turn(handoff, sys=True)
        finally:
            # 闸门必须开回去：否则聊天会永远回「上一轮还没结束」，
            # 而那看起来像「功能正常，只是不理人」—— 最坏的一种故障形态。
            with self._send_lock:
                self._busy = False
                self._idle_at = time.time()

    async def _one_turn(self, text: str, *, pics=(), files=(), sys: bool = False):
        # **显式传 history**：真相在我们这份 store 上，``caller.history`` 只是 SDK
        # 内部细节。显式传带来两个好处，都是必需的：
        #   * 切会话不必去动 SDK 的内部状态（那还得跨线程跳转）；
        #   * 「模型看到的那份」恰好等于我们从文件投影出来的那份 —— 只此一份。
        base = self._ctx()
        # 没有图时**原样传字符串**：这条路上一切照旧（SDK 自己包成 Message）。
        msg: str | aaas.Message = media.message(text, list(pics)) if pics else text
        try:
            turn = await self._caller.send(msg, instructions=self._instructions_now(),
                                           history=base)
        except Exception as exc:
            self._emit("turn.failed", {"error": f"{type(exc).__name__}: {exc}"})
            return None
        self._turn = turn
        # 轮号：这一轮之后可能被撤回，撤的是「这一轮」，所以必须有个名字。
        # 老记录没有它 → 永远不可撤回（历史里的事早就定了）。
        tid = str(getattr(turn, "turn_id", "") or "")
        self._record_user(text, sys=sys, files=files, turn=tid)
        self._emit("turn.started", {"turnId": turn.turn_id})
        parts: list[str] = []
        n_ok = 0                          # 这一轮**真的执行成功**的工具调用次数
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
                    # 数「真的动过手」的次数：撤回之后模型看不见这些调用，
                    # 只有这个数能让它知道「世界可能已经不是原样了」。
                    # 只认 ok —— error/rejected 是没造成后果的那两种。
                    if str(getattr(ev, "status", "") or "") == "ok":
                        n_ok += 1
                    self._emit("call.result", {"status": ev.status, "callId": ev.call_id,
                                               "text": "".join(p.text or "" for p in (ev.parts or ()))},
                               source="agent")
                elif kind == "message.completed":
                    pass
        except Exception as exc:
            self._emit("turn.failed", {"error": f"{type(exc).__name__}: {exc}"})
        final = await turn.end()
        text_out = "".join(parts) or turn.text
        self._record_agent(text_out, turn=tid)
        usage = final.usage.to_wire() if getattr(final, "usage", None) else None
        # 账单要能看见：stateless 每轮重发全史，会话长度就是成本。
        self.store.append_usage(self.pid, self.sid, usage)
        if usage:
            self._stat["tokensIn"] += int(usage.get("input") or 0)
            self._stat["tokensOut"] += int(usage.get("output") or 0)
        retracted = self._retract_if_stopped(tid, final, n_ok)
        self._emit("turn.completed", {
            "status": final.status, "finishReason": final.finish_reason,
            "usage": usage,
            "text": text_out,
            "turnId": tid,
            # 这一轮算不算数，由服务端一次说清：界面不自己再判一遍
            # （界面没有 _stop_asked，判不出来；判错的方向恰好是「把真答案撤掉」）。
            "retracted": retracted,
            "error": (final.error.to_wire() if getattr(final, "error", None) else None),
        }, source="agent")
        return final

    def _retract_if_stopped(self, tid: str, final, n_ok: int) -> bool:
        """终态到了：若这一轮是我们受理过停止的、且真的被取消了 —— 撤回它。

        **判据落在终态，不落在点击那一刻**（点击只是「请求」，见 :meth:`cancel`）。
        于是界面不会出现「点了停止东西就没了，可它其实还在跑」：它在终态时
        一次性地从「正文」切到「记号」，中间没有半态。

        两条都要满足：

        * ``status == "cancelled"`` —— 若这一轮在取消生效前已经跑完，答案是**真的**，
          撤回它等于删掉一份真产出；而人按停止的意思是「别跑了」，不是「收回去」。
        * ``tid ∈ _stop_asked`` —— 服务端自己取消（超时 / 上游关闭）不是人的意志。

        写是谁负责的，撤回也由谁负责：只有一个写入者（这一轮的协程），
        所以没有竞态、不需要跨线程锁，重复调用天然幂等（retract 自己判）。
        """
        asked = tid in self._stop_asked
        self._stop_asked.discard(tid)          # 一轮只判一次，别让这个集合无限长
        if not tid or not asked or getattr(final, "status", "") != "cancelled":
            return False
        if self.store.retract(self.pid, self.sid, tid, why="user-stop", tools=n_ok):
            self._load(self.sid)               # 投影整份换掉（撤回是一次追加）
            new_title = self.store.retitle_auto(self.pid, self.sid)
            if new_title:
                self.title = new_title
        # 世界没有回退：那一轮里写过的文件、跑过的命令都还在，而模型看不见。
        # 只在**真的动过手**时才提醒 —— 没执行过东西的撤回是干净的，
        # 再补一句就是把噪音当成提醒。
        if n_ok:
            self._retract_note = (
                f"[宿主] 上一轮被用户停止，已从对话历史里撤回；期间执行过 {n_ok} 次工具调用"
                f"（调用了什么、结果是什么，都不在上文）。如果这个请求涉及同一件事，"
                f"先查看当前状态再动手，不要假设文件还是原样。")
        return True

    def cancel(self) -> dict:
        """请求取消这一轮。**只表示受理** —— 终态看事件（aaas §3.4 语义 3）。

        原来这里把 run_coroutine_threadsafe 的返回值直接丢了：连接刚断、或服务端
        不认 ``aaas/cancelTurn``（-32601）时，异常只躺在那个 Future 里没人取，
        界面照样显示「已请求取消」—— 那句话是假的，而且没人会知道它是假的。
        所以现在让 Future 落地成一条事件，前端据此如实退回「运行中」。

        返回 ``{"status": ..., "message": ...}``：``status`` 给程序判，``message``
        给人看。前端原来靠 ``startswith("已请求取消")`` 分支 —— 改一个字就静默走错，
        而这条路上走错的意思是「它其实还在跑，界面以为停住了」。
        status **只有三值**，别再加第四个：``cancelling``（受理了，等终态）/
        ``idle``（本来就没在跑，等于停住了）/ ``error``（没送出去，人可以再按一次）。
        调用方按这三值分派；加一个「既没受理也没失败」的中间态，就没有人知道该
        拿它怎么办（真的试过 ``starting``：它变成一个既不执行也不重试的死胡同）。
        """
        if self._loop is None or self._caller is None:
            return {"status": "error", "message": "还没连上 aaas-service"}
        turn = self._turn
        # aaas.Turn.done 是 **property**（返回 final is not None），不是方法。
        # 轮次已经结束就别发这一趟：发出去只会拿到一条没人等的结果。
        if turn is None or turn.done:
            # ``_busy`` 与 ``_turn`` 不是同一时刻的事：``send()`` 一进来就置
            # ``_busy``（同步），而 ``_turn`` 要等 ``caller.send(...)`` 回来才赋上，
            # 一轮结束后也不会清回 None。所以「有活着的一轮、手上却没有可取消的
            # id」是**常态**，它跟「本来就没在跑」是两件事。混成一句 idle，前端会
            # 当场解锁（那正是本轮要消灭的错），而它其实还在跑。
            if self._busy:
                # 曾经的 `starting` 是个死胡同：调用方只能「继续等终态」，而这条
                # 取消请求**根本没有发出去**、也没有任何东西会重试它 —— 用户还在
                # 「正在停止…」里按不动停止键，这一轮照常跑到底。
                # 如实报 error：前端退回「运行中」并说明没送出去，人可以再按一次。
                return {"status": "error",
                        "message": "这一轮还在起步（还没拿到 turn id），请再按一次停止"}
            return {"status": "idle", "message": "没有可取消的轮次"}
        turn_id = turn.turn_id
        coro = self._caller.cancel(turn_id, reason="用户取消")
        try:
            fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        except Exception as exc:               # 调度失败：如实说，别假装受理了
            # 协程对象是在调度**之前**建出来的：事件循环已经关了/停了（正是这个
            # 分支要防的「连接刚断」）时没人 await 它，CPython 会在 GC 时朝 stderr
            # 喷 ``RuntimeWarning: coroutine 'Caller.cancel' was never awaited``
            # —— 桌面程序的日志里多一条与用户无关的噪音。自己收掉。
            close = getattr(coro, "close", None)
            if close is not None:
                close()
            return {"status": "error", "message": f"{type(exc).__name__}: {exc}"}
        # **把 turn_id 关进闭包**：回执是另一次往返，等回调真跑起来时 ``self._turn``
        # 可能已经换成下一轮了（本轮因取消结束、闸门放开、用户立刻发了下一句）——
        # 那时回执会声称「新的那一轮已受理取消」。取消的是哪一轮，必须当场记下。
        fut.add_done_callback(lambda f: self._cancel_done(f, turn_id))
        # 只记**送出去**的那些：_cancel_done 里的失败（-32601 等）不改这个判断 ——
        # 那时终态几乎不会是 cancelled，就算是我们也拦不住「服务端真的照做了」。
        self._stop_asked.add(turn_id)
        return {"status": "cancelling", "message": "已请求取消"}

    def _cancel_done(self, fut, turn_id: str) -> None:
        """cancel 的异常也要有人取 —— 否则「已请求取消」随时可能是一句空话。

        ``turn_id`` 由注册处捕获进来，不读 ``self._turn``：那是「现在」的那一轮，
        不是「被取消」的那一轮。
        """
        try:
            exc = fut.exception()
        except Exception as inner:             # CancelledError 之类的
            exc = inner
        if exc is None:
            self._emit("turn.cancel_ack", {"turnId": turn_id})
            return
        # **不拿 _send_lock**：理由同 _turn_done —— add_done_callback 遇到已完成的
        # future 会在注册线程里同步跑回调，而注册那一段正持有那把锁（非重入）。
        code = getattr(exc, "code", None)
        msg = ("服务端不支持停止（-32601）" if code == -32601
               else f"{type(exc).__name__}: {exc}")
        # 与 ack 对称：回执要说明**哪一轮**的取消失败了。它同样可能迟到
        # （超时后才断线 / -32601），而那时 `self._turn` 已经是别人的一轮了。
        self._emit("turn.cancel_failed", {"error": msg, "turnId": turn_id})

    # ------------------------------------------------------------------ 收尾
    def stop(self) -> None:
        self.status = "closed"
        self._exec.shutdown()             # 队列收工：卡住的那一次照旧跑完，但不留线程
        if self._loop and self._caller:
            try:
                asyncio.run_coroutine_threadsafe(self._caller.close(), self._loop).result(timeout=3)
            except Exception:
                pass

    def as_dict(self) -> dict:
        """给界面看的一份快照。

        ``history`` 仍然截到 60 条（界面一次画不了那么多），但现在**说出来**了：
        ``truncated`` 为真时人知道「上面还有」。静默截断比报错更坏 —— 屏幕上演着
        一段完整的对话，而模型那边是另一回事，这种不一致不报错。
        """
        SHOW = 60
        return {"pid": self.pid, "status": self.status, "error": self.error,
                "sessionId": self.session_id, "busy": self._busy,
                "reconnects": self._reconnects, "lastLostAt": self._lost_at,
                "sid": self.sid, "title": self.title,
                "turns": self._stat.get("turns", 0),
                "tokensIn": self._stat.get("tokensIn", 0),
                "tokensOut": self._stat.get("tokensOut", 0),
                "badLines": self._stat.get("bad", 0),
                "truncated": len(self.history) > SHOW,
                "total": len(self.history),
                "history": self.history[-SHOW:]}


    # ------------------------------------------------------------------ 会话
    # 四个动作都只碰**数据**，不碰连接：切会话不重连，因此不会打断正在跑的那一轮
    # 之外的东西 —— 而正在跑的那一轮是**拒绝**切换的理由（见 switch）。
    def _ctx(self) -> list[aaas.Message]:
        """当前会话投影成模型会看到的那份 —— **图也在这里被读回来**。

        历史记录里只有引用（``{name, mime, size, title}``），字节在盘上；读回
        放在这一层而不是存储层，是因为它要按 history 的两道账裁剪：
        stateless 下每轮重发全史，图是唯一一处「成本随轮数累积」的口子，
        而服务端对超过 16 张 / 16 MiB 的 history 回 -32602 —— 整轮判死。
        所以从**最老的**开始不再重发，文本照旧留着。

        一张图都没重发、文本又是空的那种条目（只有图的发言），换成一句
        ``[一张图]``：让它整条消失，模型就会看见自己的回答而没有问题。
        """
        left, budget = media.HISTORY_IMAGES, media.HISTORY_BYTES
        plan: list[tuple[dict, list[dict]]] = []
        for h in reversed(self.history):          # 名额先给最近的
            if h.get("retracted"):
                continue                          # 撤回的那一轮：模型根本不该看见它
            keep: list[dict] = []
            for f in h.get("files") or ():
                size = int(f.get("size") or 0)
                if left > 0 and size <= budget:
                    keep.append(f)
                    left -= 1
                    budget -= size
            plan.append((h, keep))
        plan.reverse()
        out: list[aaas.Message] = []
        for h, keep in plan:
            text = str(h.get("text") or "")
            pics = [im for im in (self._image_of(f) for f in keep) if im is not None]
            if not pics and not text.strip():
                dropped = len(h.get("files") or ())
                if not dropped:
                    continue                  # 本来就是空行（Message 要求 parts 非空）
                text = f"[{dropped} 张图]" if dropped > 1 else "[一张图]"
            if h.get("role") == "user":
                out.append(media.message(text, pics))
            else:
                out.append(aaas.Message.agent(text))
        return out

    def _image_of(self, ref: dict):
        """一条引用 → 一张图（读盘）。读不到返回 ``None``：文件被人删掉时，
        这一段历史就该退化成文本，而不是让整轮炸在这里。"""
        p = self.store.image_path(self.pid, self.sid, str(ref.get("name") or ""))
        if p is None or not p.is_file():
            return None
        try:
            raw = p.read_bytes()
        except OSError:
            return None
        return media.Image(name=str(ref.get("title") or ref.get("name") or "图"),
                           mime=str(ref.get("mime") or ""), raw=raw)

    def _record_user(self, text: str, *, sys: bool = False, files=(), turn: str = "") -> None:
        refs = list(files or ())
        self.store.append_user(self.pid, self.sid, text, sys=sys, files=refs, turn=turn)
        new_title = self.store.title_once(self.pid, self.sid, text)
        if new_title:
            self.title = new_title
        entry: dict = {"role": "user", "text": text, "ts": time.time(), "sys": sys,
                       "turn": str(turn or ""), "retracted": False}
        if refs:
            entry["files"] = refs
        self.history.append(entry)
        self._stat["turns"] = self._stat.get("turns", 0) + 1

    def _record_agent(self, text: str, *, turn: str = "") -> None:
        self.store.append_agent(self.pid, self.sid, text, turn=turn)
        self.history.append({"role": "agent", "text": text, "ts": time.time(),
                             "turn": str(turn or ""), "retracted": False})

    def _load(self, sid: str) -> None:
        """把展示镜像换成另一个会话。**整份替换**，不做增量 —— 增量一定会漏。"""
        self.sid = sid
        self.history = self.store.messages(self.pid, sid)
        self._stat = self.store.stats(self.pid, sid)
        self.title = self.store.meta(self.pid, sid).get("title", "")

    def _settle(self, wait: float = 1.0) -> bool:
        """等闸门放下，**有上界**。

        为什么需要：``turn.completed`` 是在 ``_one_turn`` 里发出的，而 ``_busy``
        要到 ``_open_turn`` 的 ``finally`` 才归位 —— 中间还夹着一次能力面比对。
        于是存在一个几十毫秒的窗口：屏幕上已经跑完了，闸门却还关着。
        人手点不到，但**贴着终态的自动操作一定会撞上**，然后被回一句「这一轮还在跑」，
        而那句话在用户眼里就是错的。

        所以这里等着它 —— 但**绝不无限等**：真的在跑时要立刻拒绝，
        否则点击会悬在那里，而那看起来像「点了没反应」。
        """
        end = time.time() + wait
        while True:
            if not self._busy and (self._turn is None or self._turn.done):
                return True
            if time.time() >= end:
                return False
            time.sleep(0.01)

    def switch(self, sid: str) -> dict:
        """切到另一个会话。**忙时拒绝**：半途切换会让在飞那一轮的事件落进新会话，
        而它的 history 快照是切换之前取的 —— 两份都对不上，且不会报错。"""
        if not sid or not self.store.exists(self.pid, sid):
            return {"status": "missing", "message": f"没有会话 {sid}"}
        self._settle()                             # 刚跑完的那一瞬间不算「忙」
        with self._send_lock:                      # 与 send 同一道闸门
            if self._busy or (self._turn is not None and not self._turn.done):
                return {"status": "busy",
                        "message": "这一轮还在跑：等它停下来再切，否则在飞的那一轮会落错地方"}
            if sid == self.sid:
                return {"status": "ok", "sid": sid, "title": "",
                        "message": "已经在这个会话里"}
            self._load(sid)
            title = self.title
        self._emit("session.changed", {"sid": sid, "title": title})
        return {"status": "ok", "sid": sid, "title": title, "message": f"已切到「{title}」"}

    def new_session(self, title: str = "") -> dict:
        """开一个新会话并切过去。旧会话仍在盘上，随时切回。"""
        self._settle()
        with self._send_lock:
            if self._busy or (self._turn is not None and not self._turn.done):
                return {"status": "busy", "message": "这一轮还在跑：等它停下来再开新会话"}
            sid = self.store.create(self.pid, title)
            self._load(sid)
        name = self.title
        self._emit("session.changed", {"sid": sid, "title": name})
        return {"status": "ok", "sid": sid, "title": name, "message": "新会话已开"}

    def sessions(self, limit: int = 50) -> dict:
        """这个程序的全部会话（新的在前）。

        放在 Link 上而不是 Hub 上：Hub 只是「一个 pid 一条 Link」的池子，
        它不该知道会话长什么样 —— 否则测试里那个替身就得复刻一遍同样的组装逻辑。
        """
        rows, more = self.store.list(self.pid, limit=limit)
        return {"pid": self.pid, "sid": self.sid, "truncated": more, "shown": len(rows),
                "sessions": [{"sid": r["sid"], "title": r["title"],
                              "turns": r["turns"], "tokensIn": r["tokensIn"],
                              "tokensOut": r["tokensOut"], "size": r["size"],
                              "created": r["created"], "updated": r["updated"],
                              "active": r["sid"] == self.sid} for r in rows]}

    def rename(self, sid: str, title: str) -> dict:
        if not self.store.exists(self.pid, sid):
            return {"status": "missing", "message": f"没有会话 {sid}"}
        name = self.store.rename(self.pid, sid, title)
        if not name:
            return {"status": "bad", "message": "标题不能为空"}
        if sid == self.sid:
            self.title = name
        self._emit("session.changed", {"sid": sid, "title": name})
        return {"status": "ok", "sid": sid, "title": name, "message": f"已改名为「{name}」"}

    def drop(self, sid: str) -> dict:
        """删掉一个会话 —— 移进 ``.trash/``，不硬删（对话是唯一副本）。"""
        if not self.store.exists(self.pid, sid):
            return {"status": "missing", "message": f"没有会话 {sid}"}
        self._settle()
        with self._send_lock:
            if sid == self.sid and (self._busy
                                    or (self._turn is not None and not self._turn.done)):
                return {"status": "busy", "message": "正在跑的这个会话不能删：先停下这一轮"}
            dst = self.store.drop(self.pid, sid)
            if sid == self.sid:
                # 删的正是当前这个：接上最近的一个；一个都不剩就开新的
                nxt = self.store.latest(self.pid) or self.store.create(self.pid)
                self._load(nxt)
        self._emit("session.changed", {"sid": self.sid, "title": self.title})
        return {"status": "ok", "sid": self.sid,
                "message": f"已移进回收站（{dst.name if dst else '—'}），需要时可从 sessions/.trash 捞回"}


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

    def send(self, pid: str, text: str, images: list[dict] | None = None) -> str:
        """把一句话（可以带图）交给那个程序。``images`` 是 ``[{name, mime, data}]``，
        ``data`` 是 base64 —— 界面那一侧就这样把它交过来的。"""
        l = self.link(pid, create=True)
        if l is None:
            return f"没有程序 {pid}"
        return l.send(text, images=images)

    def cancel(self, pid: str) -> dict:
        l = self.link(pid)
        if l is None:
            return {"status": "idle", "message": "没有在跑的程序"}
        return l.cancel()

    # -- 会话 -----------------------------------------------------------
    def _with_link(self, pid: str, act: str, *a, **kw) -> dict:
        """会话是数据，不依赖连接是否 ready —— 但要有个 Link 才知道「当前是哪个」。"""
        l = self.link(pid, create=True)
        if l is None:
            return {"status": "missing", "message": f"没有程序 {pid}"}
        return getattr(l, act)(*a, **kw)

    def sessions(self, pid: str, limit: int = 50) -> dict:
        return self._with_link(pid, "sessions", limit)

    def switch(self, pid: str, sid: str) -> dict:
        return self._with_link(pid, "switch", sid)

    def new_session(self, pid: str, title: str = "") -> dict:
        return self._with_link(pid, "new_session", title)

    def rename(self, pid: str, sid: str, title: str) -> dict:
        return self._with_link(pid, "rename", sid, title)

    def drop(self, pid: str, sid: str) -> dict:
        return self._with_link(pid, "drop", sid)

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
