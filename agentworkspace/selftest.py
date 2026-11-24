"""验收：一个人能在不看代码的前提下，独立地把每一层跑一遍。

这一版多了一组断言：**上下文隔离**。它不是「提示词写得好不好」，而是
「一个程序的智能体到底拿不到什么」—— 所以它验的是 Link 真的声明了什么。
"""

from __future__ import annotations

import ast
import json
import re
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


def _raw_get(base: str, path: str) -> tuple[int, dict]:
    """一次**不跟随重定向**的 GET —— 302 本身就是要验的东西。"""
    import http.client
    from urllib.parse import urlsplit

    u = urlsplit(base)
    c = http.client.HTTPConnection(u.hostname, u.port, timeout=10)
    try:
        c.request("GET", path)
        r = c.getresponse()
        r.read()
        return r.status, {k.lower(): v for k, v in r.getheaders()}
    finally:
        c.close()


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

    print("[4c] 界面入口：plugin.json 说什么，门就开什么")
    # 这一节的病根是「同一条事实算两遍」：Plugin.ui 读 plugin.json 的 ui，
    # 而 /plugin/<pid>/ 从前自己拼 "ui/index.html"。入口一旦不叫 index.html，
    # 内核认、门不认 —— 界面上是 iframe 里一段 JSON，白板且不报错。
    import shutil
    import tempfile as _tf

    tmp_ws = Path(_tf.mkdtemp(prefix="uiws-"))

    def _mk(rel: str, text: str) -> None:
        f = tmp_ws / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text, encoding="utf-8")

    # serve() 把 App.runtime / hub / web / native 写成**类级全局** —— 那是门对
    # 内核的唯一引用。这里开的是第二个门，收工必须原样还回去：否则后面每一节
    # 都在对着 tmp_ws 说话，而那会伪装成完全不相干的 bug（我第一次跑就崩在
    # [13] 的 KeyError: 'dingtalk'，看起来一点不像入口路由的问题）。
    saved_app = (App.runtime, App.hub, App.web, App.native)
    try:
        _mk("plugins/duo/plugin.json", '{"id":"duo","title":"双入口","ui":"ui/shell.html"}')
        _mk("plugins/duo/backend.py", "")
        _mk("plugins/duo/ui/shell.html", "<html><body>SHELL</body></html>")
        _mk("plugins/duo/ui/hello.html", "<html><body>HELLO</body></html>")
        _mk("plugins/duo/ui/sub/a.css", "body{color:red}")
        _mk("plugins/nat/plugin.json", '{"id":"nat","title":"原生","ui":"ui/app.py"}')
        _mk("plugins/nat/backend.py", "")
        _mk("plugins/nat/ui/app.py", "SECRET_SOURCE = 1\n")
        _mk("plugins/evil/plugin.json", '{"id":"evil","title":"越界","ui":"../config.json"}')
        _mk("plugins/evil/backend.py", "")
        # 两个真实存在的「界外文件」：越界 ui 要指向 plugins/config.json（旧内核
        # 那时会认它、hasUi=true），穿越要指向工作区根的 config.json。目标不存在
        # 的话，旧代码也会「碰巧」是对的 —— 那就测不出任何东西。
        _mk("config.json", '{"hub":"ws://127.0.0.1:1/"}')
        _mk("plugins/config.json", '{"hub":"ws://127.0.0.1:1/"}')

        rt2 = kernel.Runtime(tmp_ws)
        rt2.scan()
        httpd2 = serve(rt2, _FakeHub(), Path(__file__).parent / "web", port=0)
        base2 = f"http://127.0.0.1:{httpd2.server_address[1]}"
        threading.Thread(target=httpd2.serve_forever, daemon=True).start()
        try:
            progs2 = {p["id"]: p for p in json.loads(get(base2, "/api/state")[1])["programs"]}
            check("入口改名成 ui/shell.html：内核认得",
                  progs2.get("duo", {}).get("uiKind") == "web"
                  and progs2["duo"].get("uiName") == "ui/shell.html", str(progs2.get("duo")))
            code, body, _ = get(base2, "/plugin/duo/")
            check("门也认得：/plugin/<pid>/ 开的就是 ui/shell.html",
                  code == 200 and b"SHELL" in body, f"{code} {body[:60]!r}")
            code, body, hdrs = get(base2, "/plugin/duo/sub/a.css")
            check("子目录里的资源照样发得出去（相对入口所在目录）",
                  code == 200 and body == b"body{color:red}"
                  and "text/css" in hdrs.get("content-type", ""),
                  f"{code} {hdrs.get('content-type')}")
            st302, hd = _raw_get(base2, "/plugin/duo")
            check("/plugin/<pid> 少尾斜杠 → 302 补斜杠（相对引用的根）",
                  st302 == 302 and hd.get("location") == "/plugin/duo/",
                  f"{st302} {hd.get('location')}")
            code, body, _ = get(base2, "/plugin/nat/")
            check("原生程序没有网页界面 → 404 说人话",
                  code == 404 and "没有网页界面".encode("utf-8") in body, f"{code} {body[:80]!r}")
            for leak in ("/plugin/nat/app.py", "/plugin/nat/ui/app.py"):
                code, body, _ = get(base2, leak)
                check(f"顺手请求 {leak} → 不发后端源码",
                      code != 200 and b"SECRET_SOURCE" not in body, f"{code} {body[:80]!r}")
            code, body, _ = get(base2, "/plugin/duo/../../config.json")
            check("穿越插件目录 → 403", code == 403, f"{code} {body[:80]!r}")
            check("ui 写成 ../config.json → 当没有界面（state 与门同一个结论）",
                  progs2.get("evil", {}).get("uiKind") == "none"
                  and not progs2["evil"].get("hasUi"), str(progs2.get("evil")))
        finally:
            httpd2.shutdown()
            App.runtime, App.hub, App.web, App.native = saved_app
    finally:
        shutil.rmtree(tmp_ws, ignore_errors=True)

    src_srv = (Path(__file__).parent / "server.py").read_text(encoding="utf-8")
    check("门里不再有第二份入口约定（判据只在 Plugin.ui）",
          '"index.html" if rest' not in src_srv
          and 'base = plugin.path / "ui"' not in src_srv,
          "server.py 里又出现了自己拼的入口")

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
    check("打开后借到的**恰好**是这九项（多一项都算越界）",
          borrowed == ["dev_diff", "dev_plugin_write", "dev_read", "dev_revert",
                       "dev_search", "dev_show", "dev_tree", "dev_versions", "dev_write"],
          str(borrowed))
    check("借来的手里**没有**一件能动历史的危险动作（remote/push/branch/reset…）",
          not any(k in w for w in borrowed for k in
                  ("remote", "fetch", "push", "branch", "merge", "tag", "stash",
                   "reset", "clean", "rebase", "checkout")), str(borrowed))
    check("四个版本动词都没有 pid 参数（碰别人的仓在签名上就不存在）",
          all("pid" not in rt.devtools[fn].parameters.get("properties", {})
              for fn in ("versions", "show", "diff", "revert")),
          str([fn for fn in ("versions", "show", "diff", "revert")
               if "pid" in rt.devtools[fn].parameters.get("properties", {})]))
    check("dev_read 能分页（没有 offset/limit，「返回路径」那条路走不通）",
          {"offset", "limit"} <= set(rt.devtools["read"].parameters.get("properties", {})),
          str(sorted(rt.devtools["read"].parameters.get("properties", {}))))
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

    # 先记一个数：历史里只该有「真的成了的版本」。
    # 判据放在成功写入**之前**取，才能同时覆盖下面「写坏」那一次。
    from . import pluginvcs as _pv
    _pv_ok, _pv_why = _pv.available()
    _pdir = WS / "plugins" / pid

    res = rt.dispatch("dev", "plugin_write", {"files": {"plugin.json": pj}},
                      source="agent", caller=pid)
    check("改自己成功时说得清「改的是你自己」（同一条落盘路径）",
          res.status == "ok" and "你自己" in (res.text or ""),
          f"{res.status}: {(res.text or '')[:60]}")

    if not _pv_ok:
        print(f"  --  跳过版本化断言：{_pv_why}", flush=True)
    else:
        # 上面那次写的是**原样回写**的 plugin.json：内容没变 → 空提交 → 不建版。
        # 那是正确行为（[13b] 专门断言了它），不是版本化没生效 ——
        # 所以「多一版」必须用**内容真的变了**的写入来验，否则测到的是
        # 空提交的语义，不是版本化的语义。
        n_before = len(_pv.versions(_pdir))
        grow = bp2.read_text(encoding="utf-8") + "\n# selftest: 让内容真的变化\n"
        res_g = rt.dispatch("dev", "plugin_write", {"files": {"backend.py": grow}},
                            source="agent", caller=pid)
        n_ok = len(_pv.versions(_pdir))
        check("成功写入（内容真的变了）→ 历史多一版（懒触发：不是启动时建仓）",
              res_g.status == "ok" and n_ok == n_before + 1,
              f"{res_g.status}: {n_before} → {n_ok}")
        n_bad0 = n_ok
        res = rt.dispatch("dev", "plugin_write",
                          {"files": {"backend.py": "def broken(: pass\n"}},
                          source="agent", caller=pid)
        check("校验失败的那一次**不入历史**（历史里只有真的成了的版本）",
              res.status == "error" and len(_pv.versions(_pdir)) == n_bad0,
              f"{res.status}: {len(_pv.versions(_pdir))} 版")

        # 四个动词经内核真调一遍：读历史、看某一版、看差别
        v_list = rt.dispatch("dev", "versions", {}, source="agent", caller=pid)
        check("dev_versions 看得到自己的历史",
              v_list.status == "ok" and "v1" in (v_list.text or ""),
              (v_list.text or "")[:100])
        # 版本列表被截断时必须**说出来**：agent 看见 20 行会以为那就是全部历史。
        # 判据用「多取一个」，所以这条也顺带验了取数逻辑没写成一刀切。
        v_cut = rt.dispatch("dev", "versions", {"limit": 1}, source="agent", caller=pid)
        check("dev_versions 被 limit 截断时明说「更早的还有」",
              v_cut.status == "ok"
              and (v_cut.data or {}).get("truncated") is True
              and "更早的还有" in (v_cut.text or ""),
              f"{v_cut.status}: {str(v_cut.data)[:80]}")
        h_ver = json.loads(get(base, f"/api/p/{pid}/versions")[1])
        check("版本接口带 truncated / shown（人这一侧也不静默截断）",
              "truncated" in h_ver and "shown" in h_ver, str(sorted(h_ver)))

        v_show = rt.dispatch("dev", "show", {"version": "v1"}, source="agent", caller=pid)
        check("dev_show 不带 path → 给文件清单（不是灌全文）",
              v_show.status == "ok" and "plugin.json" in (v_show.text or ""),
              (v_show.text or "")[:100])
        v_file = rt.dispatch("dev", "show", {"version": "v1", "path": "backend.py"},
                             source="agent", caller=pid)
        check("dev_show 带 path → 给那一版的片段，并说清有多少行",
              v_file.status == "ok" and (v_file.data or {}).get("lines"),
              str(v_file.data)[:120])
        v_diff = rt.dispatch("dev", "diff", {"version": "v1"}, source="agent", caller=pid)
        check("dev_diff 给出「那一版 → 现在」的差别或明说没有差别",
              v_diff.status == "ok"
              and ("→ 你现在" in (v_diff.text or "") or "一模一样" in (v_diff.text or "")),
              (v_diff.text or "")[:100])
        v_data = rt.dispatch("dev", "show", {"version": "v1", "path": "data/store.json"},
                             source="agent", caller=pid)
        check("data/ 连只读都不给（dev_show 同样按白名单拒）",
              v_data.status != "ok" and "data" in (v_data.text or ""),
              f"{v_data.status}: {(v_data.text or '')[:80]}")
        v_bad = rt.dispatch("dev", "show", {"version": "v99"}, source="agent", caller=pid)
        check("没有这一版时说的是人话（不是裸异常）",
              v_bad.status == "error" and "没有这一版" in (v_bad.text or ""),
              f"{v_bad.status}: {(v_bad.text or '')[:80]}")

    rt.set_dev(pid, False)
    check("关掉之后手立刻收回",
          not any(w.startswith(("assistant_", "dev_")) for w in rt.capabilities_of(pid)),
          str(sorted(rt.capabilities_of(pid))))
    res = rt.dispatch("dev", "read", {"path": "plugins"}, source="agent", caller=pid)
    check("关掉之后硬调 dev_* 也被拒（不等下一轮重声明）",
          res.status == "rejected" and "开发者模式" in (res.text or ""),
          f"{res.status}: {(res.text or '')[:60]}")

    print("[13b] 版本化本身：就地 .git、白名单、按字节回读")
    if not _pv_ok:
        print(f"  --  跳过：{_pv_why}", flush=True)
    else:
        import tempfile as _tf

        box = Path(_tf.mkdtemp(prefix="vcs-"))
        pd = box / "demo"
        (pd / "ui").mkdir(parents=True)
        (pd / "data").mkdir()
        (pd / "data" / "store.json").write_text('{"secret": 1}', encoding="utf-8")
        (pd / "__pycache__").mkdir()
        (pd / "__pycache__" / "x.pyc").write_bytes(b"\x00")
        (pd / "plugin.json").write_text('{"id": "demo", "title": "演示"}', encoding="utf-8")
        (pd / "backend.py").write_text("def pong() -> dict:\n    return {'v': 1}\n",
                                       encoding="utf-8")
        (pd / "ui" / "index.html").write_text("<h1>v1</h1>", encoding="utf-8")
        AUTH = ("演示", "demo@plugin.local")

        r1 = _pv.commit_all(pd, author=AUTH, notes={"By": "agent"})
        vs = _pv.versions(pd)
        check("首次提交 → 第 1 版，仓库就地长在 plugins/<pid>/.git",
              r1[0] and len(vs) == 1 and (pd / ".git").is_dir(), str(r1)[:100])
        first = _pv.files_at(pd, vs[0]["sha"]) if vs else {}
        check("data/ 永不入库", not any(p.startswith("data/") for p in first),
              str(sorted(first)))
        check("__pycache__ / *.pyc 也没混进去",
              not any("__pycache__" in p or p.endswith(".pyc") for p in first),
              str(sorted(first)))
        check("只跟踪 plugin.json / backend.py / ui/**",
              sorted(first) == ["backend.py", "plugin.json", "ui/index.html"],
              str(sorted(first)))

        old_bytes = _pv.read_at(pd, vs[0]["sha"], "backend.py") if vs else None
        (pd / "backend.py").write_text(
            "def pong() -> dict:\n    return {'v': 2}\n\ndef echo(x) -> dict:\n"
            "    return {'x': x}\n", encoding="utf-8")
        r2 = _pv.commit_all(pd, author=AUTH, notes={"By": "agent"})
        vs2 = _pv.versions(pd)
        check("再写一次 → 第 2 版", r2[0] and len(vs2) == 2, str(r2)[:100])
        check("版本号从第一版起数（v1 最早、v2 最新）",
              [v["ver"] for v in vs2] == [2, 1], str([v["ver"] for v in vs2]))
        check("旧版按**字节**取得回来（回退的地基：decode 再 encode 会改 CRLF）",
              _pv.read_at(pd, vs2[-1]["sha"], "backend.py") == old_bytes, "")

        r3 = _pv.commit_all(pd, author=AUTH)          # 什么都没改
        check("没有改动时不白建一版", (not r3[0]) or len(_pv.versions(pd)) == 2, str(r3)[:80])

        head0 = (pd / ".git" / "HEAD").read_text(encoding="utf-8")
        files, drops, err = _pv.revert_files(pd, "v1")
        check("回退只**取内容**，不落盘、也不动 HEAD（落盘要过四道校验）",
              not err and files and "backend.py" in files
              and (pd / ".git" / "HEAD").read_text(encoding="utf-8") == head0
              and "{'v': 2}" in (pd / "backend.py").read_text(encoding="utf-8"), err)
        check("没有这一版 → 明说（白名单，不解析 HEAD~1 这类语法）",
              _pv.revert_files(pd, "v99")[2] != "", "")
        check("data/ 的只读也不给", _pv.read_at(pd, vs2[0]["sha"], "data/store.json") is None, "")
        check("路径穿越当普通字符串（不是跟踪路径就当没有）",
              _pv.read_at(pd, vs2[0]["sha"], "../plugin.json") is None, "")
        arc = _pv.archive(pd, box / "graveyard")
        check("删程序前能把 .git 归档取走（删了想找回上一版，正是最需要历史的时候）",
              arc is not None and Path(arc).is_dir() and not (pd / ".git").exists(), str(arc))

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
    check("版本面板被截断时明说「更早的还有」",
          "更早的还有" in html and "r.truncated" in html, "")
    check("版本面板在（人这一侧的「看一眼 / 退回去」）",
          'id="vmask"' in html and 'id="vlist"' in html and "退回去" in html, "")
    check("面板只说「退回去」= 再提交一版，不出现 reset 这种说法",
          "再提交一次" in html and "reset" not in html.lower().replace("复位", ""), "")
    check("回退不弹 confirm（pywebview 支持与否取决于版本，弹不出来就是点了没反应）",
          "confirm(" not in html, "")
    check("两栏之间有抓手，宽度走一个变量", 'id="gutter"' in html and "--chatw" in html, "")
    check("宽度夹在 300 与「舞台宽-320」之间（拖动/键盘/缩放共用同一个纯函数）",
          "function clampChatW" in html and "stageW || 0) - 320" in html, "")
    check("宽度按程序记住（localStorage，刷新与切程序都还在）",
          "ws.chatw" in html and "${pid}" in html and "localStorage.setItem(`${CHATW_KEY}.${pid}`" in html, "")
    check("只有对话的程序没有抓手（否则是一根拖不动的缝）",
          "const gutter = solo ? '' :" in html, "")

    print("[15] 智能体对话渲染 Markdown —— 分流、安全、打包三处都得在")
    mdpath = Path(__file__).resolve().parent / "web" / "md.js"
    mdsrc = mdpath.read_text(encoding="utf-8") if mdpath.is_file() else ""
    check("md.js 在（零依赖、可离线）", bool(mdsrc), str(mdpath))
    check("宿主页引用了它", '<script src="/web/md.js">' in html)
    code, body, hdrs = get(base, "/web/md.js")
    check("取得到，且是 javascript（取不到时页面会静默退回纯文本）",
          code == 200 and "javascript" in (hdrs.get("content-type") or ""),
          f"{code} {hdrs.get('content-type')} {len(body)}B")
    check("md.js 自己不去拉任何东西（离线壳里没有第二次请求）",
          "fetch(" not in mdsrc and "import(" not in mdsrc and "new Image" not in mdsrc)

    # 判据锚到**语义**（人那条分支里只做了 esc，没走 md），不再比整段模板串：
    # 原来比 'me"><div class="body">' —— 给这条气泡加一个 class（没送出去的虚线样式）
    # 就误报成「人说的话被 md 渲染了」，而它真正要守的从来不是那串字符。
    _i, _j = html.find("if (m.role === 'me') {"), html.find("// 降级时**不能**挂 md 类")
    _me = html[_i:_j] if 0 <= _i < _j else ""
    check("智能体的话走 md、人说的话走原文",
          "typeof mdToHtml === 'function' ? ' md' : ''" in html
          and "esc(m.text)" in _me and "bodyHtml" not in _me,
          f"人的那条分支有 {len(_me)} 字符")
    check("界面自己的提示（note）不渲染 md",
          "m.note ? esc(m.text) : bodyHtml(m)" in html)
    check("工具返回的正文不渲染 md（命令输出必须等宽原文）",
          '<div class="r">${esc(t.text)}</div>' in html)
    check("md.js 没加载时退回纯文本，不白屏",
          "typeof mdToHtml !== 'function'" in html)
    check("渲染结果按文本缓存（每个 delta 都会重绘整条 thread）",
          "m._mdFor === m.text" in html)

    check("渲染器先转义、再变换（别拿 var/let 字面量当判据，那是风格不是行为）",
          bool(re.search(r"\b(?:var|let|const)\s+t\s*=\s*esc\(raw\)", mdsrc)), "")
    check("链接只放行 http/https/mailto",
          "/^(https?:\\/\\/|mailto:)/i" in mdsrc, "")
    # 不查「源码里有没有 onclick=」——注释里写一句自我说明就会误伤（被绊过两次）。
    # 只看**代码**：去块注释、去行首注释，再枚举它到底写得出哪些属性。
    code_only = re.sub(r"/\*[\s\S]*?\*/", "", mdsrc)
    # 行尾注释也要去：md.js 里有 `break;  // 顶格又不是新项 …` 这种。
    # `(?<!:)` 是为了别把字符串里的 http:// 一起切掉。
    code_only = re.sub(r"(?<!:)//[^\n]*", "", code_only)
    check("去注释后仍留着实代码（不然下面那条是空转）",
          "mailto:" in code_only and "const render" in code_only, "")
    attrs = set(re.findall(r'\b([a-zA-Z-]+)="', code_only))
    check("渲染器只写得出 href/class/style/target/rel/start 这六种属性",
          attrs <= {"href", "class", "style", "target", "rel", "start"}, str(sorted(attrs)))
    # 与引号无关地再查一遍事件属性：单引号、无引号也都是合法 HTML，
    # 只盯 `on...="` 的话，`onclick='…'` 会从白名单眼皮底下溜过去。
    check("代码里写不出内联事件属性（不论单/双引号或无引号）",
          not re.search(r"\bon[a-z]{3,}\s*=", code_only, re.I), "")

    pp = Path(__file__).resolve().parent.parent / "pyproject.toml"
    check("web/*.js 进了 package-data（漏了就是装完 404，本地试不出来）",
          pp.is_file() and "web/*.js" in pp.read_text(encoding="utf-8"), str(pp))

    # ── 滚动条。这一块在第十轮之前是**零命中**：跑的一直是系统默认那条
    # （17px 宽、浅灰凹槽、两头各一个箭头），背景却是 #0e1013 —— 它比页面上
    # 任何内容都亮。下面守的不是"有没有写样式"，是三个会静默失效的错法：
    #   (a) 裸写 scrollbar-width   → Chromium 121+ 整段忽略 ::-webkit-scrollbar
    #   (b) thumb:hover 用 background 简写 → 重置 background-clip，滑块当场胖一圈
    #   (c) 只留 width:3px         → 热区=视觉，看得见、点不中
    idx = Path(__file__).resolve().parent / "web" / "index.html"
    hsrc = idx.read_text(encoding="utf-8") if idx.is_file() else ""
    css = re.search(r"<style>(.*?)</style>", hsrc, re.S)
    check("index.html 有 <style> 段", css is not None, str(idx))
    naked = re.sub(r"/\*.*?\*/", "", css.group(1) if css else "", flags=re.S)
    # 锚在**声明**上，不是锚在选择器上：`#rail::-webkit-scrollbar{width:8px}` 里
    # 也有 `::-webkit-scrollbar{` —— 全局那条被删掉，这条检查照样绿，等于空转。
    check("滚动条不是系统默认那条（锚在声明上，别被窄栏那条满足）",
          "::-webkit-scrollbar{width:var(--sb)" in naked, "")
    check("箭头按钮与 corner 都处置了（默认样式里最扎眼的两处）",
          "::-webkit-scrollbar-button{display:none" in naked
          and "::-webkit-scrollbar-corner{background:transparent}" in naked, "")
    outside = re.sub(r"@supports \(-moz-appearance:none\)\s*\{.*?\n  \}", "", naked, flags=re.S)
    check("scrollbar-width/color 只出现在 -moz 门内",
          "scrollbar-width" not in outside and "scrollbar-color" not in outside, "")
    # 视觉粗细来自 base 那条：3px 透明边 + background-clip:padding-box 把热区削窄。
    # 删掉任一个，滑块就画成热区全宽（4px → 10px，窄栏 2px → 8px），而下面的
    # hover 检查仍然绿 —— 它只证明"悬停没用简写"，证明不了"本来就是细的"。
    thumb = re.search(r"::-webkit-scrollbar-thumb\s*\{([^}]*)\}", naked)
    check("thumb 用 3px 透明边 + padding-box 削出视觉粗细（缺了就胖成热区）",
          bool(thumb) and "background-clip:padding-box" in thumb.group(1)
          and "border:3px solid transparent" in thumb.group(1), "")
    hov = re.search(r"::-webkit-scrollbar-thumb:hover\s*\{([^}]*)\}", naked)
    check("thumb:hover 用 background-color（简写会重置 background-clip）",
          bool(hov) and "background-color" in hov.group(1)
          and not re.search(r"(?<!-)background\s*:", hov.group(1)), "")
    check("深浅两套 color-scheme（缺浅色那套，浅色系统上仍画深色原生控件）",
          re.search(r"color-scheme\s*:\s*dark", naked) is not None
          and re.search(r"color-scheme\s*:\s*light", naked) is not None, "")
    th = re.search(r"#thread\s*\{([^}]*)\}", naked)
    check("#thread 的 gutter 单边常驻（both-edges 会让正文比标题多缩 10px）",
          bool(th) and "scrollbar-gutter:stable" in th.group(1)
          and "both-edges" not in th.group(1), "")
    # 3:1 是算出来的，不是看着够 —— .55 实测 2.67:1（这段注释第一版就这么错的）
    # 锚在**语义特征**上（这一块声明了 color-scheme:dark），不是锚在"第一个 :root"。
    # 上一版锚位置，暗色块被删掉就落到浅色块（alpha .8），照样绿 —— 空转的第二次。
    # `[\d.]+`：`1`（整数）、`.62`、`0.62` 都合法，别把写法差异当失败。
    dark_root = re.search(r":root\s*\{([^}]*color-scheme\s*:\s*dark[^}]*)\}", naked)
    light_root = re.search(r":root\s*\{([^}]*color-scheme\s*:\s*light[^}]*)\}", naked)

    def _alpha(block: str, var: str):
        """取某个 --sb* 变量的 alpha；取不到返回 None（None 一律判失败，别当 0 放过）。"""
        m = re.search(var + r"rgba\(\d+,\s*\d+,\s*\d+,\s*([\d.]+)\)", block)
        return float(m.group(1)) if m else None

    a_dark = _alpha(dark_root.group(1) if dark_root else "", "--sb-thumb:")
    check("暗色静止档 alpha ≥ .6（低于此不到 WCAG 1.4.11 的 3:1）",
          a_dark is not None and a_dark >= 0.6, f"alpha={a_dark}")
    # 只验暗色是不够的：浅色覆盖被删（合并冲突、误删）就退回暗色 .62，
    # 而 .62 落到 #f6f7f9 上只有 ~1.8:1 —— 浅色模式下滚动条基本看不见。
    a_light = _alpha(light_root.group(1) if light_root else "", "--sb-thumb:")
    check("浅色静止档 alpha ≥ .78（暗色 .62 落到浅底上只有 ~1.8:1）",
          a_light is not None and a_light >= 0.78, f"alpha={a_light}")
    h_dark = _alpha(dark_root.group(1) if dark_root else "", "--sb-thumb-hi:")
    h_light = _alpha(light_root.group(1) if light_root else "", "--sb-thumb-hi:")
    check("两套悬停档都在，且各自比静止档更亮（两档不能塌成一档）",
          None not in (h_dark, h_light) and h_dark > a_dark and h_light > a_light,
          f"暗 {a_dark}→{h_dark} 浅 {a_light}→{h_light}")
    # 让 #rail 真能滚的是这两条，不是滚动条样式：少了 overflow-y:auto 就没有容器，
    # 少了 .rbtn 的主尺寸，flex 子项会先缩到 min-content 把图标压扁 —— 滚动条永不出现。
    check("窄栏真的能滚（少了这两条，下面 keepTop 那条就是空转）",
          "overflow-y:auto" in naked
          and re.search(r"#rail\s+\.rbtn\s*\{[^}]*flex:0 0 auto", naked) is not None, "")
    rsb = re.search(r"#rail::-webkit-scrollbar\s*\{([^}]*)\}", naked)
    rw = re.search(r"width\s*:\s*(\d+)px", rsb.group(1)) if rsb else None
    check("窄栏热区 ≥ 8px（热区=视觉就点不中）",
          bool(rw) and int(rw.group(1)) >= 8, rw.group(0) if rw else "无 width")
    check("窄栏重绘保住 scrollTop（否则点靠下的图标会跳回顶部）",
          "const keepTop = rail.scrollTop" in hsrc and "rail.scrollTop = keepTop" in hsrc, "")
    # 窄栏一旦重设 thumb，特异性就抬到 (1,0,1)，全局 :hover 当场失效 ——
    # 那正是上一版"补一条 hover 覆盖回去"的来源。这一条守的是"别再加回去"。
    check("窄栏不重设 thumb（只改 width，颜色与边框全部继承全局）",
          re.search(r"#rail::-webkit-scrollbar-thumb", naked) is None, "")

    # ── 对话闸门（第十二轮）。这一节守的是**四类会静默失效的错法**：
    #   (a) 忙碌判断散在多处 → 总有一处忘了改，两轮并发就从那儿进来；
    #   (b) 清空发生在受理之前 → 被拒的那句字没了（原来就是这样丢的）；
    #   (c) 停止键还是 type=submit → 按「停止」会先提交一次表单；
    #   (d) cancel 的 Future 没人取 → 「已请求取消」可能是句空话。
    print("[16] 对话闸门：忙时禁发、按得动停止、丢不了字")
    tj = (Path(__file__).resolve().parent / "web" / "turn.js")
    tjsrc = tj.read_text(encoding="utf-8") if tj.is_file() else ""
    check("turn.js 在（纯状态机：无 DOM / 无网络 / 无定时器，才能在 node 里跑）",
          bool(tjsrc) and not any(k in tjsrc for k in
                                  ("document", "fetch(", "setTimeout", "window.")), str(tj))
    check("宿主页引用了它", '<script src="/web/turn.js">' in html, "")
    tcode, tbody, thdrs = get(base, "/web/turn.js")
    check("取得到，且是 javascript（取不到就退回只有两档的退化版：不能停）",
          tcode == 200 and "javascript" in (thdrs.get("content-type") or ""),
          f"{tcode} {thdrs.get('content-type')} {len(tbody)}B")
    # (a) 判据只有一处：按键、回车、程序化提交都汇到 submitDraft，它第一句就是判据。
    # 原来这条只证明「那一行在」：在 ta.onkeydown 或 btn.onclick 里**另加**一处
    # `if (!TM.canSubmit(...))` 也照样绿 —— 而那正是这一节声称要防的「两处判断」。
    # 判据改成数调用点。
    n_can = html.count("TM.canSubmit(")
    check("发送的忙碌判据只有一处（全文件只调一次 TM.canSubmit）",
          n_can == 1, f"调用点 {n_can} 处")
    # 名字要诚实：这条断言的是「闸门先于读输入框」，不是「空消息不占闸门」。
    # str.index 找不到就抛 ValueError —— 那会把 main() 直接打断：后面 turn.js、
    # package-data 那几条一条都跑不到，输出还从「N 通过 / M 失败」变成 traceback。
    # 缺哪段就判这一条失败，不许掀桌。
    _gate, _text = html.find("if (!TM.canSubmit(phaseOf(pid)))"), html.find("const text = ta.value.trim();")
    check("忙碌判据在取文本之前（忙时连输入框都不读，更不会去清它）",
          0 <= _gate < _text, f"gate={_gate} text={_text}")
    # (b) 被拒要能回滚：把话放回输入框，并且**不**在对话里留下「我说过的话」。
    check("被拒时把原文放回输入框", "restoreDraft(pid, text)" in html, "")
    check("被拒时把那句从对话里收回（不假装说过）", "dropMsg(pid, mine)" in html, "")
    # 原来那句话里嵌着 \n 与 8 个空格 —— 锚的是**排版**不是语义：换个缩进、
    # 或写成单行，行为没变也会红。空白一律不敏感。
    check("回滚只在 sending 这一档发生（迟到的 reject 不许解锁在跑的一轮）",
          re.search(r"case 'reject':\s*\n\s*return p === SENDING \? IDLE : p;", tjsrc) is not None, "")
    # (c) 停止键与发送键是同一个 DOM 位：它不能是 submit，否则按停止会先提交一次。
    check("发送键是 type=button（按「停止」不许顺带提交表单）",
          'id="sendb" type="button"' in html, "")
    # `"ta.disabled" not in html` 两边都不准：写 `ta.disabled = false`（重绘后
    # 显式保持可编辑）会假红；而 `ta.readOnly = true` 或 `setAttribute('disabled')`
    # 这两种**真会禁用**的写法一个都抓不到。锚到「会把它关掉」的三种形态上。
    check("忙碌时输入框**不**禁用（字是人的东西，收走的只是「现在能送出去」）",
          not re.search(r"ta\.disabled\s*=\s*(true|'disabled'|\"disabled\")"
                        r"|ta\.readOnly\s*=\s*true"
                        r"|setAttribute\(\s*['\"]disabled", html), "")
    check("按钮五态由纯函数给（界面里没有第二处「忙不忙」的判断）",
          "const spec = TM.btn(phaseOf(pid));" in html, "")
    # 停止：请求 ≠ 完成，终态看事件；等不到终态要有兜底，否则永久转圈。
    check("停止真的发了 /cancel（原来全文件只调用过 0 次）",
          "/api/p/${pid}/cancel" in html, "")
    check("终态读 status（不读就分不出「答完了」「被停止了」「失败了」）",
          "const stopped = st === 'cancelled';" in html
          and "st !== 'completed' && st !== 'cancelled'" in html, "")
    check("停止后注明「已经跑完的操作不会撤回」（停止不是撤销）",
          "已经跑完的操作不会撤回" in html, "")
    check("请求停止后等不到终态有兜底（否则界面永久「正在停止…」，只能重启）",
          "stopTimeout" in html and "STOP_WAIT_MS" in html, "")
    check("取消没被受理就退回去（不把「没停成」画成「停了」）",
          "setPhase(pid, 'cancelFailed')" in html and "turn.cancel_failed" in html, "")
    # (d) link.py：cancel 的 Future 必须有人取，而且不许拿 _send_lock（回调可能在
    #     注册线程里同步跑，而注册那段正持锁 —— 非重入，那个 HTTP handler 会永久阻塞）。
    lj = Path(__file__).resolve().parent / "link.py"
    ljsrc = lj.read_text(encoding="utf-8")
    tree = ast.parse(ljsrc)
    # ast.walk **不保证顺序**，而 link.py 里有两个同名 cancel：Link.cancel 与
    # Hub.cancel。靠「walk 到的第一个」去定位就是靠实现细节 —— 顺序一变（类顺序、
    # 换成 DFS）就会去校验 Hub.cancel，那里没有 add_done_callback，这条断言从此
    # 永远红、而且指错地方。锚到「Link 类体内的那个方法」。
    def _meth(cls_name: str, meth: str):
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == cls_name:
                for sub in node.body:
                    # async def 也算：只认 FunctionDef 的话，将来把它改成
                    # `async def` 会让下面两条断言在空集合上**自动通过**
                    # （"x" not in set() 恒真），守卫就此静默失效。
                    if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)) and sub.name == meth:
                        return sub
        return None

    fn = _meth("Link", "cancel")
    check("定位到 Link.cancel（不是同名的 Hub.cancel）", fn is not None, "")
    calls = {n.func.attr for n in ast.walk(fn) if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Attribute)} if fn else set()
    check("link.cancel 真的取了 Future（add_done_callback）",
          "add_done_callback" in calls, str(sorted(calls)))
    check("link 有一个 _cancel_done 回调（异常落地成事件，不许躺着）",
          "def _cancel_done" in ljsrc and '"turn.cancel_failed"' in ljsrc, "")
    have = _meth("Link", "_cancel_done")
    check("定位到 Link._cancel_done（定位不到，下面两条就是空转）", have is not None, "")
    # 纯 AST 判据：名字与属性都算。按源码片段比会被注释误伤 ——
    # 而 _cancel_done 的注释里恰好写了「不拿 _send_lock」，那就成了自我否定。
    used = {n.id for n in ast.walk(have) if isinstance(n, ast.Name)} if have else set()
    attr = {n.attr for n in ast.walk(have) if isinstance(n, ast.Attribute)} if have else set()
    check("_cancel_done 里不拿 _send_lock（非重入锁：会让注册它的线程永久阻塞）",
          "_send_lock" not in used and "_send_lock" not in attr,
          str(sorted(used & {"_send_lock"}) or sorted(attr & {"_send_lock"})))
    # 原来 grep 的是那五个字，而 link.py 里**一处注释**就写着它（_open_turn 的
    # finally 里「否则聊天会永远回『上一轮还没结束』」）—— 就算把真正的返回值改掉
    # 也照样绿。锚到 return 上。
    check("服务端契约不变（第二条仍回「上一轮还没结束」，界面负责让它不发生）",
          'return "上一轮还没结束"' in ljsrc, "")
    check("turn.js 也在 package-data 里（web/*.js 那条覆盖它）",
          pp.is_file() and "web/*.js" in pp.read_text(encoding="utf-8"), "")

    # ── ocr 第一轮评审（14 条）。这一节守的是「评审修完又长回去」。
    # 每条都锚到**语义特征**而不是位置：位置会漂，语义不会。
    print("[17] 评审第一轮：终态不丢字 · 快照不复活 · 索引跟着挪 · 停止可机读")
    check("终态里的 d.text 在没有气泡时也接住（中途进页面 / 重连）",
          "if (!m && d.text) m = push(pid, { role: 'it', text: d.text, tools: [] });" in html, "")
    # 数「(S.tick[pid] || 0)」出现几次是**假判据**：它一共 4 处，其中一处是 on()
    # 里的自增（`S.tick[pid] = (S.tick[pid] || 0) + 1`），不是守卫 —— 删掉 warm
    # 那条仍剩 3 处，照样绿。改锚**每个函数里**的守卫形状。
    def _jsfn(name: str) -> str:
        i = html.find("function " + name + "(")
        if i < 0:
            return ""
        j = html.find("\n}\n", i)          # 列 0 的 }：内层的都是缩进的
        return html[i:j] if j > i else ""
    _ls, _wm = _jsfn("loadState"), _jsfn("warm")
    check("loadState 里也判快照是否过期（一个旧快照就能把闸门重新打开）",
          "!== (tick0[pid] || 0)) continue;" in _ls and _ls.count("tick0") >= 2,
          f"loadState {len(_ls)} 字符")
    check("warm 里也判快照是否过期（另一个窗口在跑时尤其要命）",
          "=== tick0" in _wm, f"warm {len(_wm)} 字符")
    check("删一条消息要挪 S.cur（否则流式文本灌进别人的气泡）",
          "cur > i ? cur - 1 : cur" in html, "")
    check("回填成功才撤那句话（先回填、后决定）",
          "if (restoreDraft(pid, text)) {" in html, "")
    check("回填失败就把它留在对话里并标成没送出去（不静默丢字）",
          "mine.failed = true" in html and "m.failed ? ' failed' : ''" in html, "")
    # 判据必须是「真的完成」，不能是「不等于运行中」：被停掉的那一轮会把残余的卡
    # 收成「已中断」，而那不是跑完 —— 用它当判据，那句话就变成撒谎。
    check("「跑完的操作不会撤回」只认真正的完成态（已中断/失败都不算）",
          "t.status === '完成' || t.status === 'ok'" in html, "")
    check("停掉那一轮时，残余的工具卡当场收尾（否则组头一直报「进行中」）",
          "if (t.status === '运行中') t.status = '已中断';" in html, "")
    check("停止的结果看机器码，不看中文串（改一个字不会静默走错分支）",
          "code === 'cancelling'" in html and "startsWith('已请求取消')" not in html, "")
    # 原来 grep 的是一句中文注释：改个措辞就假红，而给退化版**真**加上停止
    # 却不碰注释就假绿 —— 两种错法都不报。改锚它的形状。
    _fb = html[html.find("const TM = (typeof TURN === 'object' && TURN) ? TURN : {"):]
    _fb = _fb[:_fb.find("\n};")] if "\n};" in _fb else ""
    check("退化版 turn.js：不给「停止」、也不复刻转移表",
          "next:" in _fb and "canStop: () => false" in _fb and "action: 'stop'" not in _fb,
          f"退化版 {len(_fb)} 字符")
    check("_pending 这个死字段不在了（设了没人读）", "_pending" not in html, "")
    # link.py 侧
    check("link.cancel 返回机器可读 status", '"status": "cancelling"' in ljsrc, "")
    check("取消回执带上**被取消那一轮**的 id（不是回调时的 self._turn）",
          "lambda f: self._cancel_done(f, turn_id)" in ljsrc
          and 'getattr(self._turn, "turn_id"' not in ljsrc, "")
    check("调度失败时把没 await 的协程 close 掉（否则 GC 喷 RuntimeWarning）",
          'getattr(coro, "close", None)' in ljsrc, "")

    # ── 会话（P0）──────────────────────────────────────────────────
    print("[18] 会话：归我们所有的一份数据，不是服务端的状态")
    # 挑一个**真的存在**的程序：写死 pomodoro 会让这一节跟着工作区数据一起红，
    # 而它想验的是会话本身。工作区里哪个在就用哪个，一个都没有就用小助理。
    _pid = next((p["id"] for p in rt.state()["programs"] if p["id"] != "assistant"),
                "assistant")
    _stmp = Path(tempfile.mkdtemp())
    fake._rt = rt                       # 从这一行起，替身开始真有会话
    fake.root = _stmp
    fake._pid = _pid
    _sid0 = fake.link(_pid).sid

    code, body, _ = get(base, f"/api/p/{_pid}/sessions")
    _sess = json.loads(body.decode("utf-8")) if code == 200 else {}
    check("会话列表 200 且带当前会话", code == 200 and _sess.get("sid") == _sid0, str(code))
    check("列表非静默（truncated/shown 都在）",
          "truncated" in _sess and "shown" in _sess, str(_sess)[:200])

    code, body, _ = get(base, f"/api/p/{_pid}/session/{_sid0}")
    _one = json.loads(body.decode("utf-8")) if code == 200 else {}
    check("单个会话 200，total/shown 齐", code == 200 and _one.get("total") == 0, str(code))
    code, body, _ = get(base, f"/api/p/{_pid}/session/{_sid0}?limit=5")
    _lim = json.loads(body.decode("utf-8")) if code == 200 else {}
    check("limit 生效且不谎报截断", _lim.get("shown") == 0 and _lim.get("truncated") is False,
          str(_lim)[:200])
    code, body, _ = get(base, f"/api/p/{_pid}/session/s_deadbeef")
    check("不存在的会话 404（不假装空会话）", code == 404, str(code))

    r = post(base, f"/api/p/{_pid}/session/new", {})
    _sid1 = r.get("sid") or ""
    check("新建会话拿到 sid", r.get("status") == "ok" and _sid1.startswith("s_"), str(r))
    check("新会话与旧的不同", _sid1 and _sid1 != _sid0, f"{_sid1} vs {_sid0}")

    r = post(base, f"/api/p/{_pid}/session/switch", {"sid": _sid0})
    check("切回旧会话", r.get("status") == "ok" and r.get("sid") == _sid0, str(r))
    r = post(base, f"/api/p/{_pid}/session/rename", {"sid": _sid1, "title": "另一个话题"})
    check("改名成功", r.get("status") == "ok" and r.get("title") == "另一个话题", str(r))

    _l = fake.link(_pid)
    _l._busy = True                    # 模拟「这一轮还在跑」
    r = post(base, f"/api/p/{_pid}/session/switch", {"sid": _sid1})
    check("忙时拒绝切换，且回 409 + 人话", r.get("status") == "busy" and "等它停下来" in str(r.get("message")),
          str(r))
    _l._busy = False

    r = post(base, f"/api/p/{_pid}/session/switch", {"sid": "bad-shape"})
    check("坏 sid 被 400 挡下（形状在门口就收紧）", r.get("status") == "bad", str(r))

    r = post(base, f"/api/p/{_pid}/session/delete", {"sid": _sid1})
    check("删除完成", r.get("status") == "ok", str(r))
    check("删除只是搬家（.trash 里有，原目录没有）",
          any((_stmp / "sessions" / ".trash" / _pid).glob(f"{_sid1}.*.jsonl"))
          and not (_stmp / "sessions" / _pid / f"{_sid1}.jsonl").exists(), "")

    # 数据层：落盘形状与投影
    _d = fake.link(_pid).as_dict()
    check("as_dict 带 sid/turns/truncated", all(k in _d for k in ("sid", "turns", "truncated")), str(_d))
    _l._record_user("人说的话")
    _l._record_user("[系统] 能力面已更新", sys=True)
    _msgs = _l.store.messages(_pid, _l.sid)
    check("接力消息被标成 sys（不冒充人话）", _msgs[-1]["sys"] is True, str(_msgs[-1]))
    check("sys 消息照样进模型上下文（当时模型确实看见了）",
          len(_l._ctx()) == 2, [m.text for m in _l._ctx()])
    _l.store.append_call(_pid, _l.sid, "status", {}, "ok")
    check("工具卡不进上下文（协议只允许 user/agent + 文本）",
          len(_l._ctx()) == 2, [m.text for m in _l._ctx()])

    # 显式 history：这一条是「切会话不需要动 SDK 内部状态」的前提
    check("每轮显式传 history（真相在我们这份 store 上）",
          "history=base" in ljsrc, "")
    check("接力消息走 sys=True", "_one_turn(handoff, sys=True)" in ljsrc, "")
    check("会话落盘由 Link 自己写（不靠 caller.history）",
          "self.store.append_user" in ljsrc and "self.store.append_agent" in ljsrc, "")

    print("[19] 选中复制：三道闸，一处没开就等于没开")
    from . import shell as sh

    sh_src = Path(__file__).with_name("shell.py").read_text(encoding="utf-8")
    html_src = (Path(__file__).parent / "web" / "index.html").read_text(encoding="utf-8")

    check("建窗时显式打开 text_select（不传 = pywebview 默认关掉选中）",
          "text_select=True" in sh_src, "")
    check("老版本 pywebview 不认这个参数时能退回（不整窗起不来）",
          "except TypeError" in sh_src, "")

    # 根因①②：用 pywebview **自己的**默认值与**自己的**判据复现，而不是我们的复述
    try:
        import webview as _wv
        _wvdir = Path(_wv.__file__).parent
        _win_src = (_wvdir / "window.py").read_text(encoding="utf-8")
        _cz = (_wvdir / "js" / "customize.js").read_text(encoding="utf-8")
        _m = re.search(r"text_select: bool = (\w+)", _win_src)
        check("pywebview 的 text_select 默认是 False（根因，不是我们配错）",
              bool(_m) and _m.group(1) == "False", _m.group(0) if _m else "没找到")
        check("默认 False 会变成 disableTextCss 的注入条件",
              "disableText = '%(text_select)s' === 'False'" in _cz, "")
        check("注入的正是 body{user-select:none}（整页，不是某个控件）",
              "body {-webkit-user-select: none" in _cz, "")
    except Exception as exc:
        check("pywebview 可读源码（读不到就跳过根因复现）", True, f"跳过：{exc}")

    # 我们的页面必须压得住它：id 选择器 (1,0,0) 胜过 body (0,0,1)
    def _spec(sel: str) -> tuple[int, int, int]:
        ids = len(re.findall(r"#[\w-]+", sel))
        cls = len(re.findall(r"\.[\w-]+|\[[^\]]*\]|:(?!:)[\w-]+", sel))
        el = len(re.findall(r"(?:^|[\s>+~])([a-zA-Z][\w-]*)", sel))
        return ids, cls, el

    check("对话区有 user-select:text（显式，不靠继承）",
          "#thread,.dlg{user-select:text" in html_src, "")
    check("它用 id 选择器，特异性压得住 body 的 none",
          _spec("#thread") > _spec("body") and "#thread,.dlg{user-select:text" in html_src,
          f"{_spec('#thread')} vs {_spec('body')}")
    check("开关标签仍然不许选中（不为了复制把整页放开）",
          "user-select:none" in html_src, "")

    # 用 find 而不是 index：旧代码里根本没有这一段，index() 会抛 ValueError
    # 把整节 selftest 带走 —— 验收在病还在的时候必须「红」，不能「炸」。
    _pos = html_src.find("if (e.key !== 'c'")
    _ctrl = html_src[_pos:_pos + 320] if _pos >= 0 else ""
    check("Ctrl+C 有兜底（accelerator 被关时也能复制）",
          "copySelection()" in _ctrl, _ctrl[:90] or "找不到兜底块")
    check("兜底不拦原生那条路（没有 preventDefault）",
          bool(_ctrl) and "preventDefault" not in _ctrl, _ctrl[:90] or "找不到兜底块")
    check("输入框自己的选区不被接管（那里交给浏览器）",
          "selectionStart !== a.selectionEnd" in html_src, "")
    check("写剪贴板失败有第二条路（execCommand 回退）",
          "execCommand('copy')" in html_src, "")
    check("流式重绘不抹掉已建立的选区",
          "sel.anchorNode" in html_src and "threadDirty = true" in html_src, "")
    check("选区消失后补画欠下的那一帧",
          "selectionchange" in html_src and "threadDirty = false" in html_src, "")

    # 真代码路径 + 假 WebView2：不猜「应该会设上」，而是调一次看它设了什么
    class _Settings:
        """字段名与 ``CoreWebView2.Settings`` **逐字一致**。

        为什么要专门写这句：第一次写成了 ctx/acc/dev，``open_copy_doors`` 照样返回
        True（Python 允许给任意对象加属性），断言却全是 None —— 那是一份假绿。
        替身的字段名错一个字母，这节测试就变成了空转。
        """

        def __init__(self) -> None:
            self.AreDefaultContextMenusEnabled = None
            self.AreBrowserAcceleratorKeysEnabled = None
            self.AreDevToolsEnabled = None

    class _Core:
        def __init__(self) -> None:
            self.Settings = _Settings()

    class _WV:
        def __init__(self) -> None:
            self.CoreWebView2 = _Core()

    class _Native:
        def __init__(self) -> None:
            self.webview = _WV()

    class _Win:
        def __init__(self) -> None:
            self.native = _Native()

    _w1 = _Win()
    _okc, _why = sh.open_copy_doors(_w1, debug=False)
    _s1 = _w1.native.webview.CoreWebView2.Settings
    check("open_copy_doors 真的打开了右键菜单",
          _okc and _s1.AreDefaultContextMenusEnabled is True, f"{_okc} {_why}")
    check("open_copy_doors 真的打开了快捷键（Ctrl+C / Ctrl+A）",
          _s1.AreBrowserAcceleratorKeysEnabled is True, "")
    check("DevTools 仍然关着（不因为要复制就把 F12 送出去）",
          _s1.AreDevToolsEnabled is False, str(_s1.AreDevToolsEnabled))

    _w2 = _Win()
    sh.open_copy_doors(_w2, debug=True)
    check("debug=True 时才放 DevTools 出去",
          _w2.native.webview.CoreWebView2.Settings.AreDevToolsEnabled is True, "")

    class _Broken:
        @property
        def native(self):
            raise RuntimeError("不是 WebView2")

    _okc3, _why3 = sh.open_copy_doors(_Broken(), debug=False)
    check("拿不到 native 不抛异常（页面里还有兜底）",
          _okc3 is False and bool(_why3), str(_why3)[:80])

    httpd.shutdown()
    if getattr(App, "native", None):
        App.native.shutdown()                       # 收尸：不留孤儿窗口
    print(f"\n{'全绿' if bad == 0 else '有失败'}：{ok} 通过 / {bad} 失败")
    return 0 if bad == 0 else 1


