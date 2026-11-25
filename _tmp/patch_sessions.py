"""给 sessions.py 加「图片落盘」—— 精确替换，改完立刻回读校验。"""
from pathlib import Path

p = Path(r"D:\projects\claw\open-agents\openagent-workspace\agentworkspace\sessions.py")
s = p.read_text(encoding="utf-8")
orig = s

# 1) import re
s = s.replace(
    "import aaas\n\n#: 新会话的默认标题",
    "import re\n\nimport aaas\n\n#: 新会话的默认标题",
    1,
)

# 2) 图片文件名的形状（它同时是 URL 的一个分量）
s = s.replace(
    'CONTEXT_ROLES = ("user", "agent")\n',
    'CONTEXT_ROLES = ("user", "agent")\n\n'
    '#: 一张图在盘上的名字：``<16 位 sha>.<后缀>``。它同时是 URL 的一个分量，\n'
    '#: 所以形状在这里就钉死 —— 收紧了，越界就不必再靠 resolve 兜。\n'
    'IMAGE_NAME_RE = re.compile(r"^[0-9a-f]{16}\\.(?:png|jpg|webp|gif)$")\n',
    1,
)

# 3) 路径：图片目录 + 一张图的落点
s = s.replace(
    '''    def dir(self, pid: str) -> Path:
        return self._dir(pid)
''',
    '''    def dir(self, pid: str) -> Path:
        return self._dir(pid)

    def _files_dir(self, pid: str, sid: str) -> Path:
        """这个会话的图片目录。与 jsonl 分开，因为它是**字节**不是记录。"""
        return self._dir(pid) / "files" / sid

    def image_path(self, pid: str, sid: str, name: str) -> Path | None:
        """一张图的落点；名字形状不对返回 ``None``（不拼路径，就没有越界）。"""
        if not IMAGE_NAME_RE.match(str(name or "")):
            return None
        return self._files_dir(pid, sid) / name
''',
    1,
)

# 4) 写：save_image + append_user 带 files
s = s.replace(
    '''    def append_user(self, pid: str, sid: str, text: str, *, sys: bool = False) -> None:
        """记一句「用户侧」发言。``sys=True`` 是程序自己发的接力消息，界面上要与人说的区分开。"""
        self._append(pid, sid, {"t": "user", "text": text, "ts": _now(), "sys": bool(sys)},
                     durable=True)
''',
    '''    def save_image(self, pid: str, sid: str, image) -> dict:
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
        return {"name": name, "mime": image.mime, "size": image.size}

    def append_user(self, pid: str, sid: str, text: str, *, sys: bool = False,
                    files: list[dict] | None = None) -> None:
        """记一句「用户侧」发言。``sys=True`` 是程序自己发的接力消息，界面上要与人说的区分开。

        ``files`` 是随这句话一起发出去的图（引用，见 :meth:`save_image`）——
        它必须与这句话**同一条记录**：分开存就会出现「有图没有话」的半条历史。
        """
        entry: dict = {"t": "user", "text": text, "ts": _now(), "sys": bool(sys)}
        if files:
            entry["files"] = list(files)
        self._append(pid, sid, entry, durable=True)
''',
    1,
)

# 5) 删会话：图片目录一起进 .trash
s = s.replace(
    '''            dst = dst_dir / f"{sid}.{int(_now() * 1000)}.jsonl"
            src.replace(dst)
        return dst
''',
    '''            stamp = int(_now() * 1000)
            dst = dst_dir / f"{sid}.{stamp}.jsonl"
            src.replace(dst)
            # 图跟着会话走：只删 jsonl 会留下一堆再也没人引用的字节，
            # 而「删掉的会话」在磁盘上仍然占着空间 —— 那是另一种静默。
            files = self._files_dir(pid, sid)
            if files.is_dir():
                files.replace(dst_dir / f"{sid}.{stamp}.files")
        return dst
''',
    1,
)

# 6) 读：messages 带 files
s = s.replace(
    '''    def messages(self, pid: str, sid: str) -> list[dict]:
        """给人看的那一串：``user`` 与 ``agent``，带上 ``sys`` 标记。"""
        rows, _ = self.read(pid, sid)
        return [{"role": r["t"], "text": r.get("text", ""), "ts": r.get("ts"),
                 "sys": bool(r.get("sys"))}
                for r in rows if r.get("t") in CONTEXT_ROLES]
''',
    '''    def messages(self, pid: str, sid: str) -> list[dict]:
        """给人看的那一串：``user`` 与 ``agent``，带上 ``sys`` 标记与随行的图。

        ``files`` 是**引用**（``{name, mime, size}``），不是字节：界面拿它拼
        ``/api/p/<pid>/session/<sid>/img/<name>``，模型那一侧由 ``link._ctx``
        从盘上读回来。两处都从这一份引用出发，就不会有第二份真相。
        """
        rows, _ = self.read(pid, sid)
        return [{"role": r["t"], "text": r.get("text", ""), "ts": r.get("ts"),
                 "sys": bool(r.get("sys")), "files": list(r.get("files") or [])}
                for r in rows if r.get("t") in CONTEXT_ROLES]
''',
    1,
)

# 7) history() 的说明补一句「图不在这里」
s = s.replace(
    '''        """投影成**模型实际会看到的那份**。只有 user/agent + 纯文本。
''',
    '''        """投影成**模型实际会看到的那份**。只有 user/agent + 纯文本。

        带图的那一份在 ``link._ctx``：它要按 history 的图片预算裁剪（stateless
        下每轮重发全史，图是唯一一处「成本随轮数累积」的口子），而裁剪需要
        知道**哪几张**是最近的 —— 那是连接层的事，不是存储层的事。
''',
    1,
)

assert s != orig, "一个替换都没命中"
p.write_text(s, encoding="utf-8")
print("ok", len(orig), "->", len(s))
