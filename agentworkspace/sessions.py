"""会话存储 —— 对话的**唯一真相**，落在 `workspace/sessions/<pid>/<sid>.jsonl`。

为什么是我们存，而不是让服务端存：

aaas 的 ``historyMode`` 是 ``stateless``（实测 events.jsonl 里 207 条 link.status
全是这个），服务端按设计不留历史 —— 每轮由调用方把全史塞进 ``params.history``。
另一条路（``historyMode="session"``）看似省事，但服务端的会话表在**它自己的内存**
里，服务一重启就全没；而我们要的正是「关掉程序、换台机器，对话还在」。

所以这里只有一份数据：**文件**。``Link.history`` 与 ``params.history`` 都是它的投影。

三条设计不变量（后面每条都有它的代价说明）：

1. **append-only。** 进程被杀最多坏最后一行，读的时候跳过即可；没有读-改-写，
   也就不会出现「两份真相」。改名 = 追一条 meta，最后一条为准。
2. **文件是完整记录，历史只是投影。** 工具卡（``call``）永远进不了 ``params.history``：
   ``aaas.Message`` 只允许 ``user/agent`` + 纯文本（types.py:272-312），这是协议的
   提示注入防线。所以工具卡另存一列，仅供界面重画。
3. **删除只搬家。** 对话是唯一副本，硬删一次就没了。移进 ``.trash/``，人反悔还能捞。
"""

from __future__ import annotations

import json
import secrets
import threading
import time
from pathlib import Path

import re

import aaas

#: 新会话的默认标题。第一次有人说话时会被自动改成首句前 24 字。
DEFAULT_TITLE = "新会话"
MAX_TITLE = 60

#: 投影进模型上下文的两种 role。``sys`` 只是给人看的分流标记，不改 role ——
#: 接力消息（``[系统] 能力面已更新…``）当时确实是以 user 身份被模型看见的，
#: 现在把它改成别的 role，等于伪造一段没发生过的对话。
CONTEXT_ROLES = ("user", "agent")

#: 一张图在盘上的名字：``<16 位 sha>.<后缀>``。它同时是 URL 的一个分量，
#: 所以形状在这里就钉死 —— 收紧了，越界就不必再靠 resolve 兜。
IMAGE_NAME_RE = re.compile(r"^[0-9a-f]{16}\.(?:png|jpg|webp|gif)$")


def new_sid() -> str:
    """新会话号。8 位 hex 足够：这是给人看的分组标签，不是安全令牌。"""
    return "s_" + secrets.token_hex(4)


def _now() -> float:
    return time.time()


def auto_title(text: str) -> str:
    """从首句取一个标题。取不到就返回空串，由调用方决定是否用默认值。"""
    line = " ".join(str(text or "").split())
    if not line:
        return ""
    return line[:24] + ("…" if len(line) > 24 else "")


def _one_line(obj: dict) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str) + "\n"


def _retracted_turns(rows: list[dict]) -> set[str]:
    """哪些轮被撤回了。**唯一一条判据**，四个消费者共用（见 :meth:`Store.messages`）。

    撤回是一条 ``{"t":"retract","turn":…}`` 记录，不是对旧行的修改 ——
    append-only 是这份存储的第一条不变量（见模块 docstring 第 1 条）。
    """
    return {str(r.get("turn") or "") for r in rows if r.get("t") == "retract"} - {""}


