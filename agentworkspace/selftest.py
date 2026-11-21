"""验收：一个人能在不看代码的前提下，独立地把每一层跑一遍。

这一版多了一组断言：**上下文隔离**。它不是「提示词写得好不好」，而是
「一个程序的智能体到底拿不到什么」—— 所以它验的是 Link 真的声明了什么。
"""

from __future__ import annotations

import json
import sys
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.request import Request, urlopen

from . import kernel, server
from .link import Hub, Link
from .server import App, serve

WS = Path(__file__).resolve().parent.parent / "workspace"
ok = 0
bad = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global ok, bad
    if cond:
        ok += 1
        print(f"  ok  {name}")
    else:
        bad += 1
        print(f"  !!  {name}   {detail}")


def get(base: str, path: str) -> tuple[int, bytes, dict]:
    req = Request(base + path)
    try:
        with urlopen(req, timeout=10) as r:
            return r.status, r.read(), dict(r.headers)
    except Exception as exc:                     # HTTPError 也是响应
        code = getattr(exc, "code", 0)
        body = getattr(exc, "read", lambda: b"")()
        return code, body, {}


def post(base: str, path: str, body: dict | None = None) -> dict:
    data = json.dumps(body or {}).encode("utf-8")
    req = Request(base + path, data=data, headers={"content-type": "application/json"},
                  method="POST")
    try:
        with urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception as exc:
        payload = getattr(exc, "read", lambda: b"")()
        try:
            return json.loads(payload.decode("utf-8"))
        except Exception:
            return {"status": "error", "text": str(exc)}


def sse_collect(base: str, want: int, out: list, stop: threading.Event) -> None:
    try:
        with urlopen(base + "/api/events", timeout=30) as r:
            for raw in r:
                if stop.is_set() or len(out) >= want:
                    return
                line = raw.decode("utf-8", "replace").strip()
                if line.startswith("data:"):
                    out.append(json.loads(line[5:].strip()))
    except Exception:
        return


def _fixture_note() -> None:
    """工作区里缺了程序时，先把话说在明处。

    这些断言验的是「内核 × 真实程序」的对接（pomodoro 的 4 个能力、memo 的原生窗口）。
    工作区里没有那些程序，它们必然失败 —— 那是**数据缺失**，不是代码坏了。
    把这两件事混在一个「失败」里，下一次真出问题就没人分得清。
    """
    need = ("pomodoro", "memo")
    gone = [p for p in need if not (WS / "plugins" / p).is_dir()]
    if gone:
        print(f"[注意] 工作区里没有 {', '.join(gone)} 这些程序；"
              f"与它们有关的断言会失败 —— 那是工作区数据缺失，不是本轮改坏的。",
              flush=True)
        pdir = WS / "plugins"
        # plugins/ 本身不在时也要如实说出来 —— 那正是这条提示想覆盖的情况，
        # 原来无条件 iterdir() 会先抛 FileNotFoundError，把整个自检掀翻
        here = sorted(x.name for x in pdir.iterdir() if x.is_dir()) if pdir.is_dir() else None
        print(f"       要恢复：从 workspace/.backup/ 拷回，或让小助理重造。"
              f"（当前 plugins/：{here if here is not None else '连 plugins/ 都没有'}）",
              flush=True)


