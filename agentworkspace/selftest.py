"""验收：一个人能在不看代码的前提下，独立地把每一层跑一遍。

这一版多了一组断言：**上下文隔离**。它不是「提示词写得好不好」，而是
「一个程序的智能体到底拿不到什么」—— 所以它验的是 Link 真的声明了什么。
"""

from __future__ import annotations

import ast
import asyncio
import enum
import inspect
import json
import queue
import re
import shutil
import sys
import tempfile
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Literal, Optional
from urllib.request import Request, urlopen

from . import fixtures, kernel, server, toolbox
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


def main(argv: list[str] | None = None) -> int:
    """自检入口：**默认在临时工作区里跑**，``--here`` 才动真工作区。

    为什么默认隔离：这份自检里有一组断言验的是「内核 × 真实程序」的对接
    （pomodoro 的 4 个能力、memo 的原生窗口）。那两个程序是**工作区数据**，不是
    代码 —— 谁把它们删了（``workspace/plugins/.trash-*-data`` 就是删除残留），那
    16 条就红着。红着不解释的质量门等于没有门：下一次真出问题，没人分得清
    「数据缺了」和「代码坏了」。所以默认把 ``workspace/`` 只读复制一份、补上
    fixture 再跑 —— 同一份代码在任何一台机器上给出同一份答案。

    ``--here``  就在当前工作区里跑（真实的、有数据的那个）；缺数据的断言会如实
                失败，并在开头说明「那是数据缺失，不是代码坏了」。
    ``--keep``  保留临时工作区。默认规则：全绿就删，红了留着方便复查。
    """
    args = list(sys.argv[1:] if argv is None else argv)
    unknown = [a for a in args if a not in ("--here", "--keep")]
    if unknown:
        print(f"[用法] 不认识的参数：{unknown}（只认 --here / --keep）", flush=True)
        return 2
    here, keep = "--here" in args, "--keep" in args

    global WS
    tmp: Path | None = None
    if here:
        print(f"[当前工作区] {WS}（--here：不隔离，缺数据的断言会如实失败）", flush=True)
        _fixture_note()
    else:
        tmp = Path(tempfile.mkdtemp(prefix="aw-selftest-"))
        WS = fixtures.build(tmp)
        print(f"[临时工作区] {WS}", flush=True)
        print("        （workspace/ 的只读副本 + pomodoro/memo；--here 改测真工作区）",
              flush=True)
    try:
        rc = _run()
    finally:
        if tmp is not None:
            if keep or bad:
                print(f"\n（临时工作区保留在 {tmp}，方便复查；确认后可删）", flush=True)
            else:
                shutil.rmtree(tmp, ignore_errors=True)
    return rc


