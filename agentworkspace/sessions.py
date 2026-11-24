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

import aaas

#: 新会话的默认标题。第一次有人说话时会被自动改成首句前 24 字。
DEFAULT_TITLE = "新会话"
MAX_TITLE = 60

#: 投影进模型上下文的两种 role。``sys`` 只是给人看的分流标记，不改 role ——
#: 接力消息（``[系统] 能力面已更新…``）当时确实是以 user 身份被模型看见的，
#: 现在把它改成别的 role，等于伪造一段没发生过的对话。
CONTEXT_ROLES = ("user", "agent")


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

    def append_user(self, pid: str, sid: str, text: str, *, sys: bool = False) -> None:
        """记一句「用户侧」发言。``sys=True`` 是程序自己发的接力消息，界面上要与人说的区分开。"""
        self._append(pid, sid, {"t": "user", "text": text, "ts": _now(), "sys": bool(sys)},
                     durable=True)

    def append_agent(self, pid: str, sid: str, text: str) -> None:
        self._append(pid, sid, {"t": "agent", "text": text, "ts": _now()}, durable=True)

    def append_call(self, pid: str, sid: str, name: str, args, result: str,
                    status: str = "") -> None:
        self._append(pid, sid, {"t": "call", "name": name, "args": args,
                                "result": str(result or "")[:4000], "status": status,
                                "ts": _now()})

    def append_usage(self, pid: str, sid: str, usage: dict | None) -> None:
        if not usage:
            return
        self._append(pid, sid, {"t": "usage", "in": usage.get("input"),
                                "out": usage.get("output"), "ts": _now()})

    def rename(self, pid: str, sid: str, title: str) -> str:
        """改名 = 再追一条 meta。**不做读-改-写**：那会开出第二个真相。"""
        clean = " ".join(str(title or "").split())[:MAX_TITLE]
        if not clean:
            return ""
        self._append(pid, sid, {"t": "meta", "sid": sid, "pid": pid,
                                "title": clean, "retitle": _now()}, durable=True)
        return clean

    def title_once(self, pid: str, sid: str, text: str) -> str:
        """第一次有人说话时，把「新会话」换成一个看得懂的标题。只改默认标题，不覆盖人改过的。

        返回新标题（没改则为空串）—— 调用方拿它更新自己缓存的那份，
        否则界面上会一直显示「新会话」，直到有人重读一遍整份文件。
        """
        if self.meta(pid, sid).get("title") not in ("", DEFAULT_TITLE):
            return ""
        name = auto_title(text)
        return self.rename(pid, sid, name) if name else ""

    def drop(self, pid: str, sid: str) -> Path | None:
        """移进 ``.trash/``，不硬删。返回落点；文件本来就不在则返回 None。"""
        src = self._file(pid, sid)
        if not src.is_file():
            return None
        with self._lock:
            dst_dir = self.root / ".trash" / pid
            dst_dir.mkdir(parents=True, exist_ok=True)
            dst = dst_dir / f"{sid}.{int(_now() * 1000)}.jsonl"
            src.replace(dst)
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
               "created": head.get("created") or 0.0,
               "updated": rows[-1].get("ts") if rows else 0.0}
        return out

    def messages(self, pid: str, sid: str) -> list[dict]:
        """给人看的那一串：``user`` 与 ``agent``，带上 ``sys`` 标记。"""
        rows, _ = self.read(pid, sid)
        return [{"role": r["t"], "text": r.get("text", ""), "ts": r.get("ts"),
                 "sys": bool(r.get("sys"))}
                for r in rows if r.get("t") in CONTEXT_ROLES]

    def history(self, pid: str, sid: str) -> list[aaas.Message]:
        """投影成**模型实际会看到的那份**。只有 user/agent + 纯文本。

        空文本跳过：``Message`` 要求 parts 非空，而一段空回答在上下文里
        只是一个没有信息的空行。
        """
        out: list[aaas.Message] = []
        for m in self.messages(pid, sid):
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
        turns = sum(1 for r in rows if r.get("t") == "user")
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