def main() -> int:
    _fixture_note()
    print("[1] 内核：能力面，以及**按程序切开**")
    rt = kernel.Runtime(WS)
    from . import assistant
    msg = assistant.install(rt)
    rt.scan()
    check("内置程序装上了（小助理）", "小助理" in msg, msg)
    caps = rt.capabilities()
    pom = sorted(w for w in caps if w.startswith("pomodoro_"))
    check("pomodoro 的 4 个能力都在", len(pom) == 4, str(pom))
    check("私有函数不暴露（_bump_done）", not any("_bump_done" in w for w in caps))

    mine = rt.capabilities_of("assistant")
    check("小助理手上有造程序的能力", "assistant_plugin_write" in mine, str(sorted(mine)))
    check("小助理有通用门（能操作别的程序）", "assistant_plugin_call" in mine)
    check("小助理没有别人的工具",
          not any(w.startswith(("pomodoro_", "todo_")) for w in mine), str(sorted(mine)))

    check("pomodoro 的上下文里没有手（结构上不能造程序）",
          all(w.startswith("pomodoro_") for w in rt.capabilities_of("pomodoro")),
          str(sorted(rt.capabilities_of("pomodoro"))))
    check("别的程序拿不到「造程序」那只手（dev_plugin_write 是改自己，另一回事）",
          not any(w.endswith("_plugin_write") and not w.startswith("dev_")
                  for pid, p in rt.plugins.items() for w in rt.capabilities_of(pid)),
          "有程序能造别的程序了")

    print("[2] 程序清单：小助理是程序，不是「内核面板」")
    st = rt.state()
    progs = st["programs"]
    a = next((p for p in progs if p["id"] == "assistant"), None)
    check("清单里有小助理", bool(a), str([p["id"] for p in progs]))
    check("小助理没有程序页面（它只有一张嘴）",
          a and a["uiKind"] == "none" and not a["hasUi"], str(a and a["uiKind"]))
    check("清单里没有 internal 的内核条目",
          not any(p.get("internal") for p in progs), str([p.get("id") for p in progs]))

    print("[3] 三态：正常 / 越界 / 越权")
    d = rt.dispatch("pomodoro", "start", {"minutes": 2}, "human")
    check("正常调用 ok", d.status == "ok", d.text)
    d2 = rt.dispatch("pomodoro", "start", {"minutes": 999}, "human")
    check("越界 → rejected（不进函数体）", d2.status == "rejected", d2.text)
    d3 = rt.dispatch("pomodoro", "status", {"zzz": 1}, "agent")
    check("越权字段 → rejected", d3.status == "rejected", d3.text)

    print("[4] 人的门：页面与状态")
    fake = _FakeHub()
    httpd: ThreadingHTTPServer = serve(rt, fake, Path(__file__).parent / "web", port=0)
    port = httpd.server_address[1]
    base = f"http://127.0.0.1:{port}"
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    code, body, _ = get(base, "/")
    html = body.decode("utf-8", "replace")
    check("宿主页 200 且有窄栏与舞台",
          code == 200 and 'id="rail"' in html and 'id="stage"' in html, str(code))
    check("每个程序都有对话面板（结构里有 thread/composer）",
          'id="thread"' in html and 'id="composer"' in html)
    check("界面里没有「N 个能力」这种开发者话术",
          "个能力" not in html and "dispatch" not in html)

    state = json.loads(get(base, "/api/state")[1])
    check("/api/state 给的是 programs", "programs" in state, str(list(state)))
    check("状态里有 pomodoro 且标了 hasUi",
          any(p["id"] == "pomodoro" and p["hasUi"] for p in state["programs"]))
    code, body, hdrs = get(base, "/plugin/pomodoro/")
    check("程序页面 200 且是 html",
          code == 200 and b"<html" in body.lower(), f"{code} {hdrs.get('content-type')}")

    print("[4b] 设置：全系统只有一个设置项 —— aaas-service 地址")
    import tempfile
    from . import settings as st
    from .link import Hub

    def why_bad(raw) -> str | None:
        """能当地址用就返回 None，不能就把那句人话返回。"""
        try:
            st.normalize(raw)
            return None
        except st.BadURL as exc:
            return str(exc)

    check("地址：去空白、协议小写", st.normalize("  WS://localhost:8888/ ") == "ws://localhost:8888/",
          st.normalize("  WS://localhost:8888/ "))
    check("地址：带路径原样保留", st.normalize("wss://h:9/aaas/") == "wss://h:9/aaas/")
    check("地址：IPv6 也行", st.normalize("ws://[::1]:8888/") == "ws://[::1]:8888/")
    check("地址：空 → 报错", why_bad("   ") is not None, str(why_bad("   ")))
    check("地址：没协议 → 报错", why_bad("localhost:8888") is not None, str(why_bad("localhost:8888")))
    check("地址：乱协议 → 报错", why_bad("ftp://x/") is not None, str(why_bad("ftp://x/")))
    check("地址：没有主机 → 报错", why_bad("ws://:8888/") is not None, str(why_bad("ws://:8888/")))
    check("地址：端口不是数字 → 报错", why_bad("ws://x:abc/") is not None, str(why_bad("ws://x:abc/")))

    tmp = Path(tempfile.mkdtemp(prefix="ws-settings-"))
    (tmp / "config.json").write_text(json.dumps({
        "aaas": {"url": "ws://localhost:8888/", "token": None},
        "agent": {"revision": "workspace-v1"},
        "server": {"port": 8777},
    }, ensure_ascii=False), encoding="utf-8")
    h2 = Hub(rt, tmp, st.hub_cfg(tmp))     # 真 Hub：rebind / url 才是被测的那份代码
    check("Hub 报的就是 config.json 里的地址", h2.url == "ws://localhost:8888/", h2.url)
    check("hub_cfg 是唯一的翻译点（带上 revision）",
          st.hub_cfg(tmp)["revision"] == "workspace-v1", str(st.hub_cfg(tmp)))

    saved = server.App.hub
    server.App.hub = h2                       # 这一段只用这一个假池
    try:
        got = json.loads(get(base, "/api/settings")[1])
        check("GET /api/settings 读回当前地址", got.get("url") == "ws://localhost:8888/", str(got))
        r = post(base, "/api/settings", {"url": " ws://127.0.0.1:9900/ "})
        check("保存成功，回执是规整过的地址", r.get("status") == "ok" and r.get("url") == "ws://127.0.0.1:9900/",
              str(r)[:160])
        check("地址真的落盘了",
              json.loads((tmp / "config.json").read_text(encoding="utf-8"))["aaas"]["url"]
              == "ws://127.0.0.1:9900/")
        check("别的键一个没丢（token / revision / port）",
              json.loads((tmp / "config.json").read_text(encoding="utf-8"))["aaas"]["token"] is None
              and st.hub_cfg(tmp)["revision"] == "workspace-v1"
              and json.loads((tmp / "config.json").read_text(encoding="utf-8"))["server"]["port"] == 8777)
        check("保存之后真的换了地址（Hub.rebind 生效）",
              h2.url == "ws://127.0.0.1:9900/", h2.url)
        r = post(base, "/api/settings", {"url": "ftp://x/"})
        check("坏地址被拒（不是 200 的假成功）", r.get("status") == "bad" and "只支持" in str(r.get("error")),
              str(r)[:160])
        check("坏地址没有写盘",
              json.loads((tmp / "config.json").read_text(encoding="utf-8"))["aaas"]["url"]
              == "ws://127.0.0.1:9900/")
        check("/api/state 反映新地址",
              json.loads(get(base, "/api/state")[1])["settings"]["url"] == "ws://127.0.0.1:9900/")
    finally:
        server.App.hub = saved

    hub3 = Hub(rt, tmp, {"url": "ws://a/"})
    check("rebind 真的换掉地址", hub3.url == "ws://a/", hub3.url)
    check("没有连接时 rebind 不炸、也不假装重连了谁",
          hub3.rebind({"url": "ws://b/"}) == [] and hub3.url == "ws://b/", hub3.url)

    print("[5] 执行同一性：人点的按钮与 agent 调用，同一份状态")
    r = post(base, "/api/p/pomodoro/call/start", {"minutes": 7})
    check("人通过 HTTP 调用成功", r.get("status") == "ok", str(r)[:120])
    s = rt.dispatch("pomodoro", "status", {}, "agent")
    check("agent 立刻读到同一份状态（7 分钟）", s.data.get("minutes") == 7, str(s.data))
    r2 = post(base, "/api/p/pomodoro/call/start", {"minutes": 999})
    check("HTTP 通道同样被 schema 挡住", r2.get("status") == "rejected", str(r2)[:120])
    r3 = post(base, "/api/p/pomodoro/call/nope", {})
    check("未知能力 → rejected", r3.get("status") == "rejected", str(r3)[:120])

    print("[6] 对话带 pid：送错门会如实拒绝")
    r = post(base, "/api/p/nosuch/chat", {"text": "在吗"})
    check("没有的程序 → 404", "没有程序" in json.dumps(r, ensure_ascii=False), str(r)[:120])
    r = post(base, "/api/p/pomodoro/chat", {"text": "   "})
    check("空消息 → 拒绝", "空消息" in json.dumps(r, ensure_ascii=False), str(r)[:120])

    print("[7] SSE：人的动作带 source=human 广播出去")
    events: list[dict] = []
    stop = threading.Event()
    threading.Thread(target=sse_collect, args=(base, 4, events, stop), daemon=True).start()
    time.sleep(0.4)
    post(base, "/api/p/pomodoro/call/stop", {})
    time.sleep(1.0)
    stop.set()
    kinds = [e.get("type") for e in events]
    srcs = [e.get("source") for e in events]
    check("收到 call.ok 事件", "call.ok" in kinds, str(kinds))
    check("事件带 source=human（agent 的动作因此可见）", "human" in srcs, str(srcs))

    print("[8] 越权：路径穿越被拒")
    check("穿越 ui 目录 → 非 200", get(base, "/plugin/pomodoro/../../config.json")[0] != 200)
    check("穿越 web 目录 → 非 200", get(base, "/web/../../config.json")[0] != 200)

    print("[9] 程序体检与原生窗口")
    rep = kernel.check_plugin(WS / "plugins" / "pomodoro")
    check("好程序报 ok", rep["ok"] and not rep["errors"], str(rep["errors"]))
    check("网页程序 uiKind=web", rep.get("uiKind") == "web", str(rep.get("uiKind")))
    memo = WS / "plugins" / "memo"
    rep = kernel.check_plugin(memo)
    check("原生程序过静态体检", rep["ok"], str(rep["errors"]))
    check("原生程序 uiKind=native", rep.get("uiKind") == "native", str(rep.get("uiKind")))
    m = next((x for x in json.loads(get(base, "/api/state")[1])["programs"]
              if x["id"] == "memo"), None)
    check("状态里标了 uiKind=native 与 uiName",
          bool(m) and m.get("uiKind") == "native" and m.get("uiName") == "ui/app.py",
          str(m and {k: m.get(k) for k in ("uiKind", "uiName")}))
    r = post(base, "/api/p/memo/call/add", {"text": "从原生窗口来的"})
    check("原生窗口的调用落到同一个 dispatch", r.get("status") == "ok", str(r)[:140])
    s2 = rt.dispatch("memo", "notes", {}, "agent")
    check("agent 立刻读到原生窗口写的数据",
          any("从原生窗口来的" == i.get("text") for i in s2.data.get("items", [])),
          str(s2.data)[:140])
    check("网页程序拒绝开原生窗口", post(base, "/api/p/pomodoro/open", {}).get("status") == "error")

    print("[10] 智能体：一个程序一条，声明的只有自己那部分")
    p_l = Link(rt, WS, {}, "pomodoro")
    tools = p_l._tools()
    names = [t.declaration.name for t in tools]
    check("pomodoro 的声明里全是 pomodoro_*",
          names and all(n.startswith("pomodoro_") for n in names), str(names))
    check("它的指令里没有「造程序」这条路",
          "plugin_write" not in p_l.instructions() and "插件契约" not in p_l.instructions())
    check("它的指令里有自己的名字与能力",
          "番茄钟" in p_l.instructions() and "pomodoro_start" in p_l.instructions(),
          p_l.instructions()[:80])

    a_l = Link(rt, WS, {}, "assistant")
    a_names = [t.declaration.name for t in a_l._tools()]
    check("小助理的声明里有手与提交能力",
          "assistant_plugin_write" in a_names and "assistant_shell" in a_names, str(a_names))
    check("小助理的指令讲的是「怎么造程序」",
          "插件契约" in a_l.instructions() and "单独" in a_l.instructions(),
          a_l.instructions()[:80])

    print("[10b] 提示词长度：协议的硬约束（>=4096 字符整轮失败）")
    from .link import MAX_INSTRUCTIONS, clip_instructions
    a_md = (WS / "prompts" / "assistant.md").read_text(encoding="utf-8")
    check(f"小助理的提示词在 {MAX_INSTRUCTIONS} 字符以内（实测 4096 就被拒）",
          len(a_md) <= MAX_INSTRUCTIONS, f"{len(a_md)} 字符")
    over = [f"{pid}={len(Link(rt, WS, {}, pid).instructions())}"
            for pid in rt.plugins
            if len(Link(rt, WS, {}, pid).instructions()) > MAX_INSTRUCTIONS]
    check("每个程序组装出的提示词都不超限", not over, ", ".join(over))
    clipped, did = clip_instructions("你" * 9000)
    check("超长提示词会被截断（而不是让整轮静默失败）",
          did and len(clipped) == MAX_INSTRUCTIONS, f"{len(clipped)} clipped={did}")

    hub = Hub(rt, WS, {})
    check("智能体池是懒的（没人开程序就一条连接也不建）", hub.status() == {}, str(hub.status()))
    check("给不存在的程序发话 → 不建连接、如实拒绝",
          hub.send("nosuch", "在吗") == "没有程序 nosuch")

    print("[11] 外壳：pywebview 是壳，不是第二条路")
    from . import shell
    check("外壳模块可用（不可用也只是回退浏览器，不影响宿主）",
          isinstance(shell.available(), bool), str(shell.available()))
    src = (Path(__file__).parent / "shell.py").read_text(encoding="utf-8")
    check("外壳不认识内核（只收一个 url）", "kernel" not in src and "dispatch" not in src)

    print("[11b] 窗口装得进工作区（否则底部的输入条永远在屏幕外）")
    import agentworkspace.shell as _sh
    for area, label in (((0, 0, 1440, 860), "1440x860"), ((0, 0, 1024, 728), "1024x728")):
        x, y, w, h = _sh.fit(1180, 800, area)
        check(f"{label} 的桌面上装得下（不留出屏幕外）",
              x >= 0 and y >= 0 and x + w <= area[2] and y + h <= area[3],
              f"x={x} y={y} w={w} h={h}")
    x, y, w, h = _sh.fit(1180, 800, (0, 0, 200, 150))
    check("极端小屏也给出正尺寸（不会算出负数）", w > 0 and h > 0, f"{w}x{h}")

    print("[12] 道的接缝：同一程序里再发一条")
    import asyncio

    import aaas as _aaas

    class _HangCaller:
        """send 返回一个永不结束的 Turn —— 模拟「上一轮还在跑」。"""

        async def send(self, text, instructions=None):
            return _aaas.Turn("t-hang")

        async def cancel(self, *a, **k):
            return None

        async def close(self):
            return None

    link2 = Link(rt, WS, {}, "pomodoro")
    loop = asyncio.new_event_loop()
    threading.Thread(target=loop.run_forever, daemon=True).start()
    link2._loop = loop
    link2._caller = _HangCaller()
    link2.status = "ready"
    r1 = link2.send("第一句")
    # aaas.Turn.done 是 **property**：写成 .done() 会 TypeError，且只在第二次发送时炸
    r2 = link2.send("第二句")
    check("第一句被送出", r1 == "sent", str(r1))
    check("上一轮没结束时再发不重入、也不抛异常", r2 == "上一轮还没结束", str(r2))
    link2._turn = None
    fut = link2._turn_task
    if fut is not None:
        loop.call_soon_threadsafe(fut.cancel)
    time.sleep(0.2)
    loop.call_soon_threadsafe(loop.stop)

    print("[13] 开发者模式：一枚开关，借来的是**改自己**那双手")
    pid = "mood" if "mood" in rt.plugins else next(iter(rt.plugins))
    for one in list(rt.plugins):                # 从干净的开关状态开始，别吃上一轮的残留
        rt.set_dev(one, False)
    check("开关不在程序目录里（它改坏自己时，人还得能把它关掉）",
          rt.dev_path.parent == WS and "plugins" not in rt.dev_path.parts,
          str(rt.dev_path))
    own = sorted(rt.capabilities_of(pid))
    check("关着的时候，一个程序只有自己的能力",
          bool(own) and all(w.startswith(pid + "_") for w in own), str(own))
    check("关着的时候看不见任何一只借来的手",
          not any(w.startswith(("assistant_", "dev_")) for w in own), str(own))

    r = post(base, f"/api/p/{pid}/dev", {"on": True})
    check("人拨开关 → ok 且 dev=True",
          r.get("status") == "ok" and r.get("dev") is True, str(r)[:140])
    dev_caps = sorted(rt.capabilities_of(pid))
    borrowed = [w for w in dev_caps if not w.startswith(pid + "_")]
    check("打开后借到的**恰好**是这五项（多一项都算越界）",
          borrowed == ["dev_plugin_write", "dev_read", "dev_search", "dev_tree", "dev_write"],
          str(borrowed))
    check("借来的手里没有一件能碰别的程序（list / call / delete / 终端）",
          not ({"assistant_plugin_list", "assistant_plugin_call", "assistant_plugin_delete",
                "assistant_shell"} & set(dev_caps)), str(borrowed))
    check("借来手之后自己的能力一项没少", set(own) <= set(dev_caps), str(own))
    check("dev_plugin_write 的参数里没有 pid（改别人在签名上就不可能）",
          "pid" not in rt.devtools["plugin_write"].parameters.get("properties", {}),
          str(sorted(rt.devtools["plugin_write"].parameters.get("properties", {}))))
    other = next((k for k in rt.plugins if k != pid), None)
    if other is None:
        print("  --  跳过「别人的隔离没有被连带打开」：工作区里没有第二个程序", flush=True)
    else:
        check("别人的隔离没有被连带打开",
              not any(w.startswith(("assistant_", "dev_")) for w in rt.capabilities_of(other)),
              other)
    check("开发者工具集不是程序：全局能力表里没有 dev_*",
          not any(w.startswith("dev_") for w in rt.capabilities()), "")
    st_now = json.loads(get(base, "/api/state")[1])
    check("state 带着工具集（工具卡要写清「谁在动手」），但左侧栏里没有它",
          "dev_read" in [f["name"] for f in st_now["devtools"]["functions"]]
          and all(x["id"] != "dev" for x in st_now["programs"]), "")
    check("state 里带着开关状态",
          {x["id"]: x["dev"] for x in
           json.loads(get(base, "/api/state")[1])["programs"]}[pid] is True)
    check("小助理没有这枚开关（它本来就有全部能力）",
          "不需要这个开关" in rt.set_dev("assistant", True))

    dev_prompt = Link(rt, WS, {}, pid).instructions()
    check("换的是自我认知，不只是工具：讲清了怎么改自己",
          "开发者模式" in dev_prompt and "改自己" in dev_prompt, dev_prompt[:60])
    check("开发者模式的提示词也在协议上限内",
          len(dev_prompt) <= MAX_INSTRUCTIONS, f"{len(dev_prompt)} 字符")

    # 底线一：不许用借来的手删掉自己（caller 是这套判定唯一的依据）
    res = rt.dispatch("assistant", "plugin_delete", {"pid": pid}, source="agent", caller=pid)
    check("用手删自己会被拒（删掉就连开关一起没了）",
          res.status == "error" and "删掉自己" in (res.text or ""),
          f"{res.status}: {(res.text or '')[:70]}")
    check("拒绝是拒绝，不是动手：程序还在", pid in rt.plugins)

    # 底线二：改坏了要整份回滚（不然「这一次失败」会变成「下次开机起不来」）
    bp = WS / "plugins" / pid / "backend.py"
    good = bp.read_text(encoding="utf-8")
    try:
        res = rt.dispatch("assistant", "plugin_write",
                          {"pid": pid, "files": {"backend.py": "def broken(: pass\n"}},
                          source="agent", caller=pid)
        check("写坏的 backend 被拒，并说清已回滚",
              res.status == "error" and "回滚" in (res.text or ""), (res.text or "")[:100])
        check("磁盘退回提交前的版本", bp.read_text(encoding="utf-8") == good,
              f"{len(bp.read_text(encoding='utf-8'))} 字符")
        check("回滚把坏的也清了：装载后文本里没有 broken",
              "def broken(" not in bp.read_text(encoding="utf-8"))
    finally:
        # 断言先失败、或后面某步抛了，也不许把这个程序留在坏版本上 ——
        # 它会拖坏**后续每一次**运行
        # 判据不能是 bp.exists()：要是坏写/校验把文件弄没了，正需要复原，
        # 而 exists() 为假反而跳过 —— 那会留下一个没有 backend.py 的程序
        if not bp.exists() or bp.read_text(encoding="utf-8") != good:
            bp.write_text(good, encoding="utf-8")
            rt.scan()

    # 底线三：改程序只有一条路（裸写会绕过四道校验）
    res = rt.dispatch("assistant", "write",
                      {"path": f"plugins/{pid}/backend.py", "content": "x"},
                      source="agent", caller=pid)
    check("裸写程序目录被拒（改程序只走 plugin_write）",
          res.status == "error" and "plugin_write" in (res.text or ""), (res.text or "")[:80])

    # 底线四：借来的手只够得到**自己**，也摸不到「人的东西」
    pj = (WS / "plugins" / pid / "plugin.json").read_text(encoding="utf-8")
    res = rt.dispatch("dev", "plugin_write", {"pid": other, "files": {"plugin.json": pj}},
                      source="agent", caller=pid)
    check("往 dev_plugin_write 里塞别的 pid → rejected（它根本没有这个参数）",
          res.status == "rejected" and "pid" in (res.text or ""),
          f"{res.status}: {(res.text or '')[:60]}")

    before = rt.dev_path.read_text(encoding="utf-8")
    res = rt.dispatch("dev", "write", {"path": "dev.json",
                                       "content": json.dumps({other: True})},
                      source="agent", caller=pid)
    check("dev_write 改不动开关（否则它能替自己或别人把权限拨开）",
          res.status == "error" and "归人" in (res.text or ""),
          f"{res.status}: {(res.text or '')[:70]}")
    check("开关文件一个字节没变", rt.dev_path.read_text(encoding="utf-8") == before, "")

    res = rt.dispatch("dev", "write", {"path": f"plugins/{pid}/backend.py", "content": "x"},
                      source="agent", caller=pid)
    check("dev_write 也不能裸写程序目录（改自己同样要过四道校验）",
          res.status == "error" and "plugin_write" in (res.text or ""), (res.text or "")[:70])

    bp2 = WS / "plugins" / pid / "backend.py"
    good2 = bp2.read_text(encoding="utf-8")
    res = rt.dispatch("dev", "plugin_write",
                      {"files": {"backend.py": "def broken(: pass\n"}},
                      source="agent", caller=pid)
    check("改自己写坏了 → 整份回滚，磁盘回到提交前",
          res.status == "error" and "回滚" in (res.text or "")
          and bp2.read_text(encoding="utf-8") == good2, (res.text or "")[:80])

    res = rt.dispatch("dev", "plugin_write", {"files": {"plugin.json": pj}},
                      source="agent", caller=pid)
    check("改自己成功时说得清「改的是你自己」（同一条落盘路径）",
          res.status == "ok" and "你自己" in (res.text or ""),
          f"{res.status}: {(res.text or '')[:60]}")

    rt.set_dev(pid, False)
    check("关掉之后手立刻收回",
          not any(w.startswith(("assistant_", "dev_")) for w in rt.capabilities_of(pid)),
          str(sorted(rt.capabilities_of(pid))))
    res = rt.dispatch("dev", "read", {"path": "plugins"}, source="agent", caller=pid)
    check("关掉之后硬调 dev_* 也被拒（不等下一轮重声明）",
          res.status == "rejected" and "开发者模式" in (res.text or ""),
          f"{res.status}: {(res.text or '')[:60]}")

    print("[14] 界面：工具默认折叠、两栏能拖 —— 断的是结构，不是手感")
    html = (Path(__file__).resolve().parent / "web" / "index.html").read_text(encoding="utf-8")
    check("工具组默认收起（.tlist 默认 display:none，展开才 block）",
          ".tlist{display:none" in html and ".tgroup.open .tlist{display:block}" in html, "")
    check("收起时也有**一行**摘要（N 次工具调用）", "次工具调用" in html, "")
    check("单项的返回正文默认也收起（.tool .r 默认 display:none）",
          ".tool .r{display:none" in html and ".tool.open .r{display:block}" in html, "")
    check("展开状态存在消息模型里，重绘不丢（gTouched / gOpen / t.open）",
          all(k in html for k in ("gTouched", "gOpen", "t.open")), "")
    check("正在跑的那一轮自动展开（跑完自动收起）",
          "!!S.busy[S.pid] && m === list[list.length - 1]" in html, "")
    check("点击走委托（thread 每次重绘都换掉，逐个绑会全部失效）",
          "$('#stage').addEventListener('click', onThreadClick)" in html, "")
    check("重绘只在贴底时跟随滚动（人往上翻就不会被拽回来）",
          "const atBottom = el.scrollTop + el.clientHeight" in html
          and "if (atBottom) el.scrollTop" in html, "")
    check("两栏之间有抓手，宽度走一个变量", 'id="gutter"' in html and "--chatw" in html, "")
    check("宽度夹在 300 与「舞台宽-320」之间（拖动/键盘/缩放共用同一个纯函数）",
          "function clampChatW" in html and "stageW || 0) - 320" in html, "")
    check("宽度按程序记住（localStorage，刷新与切程序都还在）",
          "ws.chatw" in html and "${pid}" in html and "localStorage.setItem(`${CHATW_KEY}.${pid}`" in html, "")
    check("只有对话的程序没有抓手（否则是一根拖不动的缝）",
          "const gutter = solo ? '' :" in html, "")

    httpd.shutdown()
    if getattr(App, "native", None):
        App.native.shutdown()                       # 收尸：不留孤儿窗口
    print(f"\n{'全绿' if bad == 0 else '有失败'}：{ok} 通过 / {bad} 失败")
    return 0 if bad == 0 else 1


class _FakeHub:
    """selftest 不连 aaas：门只需要一个「智能体池」的形状。"""

    def __init__(self, root: Path | None = None) -> None:
        self.root = Path(root or ".")          # 设置那条路由要用它落盘
        self.cfg: dict = {}
        self.rebound: list[dict] = []

    @property
    def url(self) -> str:
        return str(self.cfg.get("url") or "")

    def rebind(self, cfg: dict) -> list[str]:
        self.cfg = dict(cfg)
        self.rebound.append(dict(cfg))
        return []

    def status(self) -> dict:
        return {}

    def ensure(self, pid: str) -> dict:
        return {"pid": pid, "status": "offline"}

    def send(self, pid: str, text: str) -> str:
        return "sent"

    def cancel(self, pid: str) -> str:
        return "noop"

    def stop(self) -> None:
        pass


if __name__ == "__main__":
    sys.exit(main())