class _FakeHub:
    """selftest 不连 aaas：门只需要一个「智能体池」的形状。

    但**会话**是纯数据，不需要连接 —— 所以那几条走的是真的 ``Link``
    （只是从不 ``start()``，因此没有后台线程、不会连任何地址）。
    这样路由测试测的就是产品代码，而不是一份会自己漂走的复刻。
    """

    def __init__(self, root: Path | None = None, runtime=None, pid: str = "pomodoro") -> None:
        self.root = Path(root or ".")          # 设置那条路由要用它落盘
        self.cfg: dict = {}
        self.rebound: list[dict] = []
        self._rt = runtime
        self._pid = pid
        self._link = None

    def link(self, pid: str, create: bool = False):
        """只对「本替身认领的那个程序」给出一条真 Link；从不 start()。"""
        if self._rt is None or pid != self._pid:
            return None
        if self._link is None:
            self._link = Link(self._rt, self.root, self.cfg, pid)
        return self._link

    def sessions(self, pid: str, limit: int = 50) -> dict:
        l = self.link(pid)
        return {"status": "missing"} if l is None else l.sessions(limit)

    def switch(self, pid: str, sid: str) -> dict:
        l = self.link(pid)
        return {"status": "missing"} if l is None else l.switch(sid)

    def new_session(self, pid: str, title: str = "") -> dict:
        l = self.link(pid)
        return {"status": "missing"} if l is None else l.new_session(title)

    def rename(self, pid: str, sid: str, title: str) -> dict:
        l = self.link(pid)
        return {"status": "missing"} if l is None else l.rename(sid, title)

    def drop(self, pid: str, sid: str) -> dict:
        l = self.link(pid)
        return {"status": "missing"} if l is None else l.drop(sid)

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