class Store:
    """一个工作区里的全部会话。**没有缓存**：每次都读盘，磁盘即状态。

    加锁是因为 HTTP handler 是多线程的：两个窗口同时说话时，append 必须
    各自整行落盘 —— 交错写入会产生半行 JSON。锁是进程内的，跨进程并发
    写同一个会话不在支持范围内（一个工作区只该有一个宿主进程）。
    """

    def __init__(self, root: Path) -> None:
        self.root = Path(root) / "sessions"
        self._lock = threading.RLock()

    # ---------------------------------------------------------------- 路径
    def _dir(self, pid: str) -> Path:
        return self.root / pid

    def _file(self, pid: str, sid: str) -> Path:
        return self._dir(pid) / f"{sid}.jsonl"

    def dir(self, pid: str) -> Path:
        return self._dir(pid)

    def _files_dir(self, pid: str, sid: str) -> Path:
        """这个会话的图片目录。与 jsonl 分开，因为它是**字节**不是记录。"""
        return self._dir(pid) / "files" / sid

    def image_path(self, pid: str, sid: str, name: str) -> Path | None:
        """一张图的落点；名字形状不对返回 ``None``（不拼路径，就没有越界）。"""
        if not IMAGE_NAME_RE.match(str(name or "")):
            return None
        return self._files_dir(pid, sid) / name

    def exists(self, pid: str, sid: str) -> bool:
        return self._file(pid, sid).is_file()

    # ---------------------------------------------------------------- 写
    def _append(self, pid: str, sid: str, entry: dict, *, durable: bool = False) -> None:
        """追加一行。``durable`` 只给 meta/user/agent 用 —— 一轮里 call 可能有几十条，
        每条都 fsync 会把对话拖慢；而真正不能丢的是「人说了什么、模型答了什么」。
        代价说清楚：硬断电可能丢掉最后几条工具卡（界面重画时少一张卡），
        但绝不会丢掉一句对话。
        """
        p = self._file(pid, sid)
        with self._lock:
            p.parent.mkdir(parents=True, exist_ok=True)
            with open(p, "a", encoding="utf-8", newline="\n") as f:
                f.write(_one_line(entry))
                f.flush()
                if durable:
                    import os

                    os.fsync(f.fileno())

    def create(self, pid: str, title: str = "") -> str:
        """开一个新会话，返回 sid。首行是 meta —— 读的时候靠它认出这是一份会话。"""
        sid = new_sid()
        # 极端情况下（同进程内 8 位 hex 撞号）不要覆盖别人的对话
        while self._file(pid, sid).exists():
            sid = new_sid()
        self._append(pid, sid, {
            "t": "meta", "sid": sid, "pid": pid,
            "title": (title or DEFAULT_TITLE)[:MAX_TITLE],
            "created": _now(),
        }, durable=True)
        return sid

    def save_image(self, pid: str, sid: str, image) -> dict:
        """把一张图的字节落盘，返回历史里那一份**引用**。

        为什么 jsonl 只存引用、字节另存：对话记录要能一眼读（``cat`` 就能看），
        而 base64 的图进去之后它就是一堵谁也读不了的墙。同时**同 sha 只存一份**：
        同一张图发两次不该占两份空间。
        """
        name = f"{image.sha}.{image.ext}"
        p = self._files_dir(pid, sid) / name
        with self._lock:
            if not p.is_file():
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(image.raw)
        return {"name": name, "mime": image.mime, "size": image.size,
                "title": image.name}

    def append_user(self, pid: str, sid: str, text: str, *, sys: bool = False,
                    files: list[dict] | None = None, turn: str = "") -> None:
        """记一句「用户侧」发言。``sys=True`` 是程序自己发的接力消息，界面上要与人说的区分开。

        ``files`` 是随这句话一起发出去的图（引用，见 :meth:`save_image`）——
        它必须与这句话**同一条记录**：分开存就会出现「有图没有话」的半条历史。
        """
        entry: dict = {"t": "user", "text": text, "ts": _now(), "sys": bool(sys)}
        if turn:
            entry["turn"] = str(turn)
        if files:
            entry["files"] = list(files)
        self._append(pid, sid, entry, durable=True)

    def append_agent(self, pid: str, sid: str, text: str, *, turn: str = "") -> None:
        entry: dict = {"t": "agent", "text": text, "ts": _now()}
        if turn:
            entry["turn"] = str(turn)
        self._append(pid, sid, entry, durable=True)

    def append_call(self, pid: str, sid: str, name: str, args, result: str,
                    status: str = "", *, turn: str = "") -> None:
        entry: dict = {"t": "call", "name": name, "args": args,
                       "result": str(result or "")[:4000], "status": status,
                       "ts": _now()}
        if turn:
            entry["turn"] = str(turn)
        self._append(pid, sid, entry)

    def retract(self, pid: str, sid: str, turn: str, *, why: str = "user-stop",
                tools: int = 0) -> bool:
        """把一轮从**模型看到的历史**里撤回。返回是否真的追加了一条。

        撤回是**一次追加**，不是读-改-写：停止是并发动作（人点按钮的线程与正在
        跑的协程是两条线），在这里改旧行就是「撤回」与「记录」抢同一个文件位置 ——
        那正是这份存储最贵的一类故障（两份真相、半行 JSON）。

        幂等：同一轮已经有 retract 记录就返回 ``False``，不追加第二条。
        ``durable=True``：丢了它，重启后那两句会**复活**。
        """
        turn = str(turn or "")
        if not turn:
            return False
        rows, _ = self.read(pid, sid)
        if turn in _retracted_turns(rows):
            return False
        self._append(pid, sid, {"t": "retract", "turn": turn, "ts": _now(),
                                "why": str(why or ""), "tools": int(tools or 0)},
                     durable=True)
        return True

    def append_usage(self, pid: str, sid: str, usage: dict | None) -> None:
        if not usage:
            return
        self._append(pid, sid, {"t": "usage", "in": usage.get("input"),
                                "out": usage.get("output"), "ts": _now()})

    def rename(self, pid: str, sid: str, title: str, *, auto: bool = False) -> str:
        """改名 = 再追一条 meta。**不做读-改-写**：那会开出第二个真相。

        ``auto=True`` 是程序自己取的名字（首句前 24 字）。这个标记必须写进数据：
        撤回之后要「照新的首句重取标题」，而判据只能是「这个名字是不是我取的」——
        靠「标题不等于默认标题」去猜，会把人改过的名字改回去。
        """
        clean = " ".join(str(title or "").split())[:MAX_TITLE]
        if not clean:
            return ""
        entry: dict = {"t": "meta", "sid": sid, "pid": pid,
                       "title": clean, "retitle": _now()}
        if auto:
            entry["auto"] = True
        self._append(pid, sid, entry, durable=True)
        return clean

    def title_once(self, pid: str, sid: str, text: str) -> str:
        """第一次有人说话时，把「新会话」换成一个看得懂的标题。只改默认标题，不覆盖人改过的。

        返回新标题（没改则为空串）—— 调用方拿它更新自己缓存的那份，
        否则界面上会一直显示「新会话」，直到有人重读一遍整份文件。
        """
        if self.meta(pid, sid).get("title") not in ("", DEFAULT_TITLE):
            return ""
        name = auto_title(text)
        return self.rename(pid, sid, name, auto=True) if name else ""

    def retitle_auto(self, pid: str, sid: str) -> str:
        """标题若是**程序取的**、而它的出处被撤回了，就照新的首句重取。

        返回新标题（没改则为空串）。人改过的名字一个字都不动 —— 判据是 meta 里的
        ``auto``，不是「标题长什么样」。
        """
        rows, _ = self.read(pid, sid)
        metas = [r for r in rows if r.get("t") == "meta"]
        if not metas or not metas[-1].get("auto"):
            return ""
        gone = _retracted_turns(rows)
        first = ""
        for r in rows:
            if r.get("t") == "user" and str(r.get("turn") or "") not in gone:
                first = str(r.get("text") or "")
                break
        # 一句都不剩（唯一那一轮被撤回了）→ 退回默认标题。留着旧名字会让人
        # 在会话列表里看见一个「这个会话里已经不存在的问题」。
        want = auto_title(first) or DEFAULT_TITLE
        if want == metas[-1].get("title"):
            return ""
        return self.rename(pid, sid, want, auto=True)

    def drop(self, pid: str, sid: str) -> Path | None:
        """移进 ``.trash/``，不硬删。返回落点；文件本来就不在则返回 None。"""
        src = self._file(pid, sid)
        if not src.is_file():
            return None
        with self._lock:
            dst_dir = self.root / ".trash" / pid
            dst_dir.mkdir(parents=True, exist_ok=True)
            stamp = int(_now() * 1000)
            dst = dst_dir / f"{sid}.{stamp}.jsonl"
            src.replace(dst)
            # 图跟着会话走：只删 jsonl 会留下一堆再也没人引用的字节，
            # 而「删掉的会话」在磁盘上仍然占着空间 —— 那是另一种静默。
            files = self._files_dir(pid, sid)
            if files.is_dir():
                files.replace(dst_dir / f"{sid}.{stamp}.files")
        return dst

    # ---------------------------------------------------------------- 读
    def read(self, pid: str, sid: str) -> tuple[list[dict], int]:
        """读全部条目与坏行数。

        **坏行跳过、不整份丢弃**：一次断电只该丢掉被截断的那一行，
        而不是让人「这个会话打不开」。
        """
        p = self._file(pid, sid)
        if not p.is_file():
            return [], 0
        rows: list[dict] = []
        bad = 0
        try:
            raw = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return [], 1
        for line in raw.splitlines():
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                bad += 1
                continue
            if isinstance(obj, dict) and obj.get("t"):
                rows.append(obj)
            else:
                bad += 1
        return rows, bad

    def meta(self, pid: str, sid: str) -> dict:
        """会话元信息：最后一行的 meta 为准（改名走的是这条路）。"""
        rows, _ = self.read(pid, sid)
        head = next((r for r in rows if r.get("t") == "meta"), {})
        tail = next((r for r in reversed(rows) if r.get("t") == "meta"), {})
        out = {"sid": sid, "pid": pid,
               "title": tail.get("title") or head.get("title") or DEFAULT_TITLE,
               "auto": bool(tail.get("auto")),
               "created": head.get("created") or 0.0,
               "updated": rows[-1].get("ts") if rows else 0.0}
        return out

    def messages(self, pid: str, sid: str, *, include_retracted: bool = True) -> list[dict]:
        """给人看的那一串：``user`` 与 ``agent``，带上 ``sys`` 标记与随行的图。

        ``files`` 是**引用**（``{name, mime, size}``），不是字节：界面拿它拼
        ``/api/p/<pid>/session/<sid>/img/<name>``，模型那一侧由 ``link._ctx``
        从盘上读回来。两处都从这一份引用出发，就不会有第二份真相。

        每条多两个键：``turn``（属于哪一轮，老记录为空）与 ``retracted``
        （这一轮被撤回了）。**一个判据贯穿四个消费者** —— 模型（``history``）、
        界面（``messages``）、计数（``stats``）、连接层（``link._ctx``）——
        不再有第二份「这条算不算被撤回」。``include_retracted=False`` 就是
        「模型看到的那份」：撤回的那一轮整组消失。
        """
        rows, _ = self.read(pid, sid)
        gone = _retracted_turns(rows)
        out: list[dict] = []
        for r in rows:
            if r.get("t") not in CONTEXT_ROLES:
                continue
            turn = str(r.get("turn") or "")
            hit = bool(turn) and turn in gone
            if hit and not include_retracted:
                continue
            out.append({"role": r["t"], "text": r.get("text", ""), "ts": r.get("ts"),
                        "sys": bool(r.get("sys")), "files": list(r.get("files") or []),
                        "turn": turn, "retracted": hit})
        return out

    def history(self, pid: str, sid: str) -> list[aaas.Message]:
        """投影成**模型实际会看到的那份**。只有 user/agent + 纯文本。

        带图的那一份在 ``link._ctx``：它要按 history 的图片预算裁剪（stateless
        下每轮重发全史，图是唯一一处「成本随轮数累积」的口子），而裁剪需要
        知道**哪几张**是最近的 —— 那是连接层的事，不是存储层的事。

        空文本跳过：``Message`` 要求 parts 非空，而一段空回答在上下文里
        只是一个没有信息的空行。
        """
        out: list[aaas.Message] = []
        for m in self.messages(pid, sid, include_retracted=False):
            text = str(m.get("text") or "")
            if not text.strip():
                continue
            out.append(aaas.Message.user(text) if m["role"] == "user"
                       else aaas.Message.agent(text))
        return out

    def stats(self, pid: str, sid: str) -> dict:
        """轮数 / 累计 token。stateless 是**每轮重发全史**，会话长度就是账单 ——
        这个数得让人看得见。"""
        rows, bad = self.read(pid, sid)
        gone = _retracted_turns(rows)
        # 被撤回的那一轮不算「说过一轮」：它从上下文里退回去了，计数跟着退。
        # token 不撤 —— 撤回的是上下文，不是账单（真的花了）。
        turns = sum(1 for r in rows if r.get("t") == "user"
                    and str(r.get("turn") or "") not in gone)
        tin = tout = 0
        for r in rows:
            if r.get("t") == "usage":
                tin += int(r.get("in") or 0)
                tout += int(r.get("out") or 0)
        return {"turns": turns, "tokensIn": tin, "tokensOut": tout, "bad": bad}

    def list(self, pid: str, limit: int = 50) -> tuple[list[dict], bool]:
        """这个程序的全部会话（新的在前）。返回（清单，是否还有更老的）。"""
        d = self._dir(pid)
        if not d.is_dir():
            return [], False
        files = sorted(d.glob("s_*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
        more = len(files) > limit
        rows: list[dict] = []
        for f in files[:limit]:
            sid = f.stem
            m = self.meta(pid, sid)
            s = self.stats(pid, sid)
            rows.append({**m, **s, "size": f.stat().st_size})
        return rows, more

    def latest(self, pid: str) -> str | None:
        """最近改动过的那个会话。程序第一次被打开时用它接上。"""
        d = self._dir(pid)
        if not d.is_dir():
            return None
        files = sorted(d.glob("s_*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
        return files[0].stem if files else None
