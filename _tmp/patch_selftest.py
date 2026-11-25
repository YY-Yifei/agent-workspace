"""selftest：给图片这条路加门（先红后绿的那一端）。"""
from pathlib import Path

p = Path(r"D:\projects\claw\open-agents\openagent-workspace\agentworkspace\selftest.py")
s = p.read_text(encoding="utf-8")
orig = s


def sub(old, new, count=1):
    global s
    assert old in s, f"没命中：{old[:80]!r}"
    s = s.replace(old, new, count)


# 1) 替身池把收到的图记下来 —— 路由门要能看见「交下去了没有」
sub(
    """    def send(self, pid: str, text: str) -> str:
        return "sent"
""",
    """    def send(self, pid: str, text: str, images=None) -> str:
        self.sent.append({"pid": pid, "text": text, "images": list(images or ())})
        return "sent"
""",
)
sub(
    """        self._rt = runtime
        self._pid = pid
        self._link = None
""",
    """        self._rt = runtime
        self._pid = pid
        self._link = None
        self.sent: list[dict] = []          # 门把话说给池子之后，池子看见了什么
""",
)

# 2) 新的一节：图片
sub(
    """    httpd.shutdown()
    if getattr(App, "native", None):""",
    '''    print("[H] 图片：预检 → 落盘 → 进历史 → 每一轮都还在")

    import base64 as _b64
    import struct as _struct
    import zlib as _zlib

    from . import media as _media

    def _png(w: int = 1, h: int = 1) -> bytes:
        """一张**真** PNG。不是随便几个字节贴个魔数 —— sniff/size 都要吃得下，
        而服务端那道 inspect_image 读的正是 IHDR。"""
        def chunk(tag: bytes, data: bytes) -> bytes:
            body = _struct.pack(">I", len(data)) + tag + data
            return body + _struct.pack(">I", _zlib.crc32(tag + data) & 0xFFFFFFFF)

        ihdr = _struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0)
        rows = b"".join(b"\\x00" + b"\\x00\\x00\\x00\\x00" * w for _ in range(h))
        return (b"\\x89PNG\\r\\n\\x1a\\n" + chunk(b"IHDR", ihdr)
                + chunk(b"IDAT", _zlib.compress(rows)) + chunk(b"IEND", b""))

    PNG = _png()
    B64 = _b64.b64encode(PNG).decode("ascii")

    # H1：预检四拒 —— 每一拒都要是一句**人话**，且整批不发
    got, why = _media.prepare([{"name": "a.txt", "data": _b64.b64encode(b"hello").decode()}])
    check("H1 不是图片就明说（不是抛异常、不是静默发出去）",
          not got and "不是图片" in why, why)
    got, why = _media.prepare([{"name": "big.png", "data": B64}], max_bytes=10)
    check("H2 超过上限时说清「多大 / 上限多少」", not got and "上限" in why and "先缩小" in why, why)
    got, why = _media.prepare([{"name": "a.png", "data": B64}] * (MAX_PICS := 9))
    check("H3 超过张数就整批不发（不发一半，让人以为都到了）",
          not got and "最多 8 张" in why, why)
    got, why = _media.prepare([{"name": "a.png", "data": "!!!not base64!!!"}])
    check("H4 坏 base64 也是一句人话", not got and "base64" in why, why)
    got, why = _media.prepare([{"name": "a.png", "mime": "image/jpeg", "data": B64}])
    check("H5 自称的后缀被**实际**纠正（服务端只收自洽的帧）",
          bool(got) and not why and got[0].mime == "image/png" and got[0].name.endswith(".png"),
          f"{why or got[0].name if got else why}")
    got, why = _media.prepare([{"name": "a.png", "data": "data:image/png;base64," + B64}])
    check("H6 整段 data URL 也能吃（切掉前缀，不把逗号后面当坏编码）",
          bool(got) and not why, why)

    # H7-H10：真的走一遍 Link.send —— 这一节不连 aaas，替身 caller 只记下发了什么
    import aaas as _aaas

    class _PicTurn:
        def __init__(self) -> None:
            self.turn_id = "t-pic"
            self.text = ""

        @property
        def done(self) -> bool:
            return self._done

        _done = False

        def __aiter__(self):
            async def _gen():
                if False:                     # 只为了把它写成异步生成器
                    yield None
            return _gen()

        async def end(self):
            self._done = True

            class _Final:
                status = "completed"
                finish_reason = "stop"
                usage = None
                error = None

            return _Final()

    class _PicCaller:
        """`part.file` 协商与否是这条替身的**唯一**参数 —— 特性门要能判到它。"""

        def __init__(self, *, vision: bool, max_bytes: int = 4 << 20) -> None:
            self.sent: list = []
            self._cr = _CR(vision, max_bytes)
            self.connect_result = self._cr

        async def send(self, message, instructions=None, history=None):
            self.sent.append({"message": message, "history": history})
            return _PicTurn()

        async def declare(self, definition, tools=()):
            return "rev-pic", [t.declaration.name for t in tools]

        async def close(self) -> None:
            return None

    class _CR:
        def __init__(self, vision: bool, max_bytes: int) -> None:
            self.features = ("functions", "turn.cancel") + (("part.file",) if vision else ())
            self.limits = {"maxFileBytes": max_bytes if vision else 0}
            self.session_id = "sess-pic"
            self.history_mode = "stateless"

        def has(self, feature: str) -> bool:
            return feature in self.features

    class _PicRT:
        """Link 要的最小 Runtime：没有能力、事件只留痕。"""

        version = 0

        def __init__(self) -> None:
            self.kinds: list[str] = []

        def capabilities_of(self, pid):
            return {}

        def program(self, pid):
            return object()

        def dev_mode(self, pid):
            return False

        def publish(self, kind, **kw):
            self.kinds.append(kind)

        def dispatch(self, *a, **kw):
            raise AssertionError("这一节不执行工具")

    def _ready_link(tmp: Path, caller) -> "link.Link":
        l = link.Link(_PicRT(), tmp, {"url": "ws://x"}, "assistant")
        l._caller = caller
        l.status = "ready"
        loop = asyncio.new_event_loop()
        threading.Thread(target=loop.run_forever, daemon=True).start()
        l._loop = loop
        l._pic_loop = loop                    # 收尾要用它
        return l

    def _wait_idle(l, secs: float = 5.0) -> None:
        end = time.time() + secs
        while time.time() < end and l._busy:
            time.sleep(0.02)

    tmp_pic = Path(tempfile.mkdtemp(prefix="aw-pic-"))
    try:
        l1 = _ready_link(tmp_pic, _PicCaller(vision=True))
        st = l1.send("这是什么", images=[{"name": "a.png", "mime": "image/png", "data": B64}])
        _wait_idle(l1)
        sent = l1._caller.sent
        msg = sent[0]["message"] if sent else None
        files = [p.file for p in getattr(msg, "parts", ()) if getattr(p, "file", None)]
        check("H7 传了图就必须**真的**发出去（images 不是死参数）",
              st == "sent" and len(files) == 1, f"{st} / {type(msg).__name__}")
        check("H7b 一句话里既有字也有图（不是拆成两句）",
              isinstance(msg, _aaas.Message)
              and any(p.text == "这是什么" for p in msg.parts),
              str([type(p.text).__name__ if p.text else "file" for p in msg.parts]))
        check("H7c 字节原样到了对面（不是占位、不是空 base64）",
              bool(files) and files[0].bytes() == PNG,
              f"{len(files[0].data) if files else 0} 字符")

        # 落盘 → 下一轮读回来：stateless 每轮重发全史，图必须还在
        ctx = l1._ctx()
        ctx_files = [p.file for m in ctx for p in m.parts if getattr(p, "file", None)]
        check("H8 第二轮的 history 里图还在（模型不会忘掉刚看过的图）",
              len(ctx_files) == 1 and ctx_files[0].bytes() == PNG, f"{len(ctx_files)} 张")
        on_disk = list((tmp_pic / "sessions" / "assistant" / "files" / l1.sid).glob("*.png"))
        check("H8b 盘上真的存了一份（jsonl 只有引用，字节在 files/）",
              len(on_disk) == 1, f"{len(on_disk)} 个文件")
        rows = l1.store.messages("assistant", l1.sid)
        check("H8c 记录里是引用而不是 base64（对话记录还得能一眼读）",
              bool(rows) and isinstance(rows[-1].get("files"), list)
              and len(rows[-1]["files"]) == 1
              and set(rows[-1]["files"][0]) >= {"name", "mime", "size", "title"},
              str(rows[-1].get("files") if rows else None))

        # 只有图、没有字也是一句话
        l1.send("", images=[{"name": "b.png", "mime": "image/png", "data": B64}])
        _wait_idle(l1)
        only = l1._caller.sent[-1]["message"] if len(l1._caller.sent) > 1 else None
        check("H9 只有图没有字也发得出去（省略式的一句话）",
              isinstance(only, _aaas.Message)
              and all(p.file is not None for p in only.parts), str(only))

        # 特性门：没协商 part.file 就**不发**，且说人话
        l2 = _ready_link(tmp_pic, _PicCaller(vision=False))
        st2 = l2.send("看图", images=[{"name": "a.png", "data": B64}])
        check("H10 没协商 part.file 时：明说「不收图」，且一次都没发出去",
              st2 != "sent" and "不收图" in st2 and not l2._caller.sent,
              f"{st2} / 发了 {len(l2._caller.sent)} 次")
        check("H10b 这句话得给出下一步（换文字描述 / 换模型）",
              "文字" in st2 and ("模型" in st2 or "换一个" in st2), st2)

        # 历史预算：超过 16 张时从**最老的**开始不重发
        ref = {"name": f"{_media.Image(name='x.png', mime='image/png', raw=PNG).sha}.png",
               "mime": "image/png", "size": len(PNG), "title": "x.png"}
        for i in range(20):
            l1._record_user(f"第 {i} 张", files=[dict(ref)])
        l1._load(l1.sid)
        kept = [p.file for m in l1._ctx() for p in m.parts if getattr(p, "file", None)]
        check(f"H11 history 里的图按 {_media.HISTORY_IMAGES} 张封顶（超了整轮会被 -32602 拒）",
              len(kept) == _media.HISTORY_IMAGES, f"{len(kept)} 张")
        check("H11b 留下的必须是**最近的**那些（老的先让位）",
              len(l1._ctx()) >= _media.HISTORY_IMAGES and
              any("第 19 张" == m.text for m in l1._ctx()), "最新那条不见了")

        # 收尾：那几个事件循环是 daemon 线程上的，停掉不留着
        for l in (l1, l2):
            lp = getattr(l, "_pic_loop", None)
            if lp is not None:
                lp.call_soon_threadsafe(lp.stop)
    finally:
        shutil.rmtree(tmp_pic, ignore_errors=True)

    # H12-H14：人的门 —— chat 路由得把图交下去，别在半路吃掉
    j = post(base, "/api/p/pomodoro/chat",
             {"text": "这张呢", "images": [{"name": "a.png", "mime": "image/png", "data": B64}]})
    got_pic = fake.sent[-1] if fake.sent else {}
    check("H12 chat 路由把 images 交给了池子（没有在半路吃掉）",
          j.get("status") == "sent" and len(got_pic.get("images") or ()) == 1,
          f"{j.get('status')} / {len(got_pic.get('images') or ())} 张")
    j2 = post(base, "/api/p/pomodoro/chat",
              {"text": "   ", "images": [{"name": "a.png", "data": B64}]})
    check("H13 只有图不算空消息（空消息的判据是两样都没有）",
          j2.get("status") == "sent", str(j2))
    j3 = post(base, "/api/p/pomodoro/chat", {"text": "   ", "images": []})
    check("H13b 两样都没有才是空消息", "空消息" in str(j3.get("error") or ""), str(j3))
    code_img, _body, _h = get(base, f"/api/p/pomodoro/session/s_00000000/img/{'0' * 16}.png")
    check("H14 图端点存在的形状是 404 而不是 200（pid/sid/名字三段都判过）",
          code_img == 404, str(code_img))

    # H15：界面真的接了三条入口，并且气泡里画得出缩略图
    check("H15 界面有粘贴 / 拖入 / 选文件三条入口（同一个列表）",
          "addEventListener('paste'" in html and "ondrop" in html and 'id="pickf"' in html,
          "")
    check("H15b 气泡里按引用拼图（而不是把 base64 塞进历史）",
          "/img/${f.name}" in html and "shotsHtml" in html, "")

    httpd.shutdown()
    if getattr(App, "native", None):''',
)

assert s != orig
p.write_text(s, encoding="utf-8")
print("ok", len(orig), "->", len(s))
