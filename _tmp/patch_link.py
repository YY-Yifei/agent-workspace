"""给 link.py 接上图片：预检 → 落盘 → 拼 Message → 进历史 → 下一轮读回来。"""
from pathlib import Path

p = Path(r"D:\projects\claw\open-agents\openagent-workspace\agentworkspace\link.py")
s = p.read_text(encoding="utf-8")
orig = s


def sub(old, new, count=1):
    global s
    assert old in s, f"没命中：{old[:70]!r}"
    s = s.replace(old, new, count)


# 1) import media
sub("from . import sessions\n", "from . import media, sessions\n")

# 2) send：把死参数变成真的
sub(
    '''        """把人的一句话送进去。立刻返回，事件经总线流回前端。

        **首句不能丢。**''',
    '''        """把人的一句话送进去（可以带图）。立刻返回，事件经总线流回前端。

        **首句不能丢。**''',
)
sub(
    '''        with self._send_lock:                      # 「检查 + 置位」必须是一步
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
                    self._open_turn(msg), self._loop)''',
    '''        with self._send_lock:                      # 「检查 + 置位」必须是一步
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
                    self._open_turn(text, pics=pics, files=files), self._loop)''',
)

# 3) 两个查询：这条连接收不收图 / 它自报的图片上限
sub(
    '''    def _turn_done(self, fut) -> None:''',
    '''    def _no_vision_reason(self) -> str:
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

    def _turn_done(self, fut) -> None:''',
)

# 4) _open_turn / _one_turn 带上图
sub(
    '''    async def _open_turn(self, text: str) -> None:
        """跑一轮；若本轮内能力面变过，就**接力**跑下一轮。''',
    '''    async def _open_turn(self, text: str, *, pics=(), files=()) -> None:
        """跑一轮；若本轮内能力面变过，就**接力**跑下一轮。''',
)
sub(
    """            final = await self._one_turn(text)
            while""",
    """            final = await self._one_turn(text, pics=pics, files=files)
            while""",
)
# 接力那一轮是程序自己发的，不带图
sub(
    """    async def _one_turn(self, text: str, *, sys: bool = False):""",
    """    async def _one_turn(self, text: str, *, pics=(), files=(), sys: bool = False):""",
)
sub(
    '''        base = self._ctx()
        try:
            turn = await self._caller.send(text, instructions=self.instructions(), history=base)''',
    '''        base = self._ctx()
        # 没有图时**原样传字符串**：这条路上一切照旧（SDK 自己包成 Message）。
        msg: str | aaas.Message = media.message(text, list(pics)) if pics else text
        try:
            turn = await self._caller.send(msg, instructions=self.instructions(), history=base)''',
)
sub(
    """        self._record_user(text, sys=sys)""",
    """        self._record_user(text, sys=sys, files=files)""",
)

# 5) _ctx：从盘上读回图，并按 history 的两道账裁剪
sub(
    '''    def _ctx(self) -> list[aaas.Message]:
        """当前会话投影成模型会看到的那份。空文本跳过（Message 要求 parts 非空）。"""
        out: list[aaas.Message] = []
        for h in self.history:
            t = str(h.get("text") or "")
            if not t.strip():
                continue
            out.append(aaas.Message.user(t) if h.get("role") == "user"
                       else aaas.Message.agent(t))
        return out
''',
    '''    def _ctx(self) -> list[aaas.Message]:
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
''',
)

# 6) _record_user 记下随行的图
sub(
    '''    def _record_user(self, text: str, *, sys: bool = False) -> None:
        self.store.append_user(self.pid, self.sid, text, sys=sys)
        new_title = self.store.title_once(self.pid, self.sid, text)
        if new_title:
            self.title = new_title
        self.history.append({"role": "user", "text": text, "ts": time.time(), "sys": sys})
        self._stat["turns"] = self._stat.get("turns", 0) + 1
''',
    '''    def _record_user(self, text: str, *, sys: bool = False, files=()) -> None:
        refs = list(files or ())
        self.store.append_user(self.pid, self.sid, text, sys=sys, files=refs)
        new_title = self.store.title_once(self.pid, self.sid, text)
        if new_title:
            self.title = new_title
        entry: dict = {"role": "user", "text": text, "ts": time.time(), "sys": sys}
        if refs:
            entry["files"] = refs
        self.history.append(entry)
        self._stat["turns"] = self._stat.get("turns", 0) + 1
''',
)

# 7) Hub.send 透传图
sub(
    '''    def send(self, pid: str, text: str) -> str:
        l = self.link(pid, create=True)
        if l is None:
            return f"没有程序 {pid}"
        return l.send(text)
''',
    '''    def send(self, pid: str, text: str, images: list[dict] | None = None) -> str:
        """把一句话（可以带图）交给那个程序。``images`` 是 ``[{name, mime, data}]``，
        ``data`` 是 base64 —— 界面那一侧就这样把它交过来的。"""
        l = self.link(pid, create=True)
        if l is None:
            return f"没有程序 {pid}"
        return l.send(text, images=images)
''',
)

assert s != orig
p.write_text(s, encoding="utf-8")
print("ok", len(orig), "->", len(s))