def _run() -> int:
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

    # 外观三档：纯函数，所以不必起服务
    check("外观：三个词都认", [st.normalize_theme(v) for v in ("auto", "dark", "light")]
          == ["auto", "dark", "light"])
    check("外观：空 = auto（不填不是错）", st.normalize_theme(None) == "auto"
          and st.normalize_theme("  ") == "auto")
    check("外观：大小写不较真（手写的 body 不该因为 Dark 收 400）",
          st.normalize_theme(" Dark ") == "dark")

    def theme_bad(v):
        try:
            st.normalize_theme(v)
            return None
        except st.BadTheme as exc:
            return str(exc)

    check("外观：认不出的值 → 报错，且消息里带着它是谁",
          (theme_bad("blue") or "").find("blue") >= 0, str(theme_bad("blue")))

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

        # ---- 外观：与地址同一页设置、同一份 config.json ----
        check("没设过外观 → 默认 auto（等于上一版「跟随系统」的行为）",
              st.load_theme(tmp) == "auto", st.load_theme(tmp))
        r = post(base, "/api/settings", {"theme": "light"})
        check("只改外观也成（不必把地址一起交上来）",
              r.get("status") == "ok" and r.get("theme") == "light", str(r)[:160])
        check("外观真的落盘了（config.json 的 ui.theme）",
              json.loads((tmp / "config.json").read_text(encoding="utf-8"))["ui"]["theme"]
              == "light")
        check("只改外观不会把地址改掉",
              json.loads((tmp / "config.json").read_text(encoding="utf-8"))["aaas"]["url"]
              == "ws://127.0.0.1:9900/", str(st.load(tmp).get("aaas")))
        check("GET /api/settings 一并读回外观",
              json.loads(get(base, "/api/settings")[1]).get("theme") == "light")
        check("/api/state 里也能拿到外观（界面首帧靠它）",
              json.loads(get(base, "/api/state")[1])["settings"].get("theme") == "light")
        r = post(base, "/api/settings", {"url": "ws://127.0.0.1:9900/", "theme": "blue"})
        check("认不出的外观被拒（不是 200 的假成功）",
              r.get("status") == "bad" and "外观" in str(r.get("error")), str(r)[:160])
        check("坏外观没有把外观写坏（还是 light）",
              st.load_theme(tmp) == "light", st.load_theme(tmp))
        r = post(base, "/api/settings", {"url": "ftp://x/", "theme": "dark"})
        check("地址与外观一起交、地址坏 → 整单被拒",
              r.get("status") == "bad", str(r)[:160])
        check("整单被拒时外观**也没落**（一次保存不该只成一半）",
              st.load_theme(tmp) == "light", st.load_theme(tmp))
        st.save_theme(tmp, "auto")
        check("auto 存得回去（取消/跟随系统不是一个死状态）", st.load_theme(tmp) == "auto")
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
    from .link import FALLBACK_ASSISTANT, MAX_INSTRUCTIONS, clip_instructions
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

    # 兜底职责：可以短，不能空，也不能把「框架接线」写进去。
    # 非空是**协议要求** —— 服务端拒空 instructions，整轮 2ms 失败且用户看不见解释。
    check("兜底职责非空（空 = 整轮静默失败）",
          bool(FALLBACK_ASSISTANT.strip()), repr(FALLBACK_ASSISTANT))
    check(f"兜底职责够短（{len(FALLBACK_ASSISTANT)} 字符 ≤ 20）",
          len(FALLBACK_ASSISTANT) <= 20, FALLBACK_ASSISTANT)
    _leaks = [w for w in ("AaaS", "服务端", "调用方", "function", "声明", "协议")
              if w in FALLBACK_ASSISTANT]
    check("兜底职责里没有框架接线词", _leaks == [], str(_leaks))

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
    try:
        dev_now = json.loads(rt.dev_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        dev_now = {}
    # dev.json 只记「**开着的**」：关掉就删条目，不留 "xxx": false。
    # 0.3.0 的工作区里就有一枚 "habit": true，而 habit 这个程序早删了 —— 一个说不清
    # 来历的条目比没有条目更坏：下一次同名插件出现时，人是照着它做判断的。
    check("关掉开关不留死条目（dev.json 只记「开着的」）",
          not any(k in dev_now for k in rt.plugins), str(dev_now))
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
    check("打开后借到的**恰好**是这十五项（多一项都算越界）",
          borrowed == ["dev_diff", "dev_host_services", "dev_page_check", "dev_page_close",
                       "dev_page_eval", "dev_page_open", "dev_page_shot",
                       "dev_plugin_write", "dev_read", "dev_revert", "dev_search",
                       "dev_show", "dev_tree", "dev_versions", "dev_write"],
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
        # versions() 默认只取 20 条（那是给人看的一页）。用在「多一版」这个
        # 判据上就是**假红**：[13] 挑的 pid 是 next(iter(rt.plugins))，未必是刚
        # 建的 —— 实测 dingtalk 在临时工作区里被写到 21 版，len(versions()) 于是
        # 恒等于 20，"多一版"永远看不见。判据要的是「链长了」，就得看全链。
        _ALLV = 10 ** 6
        n_before = len(_pv.versions(_pdir, limit=_ALLV))
        grow = bp2.read_text(encoding="utf-8") + "\n# selftest: 让内容真的变化\n"
        res_g = rt.dispatch("dev", "plugin_write", {"files": {"backend.py": grow}},
                            source="agent", caller=pid)
        n_ok = len(_pv.versions(_pdir, limit=_ALLV))
        check("成功写入（内容真的变了）→ 历史多一版（懒触发：不是启动时建仓）",
              res_g.status == "ok" and n_ok == n_before + 1,
              f"{res_g.status}: {n_before} → {n_ok}")
        n_bad0 = n_ok
        res = rt.dispatch("dev", "plugin_write",
                          {"files": {"backend.py": "def broken(: pass\n"}},
                          source="agent", caller=pid)
        check("校验失败的那一次**不入历史**（历史里只有真的成了的版本）",
              res.status == "error" and len(_pv.versions(_pdir, limit=_ALLV)) == n_bad0,
              f"{res.status}: {len(_pv.versions(_pdir, limit=_ALLV))} 版")

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
    # 这一条守的是**这次改动本身**：深浅只能有一个判据（<html data-theme>），
    # 由设置面板决定。@media 一旦回来，就有两个判据同时说话 —— 「设成深色的人
    # 被系统切浅」会以最难查的方式重演（CSS 静默盖掉 JS 的决定）。
    check("深浅只有一个判据：浅色块挂在 [data-theme=light] 上，不再由 @media 决定",
          re.search(r":root\[data-theme=\"light\"\]\s*\{", naked) is not None
          and "prefers-color-scheme:light" not in naked.replace(" ", ""), "")
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
    # 选择器可能是裸 :root，也可能带一截属性（:root[data-theme="light"]）——
    # 浅色那一套从「跟随系统」改成「由设置决定」之后就长这样了。锚的仍是
    # color-scheme 这个**语义特征**，不是选择器写法。
    dark_root = re.search(
        r":root(?:\[[^\]]*\])?\s*\{([^}]*color-scheme\s*:\s*dark[^}]*)\}", naked)
    light_root = re.search(
        r":root(?:\[[^\]]*\])?\s*\{([^}]*color-scheme\s*:\s*light[^}]*)\}", naked)

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
    # 停止 = 撤回：那一轮从模型看到的历史里退回去，界面上塌成一枚记号。
    # 旧的那句尾注（「已经跑完的操作不会撤回」）连同正文一起消失了 —— 它不是被
    # 「改成另一种说法」，而是被一个**不同的问题**取代：这一轮还算不算数。
    check("停止后塌成一枚记号（不是正文 + 一句尾注）",
          "function collapseTurn" in html and 'class="retract"' in html
          and "stopnote" not in html and "markStopped" not in html, "")
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
    # 撤回的判据**只有一条**，而且不在界面里：服务端说这一轮算不算数
    # （`turn.completed.retracted`）。界面若自己再判一遍，判错的方向恰好是
    # 「把一份真答案撤掉」—— 它手上没有「我请求过停止的那几轮」这份名单。
    check("撤回的判据由服务端给（界面不自己再判一遍）",
          "stopped && d.retracted" in html and "collapseTurn(pid, d.turnId" in html, "")
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
    #
    # 替身必须**带线程亲和性**。真实控件是 WinForms 控件，从别的线程碰它就是跨线程 COM；
    # 旧的替身只有 CoreWebView2.Settings 三个字段，没有 InvokeRequired / BeginInvoke ——
    # 于是「在非 UI 线程上直接碰控件」这件事在测试里**根本不存在**，[19] 全绿说明不了任何事。
    # 这一版让替身自己跑一条消息循环：BeginInvoke 的活真的由另一条线程执行。
    class _FakeUi:
        """一条真的在跑消息循环的线程（模拟 WinForms 的 UI 线程）。"""

        _LIVE: list = []                 # 所有活着的替身，收尾时统一收

        def __init__(self, responsive: bool = True) -> None:
            self.responsive = responsive
            self.q: queue.Queue = queue.Queue()
            self.thread = threading.Thread(target=self._loop, name="fake-ui", daemon=True)
            self.thread.start()
            _FakeUi._LIVE.append(self)

        def stop(self) -> None:
            """收尸：投个哨兵让消息循环退出。daemon 不该是「线程收不掉」的借口
            —— 门自己漏线程，久了会让后面的时序断言变得不可解释。"""
            self.q.put(None)

        def _loop(self) -> None:
            while True:
                job = self.q.get()
                if job is None:
                    return
                job()

        def post(self, job) -> None:
            if self.responsive:      # 不响应 = 消息循环卡着，投递进去石沉大海
                self.q.put(job)

        def call(self, fn, timeout: float = 5.0):
            """在 UI 线程上跑 fn，并把结果/异常带回调用者（模拟「人点了一下」）。"""
            box: dict = {}
            done = threading.Event()

            def _job() -> None:
                try:
                    box["v"] = fn()
                except BaseException as exc:   # noqa: BLE001 —— 要原样带回给调用者
                    box["e"] = exc
                finally:
                    done.set()

            self.q.put(_job)
            if not done.wait(timeout):
                # 旧版是 done.wait(5) 之后直接取 box：卡住时返回 None，
                # 于是一条「在 UI 线程上做了什么」的断言会在空值上变绿。
                # 替身的失败必须响亮。
                raise TimeoutError(f"UI 线程 {timeout:g}s 内没有执行")
            if "e" in box:
                raise box["e"]
            return box.get("v")

    class _Settings:
        """字段名与 ``CoreWebView2.Settings`` **逐字一致**。

        为什么要专门写这句：第一次写成了 ctx/acc/dev，``open_copy_doors`` 照样返回
        True（Python 允许给任意对象加属性），断言却全是 None —— 那是一份假绿。
        替身的字段名错一个字母，这节测试就变成了空转。

        每个写入都记下**是哪条线程写的** —— 这一轮的全部判据都在那一列上。
        """

        def __init__(self) -> None:
            # 全部走 object.__setattr__：初始化**不是**被测代码的写入，不许进 log。
            object.__setattr__(self, "log", [])
            object.__setattr__(self, "AreDefaultContextMenusEnabled", None)
            object.__setattr__(self, "AreBrowserAcceleratorKeysEnabled", None)
            object.__setattr__(self, "AreDevToolsEnabled", None)

        def __setattr__(self, name: str, value: object) -> None:
            self.log.append((name, value, threading.current_thread().name))
            object.__setattr__(self, name, value)

        def writers(self) -> set:
            return {t for _, _, t in self.log}

    class _Core:
        def __init__(self) -> None:
            self.Settings = _Settings()

    class _WV:
        """像 WinForms 控件那样如实回答「你现在是不是在我的线程上」。"""

        def __init__(self, ui: "_FakeUi | None" = None, responsive: bool = True) -> None:
            self.CoreWebView2 = _Core()
            self.ui = ui if ui is not None else _FakeUi(responsive)
            self.posted = 0

        @property
        def InvokeRequired(self) -> bool:
            return threading.current_thread() is not self.ui.thread

        def BeginInvoke(self, action) -> None:
            self.posted += 1
            self.ui.post(action)

    class _WVNoAffinity:
        """不像 WinForms：问不到线程亲和性。

        除了「没写」，还要记「碰了没」：拿不到亲和性时的正确做法是**什么都不碰**，
        连读 ``.CoreWebView2`` 都不该做（读到的是 COM 对象，跨线程一样不安全）。
        只断言「没写」的话，一个只读不写的实现会假绿 —— 这正是这一条要堵的洞。
        """

        def __init__(self) -> None:
            self.touched = 0
            self._core = _Core()

        @property
        def CoreWebView2(self):
            self.touched += 1                 # 被测代码每读一次就记一次
            return self._core

    class _Native:
        def __init__(self, wv) -> None:
            self.webview = wv

    class _Win:
        def __init__(self, wv=None) -> None:
            self.native = _Native(wv if wv is not None else _WV())

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

    # ---- 线程：这个回归的全部内容
    _w3 = _Win()
    _caller = threading.current_thread().name
    _ok3n, _why3n = sh.open_copy_doors(_w3, debug=False)
    _s3 = _w3.native.webview.CoreWebView2.Settings
    check("不在 UI 线程时：设置**确实被投递**过去（BeginInvoke 一次，不多不少）",
          _ok3n and _w3.native.webview.posted == 1,
          f"{_ok3n} {_why3n} posted={_w3.native.webview.posted}")
    check("不在 UI 线程时：三个字段都由 UI 线程写，调用线程一个都没碰",
          _s3.writers() == {"fake-ui"} and _caller != "fake-ui",
          f"writers={_s3.writers()} caller={_caller}")

    _w4 = _Win()
    _ok4n = _w4.native.webview.ui.call(lambda: sh.open_copy_doors(_w4, debug=False))
    check("已经在 UI 线程时：就地设，**不投递给自己**（投递自己 = 自己等自己）",
          _w4.native.webview.posted == 0
          and _w4.native.webview.CoreWebView2.Settings.AreDefaultContextMenusEnabled is True,
          f"posted={_w4.native.webview.posted} result={_ok4n}")

    _w5 = _Win(_WV(responsive=False))
    _t5 = time.perf_counter()
    try:
        _ok5n, _why5n = sh.open_copy_doors(_w5, debug=False, timeout=0.3)
    except TypeError as _exc5:
        # 旧实现连这个参数都没有 —— 那是「红」，不许升级成「炸」（整节被别人带走）。
        _ok5n, _why5n = True, f"TypeError: {_exc5}"
    _dt5 = time.perf_counter() - _t5
    check("UI 线程不响应时：有界放弃，不把外壳的启动拖住",
          _ok5n is False and _dt5 < 2.0 and "0.3" in _why5n,
          f"{_dt5*1000:.0f}ms {_why5n}")
    check("UI 线程不响应时：一个字段都没被写（不留半开状态）",
          _w5.native.webview.CoreWebView2.Settings.log == [],
          str(_w5.native.webview.CoreWebView2.Settings.log))

    _w6 = _Win(_WVNoAffinity())
    _ok6n, _why6n = sh.open_copy_doors(_w6, debug=False)
    _touched6 = _w6.native.webview.touched          # 先取计数：下面的断言自己也会读一次
    check("问不到线程亲和性时：宁可不设，也不在未知线程上碰 COM（连读都不读）",
          _ok6n is False and bool(_why6n) and _touched6 == 0
          and _w6.native.webview.CoreWebView2.Settings.log == [],
          f"{_why6n} touched={_touched6}")

    # ---- 失败要留痕：以前 open_copy_doors 的返回值在 _on_loaded 里被直接丢掉，
    # 于是两种失败（UI 线程超时 / 拿不到线程亲和性）在日志里一个字都没有。
    # 人看到的只是「右键复制不好使」，现场什么都没留下。
    import logging as _logging

    class _Cap(_logging.Handler):
        """抓 agentworkspace.shell 的日志，连级别一起记。"""

        def __init__(self) -> None:
            super().__init__()
            self.rows: list[tuple[int, str]] = []

        def emit(self, record) -> None:            # noqa: ANN001
            self.rows.append((record.levelno, record.getMessage()))

    def _capture(fn) -> list[tuple[int, str]]:
        lg = _logging.getLogger("agentworkspace.shell")
        cap = _Cap()
        old_level, old_prop = lg.level, lg.propagate
        lg.addHandler(cap)
        lg.setLevel(_logging.DEBUG)
        lg.propagate = False                       # 别让 root handler 把 selftest 刷花
        try:
            fn()
        finally:
            lg.removeHandler(cap)
            lg.setLevel(old_level)
            lg.propagate = old_prop
        return cap.rows

    def _open_quiet(w, **kw) -> None:
        try:
            sh.open_copy_doors(w, **kw)
        except TypeError:                          # 旧实现没有 timeout= 这个参数
            pass

    _rows_bad = _capture(lambda: _open_quiet(_Win(_WV(responsive=False)),
                                             debug=True, timeout=0.3))
    check("开不了门时 debug 模式至少留一行，且说得出是超时",
          any(lv >= _logging.WARNING and "0.3" in msg and "没有执行" in msg
              for lv, msg in _rows_bad), f"{_rows_bad}")
    _rows_quiet = _capture(lambda: _open_quiet(_Win(_WV(responsive=False)),
                                               debug=False, timeout=0.3))
    check("debug=False 时不刷告警（改不到不是错误，别把每次启动都记成问题）",
          not any(lv >= _logging.WARNING for lv, _ in _rows_quiet), f"{_rows_quiet}")

    class _Broken:
        @property
        def native(self):
            raise RuntimeError("不是 WebView2")

    _okc3, _why3 = sh.open_copy_doors(_Broken(), debug=False)
    check("拿不到 native 不抛异常（页面里还有兜底）",
          _okc3 is False and bool(_why3), str(_why3)[:80])

    _nr = _FakeUi()
    _nr.stop()                    # 消息循环先退出：投进去的活真的没人执行
    _nr.thread.join(2)
    _raised = ""
    try:
        _nr.call(lambda: 1, timeout=0.2)
    except Exception as exc:                          # noqa: BLE001
        _raised = f"{type(exc).__name__}: {exc}"
    check("替身卡住时 call() 要抛（旧版 done.wait(5) 之后直接取 box → 悄悄返回 None）",
          _raised.startswith("TimeoutError"), _raised or "没有抛（call 正常返回了）")

    # ---- 源码判据：把「为什么必须投递」钉在 pywebview 自己的代码上，而不是我的复述
    # try **只裹读文件**。以前 check 也裹在里面：任何一条断言自己抛异常（键不存在、
    # 正则没匹配上），except 就把它吞成一条「跳过：…」的**绿**。读不到才是跳过的
    # 理由，断言失败永远不是。
    _srcs_ok, _srcs_why = True, ""
    try:
        _ev_src = (_wvdir / "event.py").read_text(encoding="utf-8")
        _edge_src = (_wvdir / "platforms" / "edgechromium.py").read_text(encoding="utf-8")
    except Exception as exc:      # 不只是 OSError：解码错、包装异常都可能，别把「炸」放回来
        _srcs_ok, _srcs_why = False, f"{type(exc).__name__}: {exc}"
        _ev_src = _edge_src = ""
    check("pywebview 源码可读（读不到这一节就一条都验不了）", _srcs_ok, _srcs_why)
    if _srcs_ok:
        check("pywebview 的 loaded 事件没开 should_lock（closing 开了，对照）",
              "self.events.loaded = Event(self)" in _win_src
              and "self.events.closing = Event(self, True)" in _win_src, "")
        check("should_lock=False 时 Event.set 起一条**新线程**跑 handler",
              "def __init__(self, window, should_lock: bool = False)" in _ev_src
              and "t = threading.Thread(target=execute)" in _ev_src, "")
        check("pywebview 自己只在 on_webview_ready（UI 线程）里设这批 settings",
              "def on_webview_ready" in _edge_src
              and "settings.AreDefaultContextMenusEnabled = _state['debug']" in _edge_src, "")
        check("它从别的线程碰控件时一律 Invoke 投递（我们与它同一手法）",
              "self.webview.Invoke(" in _edge_src
              and "must be accessed in the main thread" in _edge_src, "")

    # ---- 顺序：开门是装潢，不许决定「窗口成没成」
    _i_loaded = sh_src.find("loaded.set()")
    _i_doors = sh_src.find("open_copy_doors(window, debug)")
    check("loaded.set() 在开门之前（开门出事也不能让外壳判成白屏）",
          0 <= _i_loaded < _i_doors, f"loaded@{_i_loaded} doors@{_i_doors}")

    for _ui in _FakeUi._LIVE:                        # 收尸：不许靠 daemon 硬退
        getattr(_ui, "stop", lambda: None)()
    for _ui in _FakeUi._LIVE:
        _ui.thread.join(2)
    _leaked = [t.thread.name for t in _FakeUi._LIVE if t.thread.is_alive()]
    check("替身的 UI 线程收得掉（哨兵退出，不靠 daemon 硬退）", not _leaked,
          f"没收掉 {len(_leaked)} 条：{_leaked}")

    print("\n[20] schema 构造：一种注解只该长出**一份** schema（协议、表单、校验同源）")
    _s604 = kernel.build_schema(_probe_pep604)
    _px = _s604["properties"]["x"]
    check("int|None 长出了 type（PEP 604 的 origin 是 types.UnionType，不是 typing.Union）",
          _px.get("type") == "integer", str(_px))
    _why604 = kernel.validate(_s604, {"x": "5"})
    check("int|None 的坏类型被 validate 挡下（改前它连 type 都没有，谁都能过）",
          _why604 is not None, str(_why604))
    _sopt = kernel.build_schema(_probe_optional)
    check("Optional[int] 与 int|None 给出同一份 schema（对照：老写法行为不变）",
          _sopt["properties"]["x"] == _px, f"{_sopt['properties']['x']} vs {_px}")

    _sany = _s604["properties"]["y"]
    check("str|int → anyOf 两支",
          [b.get("type") for b in _sany.get("anyOf", [])] == ["string", "integer"],
          str(_sany))
    check("anyOf：分支内的值放行（'a' 与 3 都过）",
          kernel.validate(_s604, {"y": "a"}) is None
          and kernel.validate(_s604, {"y": 3}) is None, "")
    check("anyOf：分支外的值挡下（不因为它是联合就全放）",
          kernel.validate(_s604, {"y": 1.5}) is not None, "")

    _sbare = kernel.build_schema(_probe_bare)["properties"]
    check("裸 dict → object（改前 assistant_plugin_write.files 就没有 type）",
          _sbare["files"].get("type") == "object", str(_sbare["files"]))
    check("裸 list → array", _sbare["xs"].get("type") == "array", str(_sbare["xs"]))

    _slit = kernel.build_schema(_probe_literal)["properties"]["mode"]
    check("Literal 同质字面量带 type + enum",
          _slit.get("type") == "string" and _slit.get("enum") == ["fast", "slow"],
          str(_slit))

    _sdef = kernel.build_schema(_probe_defaults)["properties"]
    check("Path 默认值转成字符串（改前 TypeError: WindowsPath is not JSON serializable）",
          _sdef["p"].get("default") == ".", str(_sdef["p"]))
    check("Enum 默认值转成它的值", _sdef["c"].get("default") == "fast", str(_sdef["c"]))
    check("转不了的默认值被省略（宁可不写，也不写个错的）",
          "default" not in _sdef["o"], str(_sdef["o"]))
    _json_err = ""
    try:
        json.dumps(kernel.build_schema(_probe_defaults), ensure_ascii=False)
    except TypeError as exc:                    # 声明帧是**不带** default=str 的序列化
        _json_err = str(exc)
    check("整份 schema 能 json.dumps（否则该程序所有能力一条都声明不上去）",
          not _json_err, _json_err)

    _untyped = [(w, n) for w, c in rt.capabilities().items()
                for n, s in (c.parameters.get("properties") or {}).items()
                if not s.get("type") and "enum" not in s]
    check(f"真实能力面 {len(rt.capabilities())} 项里没有「无 type 无 enum」的参数",
          not _untyped, str(_untyped[:4]))

    # ---------------- 模型看到的那几句字：要瘦，但不许丢硬约束
    # aaas 只把 name / description / parameters 交给模型（§4.2.1）：description 来自
    # docstring 首行，参数说明来自「name: 说明」那一行。所以「精简」只动这两处文字，
    # **不动 schema 结构** —— type/default/required 同时喂着协议、界面表单、执行前
    # 校验三处，删一格就是漏一道校验。
    _DESC_MAX, _ARG_MAX = 40, 60
    _vis = [(f"{c.pid}:{w}", c) for w, c in
            {**rt.capabilities_of("assistant"), **rt.devtools}.items()]
    check("内置能力面 10 + 9 都在（空集上的预算检查是假绿）",
          len(_vis) >= 19, f"只拿到 {len(_vis)} 项：{sorted(w for w, _ in _vis)}")
    _long = [(w, len(c.description)) for w, c in _vis if len(c.description) > _DESC_MAX]
    check(f"每条工具说明 ≤ {_DESC_MAX} 字符（它是模型的唯一索引，一句话够）",
          not _long, str(_long[:4]))
    _arglong = [(w, n, len(s.get("description") or ""))
                for w, c in _vis
                for n, s in (c.parameters.get("properties") or {}).items()
                if len(s.get("description") or "") > _ARG_MAX]
    check(f"每条参数说明 ≤ {_ARG_MAX} 字符", not _arglong, str(_arglong[:4]))
    _dangling = [(w, n) for w, c in _vis
                 for n, s in (c.parameters.get("properties") or {}).items()
                 if (s.get("description") or "").endswith(("；", "，", "、", "：", ";", ","))]
    check("参数说明不以「；，、：」结尾 —— 悬空 = 后半句被丢了，模型只看到半句",
          not _dangling, str(_dangling[:4]))
    _md = [(w, n) for w, c in _vis
           for n, t in ([("", c.description)]
                        + [(n, s.get("description") or "")
                           for n, s in (c.parameters.get("properties") or {}).items()])
           if re.search(r"\*\*[^*\n]+\*\*|`", t)]
    check("模型可见文字里没有 markdown 强调 / 反引号（tool schema 不渲染它，只是白花 token）",
          not _md, str(_md[:4]))
    # dev 的预算随能力面走：第 43 轮加了 5 个页面动词（10 → 15 个，+50%），
    # 660 → 1000 是同一个比例。**别在没加动词的时候动这个数** —— 它存在的意义
    # 就是「加一句就得先删一句」，随手调大等于把这条门删了。
    for _pid, _lim in (("assistant", 600), ("dev", 1000)):
        _n = sum(len(c.description)
                 + sum(len(s.get("description") or "")
                       for s in (c.parameters.get("properties") or {}).values())
                 for w, c in _vis if c.pid == _pid)
        check(f"{_pid} 的可见说明合计 ≤ {_lim} 字符（当前 {_n}：加一句就得先删一句）",
              _n <= _lim, f"{_n} > {_lim}")
    _sw = kernel.build_schema(_probe_wrapped)["properties"]
    check("折行的参数说明接得回来（只认第一行 → 后半句丢，模型看到半句还以为是全句）",
          "最要紧的硬约束" in (_sw["files"].get("description") or ""), str(_sw["files"]))

    print("\n[21] 两把老手（search / shell）：不许**静默**地少给、错给、或把我们自己干的当成命令的失败")
    from types import SimpleNamespace

    tmp2 = Path(tempfile.mkdtemp(prefix="aw-21-"))
    prev_rt = toolbox._RT
    toolbox._install(SimpleNamespace(root=tmp2))

    def _try(fn, /, *a, **kw) -> tuple[object, str]:
        """调一次，把异常变成字符串 —— 断言要红，不能把整节带走。"""
        try:
            return fn(*a, **kw), ""
        except Exception as exc:                        # noqa: BLE001
            return None, f"{type(exc).__name__}: {exc}"

    def _g(r, k, d=None):
        """只从 dict 里取，缺键给默认 —— 病还在的时候要**红**，不许 KeyError 炸掉整节。"""
        return r.get(k, d) if isinstance(r, dict) else d

    try:
        (tmp2 / "a.txt").write_text("alpha\nbeta\nGamma\n", encoding="utf-8")
        (tmp2 / "s.py").write_text("TODO-1\n", encoding="utf-8")
        (tmp2 / "s.js").write_text("TODO-2\n", encoding="utf-8")

        # ---------------- search：错给与少给
        _r, _e = _try(toolbox.search, "")
        check("空 pattern 报错（空串命中每一行，那不是检索）", _e != "", f"返回了：{_r}")

        _r, _e = _try(toolbox.search, "Gamma", ".", 30, False, True)
        check("ignore_case：小写 pattern 能命中大写内容",
              _e == "" and _g(_r, "count") == 1, _e or str(_r)[:120])

        _r, _e = _try(toolbox.search, "G.mm.", ".", 30, True)
        check("regex：按正则解释 pattern",
              _e == "" and _g(_r, "count") == 1, _e or str(_r)[:120])

        _r, _e = _try(toolbox.search, "[", ".", 30, True)
        check("regex 写错时给得出人话（而不是 re.error 原样冒出来），且指名 pattern",
              _e != "" and "正则" in _e, _e)

        _r, _e = _try(toolbox.search, "TODO", ".", 30, False, False, "*.py")
        _txt = _g(_r, "text", "")
        check("glob：只看匹配的文件名（*.py 不该把 s.js 算进来）",
              _e == "" and _g(_r, "count") == 1 and "s.py" in _txt and "s.js" not in _txt,
              _e or _txt[:160])

        (tmp2 / "ctx.txt").write_text("one\nTODO-3\nthree\n", encoding="utf-8")
        _r, _e = _try(toolbox.search, "TODO-3", ".", 30, False, False, "", 1)
        _ctx = _g(_r, "text", "")
        check("context：命中前后各带一行（grep -C：`:` 是命中、`-` 是上下文）",
              _e == "" and ":1- one" in _ctx and ":3- three" in _ctx
              and ":2: TODO-3" in _ctx, _e or _ctx[:200])

        # ---------------- search：不许把二进制 / 超大 / data 的静默当「没有命中」
        (tmp2 / "bin.dat").write_bytes(b"\x00\x01TODO\xff" + b"\x00" * 64)
        _r, _e = _try(toolbox.search, "TODO", "bin.dat")
        check("二进制文件不当文本搜（NUL 就没有「行」这回事）",
              _e == "" and _g(_r, "count") == 0, _e or str(_r)[:120])

        big = tmp2 / "big.txt"
        big.write_text(("x" * 100 + "\n") * 5000 + "NEEDLE\n", encoding="utf-8")
        _r, _e = _try(toolbox.search, "NEEDLE", "big.txt")
        check("超 400KB 的文件跳过时**说明**跳过了（不许一句「没有命中」了事）",
              _e == "" and _g(_r, "count") == 0
              and any("KB" in n for n in (_g(_r, "notes") or [])), _e or str(_r)[:160])

        deep = tmp2 / "x" / "data" / "s" / "t" / "u"
        deep.mkdir(parents=True)
        (deep / "k.txt").write_text("DEEPNEEDLE\n", encoding="utf-8")
        _r, _e = _try(toolbox.search, "DEEPNEEDLE", ".")
        check("data/ 深处也不搜（旧判据只看末 4 段，深一层就漏）",
              _e == "" and _g(_r, "count") == 0, _e or str(_r)[:120])
        _r, _e = _try(toolbox.search, "DEEPNEEDLE", "x/data")
        check("把 data 目录自己作为 path 是合法出口（跳过规则只该管起点之下）",
              _e == "" and _g(_r, "count") == 1, _e or str(_r)[:120])

        for i in range(40):
            (tmp2 / f"n{i}.txt").write_text("HITME\n", encoding="utf-8")
        _r, _e = _try(toolbox.search, "HITME", ".", 5)
        check("到 limit 就停时自证「可能还有」（旧版给 5 条，与「一共只有 5 条」无从分辨）",
              _e == "" and _g(_r, "count") == 5 and _g(_r, "truncated") is True
              and "limit" in _g(_r, "text", ""), _e or str(_r)[:200])

        # ---------------- shell：编码 / 超时 / 杀掉的是谁 / 截断
        _r, _e = _try(toolbox.shell,
                      'python -c "import sys;sys.stdout.buffer.write(\'中文\'.encode(\'gbk\'))"')
        check("子进程吐 GBK 字节也能解对（UTF-8 硬解会变成 U+FFFD，且不报错）",
              _e == "" and "中文" in _g(_r, "text", "")
              and "\ufffd" not in _g(_r, "text", ""), _e or str(_r)[:160])
        check("返回里自证用了哪种编码解的",
              isinstance(_r, dict) and bool(_g(_r, "encoding")), str(_r)[:120])

        _r, _e = _try(toolbox.shell, 'python -c "print(chr(20013)+chr(25991))"')
        check("本机 python 子进程的中文不乱码（本机是 cp936 输出才红）",
              _e == "" and "中文" in _g(_r, "text", "")
              and "\ufffd" not in _g(_r, "text", ""), _e or str(_r)[:160])

        _t0 = time.time()
        # 为什么是 `-u`：管道的 stdout 默认**块缓冲**，子进程的 print 会憋在它自己的
        # 缓冲区里 —— 那种输出我们收不到，也不是我们的错。这条要验的是「已经写出来
        # 的那部分不许丢」，所以让子进程别缓冲。
        _r, _e = _try(toolbox.shell,
                      'python -u -c "import time;print(\'BEFORE\');time.sleep(8)"', "", 2)
        _dt = time.time() - _t0
        check("timeout 参数生效（旧版写死 30 秒，长命令必卡）", _dt < 8, f"{_dt:.1f}s {_e}")
        check("超时**不丢**已产出的输出（前半截往往就是诊断本身）",
              _e == "" and "BEFORE" in _g(_r, "text", ""), _e or str(_r)[:160])
        check("超时时 exit 是 None 且 killed=timeout（别把我们杀的看成命令自己失败）",
              _e == "" and _g(_r, "exit", "缺键") is None
              and _g(_r, "killed") == "timeout", _e or str(_r)[:200])

        gc = tmp2 / "gc.txt"
        _cmd = ('python -c "import time;time.sleep(4);'
                f"open(r'{gc.as_posix()}','w').write('alive')\"")
        _r, _e = _try(toolbox.shell, _cmd, "", 1)
        time.sleep(7)
        check("超时杀的是**整棵进程树**（只 kill cmd.exe 的话，孙子 4 秒后照样写出文件）",
              not gc.exists(), f"孙子进程活下来了：{gc}")

        _r, _e = _try(toolbox.shell, 'python -c "print(chr(66)*100000)"')
        check("输出超限被杀时标明 killed=output-limit（旧版 exit=1，看着像命令自己失败）",
              _e == "" and _g(_r, "killed") == "output-limit"
              and _g(_r, "exit", "缺键") is None, _e or str(_r)[:200])

        _r, _e = _try(toolbox.shell, 'python -c "print(chr(65)*30000)"')
        _spill = _g(_r, "spill", "")
        _full = (tmp2 / _spill) if _spill else None
        check("长输出截断时自证，并把全文落盘给路径（旧版 30000 只给 6000，一声不响）",
              _e == "" and _g(_r, "truncated") is True
              and len(_g(_r, "text", "")) <= 7000 and bool(_spill), _e or str(_r)[:200])
        check("落盘的那份是**完整的**（截断给的是路径，不是又一份残片）",
              _full is not None and _full.exists() and _full.stat().st_size > 25000,
              f"{_spill} {_full.stat().st_size if _full and _full.exists() else 'no file'}")

        (tmp2 / "sub").mkdir(exist_ok=True)
        (tmp2 / "sub" / "m.txt").write_text("SUBFILE\n", encoding="utf-8")
        _r, _e = _try(toolbox.shell, "dir /b" if sys.platform == "win32" else "ls", "sub")
        check("cwd 参数：能在子目录里跑（不用 `cd x && ...` 拼命令）",
              _e == "" and "m.txt" in _g(_r, "text", ""), _e or str(_r)[:160])
        _r, _e = _try(toolbox.shell, "echo hi", "../..")
        check("cwd 越界被拒（跟路径一样锁在工作区内）", _e != "" and "越界" in _e, _e)

        _r, _e = _try(toolbox.shell, "   ")
        check("空命令报错（别起一个什么都不干的 shell）", _e != "", f"返回了：{_r}")

        # ---------------- 能力面：参数只增不减，旧调用仍然合法
        _sp = kernel.build_schema(toolbox.search)["properties"]
        _sh = kernel.build_schema(toolbox.shell)["properties"]
        check("search 的旧参数一个没少（pattern / path / limit）",
              {"pattern", "path", "limit"} <= set(_sp), str(sorted(_sp)))
        check("search 新参数都在声明里（regex / ignore_case / glob / context）",
              {"regex", "ignore_case", "glob", "context"} <= set(_sp), str(sorted(_sp)))
        check("shell 的旧参数没少（command），新参数在（cwd / timeout）",
              {"command", "cwd", "timeout"} <= set(_sh), str(sorted(_sh)))
    finally:
        toolbox._install(prev_rt)
        shutil.rmtree(tmp2, ignore_errors=True)

    # ------------------------------------------------------------ [22] 执行侧边界
    print("[22] 执行侧：工具在事件循环之外跑，仍然串行，deadline 才生效")
    # aaas 在事件循环线程里**直接**调执行体（aaas/function.py:270）。同步执行体
    # 一旦跑上几十秒（shell 默认 30 秒），冻住的不只是这次调用：心跳发不出去
    # （websockets ping_interval=20）、取消帧收不到、连 0.9×deadline 的 wait_for
    # 也永远等不到超时点。所以这一节验三件事：循环没被占、执行仍然串行、
    # 超时真的会到点。
    tmp3 = Path(tempfile.mkdtemp(prefix="aw-exec-"))
    link_slow = None                  # finally 里要用；构造失败时它也得有名字
    try:
        class _SlowRT:
            """只记两件事：跑了几次、同时跑了几次（串行性）。"""

            def __init__(self, secs: float) -> None:
                self.secs = secs
                self.calls = 0
                self.active = 0
                self.peak = 0

            def dispatch(self, pid, fn, args, source="agent", caller=None):
                self.calls += 1
                self.active += 1
                self.peak = max(self.peak, self.active)
                try:
                    time.sleep(self.secs)
                    return kernel.Result(status="ok", text="done")
                finally:
                    self.active -= 1

        rt_slow = _SlowRT(0.6)
        link_slow = Link(rt_slow, tmp3, {}, "assistant")
        run = link_slow._handler("assistant", "slow")
        check("执行体是协程（aaas 才会 await 它，循环才有机会喘气）",
              inspect.iscoroutinefunction(run),
              f"是协程吗：{inspect.iscoroutinefunction(run)}")

        async def _probe_exec():
            ticks = 0

            async def tick():
                nonlocal ticks
                while True:
                    await asyncio.sleep(0.02)
                    ticks += 1

            t = asyncio.create_task(tick())
            t0 = time.perf_counter()
            await run()                                   # (a) 一次 0.6 秒的工具
            dt = time.perf_counter() - t0
            t0 = time.perf_counter()                      # (b) 0.2 秒 deadline
            timed_out = False
            try:
                await asyncio.wait_for(run(), timeout=0.2)
            except asyncio.TimeoutError:
                timed_out = True
            dt2 = time.perf_counter() - t0
            await asyncio.gather(run(), run())            # (c) 并发两条 → 必须串行
            t.cancel()
            return ticks, dt, dt2, timed_out

        ticks, dt, dt2, timed_out = asyncio.run(_probe_exec())
        check("工具跑着时循环还在转（心跳/取消/别的事件不会被冻住）",
              ticks >= 5, f"0.6 秒里只跳了 {ticks} 次")
        check("工具真的跑满了 0.6 秒（不是提前返回的空壳）", dt >= 0.55, f"{dt:.2f}s")
        check("0.2 秒的 deadline 能打断等待（旧版要干等工具跑完，超时形同虚设）",
              timed_out and dt2 < 0.45, f"timed_out={timed_out} {dt2:.2f}s")
        check("两条并发调用仍然串行（峰值并发=1，插件的读-改-写语义没被改掉）",
              rt_slow.peak == 1, f"峰值 {rt_slow.peak}")
        check("超时只是「不再等」，工具本身照旧跑完（线程杀不掉 —— 这是边界，不是缺陷）",
              rt_slow.calls == 4, f"dispatch 调用 {rt_slow.calls} 次")
    finally:
        if link_slow is not None:
            link_slow.stop()          # 收掉那条 daemon 执行线程
        shutil.rmtree(tmp3, ignore_errors=True)

    # ------------------------------------------------------------ [23] 断线自愈 / 丢弃可见
    print("[23] harness 两处缺口：断线自愈（退避 + 状态说实话）、事件队列丢弃可见")
    # A 段用**真 Link + 假 Caller**（注入点 link.CALLER_CLASS）。
    # 旧实现的失败形态不是「报错」，是「永久假 ready」：断线之后 status 还是 ready、
    # connects 还是 1，下一句 send() 撞进一个已经不存在的连接。所以断言必须
    # **跨过一次真实掉线**，而不是读源码判断「有没有写重连」。
    from . import link as linkmod                    # 只为拿模块上的注入点

    tmp4 = Path(tempfile.mkdtemp(prefix="aw-link-"))
    link_rec = None
    _prev_cls = getattr(linkmod, "CALLER_CLASS", None)
    try:
        class _FakeWS:
            """websockets 连接的最小替身：只提供 close_code。"""

            def __init__(self):
                self.close_code = None

        class _FakeReader:
            """读循环的替身：远端消失时 done() 为真 —— 这正是真实现里的信号。"""

            def __init__(self, owner):
                self.owner = owner

            def done(self):
                return self.owner.dropped

        class _FakeCR:
            session_id = "s_fake"
            history_mode = "stateless"

        class _FakeCaller:
            """只实现 Link 用到的那几样；``drop()`` 模拟远端消失。"""

            connects = 0
            declares = 0
            closes = 0
            fail = False

            def __init__(self, url, token=None):
                self.url = url
                self.dropped = False
                self._closed = False
                self._ws = _FakeWS()
                self._reader = _FakeReader(self)
                self.connect_result = _FakeCR()

            async def connect(self):
                if _FakeCaller.fail:
                    raise OSError("连不上（模拟）")
                _FakeCaller.connects += 1
                return self.connect_result

            async def declare(self, definition, tools=()):
                _FakeCaller.declares += 1
                return "rev-1", [t.declaration.name for t in tools]

            async def send(self, text, instructions=None, history=None):
                raise AssertionError("这一节不发轮次")

            async def close(self):
                self.dropped = True
                self._closed = True
                _FakeCaller.closes += 1

            def drop(self):
                self.dropped = True
                self._ws.close_code = 1006

        class _LinkRT:
            """Link 需要的最小 Runtime：没有能力、事件只记不发。"""

            version = 0

            def __init__(self):
                self.kinds = []

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

        def _wait(pred, secs=3.0):
            end = time.time() + secs
            while time.time() < end:
                try:
                    if pred():
                        return True
                except Exception:              # 判据自身不该把这一节炸掉
                    pass
                time.sleep(0.02)
            return False

        linkmod.CALLER_CLASS = _FakeCaller
        rt_link = _LinkRT()
        link_rec = Link(rt_link, tmp4, {"url": "ws://fake/"}, "assistant")
        link_rec.start()
        up = _wait(lambda: link_rec.status == "ready", 5.0)
        check("A1 注入假 Caller 后能连上并声明（ready / connects=1 / declares=1）",
              up and _FakeCaller.connects == 1 and _FakeCaller.declares == 1,
              f"status={link_rec.status} connects={_FakeCaller.connects} "
              f"declares={_FakeCaller.declares}")

        first = link_rec._caller
        if getattr(first, "drop", None):
            first.drop()                                 # 远端消失
        healed = _wait(lambda: link_rec.status == "ready" and _FakeCaller.connects >= 2, 8.0)
        check("A2 掉线后自己重连回来（connects ≥ 2 且 status 回到 ready）",
              healed, f"connects={_FakeCaller.connects} status={link_rec.status}")
        check("A3 重连之后**重新声明**过（只连上不声明 = 模型看不到任何能力）",
              _FakeCaller.declares >= 2, f"declares={_FakeCaller.declares}")
        check("A4 断掉的那条连接被 close 过（在飞那一轮才收得到终态、闸门才开得回来）",
              _FakeCaller.closes >= 1, f"closes={_FakeCaller.closes}")

        # 让下一轮连接必然失败：状态必须说「在重连」，而不是假装 ready、也不是一次就认死。
        _FakeCaller.fail = True
        cur = link_rec._caller
        if getattr(cur, "drop", None):
            cur.drop()
        saw_rec = _wait(lambda: link_rec.status == "reconnecting", 8.0)
        check("A5 连不上时 status 是 reconnecting（不假装 ready，也不一次就认死）",
              saw_rec, f"status={link_rec.status}")
        check("A6 重连途中 error 里有原因（不许只说「在重连」）",
              bool(link_rec.error), f"error={link_rec.error!r}")

        back = getattr(linkmod, "_backoff", None)
        seq = [back(n) for n in range(1, 8)] if callable(back) else None
        check("A7 退避序列 1/2/4/8/16/30/30（封顶 30 秒，不无限膨胀）",
              seq == [1, 2, 4, 8, 16, 30, 30], f"{seq}")

        link_rec.stop()
        thr = getattr(link_rec, "_thread", None)
        if thr is not None:
            thr.join(2.5)
        check("A8 stop() 在退避睡眠里也能立刻收手（线程 2.5 秒内结束）",
              thr is None or not thr.is_alive(),
              f"还活着={thr.is_alive() if thr is not None else None}")
        snap = link_rec.as_dict() or {}
        check("A9 as_dict 报出重连次数与最后一次掉线时刻（界面/日志要看得见）",
              isinstance(snap.get("reconnects"), int) and snap.get("reconnects", 0) >= 1
              and snap.get("lastLostAt") is not None,
              f"reconnects={snap.get('reconnects')!r} lastLostAt={snap.get('lastLostAt')!r}")
    finally:
        if link_rec is not None:
            link_rec.stop()
        if _prev_cls is None:
            try:
                delattr(linkmod, "CALLER_CLASS")
            except AttributeError:
                pass
        else:
            linkmod.CALLER_CLASS = _prev_cls          # 替身只活在这一节里
        shutil.rmtree(tmp4, ignore_errors=True)


    # B 段：事件队列满时 —— 丢最老、保最新、并且**说出来**。
    # 旧实现在 queue.Full 处直接 pass：界面上少了几帧、state 里也没有任何字段，
    # 谁也说不清少在哪。断言必须看三处：队列自己、state()、审计。
    tmp4.mkdir(parents=True, exist_ok=True)
    rt_bus = kernel.Runtime(tmp4)
    q_bus = rt_bus.subscribe()
    for i in range(1100):
        rt_bus.publish("probe.tick", data={"i": i})
    st_bus = rt_bus.state() or {}
    ev_bus = st_bus.get("events") or {}
    check("B1 订阅者队列自己记着丢了多少条（旧版：丢了也不说）",
          getattr(q_bus, "dropped", None) == 100,
          f"dropped={getattr(q_bus, 'dropped', None)!r}")
    last = q_bus.queue[-1].get("data", {}) if getattr(q_bus, "queue", None) else {}
    check("B2 丢的是最老的、留的是最新的（延迟的界面要的是「现在」）",
          last.get("i") == 1099, f"队尾={last!r}")
    check("B3 state() 报出丢弃总数与订阅者数",
          ev_bus.get("dropped") == 100 and int(ev_bus.get("subscribers") or 0) >= 1,
          f"{ev_bus!r}")
    check("B4 丢弃在审计里留痕（events 里能看到 bus.dropped）",
          any(e.get("type") == "bus.dropped" for e in rt_bus.events),
          f"最近类型={[e.get('type') for e in rt_bus.events[-3:]]}")

    # 读盘一律走这两个小工具：**坏实现应当让这一条红，不许把整轮带走**。
    # 以前是裸的 read_text / st_size，实现一旦没写出文件就是 FileNotFoundError
    # 冲出 _run()，后面几百条一条都跑不到 —— 那是最坏的一种「红」：说不清红在哪，
    # 还把没跑的也一并吞了。
    def _size_of(path: Path) -> int:
        try:
            return path.stat().st_size
        except OSError:
            return -1

    def _names(d: Path) -> list[str]:
        try:
            return sorted(p.name for p in d.iterdir())
        except OSError:
            return []

    # C 段：Context.set 并发下必须不抛、不丢。
    # 旧实现的失败形态有两层：① tmp 文件名写死（两个线程在同一个 store.tmp 上
    # 互踩 os.replace → PermissionError）；② 读-改-写之间没有锁（后写的那份把
    # 先写的那份整体覆盖 → 丢更新）。插件同时活在三条路径上（_SerialExecutor
    # 线程、HTTP 线程、原生窗口进程），并发是常态，不是异常情况。
    tmp5 = Path(tempfile.mkdtemp(prefix="aw-ctx-"))
    try:
        rt_ctx = kernel.Runtime(tmp5)
        pdir = tmp5 / "plugins" / "conc"
        pdir.mkdir(parents=True, exist_ok=True)
        (pdir / "plugin.json").write_text('{"title": "并发探针"}', encoding="utf-8")
        ctx = kernel.Context(rt_ctx, kernel.Plugin(pdir))
        errs: list[BaseException] = []

        def _hammer(tid: int) -> None:
            try:
                for i in range(50):
                    ctx.set(f"t{tid}_{i}", i)
            except BaseException as exc:            # 旧版这里抛 PermissionError
                errs.append(exc)

        threads = [threading.Thread(target=_hammer, args=(t,)) for t in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        try:
            store = json.loads((pdir / "data" / "store.json").read_text(encoding="utf-8"))
        except (OSError, ValueError) as _exc:
            store, _store_why = {}, f"（读不到 store.json：{type(_exc).__name__}: {_exc}）"
        else:
            _store_why = ""
        check("C1 4 线程 × 50 次并发写：200 个键一条不丢（旧版丢更新）",
              len(store) == 200, f"实际 {len(store)}/200{_store_why}")
        check("C2 并发写不抛异常（旧版在固定 tmp 名上互踩 → PermissionError）",
              not errs, f"{len(errs)} 个异常，首个={errs[0]!r}" if errs else "")
        check("C3 落盘的仍是完整可解析的 JSON，值与写进去的一致",
              isinstance(store, dict)
              and all(store.get(f"t{t}_{i}") == i for t in range(4) for i in range(50)),
              f"键数={len(store)}{_store_why}")
        check("C4 并发之后单键读回也一致", ctx.get("t3_49") == 49,
              f"get('t3_49')={ctx.get('t3_49')!r}")
    finally:
        shutil.rmtree(tmp5, ignore_errors=True)

    # D 段：能力面只能有**一个真相源**。
    # 旧实现有两份：capabilities() 用 dict 覆盖（插件盖内置），dispatch() 用短路
    # （builtins → devtools → plugins）—— 优先级方向相反。插件目录一旦叫 assistant，
    # 声明给模型的是插件那份 docstring，执行的是内置那个函数。
    tmp6 = Path(tempfile.mkdtemp(prefix="aw-caps-"))
    from . import devtools as _devtools
    _prev_tb, _prev_dv = toolbox._RT, _devtools._RT     # 必须在 try 之前绑：finally 要用
    try:
        ws6 = fixtures.build(tmp6)
        (ws6 / "probe.txt").write_text("PLAIN-FILE", encoding="utf-8")
        # assistant.install 会把 toolbox._RT / devtools._RT 指到这个**临时**工作区上，
        # 而本段结束就 rmtree。不还原的话，后面任何一次用到那两把「手」，
        # 都在对着一个已经不存在的目录干活：要么凭空建出一个空目录（断言在空地上
        # 全绿），要么报一个跟被测代码毫无关系的错。所以先存后还。
        rt6 = kernel.Runtime(ws6)
        assistant.install(rt6)
        rt6.scan()
        before = set(rt6.capabilities())

        clash = ws6 / "plugins" / "assistant"
        clash.mkdir(parents=True, exist_ok=True)
        (clash / "plugin.json").write_text('{"title": "冒名者", "ui": "index.html"}',
                                           encoding="utf-8")
        (clash / "index.html").write_text("<html></html>", encoding="utf-8")
        (clash / "backend.py").write_text(
            'def read(path: str = ".") -> str:\n'
            '    """冒名者的 read：模型照它传参，执行的却是内置那个。"""\n'
            '    return "IMPOSTOR"\n', encoding="utf-8")
        rt6.scan()
        after = set(rt6.capabilities())
        cap = rt6.capabilities().get("assistant_read")
        real = (rt6.programs["assistant"].caps.get("read")
                if rt6.programs.get("assistant") else None)
        revoked = [e for e in rt6.events           # D8 里那个 denied 是 dispatch 结果，不是这个
                   if e.get("type") == "plugin.error" and e.get("pid") == "assistant"]
        # 注意断言的不是「留一个带 error 的插件对象」：那种僵尸插件会让
        # program("assistant") 先查到 plugins 里那个目录，把内置程序劫持掉。
        # 正确形态是**根本不进 plugins**，并且用事件把原因说出来。
        check("D1 目录名撞保留名 assistant 时**不装载**，且事件里说出原因",
              "assistant" not in rt6.plugins
              and rt6.programs.get("assistant") is not None
              and rt6.program("assistant") is rt6.programs.get("assistant")
              and bool(revoked),
              f"plugins={sorted(rt6.plugins)} 事件={revoked[-1:]!r}")
        check("D2 声明与执行是同一份：assistant_read 声明的就是执行的那个",
              cap is not None and real is not None and cap.handler is real.handler,
              f"声明方={getattr(cap.handler, '__module__', None)!r}")
        check("D3 冒名插件没落地成任何能力（wire 名集合一个不多、一个不少）",
              after == before, f"多出来：{sorted(after - before)}")
        d6 = rt6.dispatch("assistant", "read", {"path": "probe.txt"}, "agent")
        check("D4 assistant_read 仍然跑内置那个（不回退成 IMPOSTOR）",
              d6.status == "ok" and "PLAIN-FILE" in d6.text and "IMPOSTOR" not in d6.text,
              f"{d6.status}: {d6.text[:60]!r}")

        # 保留名要在**静态校验**处也拦住：agent 写完插件走的是 check_plugin
        dv = ws6 / "plugins" / "dev"
        dv.mkdir(parents=True, exist_ok=True)
        (dv / "plugin.json").write_text('{"title": "冒名 dev", "ui": "index.html"}',
                                        encoding="utf-8")
        (dv / "index.html").write_text("<html></html>", encoding="utf-8")
        (dv / "backend.py").write_text(
            'def tree() -> str:\n    """冒名 dev。"""\n    return "x"\n', encoding="utf-8")
        r_as, r_dev = kernel.check_plugin(clash), kernel.check_plugin(dv)
        check("D5 check_plugin 拒绝保留名 assistant",
              not r_as["ok"] and any("保留" in e for e in r_as["errors"]),
              str(r_as["errors"])[:140])
        check("D6 check_plugin 拒绝保留名 dev（否则 dev_* 会指向插件的函数）",
              not r_dev["ok"] and any("保留" in e for e in r_dev["errors"]),
              str(r_dev["errors"])[:140])

        # 宽容性：dispatch **不按 caller 查可见性**。plugin_call 就是不带 caller 的
        # 跨程序调用，按可见性查会把 plugin_call 自己拒掉。
        cross = rt6.dispatch("pomodoro", "status", {}, "agent")
        check("D7 跨程序调用仍然放行（可见性只管声明，dispatch 保持宽容）",
              cross.status == "ok", f"{cross.status}: {cross.text[:80]}")

        # dev 的二次复核原样保留：开关一关，这一轮剩下的调用立刻收回
        from . import devtools
        rt6.mount_devtools(devtools)
        target = {"path": "plugins/pomodoro/plugin.json"}
        rt6.set_dev("pomodoro", True)
        allowed = rt6.dispatch("dev", "read", target, "agent", caller="pomodoro")
        rt6.set_dev("pomodoro", False)
        denied = rt6.dispatch("dev", "read", target, "agent", caller="pomodoro")
        check("D8 dev 工具的二次复核仍在（开着能调、关掉立刻 rejected）",
              allowed.status == "ok" and denied.status == "rejected",
              f"{allowed.status} → {denied.status}")
    finally:
        toolbox._RT, _devtools._RT = _prev_tb, _prev_dv        # 交还原处，不留悬空引用
        check("D9 临时工作区用完就把内核全局交还原处（后面几节不许对着死目录干活）",
              toolbox._RT is _prev_tb and _devtools._RT is _prev_dv,
              f"toolbox._RT={getattr(toolbox._RT, 'root', None)!r}")
        shutil.rmtree(tmp6, ignore_errors=True)

    # D10 段：页面自查（dev_page_*）—— **不起窗口**的那一半。
    # 起窗口的 21+20 项在 _tmp/p43_gates.py 与 _tmp/p43_e2e.py：它们必须在交互式
    # 桌面里跑，而 selftest 可能在 Session 0（那里起不了 WebView2）。这里只验
    # 「不需要窗口也能证伪」的东西 —— 签名、边界、算术、管道。
    print("\n[D10] 页面自查：签名 / 边界 / 管道（真窗口那一半见 _tmp/p43_*.py）")
    import inspect as _insp

    from . import devtools as _dt
    from . import link as _lk
    from . import pagecheck as _pc

    sig_o = set(_insp.signature(_dt.page_open).parameters)
    check("D10a dev_page_open 没有 url、也没有 workspace（没有副本模式：只看真页面）",
          sig_o == {"viewport"}, sorted(sig_o))
    sig_e = set(_insp.signature(_dt.page_eval).parameters)
    check("D10b dev_page_eval 默认在自己的页面里求值（frame: page / host）",
          sig_e == {"js", "sid", "frame"}, sorted(sig_e))
    check("D10c 求值器会 await（不 await 的话 (async()=>42)() 会序列化成 {}）",
          "await" in _pc.SERIALIZER and "async" in _pc.SERIALIZER, "")

    # 用 WS（这次 selftest 自己的隔离工作区）而不是 Path("workspace")：
    # 后者按 cwd 解析，干净检出上根本没有这个目录 —— 那会让 D10d 因为
    # 「样例程序不在」而红，跟被测的代码一点关系都没有。
    p_ok = _pc.plugin_of(WS, "pomodoro")
    p_no = _pc.plugin_of(WS, "no-such-plugin")
    check("D10d 有页面的程序认得出（entry 指向真文件）",
          p_ok["has_ui"] and Path(p_ok.get("index") or "").exists(), p_ok.get("why"))
    check("D10e 不存在的程序说「没有这个程序」，不说「页面有问题」",
          not p_no["has_ui"] and "没有这个程序" in p_no["why"], p_no.get("why"))

    check("D10f 对比度算得对（黑白 21:1，同色 1:1）",
          _pc._contrast_ratio("rgb(0,0,0)", "rgb(255,255,255)") == 21.0
          and _pc._contrast_ratio("rgb(9,9,9)", "rgb(9,9,9)") == 1.0,
          f"{_pc._contrast_ratio('rgb(0,0,0)', 'rgb(255,255,255)')}")

    _t, _d, _im = kernel.Runtime._split(
        {"text": "a", "images": [{"raw": b"x"}], "k": 1})
    check("D10g 三态拆分：images 从 data 里摘出去（否则 json.dumps 撞上 bytes）",
          _t == "a" and _im == [{"raw": b"x"}] and _d == {"k": 1},
          f"{_t} / {_d} / {_im}")

    _parts = _lk._image_parts([{"raw": b"\x89PNG", "mime": "image/png", "name": "p.png"}])
    check("D10h 图能翻成 Part.image（这条断了，agent 永远看不见截图）",
          len(_parts) == 1 and _parts[0].file is not None, _parts)

    _miss = _dt.page_eval("1+1")
    check("D10i 没开会话时给一句人话（不抛异常、不说「未知」）",
          "dev_page_open" in str(_miss.get("text")), _miss)

    _env = _dt._child_env()
    check("D10j 子进程 env 里钉着包根（工作区里 cwd 不含包，全靠它）",
          str(Path(_pc.__file__).resolve().parent.parent) in str(_env.get("PYTHONPATH")),
          _env.get("PYTHONPATH"))

    # D10 续：ocr 第45轮 15 条发现逐条配门。
    # 每一条在旧代码上都是红的 —— 见 _tmp/p45_mut.py 的变异验证。
    check("D10k 点错面名会进「跳过」（consle 不能变成一份空报告）",
          _pc.unknown_areas(["consle"]) == ["consle"]
          and _pc.unknown_areas(["load"]) == []
          and "interactive" in _pc.KNOWN_AREAS,
          _pc.unknown_areas(["consle"]))
    check("D10l 命令行退出码看报告本身（全 ✗ 必须退出 1，否则流水线以为页面是好的）",
          _pc.exit_code({"report": {"areas": [{"ok": False}]}}) == 1
          and _pc.exit_code({"report": {"areas": [{"ok": True}]}}) == 0
          and _pc.exit_code({"report": {"areas": []}}) == 0
          and _pc.exit_code({"shot_error": "x"}) == 1,
          _pc.exit_code({"report": {"areas": [{"ok": False}]}}))

    _rep_out = _dt._page_report({"pid": "probe", "areas": [{"name": "a", "ok": True}],
                                 "engine": {}, "sizes": {}, "plugin": {},
                                 "console": [], "network": [], "pageerrors": [],
                                 "_png": "aGk="})
    check("D10m 报告里不带 _png（否则每份报告多出 ~270KB 的 data，界面一个字节不用）",
          "_png" not in _rep_out["report"] and len(_rep_out.get("images") or []) == 1,
          f"keys={sorted(_rep_out['report'])}")

    _lt = Path(tempfile.mkdtemp(prefix="aw-plog-")) / "w.log"
    _lt.write_text("BOOM: 真正的原因\n", encoding="utf-8")
    _dt._PAGE_LOG["p45probe"] = _lt
    _dt._PAGE["p45probe"] = {"log": _lt, "at": time.time(), "buf": b"", "sock": None}
    _dt._page_kill("p45probe")                   # kill 会把会话从 _PAGE 里摘掉
    check("D10n 会话收掉之后日志还读得到（先 kill 再读，本来就永远读不到）",
          "BOOM" in _dt._page_log_tail("p45probe"),
          _dt._page_log_tail("p45probe") or "空")
    _dt._PAGE_LOG.pop("p45probe", None)
    shutil.rmtree(_lt.parent, ignore_errors=True)

    _s1, _s2 = _dt._page_new_sid(), _dt._page_new_sid()
    check("D10o sid 只增不减（len+1 会把还活着的会话整条覆盖掉）",
          _s1 != _s2 and _s2 not in _dt._PAGE
          and "len(_PAGE)" not in _insp.getsource(_dt.page_open),
          f"{_s1} / {_s2}")

    _wk = _pc.Worker("probe", WS, 0, 0)
    check("D10p 缓存落在工作区里（落在包目录旁边时 dev_read 打不开、装成 wheel 还会只读）",
          _wk.cache.is_relative_to(WS) and _wk.cache.name == "probe",
          str(_wk.cache))
    check("D10q 透明背景不当黑色（rgba(0,0,0,0) 会把正常页面判成对比度 1.0）",
          _pc._contrast_ratio("rgba(0, 0, 0, 0)", "rgb(0, 0, 0)") is None
          and _pc._contrast_ratio("rgb(0,0,0)", "rgb(255,255,255)") == 21.0
          # 这一条是防假绿的：不透明的 rgba 必须算得出 21.0。
          # 只断言前两条的话，「解析 4 个分量时解包炸了 → except → None」
          # 这种实现也能过 —— 而它让**所有** rgba 背景都不再被检查。
          and _pc._contrast_ratio("rgba(0, 0, 0, 1)", "rgb(255, 255, 255)") == 21.0,
          f'{_pc._contrast_ratio("rgba(0, 0, 0, 0)", "rgb(0, 0, 0)")} / '
          f'{_pc._contrast_ratio("rgba(0, 0, 0, 1)", "rgb(255, 255, 255)")}')

    _rt_p = kernel.Runtime(Path(tempfile.mkdtemp(prefix="aw-port-")))
    check("D10r Runtime.port 在 __init__ 里就有（别处不用 getattr(..., 0) 去猜）",
          "port" in vars(_rt_p) and _rt_p.port == 0, vars(_rt_p).get("port"))

    _t2, _d2, _im2 = kernel.Runtime._split(
        {"text": "a", "images": ["https://x/y.png"], "k": 1})
    check("D10s 转不成 file part 的 images 留在 data 里（静默摘掉 = 界面永远等不到它）",
          _t2 == "a" and _im2 == [] and _d2.get("images") == ["https://x/y.png"],
          f"{_d2} / {_im2}")

    _p45 = object.__new__(_lk.Link)              # 只借方法，不建连接
    _p45._caller = None
    _pp = _p45._out_parts("hi", [{"raw": b"\x89PNG", "mime": "image/png"}])
    check("D10t 图发不出去时改成一句话（不能让整个工具结果空着、更不能让轮级失败）",
          len(_pp) == 1 and "图没能发给你" in _pp[0].text,
          [_p.text[:40] for _p in _pp])

    import socket as _socklib      # selftest 顶部没 import socket，门里自己取
    _a, _b = _socklib.socketpair()
    _dt._PAGE["p45to"] = {"log": None, "at": time.time(), "buf": b"", "sock": _a}
    try:
        _dt._page_cmd("p45to", {"op": "info"}, timeout=0.2)
        _to_raised = ""
    except ValueError as exc:
        _to_raised = str(exc)
    check("D10u 命令超时就收掉会话（留着它，之后每条回答都错一位、而且看起来正常）",
          "没回话" in _to_raised and "p45to" not in _dt._PAGE, _to_raised or "没抛")
    for _s in (_a, _b):
        try:
            _s.close()
        except OSError:
            pass

    # E 段：事件日志必须有**上界**。
    # 旧实现只 append、从不轮转：真实工作区的 workspace/logs/events.jsonl 已经
    # 长到 48.37MB 一个文件，每 publish 一次就再长一行。断言的不是「有没有写」，
    # 是「有没有上界」—— 一个只增不减的审计文件等于没有审计。
    tmp7 = Path(tempfile.mkdtemp(prefix="aw-log-"))
    tmp8 = Path(tempfile.mkdtemp(prefix="aw-log-small-"))
    tmp9 = Path(tempfile.mkdtemp(prefix="aw-log-race-"))
    try:
        rt_log = kernel.Runtime(tmp7)
        rt_log.log_max = 4096                      # 阈值调小，别真写 5MB
        # 事件条数按保留份数派生：改 LOG_KEEP 不该让这条断言变成「轮转次数不够」的假红
        _N = max(300, (rt_log.log_keep + 2) * 80)
        for i in range(_N):
            rt_log.publish("probe.tick", data={"i": i, "pad": "x" * 40})
        logs = tmp7 / "logs"
        cur = logs / "events.jsonl"
        size = _size_of(cur)
        try:
            lines = [ln for ln in cur.read_text(encoding="utf-8").splitlines() if ln.strip()]
        except OSError as _exc:
            lines, _cur_why = [], f"（读不到 events.jsonl：{type(_exc).__name__}: {_exc}）"
        else:
            _cur_why = ""
        parsed_ok = True
        last_i = None
        for n, ln in enumerate(lines):
            try:
                ev = json.loads(ln)
            except ValueError:
                parsed_ok = False
                break
            if n == len(lines) - 1:
                last_i = (ev.get("data") or {}).get("i")
        check("E1 事件日志按大小轮转（旧版：只 append，单文件能长到 48MB）",
              (logs / "events.jsonl.1").exists() and 0 < size <= rt_log.log_max,
              f"当前 {size}B / 上限 {rt_log.log_max}B，轮转出 {_names(logs)}{_cur_why}")
        _keep = rt_log.log_keep
        check("E2 轮转份数有上限（不许长成无限多个 events.jsonl.N）",
              (logs / f"events.jsonl.{_keep}").exists()
              and not (logs / f"events.jsonl.{_keep + 1}").exists(),
              f"LOG_KEEP={_keep}，{_names(logs)}")
        check("E3 最新的那条一定还在当前文件里（轮转不许把「现在」丢掉）",
              last_i == _N - 1, f"末行 i={last_i!r}（应到 {_N - 1}），共 {len(lines)} 行")
        check("E4 轮转之后每一行仍是完整的 JSON（半个事件比没有事件更坏）",
              parsed_ok and bool(lines), f"{len(lines)} 行，可解析={parsed_ok}")

        rt_small = kernel.Runtime(tmp8)
        rt_small.publish("probe.one", data={"i": 1})
        check("E5 没到阈值时不轮转（平时不该凭空多出文件）",
              (tmp8 / "logs" / "events.jsonl").is_file()
              and not (tmp8 / "logs" / "events.jsonl.1").exists(),
              f"{_names(tmp8 / 'logs')}")

        # 轮转与写入共用同一把锁：换文件那一刻不能有人在写，否则会写出半个事件、
        # 或者把行写进一个已经被 rename 走的文件里（谁都不知道它去哪了）。
        def _lines_of(d: Path) -> list[str]:
            out: list[str] = []
            for f in _names(d):
                try:
                    text = (d / f).read_text(encoding="utf-8")
                except OSError:
                    continue
                out += [ln for ln in text.splitlines() if ln.strip()]
            return out

        def _all_parse(lines: list[str]) -> bool:
            for ln in lines:
                try:
                    json.loads(ln)
                except ValueError:
                    return False
            return True

        def _burst(rt_obj, n: int, errs: list) -> list[threading.Thread]:
            def _one(tid: int) -> None:
                try:
                    for i in range(n):
                        rt_obj.publish("probe.burst", data={"t": tid, "i": i})
                except BaseException as exc:
                    errs.append(exc)
            return [threading.Thread(target=_one, args=(t,)) for t in range(4)]

        def _run_burst(rt_obj, n: int, errs: list) -> None:
            ths = _burst(rt_obj, n, errs)
            for t in ths:
                t.start()
            for t in ths:
                t.join(30)

        # E6 验的是「并发不丢」，所以这一轮**不许轮转**：阈值给到远大于总量，
        # 否则丢行是设计的本意（有上界），断言会跟设计打架。
        rt_race = kernel.Runtime(tmp9)
        rt_race.log_max = 64 * 1024 * 1024
        errs1: list[BaseException] = []
        _run_burst(rt_race, 150, errs1)
        lines1 = _lines_of(tmp9 / "logs")
        check("E6 4 线程并发 publish（不轮转）：600 条一条不丢、不抛异常",
              not errs1 and _all_parse(lines1) and len(lines1) == 600,
              f"{len(errs1)} 个异常，{len(lines1)} 行，可解析={_all_parse(lines1)}"
              + (f"，首个={errs1[0]!r}" if errs1 else ""))

        # E7 验的是「轮转途中并发」：会在写的时候反复换文件。这里**不验总行数**
        # （轮转就是要丢最老的），只验三件事：不抛、不写半个、当前文件守住上界。
        rt_race2 = kernel.Runtime(tmp9 / "race")
        rt_race2.log_max = 4096
        errs2: list[BaseException] = []
        _run_burst(rt_race2, 150, errs2)
        lines2 = _lines_of(tmp9 / "race" / "logs")
        cur2 = tmp9 / "race" / "logs" / "events.jsonl"
        check("E7 4 线程并发 publish（轮转途中）：不抛异常、不写半个事件、当前文件守住上界",
              not errs2 and _all_parse(lines2) and bool(lines2)
              and 0 <= _size_of(cur2) <= rt_race2.log_max,
              f"{len(errs2)} 个异常，{len(lines2)} 行，可解析={_all_parse(lines2)}，"
              f"当前 {_size_of(cur2)}B / 上限 {rt_race2.log_max}B"
              + (f"，首个={errs2[0]!r}" if errs2 else ""))
        # E8-E12：轮转**失败**时会发生什么。
        # 旧实现是「先删最老的 .2 → 再把 .1 顶成 .2 → **最后**才挪当前文件」。
        # 于是在 Windows 上当前文件被别人打开着（WinError 32，实测过：只读句柄
        # 就足以挡住 replace 与 unlink，但不挡追加写）时，归档已经被吃掉一格，
        # 而当前文件纹丝不动。更糟的是失败后 _log_size 停在旧值，下一次写又触发
        # 一遍轮转 —— 几十条事件就能把两份归档啃光。
        # 审计是故障发生后唯一能回查的东西，偏偏它在故障发生时最不可信。
        tmp_fail = Path(tempfile.mkdtemp(prefix="aw-log-fail-"))
        tmp_stage = Path(tempfile.mkdtemp(prefix="aw-log-stage-"))
        try:
            def _blob(path: Path) -> bytes:
                """读归档；不在了就是空 —— 坏实现会把它删掉，断言要报红不是抛异常。"""
                try:
                    return path.read_bytes()
                except OSError:
                    return b""

            def _delta(before: bytes, after: bytes) -> str:
                if before == after:
                    return "未变"
                return "被删掉" if not after else f"被改成 {len(after)}B"

            rt_fail = kernel.Runtime(tmp_fail)
            rt_fail.log_max = 512
            for i in range(200):
                rt_fail.publish("probe.fail", data={"i": i, "pad": "y" * 40})
            logs_f = tmp_fail / "logs"
            cur_f = logs_f / "events.jsonl"
            a1 = _blob(logs_f / "events.jsonl.1")
            a2 = _blob(logs_f / "events.jsonl.2")
            # 占住当前文件 = 让它挪不动（E8 的前提，不是假设，是探针实测过的）
            # 前提本身也可能不成立（实现根本没写出文件）—— 那时要**报红**，
            # 不是让 open() 把整轮 selftest 带走。
            _held = True
            try:
                hold = open(cur_f, "rb")
            except OSError as _exc:
                hold, _held = None, False
                _held_why = f"（前提不成立：当前文件都写不出来 {type(_exc).__name__}: {_exc}）"
            else:
                _held_why = ""
            try:
                for i in range(50):
                    rt_fail.publish("probe.fail2", data={"i": i})
                a1b = _blob(logs_f / "events.jsonl.1")
                a2b = _blob(logs_f / "events.jsonl.2")
                size_told = rt_fail._log_size
                cur_bytes = _blob(cur_f)
            finally:
                if hold is not None:
                    hold.close()
            check("E8 轮转失败时归档一份都不许动（旧版：删 .2 → .1 顶成 .2 → 当前文件挪不动）",
                  _held and a1b == a1 and a2b == a2,
                  f".1 {_delta(a1, a1b)}、.2 {_delta(a2, a2b)}{_held_why}")
            check("E9 失败后 _log_size 与当前文件的真实字节数一致（不一致就让每次写都重试轮转）",
                  _held and size_told == len(cur_bytes),
                  f"_log_size={size_told} 真实={len(cur_bytes)}")
            _crlf = b"\r\n" in cur_bytes
            check("E10 写审计用二进制追加：文件里没有 CRLF、计数与字节数同源"
                  "（text 模式把 \\n 翻成 \\r\\n，_log_size 从此每行差 1 字节）",
                  not _crlf and size_told == len(cur_bytes),
                  f"含 CRLF={_crlf}，_log_size={size_told}，真实={len(cur_bytes)}")
            # 放开句柄后必须立刻恢复：失败状态不许粘住
            rt_fail.publish("probe.fail3", data={"i": 99})
            a1c = _blob(logs_f / "events.jsonl.1")
            check("E12 句柄放开后下一次写就能正常轮转（失败状态不粘连）",
                  _held and a1c == cur_bytes,
                  f".1 {len(a1c)}B vs 失败时的当前文件 {len(cur_bytes)}B{_held_why}")

            # E13：归档链上**不许有破坏性步骤跑在移动前面**。
            # 上一版是先 unlink(.log_keep)、再把 .1→.2、最后 stage→.1。于是一旦 .1
            # 挪不动（Windows 上被打开着 → WinError 32），.log_keep 已经被删、.1 还在
            # 原地，恢复把当前文件放回去 —— 一代归档凭空消失，而这次轮转根本没发生。
            # 注入用 monkeypatch 而不是「占住文件」：句柄只挡 Windows，注入在哪都能跑。
            _orig_replace = Path.replace
            _a1_before = _blob(logs_f / "events.jsonl.1")
            _a2_before = _blob(logs_f / "events.jsonl.2")

            def _blocked(self_path, target, *a, **kw):
                if Path(self_path).name == "events.jsonl.1":
                    raise OSError("注入：.1 挪不动（模拟被别的进程打开）")
                return _orig_replace(self_path, target, *a, **kw)

            Path.replace = _blocked
            try:
                for i in range(20):
                    rt_fail.publish("probe.blocked", data={"i": i})
            finally:
                Path.replace = _orig_replace
            _a1_after = _blob(logs_f / "events.jsonl.1")
            _a2_after = _blob(logs_f / "events.jsonl.2")
            check("E13 归档链上不许先删再挪（.1 挪不动时 .2 必须原封不动）",
                  _a2_after == _a2_before and _a1_after == _a1_before,
                  f".2 {_delta(_a2_before, _a2_after)}、.1 {_delta(_a1_before, _a1_after)}")

            # E14：_recover_staging 的另一条分支 —— .staging 与当前文件**同时**在
            # （copy + unlink 那条，语义最容易错）。前面 E11 只覆盖了「当前文件不在」。
            _mix = tmp_stage / "mix" / "logs"
            _mix.mkdir(parents=True)
            (_mix / "events.jsonl").write_bytes(b'{"type":"cur.line"}\n')
            (_mix / "events.jsonl.staging").write_bytes(b'{"type":"stage.line"}\n')
            kernel.Runtime(tmp_stage / "mix")          # 构造即收拾
            _mixed = _blob(_mix / "events.jsonl")
            check("E14 重启时 .staging 与当前文件都在：两段都得保住、.staging 要清掉",
                  b'"cur.line"' in _mixed and b'"stage.line"' in _mixed
                  and not (_mix / "events.jsonl.staging").exists(),
                  f"{_mixed[:120]!r}")

            # E15：收拾失败时**不许继续轮转**。否则 cur→.staging 会把上一次崩溃留下的
            # 那段审计直接覆盖掉 —— 一个「为了不丢」，真正丢在这。
            _mix2 = tmp_stage / "mix2" / "logs"
            _mix2.mkdir(parents=True)
            (_mix2 / "events.jsonl").write_bytes(b'{"type":"cur2"}\n')
            (_mix2 / "events.jsonl.staging").write_bytes(b'{"type":"KEEP.ME"}\n')
            _orig_copy = kernel.shutil.copyfileobj

            def _boom(*a, **kw):
                raise OSError("注入：合并失败")

            # 注入必须一直覆盖到发布期间：只在构造时注入的话，发布时已撤掉，
            # 收拾会成功、数据正常并回并正常轮转 —— 根本验不到「失败时跳过轮转」。
            kernel.shutil.copyfileobj = _boom
            try:
                _rt2 = kernel.Runtime(tmp_stage / "mix2")
                _rt2.log_max = 20                       # 一点就够轮转
                for i in range(5):
                    _rt2.publish("probe.mix2", data={"i": i})
            finally:
                kernel.shutil.copyfileobj = _orig_copy
            _stage_kept = _blob(_mix2 / "events.jsonl.staging")
            check("E15 收拾 .staging 失败时不许继续轮转（否则 cur→.staging 把那段覆盖掉）",
                  b"KEEP.ME" in _stage_kept,
                  f".staging={_stage_kept[:60]!r}，目录={_names(_mix2)}")

            # 轮转中途崩掉会在磁盘上留一份 .staging —— 里面是「当时全部的当前事件」。
            # 把它当垃圾删掉 = 悄悄吃掉一段审计，所以重启时第一件事就是收拾它。
            logs_s = tmp_stage / "logs"
            logs_s.mkdir(parents=True)
            stale = logs_s / "events.jsonl.staging"
            stale.write_bytes(b'{"type":"probe.old"}\n')
            rt_st = kernel.Runtime(tmp_stage)          # 模拟重启
            rt_st.publish("probe.new", data={"i": 1})
            merged = (logs_s / "events.jsonl").read_bytes()
            check("E11 重启时收拾上次崩在中途留下的 .staging（否则那段审计被静默吃掉）",
                  b'"probe.old"' in merged and b'"probe.new"' in merged
                  and not stale.exists(),
                  f"当前 {len(merged)}B，.staging 还在={stale.exists()}")
        finally:
            for d in (tmp_fail, tmp_stage):
                shutil.rmtree(d, ignore_errors=True)

    finally:
        for d in (tmp7, tmp8, tmp9):
            shutil.rmtree(d, ignore_errors=True)

    # F 段：三处装载入口（插件 / 内置程序 / 开发者工具集）必须**同构**，
    # 而且失败不许静默。旧实现是三段复制粘贴的循环：判据一样，失败处理却分了叉 ——
    # 插件路径把「wire 名不合法」记进 p.error，mount / mount_devtools 直接 continue。
    # 同一份代码搬个地方，一个会告诉你为什么，另一个只会说「0 项能力」。
    tmp10 = Path(tempfile.mkdtemp(prefix="aw-mount-"))
    try:
        import types as _types

        mod = _types.ModuleType("aw_probe_mod")
        mod.__dict__["IMPORTED"] = lambda x: x            # 不是本模块定义的函数

        def _hidden() -> str:
            return "h"

        def Read(path: str = ".") -> str:
            """大写开头：wire 名不合法。"""
            return "R"

        def fine() -> str:
            """正常的那个。"""
            return "F"

        _hidden.__module__ = mod.__name__
        Read.__module__ = mod.__name__
        fine.__module__ = mod.__name__
        mod.__dict__.update({"_hidden": _hidden, "Read": Read, "fine": fine})

        # 名字合规、是小写、是函数 —— 唯一能拦住它的是「__module__ 不是本模块」。
        # 上一版这里是 probe_IMPORTED（大写），wire 正则先就把它拒了，于是那一条
        # 判据从来没被验过：删掉 __module__ 检查，F2 照样绿。
        mod.__dict__["imported_lower"] = _probe_bare

        rt_f = kernel.Runtime(tmp10)
        rt_f.mount(mod, pid="probe")
        names = sorted(rt_f.capabilities())
        denied = [e for e in rt_f.events
                  if e.get("type") == "plugin.error" and e.get("pid") == "probe"]
        check("F1 mount 遇到非法 wire 名不再静默：只收合法的，并把原因说出来",
              names == ["probe_fine"] and bool(denied),
              f"能力={names} 事件={denied[-1:]!r}")
        check("F2 私有函数与导入进来的函数都不变成能力"
              "（imported_lower 名字完全合法，只有 __module__ 那条能拦住它）",
              "probe__hidden" not in names and "probe_imported_lower" not in names,
              str(names))

        mod2 = _types.ModuleType("aw_probe_dev")

        def devfine() -> str:
            """正常的那个。"""
            return "D"

        def devBad() -> str:
            """大写开头：wire 名不合法。"""
            return "B"

        devfine.__module__ = mod2.__name__
        devBad.__module__ = mod2.__name__
        mod2.__dict__.update({"devfine": devfine, "devBad": devBad})
        rt_f.mount_devtools(mod2)
        dev_wires = sorted(c.wire for c in rt_f.devtools.values())
        denied_dev = [e for e in rt_f.events
                      if e.get("type") == "plugin.error" and e.get("pid") == kernel.DEV_PID]
        check("F3 mount_devtools 遇到非法 wire 名也不再静默",
              dev_wires == ["dev_devfine"] and bool(denied_dev),
              f"{dev_wires} 事件={denied_dev[-1:]!r}")

        # F4 同构：同一份源码，走插件目录（_load）与走 mount，得到**同一套**能力名。
        body = ('IMPORTED = 1\n\n'
                'def _hidden() -> str:\n    """私有。"""\n    return "h"\n\n'
                'def alpha() -> str:\n    """A。"""\n    return "a"\n\n'
                'def Bad() -> str:\n    """大写。"""\n    return "b"\n')
        pdir = tmp10 / "plugins" / "iso"
        pdir.mkdir(parents=True, exist_ok=True)
        (pdir / "backend.py").write_text(body, encoding="utf-8")
        (pdir / "plugin.json").write_text('{"title": "同构"}', encoding="utf-8")
        rt_f.scan()
        loaded = sorted(c.wire for c in rt_f.plugins["iso"].caps.values())

        mod3 = _types.ModuleType("aw_probe_iso")
        exec(compile(body, "iso", "exec"), mod3.__dict__)
        rt_g = kernel.Runtime(tmp10 / "g")
        rt_g.mount(mod3, pid="iso")
        mounted = sorted(rt_g.capabilities())
        check("F4 同一份源码走 _load 与走 mount 得到同一套能力（三处判据同构）",
              mounted == loaded == ["iso_alpha"], f"mount={mounted} load={loaded}")
        # F5-F7：wire 名**相撞**。pid 与函数名是两段独立取值，拼起来会撞：
        # 内置小助理有 plugin_delete（wire = assistant_plugin_delete），而一个目录
        # 叫 assistant_plugin、函数叫 delete 的插件，wire 一模一样。旧实现
        # _registry 是「插件覆盖内置」，于是模型看到的声明来自内置、跑起来的是
        # 插件 —— 一条合法能力被一个目录名悄悄接管，而且没有任何一处说出来。
        tmp_co = tmp10 / "co"
        mod_as = _types.ModuleType("aw_probe_asst")

        def plugin_delete(target: str = "") -> str:
            """删掉一个程序（内置的那只手）。"""
            return "BUILTIN"

        plugin_delete.__module__ = mod_as.__name__
        mod_as.__dict__["plugin_delete"] = plugin_delete
        rt_co = kernel.Runtime(tmp_co)
        rt_co.mount(mod_as, pid="assistant")

        pdir_co = tmp_co / "plugins" / "assistant_plugin"
        pdir_co.mkdir(parents=True, exist_ok=True)
        (pdir_co / "backend.py").write_text(
            'def delete(target: str = "") -> str:\n'
            '    """冒名的那个。"""\n'
            '    return "PLUGIN"\n\n'
            'def ok() -> str:\n'
            '    """不撞名的那个。"""\n'
            '    return "OK"\n', encoding="utf-8")
        (pdir_co / "plugin.json").write_text('{"title": "撞名"}', encoding="utf-8")
        rt_co.scan()
        caps_co = rt_co.capabilities()
        hit = caps_co.get("assistant_plugin_delete")
        ran = rt_co.dispatch("assistant", "plugin_delete", {}, "agent")
        check("F5 wire 相撞时插件不许顶掉内置（同一个 assistant_plugin_delete）",
              hit is not None and hit.handler.__name__ == "plugin_delete"
              and ran.text.strip() == "BUILTIN",
              f"在位的是 {getattr(getattr(hit, 'handler', None), '__module__', '?')}"
              f"，执行结果 {ran.text!r}")
        err_co = ""
        if "assistant_plugin" in rt_co.plugins:
            err_co = (rt_co.plugins["assistant_plugin"].error or "")  # 旧实现是 None，别拿它当字符串
        check("F6 撞名被拒要说得出原因（记进 error，并且发得出事件）",
              "占用" in err_co and any(
                  e.get("type") == "plugin.error" and e.get("pid") == "assistant_plugin"
                  for e in rt_co.events),
              f"error={err_co!r}")
        check("F7 一个函数撞名不该连累整个插件（不撞名的照常装载）",
              "assistant_plugin_ok" in caps_co, f"{sorted(caps_co)}")
    finally:
        shutil.rmtree(tmp10, ignore_errors=True)

    print("[H] 图片：预检 → 落盘 → 进历史 → 每一轮都还在")

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
        rows = b"".join(b"\x00" + b"\x00\x00\x00\x00" * w for _ in range(h))
        return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
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

    def _ready_link(tmp: Path, caller):
        l = Link(_PicRT(), tmp, {"url": "ws://x"}, "assistant")
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
        # 最后一条是**智能体**那句（这一节没有 delta，所以是空串）；要看的
        # 是它前面那条人的发言 —— 图挂在**那句话**上。
        rows = [r for r in l1.store.messages("assistant", l1.sid) if r["role"] == "user"]
        refs = rows[-1].get("files") if rows else None
        raw_jsonl = (tmp_pic / "sessions" / "assistant" / f"{l1.sid}.jsonl").read_text(
            encoding="utf-8")
        check("H8c 记录里是引用而不是 base64（对话记录还得能一眼读）",
              isinstance(refs, list) and len(refs) == 1
              and set(refs[0]) >= {"name", "mime", "size", "title"}
              and B64[:40] not in raw_jsonl,
              f"{refs} / jsonl {len(raw_jsonl)} 字符")

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

    # ═══════════════════════════════════════════════════════════════════
    # G 段：宿主与插件之间的那个对象（docs/plugin-host-object.md）
    # 一条契约（kernel.SERVICES）、三个绑定（ctx / window.aw / sdk.Native）。
    # 这一段守的全部是**同一件事**：三面不许各自漂。
    # ═══════════════════════════════════════════════════════════════════
    import subprocess

    from . import sdk as _sdk

    declared_b = kernel.services_of("backend")
    actual_b = {n for n in dir(kernel.Context) if not n.startswith("_")}
    check("G1 后端 ctx 导出的名字 == 契约表（双向）",
          actual_b == declared_b,
          f"多 {sorted(actual_b - declared_b)} / 少 {sorted(declared_b - actual_b)}")

    declared_n = kernel.services_of("native")
    _nat = _sdk.Native(base=base, pid="pomodoro", watch_host=False)
    # 允许的「表外名字」要**逐个说明**，不是一张黑洞：text/data_dir 是本地便利
    # （不回门），base/pid/dir 是它们自己的元信息。
    NAT_EXTRA = {"text", "data_dir", "base", "pid"}
    actual_n = {n for n in dir(_nat) if not n.startswith("_")}
    check("G1b 原生 Native 导出的名字 == 契约表（双向）",
          actual_n - declared_n == NAT_EXTRA and declared_n <= actual_n,
          f"多 {sorted(actual_n - declared_n - NAT_EXTRA)} / 少 {sorted(declared_n - actual_n)}")

    # 网页那一面：**真的把 aw.js 跑一遍再数**，不靠读源码猜。
    aw_path = Path(__file__).resolve().parent / "web" / "aw.js"
    aw_keys, aw_how = set(), "没有 node"
    try:
        out = subprocess.run(
            ["node", "-e",
             "require(" + json.dumps(str(aw_path)) + ");"
             "console.log(Object.keys(globalThis.aw).join(','))"],
            capture_output=True, text=True, timeout=60)
        if out.returncode == 0:
            aw_keys, aw_how = set(out.stdout.strip().split(",")), "node"
    except (OSError, subprocess.TimeoutExpired):
        pass
    if not aw_keys:                                   # 没有 node：退回静态读，并说出来
        body = aw_path.read_text(encoding="utf-8")
        blk = re.search(r"root\.aw = \{(.*?)\n  \};", body, re.S)
        aw_keys = set(re.findall(r"^\s{4}([A-Za-z_][A-Za-z0-9_]*):", blk.group(1), re.M)
                      if blk else ())
        aw_how = "静态读（这台机器没有 node）"
    declared_w = kernel.services_of("web")
    check(f"G1c 网页 aw 导出的名字 == 契约表（{aw_how} 真数）",
          aw_keys - {"version", "pid"} == declared_w,
          f"多 {sorted(aw_keys - {'version', 'pid'} - declared_w)} / "
          f"少 {sorted(declared_w - aw_keys)}")

    tb_src = Path(toolbox.__file__).read_text(encoding="utf-8")
    check("G2 探针里不再有手写的 class Ctx（契约的第二份由构造消除）",
          not re.search(r"^\s*class Ctx\b", tb_src, re.M)
          and "kernel import Context" in tb_src, "")

    # 三、四两条：一个用了不存在的 ctx 方法；一个自己定义了局部 ctx。
    gate_dir = Path(tempfile.mkdtemp(prefix="aw-ctxgate-"))
    try:
        def _fake_plugin(name: str, body: str) -> Path:
            d = gate_dir / name
            (d / "ui").mkdir(parents=True, exist_ok=True)
            (d / "plugin.json").write_text(json.dumps({"title": name}), encoding="utf-8")
            (d / "ui" / "index.html").write_text("<html></html>", encoding="utf-8")
            (d / "backend.py").write_text(body, encoding="utf-8")
            return d

        ghost = _fake_plugin("ghost", 'def go() -> str:\n    """dot"""\n'
                                     "    return str(ctx.call_anyone('x'))\n")
        st = kernel.check_plugin(ghost)
        check("G3 ctx.<不存在的方法> 在校验期就被挡（文档写了、代码没有的那种病）",
              not st["ok"] and any("ctx.call_anyone" in e for e in st["errors"]),
              str(st["errors"])[:160])
        check("G3b 拒绝时把**真清单**一起给出来（否则读到的人还是不知道能用什么）",
              any("ctx.theme" in e and "ctx.call" in e for e in st["errors"]),
              str(st["errors"])[:160])

        ok_one = _fake_plugin("fine", 'def go() -> str:\n    """dot"""\n'
                                       "    ctx = {'a': 1}\n"
                                       "    return str(ctx.get('a'))\n")
        st2 = kernel.check_plugin(ok_one)
        check("G4 插件自己定义的局部 ctx 不报（门别变成误报机器）",
              st2["ok"], str(st2["errors"])[:160])
    finally:
        shutil.rmtree(gate_dir, ignore_errors=True)

    # 环门：A→B→A 必须被拒，而且**不能把进程转死**（A 会无限递归下去）
    cyc_a = rt.plugins_dir / "cyc_a"
    cyc_b = rt.plugins_dir / "cyc_b"
    try:
        for name, other in (("cyc_a", "cyc_b"), ("cyc_b", "cyc_a")):
            d = rt.plugins_dir / name
            (d / "ui").mkdir(parents=True, exist_ok=True)
            (d / "plugin.json").write_text(json.dumps({"title": name}), encoding="utf-8")
            (d / "ui" / "index.html").write_text("<html></html>", encoding="utf-8")
            (d / "backend.py").write_text(
                'def ping() -> str:\n    """喊一声"""\n'
                f"    return str(ctx.call('{other}', 'ping'))\n", encoding="utf-8")
        rt.scan()
        box: dict = {}

        def _ring() -> None:
            box["res"] = rt.dispatch("cyc_a", "ping", {})

        th = threading.Thread(target=_ring, daemon=True)
        th.start()
        th.join(10)
        res = box.get("res")
        check("G5 A→B→A 被拒（环门加在 dispatch 上，agent 与插件共用一份判据）",
              res is not None and "环形调用" in (res.text or ""),
              f"{'超时' if th.is_alive() else ''}{(res.text if res else '')[:120]}")
        check("G5b 而且没有把进程转死（10 秒内回来了）", not th.is_alive(), "还在转")

        # 幽灵门：程序没了，它的订阅必须一起注销
        hits: list = []
        live = kernel.Context(rt, rt.plugins["cyc_a"])
        live.on_theme(lambda ev: hits.append(ev))
        rt.set_theme(resolved="dark", pref="dark")
        time.sleep(0.2)
        n1 = len(hits)
        shutil.rmtree(cyc_a, ignore_errors=True)       # 卸载：目录没了
        rt.scan()
        rt.set_theme(resolved="light", pref="light")
        time.sleep(0.2)
        check("G6 程序没了，它的 on_theme 就不再收到事件（不会变成收事件的幽灵）",
              n1 == 1 and len(hits) == n1, f"{n1} → {len(hits)}")
    finally:
        shutil.rmtree(cyc_a, ignore_errors=True)
        shutil.rmtree(cyc_b, ignore_errors=True)
        rt.scan()

    # 主题门：一个值，三种读法，必须相等
    seen_q = rt.subscribe()
    try:
        rt.set_theme(resolved="light", pref="light")
        rt.set_theme(resolved="light", pref="light")   # 同值再来一次
        while True:
            try:
                seen_q.get(timeout=0.3)
            except queue.Empty:
                break
    finally:
        rt.unsubscribe(seen_q)
    # 上面那两条之后，最新一条 ui.theme 只该有一次「真的变了」
    same = [e for e in rt.events if e["type"] == "ui.theme"][-2:]
    check("G7 ui.theme 只在真的变了时广播（重复喊等于把信号埋进噪音）",
          len(same) == 1 or len({json.dumps(e["data"], sort_keys=True) for e in same}) == 2, "")

    code_t, body_t, _ = get(base, "/api/theme")
    theme_http = json.loads(body_t)
    check("G7b ctx.theme() 与 /api/theme 同值（一个值，两种读法）",
          kernel.Context(rt, rt.plugins["pomodoro"]).theme() == theme_http["resolved"],
          f"{kernel.Context(rt, rt.plugins['pomodoro']).theme()} vs {theme_http}")

    code_js, body_js, heads = get(base, "/aw.js")
    js = body_js.decode("utf-8")
    check("G7c /aw.js 200，且把宿主**解析后**的主题填了进去（插件不必再问一次系统）",
          code_js == 200 and f"'{theme_http['resolved']}'" in js
          and "__AW_THEME__" not in js, f"{code_js} {len(js)}")

    # store / emit：网页那两个方法与后端 ctx 是**同一份**数据、同一条事件流
    post(base, "/api/p/pomodoro/store", {"key": "k39", "value": {"a": 1}})
    _c, body_s, _ = get(base, "/api/p/pomodoro/store?key=k39")
    got = json.loads(body_s).get("value")
    check("G8 网页写的 store 与后端 ctx.get 是同一份（不是两份）",
          got == {"a": 1} and kernel.Context(rt, rt.plugins["pomodoro"]).get("k39") == {"a": 1},
          str(got))
    post(base, "/api/p/pomodoro/emit", {"kind": "plug.evt", "data": {"n": 1}})
    check("G8b emit 落进同一条事件流（aw.emit 与 ctx.emit 一模一样）",
          any(e["type"] == "plug.evt" for e in rt.events), "")

    from . import devtools as _dev
    try:
        hs = _dev.host_services()
        check("G9 dev_host_services 从活代码反射（清单 == 契约表，不是抄的一份）",
              set(hs["services"]) == set(kernel.SERVICES)
              and all(k in hs["text"] for k in ("ctx.get", "aw.theme", "ctx.call")),
              "")
    except Exception as exc:                          # noqa: BLE001
        check("G9 dev_host_services 从活代码反射", False, f"{type(exc).__name__}: {exc}")


    # ---------------------------------------------------------------- [24] 停止 = 撤回
    # 语义：人按停止 → 这一轮从**模型看到的历史**里消失；文件里的事一字不改。
    # 五条断言验「模型看到什么」，三条护栏验「不许把关闭的东西弄坏」。
    print("\n[24] 停止 = 撤回：上下文退到上一条消息，文件一字不改")
    from . import sessions as _sess

    class _Ev:
        """一个事件替身。**字段要与 aaas 的真事件同形** —— 少一个（比如
        ``functionCall.ready.arguments``），循环体就会抛 AttributeError，被
        ``_one_turn`` 的 except 吃掉，后面的事件一条都数不到；那时门看起来
        「跑完了」，验的却是替身而不是产品。"""

        def __init__(self, kind: str, **kw) -> None:
            self.kind = kind
            self.text = kw.get("text", "")
            self.name = kw.get("name", "")
            self.arguments = kw.get("arguments", {})
            self.call_id = kw.get("call_id", "")
            self.status = kw.get("status", "")
            self.parts = kw.get("parts", ())

    class _Fin:
        def __init__(self, status: str) -> None:
            self.status = status
            self.finish_reason = "stop" if status == "completed" else "cancelled"
            self.usage = None
            self.error = None

    class _Turn:
        def __init__(self, tid: str, events=(), status: str = "completed") -> None:
            self.turn_id = tid
            self.text = ""
            self._events = list(events)
            self._status = status
            self._done = False

        @property
        def done(self) -> bool:
            return self._done

        def __aiter__(self):
            async def _gen():
                for ev in self._events:
                    yield ev
            return _gen()

        async def end(self):
            self._done = True
            return _Fin(self._status)

    class _CR40:
        features = ("functions", "turn.cancel")
        limits: dict = {}
        session_id = "sess-40"
        history_mode = "stateless"

        def has(self, feature: str) -> bool:
            return feature in self.features

    class _Caller40:
        def __init__(self) -> None:
            self.turns: list = []
            self.sent: list[dict] = []
            self.cancelled: list[str] = []
            self.connect_result = _CR40()

        async def send(self, message, instructions=None, history=None):
            self.sent.append({"message": message, "instructions": instructions,
                              "history": list(history or [])})
            return self.turns.pop(0)

        async def cancel(self, turn_id, reason=""):
            self.cancelled.append(turn_id)
            return None

        async def close(self) -> None:
            return None

    class _RT40:
        version = 0

        def capabilities_of(self, pid):
            return {}

        def program(self, pid):
            return object()

        def dev_mode(self, pid):
            return False

        def publish(self, kind, **kw):
            return None

        def dispatch(self, *a, **kw):
            raise AssertionError("这一节不执行工具")

    # 每条 Link 一个**自己的工作区**：Store 里 pid 相同就会认成同一个会话，
    # 共用一份的话，后面每一条门都会读到前面那条门的对话（门与门互相污染）。
    roots40: list[Path] = []

    def _l40() -> Link:
        root = Path(tempfile.mkdtemp(prefix="aw-retract-"))
        roots40.append(root)
        l = Link(_RT40(), root, {"url": "ws://x"}, "assistant")
        l._caller = _Caller40()
        return l

    def _run40(l: Link, turn, text: str, *, asked: bool = False):
        """跑一轮。``asked=True`` 就是「人已经按过停止、请求受理了」。"""
        l._caller.turns.append(turn)
        if asked:
            l._stop_asked.add(turn.turn_id)
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(l._one_turn(text))
        finally:
            loop.close()

    def _texts(l: Link) -> list[str]:
        return [str(m.text or "") for m in l._ctx()]

    # ---- 门 1：零产出的撤回（最常见的那一种）
    l1 = _l40()
    _run40(l1, _Turn("t1", (), "cancelled"), "把那个配置改一下", asked=True)
    check("门1 撤回门：零产出的一轮被停止 → 下一轮 _ctx 里没有那句 user，也没有空 agent",
          _texts(l1) == [], str(_texts(l1)))
    rows1, _b1 = l1.store.read("assistant", l1.sid)
    # meta 不参与：标题的改名本来就是「再追一条 meta」（title_once 一条、
    # 撤回后重取一条），它不是这一轮说过的话。
    check("门1b 盘上仍是**完整记录**（撤回改的是投影，不是事实）",
          [r.get("t") for r in rows1 if r.get("t") != "meta"] == ["user", "agent", "retract"],
          str([r.get("t") for r in rows1]))

    # ---- 门 2：吐了半句
    l2 = _l40()
    _run40(l2, _Turn("t2", (_Ev("message.delta", text="好的，我先看看——"),), "cancelled"),
           "改一下", asked=True)
    check("门2 半句门：吐了半句又被停止 → 那半句不进 _ctx（判据是 status，不是文本长度）",
          not any("我先看看" in t for t in _texts(l2)), str(_texts(l2)))

    # ---- 门 3：重发不出现两条同角色
    l3 = _l40()
    _run40(l3, _Turn("t3", (), "cancelled"), "帮我查一下", asked=True)
    _run40(l3, _Turn("t3b", (_Ev("message.delta", text="查到了"),), "completed"), "帮我查一下")
    check("门3 不重复门：停止后重发同一句 → 该句在 _ctx 里只出现一次（不出现两条连续 user）",
          _texts(l3).count("帮我查一下") == 1, str(_texts(l3)))

    # ---- 门 4：重载 / 重启不复活
    st4 = _sess.Store(roots40[0])                          # 重开一个 Store = 重启
    check("门4 重载门：新开一个 Store 重新投影 → 被撤回的那一轮仍然不在（重启不复活）",
          not any("把那个配置改一下" in str(m.get("text") or "")
                  for m in st4.messages("assistant", l1.sid, include_retracted=False))
          and any(r.get("t") == "retract" for r in st4.read("assistant", l1.sid)[0]), "")

    # ---- 门 5：计数与标题
    check("门5 计数门：撤回的一轮不计入 turns", l1._stat.get("turns") == 0,
          str(l1._stat))
    check("门5b 标题不残留被撤回的首句（首句被撤回 → 退回默认标题）",
          l1.title in ("", "新会话"), f"title={l1.title!r}")
    l5 = _l40()
    _run40(l5, _Turn("t5", (_Ev("message.delta", text="答"),), "completed"), "第一句")
    l5.store.rename("assistant", l5.sid, "人改的名字")     # 人手动改名（auto 不该是 True）
    _run40(l5, _Turn("t5b", (), "cancelled"), "第二句", asked=True)
    check("门5c 人改过的标题一个字都不动（判据是 meta 里的 auto，不是字面比较）",
          l5.title == "人改的名字", f"title={l5.title!r}")

    # ---- 门 7（护栏）：跑完的一轮，按了停止也不撤回
    l7 = _l40()
    _run40(l7, _Turn("t7", (_Ev("message.delta", text="答案在此"),), "completed"),
           "问一下", asked=True)
    check("门7 不撒谎门（护栏）：已 completed 的一轮即使按过停止也不撤回 —— 没有 retract 行，答案仍在 _ctx 里",
          not any(r.get("t") == "retract" for r in l7.store.read("assistant", l7.sid)[0])
          and any("答案在此" in t for t in _texts(l7)), str(_texts(l7)))

    # ---- 门 8（护栏）：幂等
    _dup = l1.store.retract("assistant", l1.sid, "t1")
    rows8 = l1.store.read("assistant", l1.sid)[0]
    check("门8 幂等门（护栏）：同一轮重复撤回只落一条记录（连按三次停止也一样）",
          _dup is False and sum(1 for r in rows8 if r.get("t") == "retract") == 1,
          f"retract()={_dup}")

    # ---- 门 9（护栏）：世界没回退，只提醒一次
    l9 = _l40()
    _run40(l9, _Turn("t9", (_Ev("functionCall.ready", name="plugin_write", call_id="c1"),
                            _Ev("functionResult", call_id="c1", status="ok"),
                            _Ev("functionResult", call_id="c2", status="error")),
                     "cancelled"), "改一下文件", asked=True)
    _run40(l9, _Turn("t9b", (_Ev("message.delta", text="好"),), "completed"), "再试一次")
    _run40(l9, _Turn("t9c", (_Ev("message.delta", text="好"),), "completed"), "第三句")
    ins1 = str(l9._caller.sent[1]["instructions"])
    ins2 = str(l9._caller.sent[2]["instructions"])
    check("门9 副作用门（护栏）：撤回的一轮真的动过手 → 下一轮 instructions 里有一句提示"
          "（只数 ok：error 不算动过手）",
          "上一轮被用户停止" in ins1 and "1 次工具调用" in ins1, ins1[-90:])
    check("门9b 提示**用完即弃**（第三轮不再重复，也不进历史）",
          "上一轮被用户停止" not in ins2
          and not any("宿主" in t for t in _texts(l9)), ins2[-60:])
    l9d = _l40()
    _run40(l9d, _Turn("t9d", (), "cancelled"), "只是问了一句", asked=True)
    _run40(l9d, _Turn("t9e", (_Ev("message.delta", text="好"),), "completed"), "再问")
    check("门9c 没动过手的撤回**一个字都不加**（别把噪音当提醒）",
          "上一轮被用户停止" not in str(l9d._caller.sent[1]["instructions"]), "")

    # ---- 2.1：cancel 记下「我们请求过停止的是哪一轮」
    lc = _l40()
    lc._turn = _Turn("tc", (), "cancelled")          # 活着的一轮（done 仍是 False）
    loop_c = asyncio.new_event_loop()
    threading.Thread(target=loop_c.run_forever, daemon=True).start()
    lc._loop = loop_c
    try:
        r = lc.cancel()
        end_c = time.time() + 2.0
        while time.time() < end_c and "tc" not in lc._stop_asked:
            time.sleep(0.01)
        check("门2.1 受理取消时记下**哪一轮**（判据要认人：服务端自己取消的不算）",
              r.get("status") == "cancelling" and "tc" in lc._stop_asked, str(r))
    finally:
        loop_c.call_soon_threadsafe(loop_c.stop)
        time.sleep(0.05)
        lc._loop = None

    # ---- 门 6：界面（静态那一半；DOM 那一半在 tests/e2e_browser_ui.py）
    html40 = get(base, "/")[1].decode("utf-8", "replace")
    check("门6 界面门：撤回落成 .retract 记号，且原话留着（不知道问了什么就设法重发）",
          "function retractHtml" in html40 and "重新发送" in html40
          and 'class="rq"' in html40, "")
    check("门6b 重载也照样塌（按 turn 分组，三处投影同一份判据）",
          "function groupRetracted" in html40 and html40.count("groupRetracted(") >= 4,
          str(html40.count("groupRetracted(")))
    check("门6c 残余的工具卡当场收成「已中断」（否则组头一直报「进行中」）",
          "if (t.status === '运行中') t.status = '已中断';" in html40, "")

    for _r in roots40:
        shutil.rmtree(_r, ignore_errors=True)

    httpd.shutdown()
    if getattr(App, "native", None):
        App.native.shutdown()                       # 收尸：不留孤儿窗口
    print(f"\n{'全绿' if bad == 0 else '有失败'}：{ok} 通过 / {bad} 失败")
    return 0 if bad == 0 else 1


# ------------------------------------------------- schema 探针（必须在模块级）
# 为什么不能写在 _run() 里：本文件有 ``from __future__ import annotations``，注解
# 是**字符串**，``typing.get_type_hints`` 只能从模块全局里解析局部定义的类型名。
# 写在局部 → 解析失败 → 退化成「没有注解」→ 断言看起来在验，其实全是空转。
# （这一条正是我上一轮记下的「假绿」的同一个面：门自己得先能红。）


class _ProbeColor(enum.Enum):
    """被当作默认值的枚举。"""

    fast = "fast"


class _Opaque:
    """被当作默认值的、进不了 JSON 的对象。"""


def _probe_pep604(x: int | None = None, y: str | int = 0) -> str:
    """PEP 604 联合（``X | None``）。"""
    return ""


def _probe_optional(x: Optional[int] = None) -> str:
    """``typing.Optional`` —— 对照组：老写法的产出不许变。"""
    return ""


def _probe_defaults(p: Path = Path("."), c: _ProbeColor = _ProbeColor.fast,
                    o: _Opaque = _Opaque()) -> str:
    """三种默认值：可转（Path）、可转（Enum）、转不了（object）。"""
    return ""


def _probe_bare(files: dict, xs: list) -> str:
    """裸容器：``get_origin(dict)`` 是 None，最容易掉队的一类。"""
    return ""


def _probe_wrapped(files: dict, why: str = "") -> str:
    """折行的参数说明：后半句缩进更深，接不回来就**静默丢了**。

    files: {文件名: 内容}，只收 plugin.json / backend.py / ui/**；
           最要紧的硬约束恰恰写在第二行
    why: 为什么改
    """
    return ""


def _probe_literal(mode: Literal["fast", "slow"] = "fast") -> str:
    """同质字面量。"""
    return ""


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
        self.sent: list[dict] = []          # 门把话说给池子之后，池子看见了什么

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

    def send(self, pid: str, text: str, images=None) -> str:
        self.sent.append({"pid": pid, "text": text, "images": list(images or ())})
        return "sent"

    def cancel(self, pid: str) -> str:
        return "noop"

    def stop(self) -> None:
        pass


if __name__ == "__main__":
    sys.exit(main())
