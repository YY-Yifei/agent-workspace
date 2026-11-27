"""人这一侧的门 —— 本地 HTTP + SSE。**零智能。**

它只做三件事：

    1. 把界面文件发出去（宿主页面 + 各程序页面）
    2. 把人点的按钮翻译成一次 ``Runtime.dispatch(..., source="human")``
    3. 把人的话交给那个程序的智能体，把事件流（SSE）推回界面

它不决定任何事：不选程序、不排顺序、不解释模型输出。
和 agent 走 invokeFunction 的那条路相比，**唯一的区别是 source 的取值**。

一条路由上的规矩：**所有对话都带 pid**（``/api/p/<pid>/chat``）。
因为「一句话属于哪个程序」不是一个可以猜的东西 —— 猜错就会把「造个番茄钟」
送进番茄钟自己的上下文里，而它根本听不懂（它只有 pomodoro_* 的能力）。
"""

from __future__ import annotations

import json
import mimetypes
import queue
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from . import sessions, settings

mimetypes.add_type("text/javascript", ".js")
mimetypes.add_type("text/css", ".css")

CALL_RE = re.compile(r"^/api/p/(?P<pid>[a-z][a-z0-9_]*)/call/(?P<fn>[A-Za-z_][A-Za-z0-9_]*)$")
OPEN_RE = re.compile(r"^/api/p/(?P<pid>[a-z][a-z0-9_]*)/(?P<act>open|close)$")
AGENT_RE = re.compile(r"^/api/p/(?P<pid>[a-z][a-z0-9_]*)/(?P<act>chat|cancel|link)$")
DEV_RE = re.compile(r"^/api/p/(?P<pid>[a-z][a-z0-9_]*)/dev$")
PLUGIN_RE = re.compile(r"^/plugin/(?P<pid>[a-z][a-z0-9_]*)(?P<rest>/.*)?$")
# 版本历史：人这一侧的「看一眼 / 退回去」。两个都只作用于 plugins/<pid>，
# 与 static 路由一样按 pid 分段匹配 —— 不拼路径，就不会有越界。
VERSIONS_RE = re.compile(r"^/api/p/(?P<pid>[a-z][a-z0-9_]*)/versions$")
# 宿主对象的另外两条腿：状态（与后端 ctx.get/set 同一份 store.json）与「喊一声」。
# 它们**不是**新的执行入口 —— 没有 handler、不 import 插件，只碰这一个 pid 的
# 自己的数据；所以它们不必经过 dispatch，也不该被当成能力声明给模型。
STORE_RE = re.compile(r"^/api/p/(?P<pid>[a-z][a-z0-9_]*)/store$")
EMIT_RE = re.compile(r"^/api/p/(?P<pid>[a-z][a-z0-9_]*)/emit$")
# 会话：列表 / 单个 / 四个动作。sid 的形状在这里就钉死（s_ + 8 位 hex）——
# 它同时是路径分量，形状收紧了，越界就不必再靠 resolve 兜。
SESSIONS_RE = re.compile(r"^/api/p/(?P<pid>[a-z][a-z0-9_]*)/sessions$")
SESSION_ONE_RE = re.compile(r"^/api/p/(?P<pid>[a-z][a-z0-9_]*)/session/(?P<sid>s_[0-9a-f]{8})$")
SESSION_SID_RE = re.compile(r"^s_[0-9a-f]{8}$")
SESSION_ACT_RE = re.compile(
    r"^/api/p/(?P<pid>[a-z][a-z0-9_]*)/session/(?P<act>new|switch|rename|delete)$")
# 一张图。名字就是盘上那个名字（``<16 位 sha>.<后缀>``），形状在这里钉死。
SESSION_IMG_RE = re.compile(
    r"^/api/p/(?P<pid>[a-z][a-z0-9_]*)/session/(?P<sid>s_[0-9a-f]{8})"
    r"/img/(?P<name>[0-9a-f]{16}\.(?:png|jpg|webp|gif))$")


class App:
    """四个依赖：内核、智能体池、静态根目录、原生窗口宿主。

    宿主页面上的一切都从它们长出来 —— 这里没有第五样东西。
    """

    runtime = None          # kernel.Runtime
    hub = None              # link.Hub（每个程序一条）
    native = None           # native.NativeHost（原生程序用的进程账本）
    web: Path = Path("web")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a) -> None:      # 别把每条请求刷到终端
        pass

    # -- 基础 ----------------------------------------------------------
    def _send(self, code: int, body: bytes, ctype: str,
              headers: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("content-type", ctype)
        self.send_header("content-length", str(len(body)))
        self.send_header("cache-control", "no-store")
        # 额外的头是**调用方给的**，不是这里发明的：目前只有一个用户 ——
        # 导出的 content-disposition（文件下载要它，否则浏览器会把它当一页打开）。
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _file(self, path: Path) -> None:
        if not path.is_file():
            self._json({"error": "not found", "path": str(path)}, 404)
            return
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
            ctype += "; charset=utf-8"
        self._send(200, path.read_bytes(), ctype)

    def _origin_ok(self) -> bool:
        """状态变更请求只接受**本机页面**发来的。

        为什么需要：这道门只听 127.0.0.1，但「只听本机」不等于「只有本机页面能用」——
        任何网页都能往 http://127.0.0.1:<port>/api/p/<pid>/chat 发一个 text/plain 的
        JSON body（不需要 CORS 预检），替人开口说话、拨开发者模式、改设置；
        DNS rebinding 还能把 Host 变成攻击者的域名。

        判据两条：Host 必须是本机名；有 Origin 就必须是本站。
        （curl / --cli 这类没有 Origin 的本机客户端照旧放行。）
        """
        host = (self.headers.get("host") or "").strip().lower()
        name = host.rsplit(":", 1)[0] if not host.startswith("[") else host.split("]")[0] + "]"
        if name and name not in ("127.0.0.1", "localhost", "[::1]", "::1"):
            return False
        origin = self.headers.get("origin")
        if origin:
            try:
                o = urlsplit(origin)
                port = o.port                    # 畸形值（http://[、端口越界）会抛 ValueError
            except ValueError:
                return False                     # 头完全由客户端控制，解析失败就当来路不明
            if (o.hostname or "").lower() not in ("127.0.0.1", "localhost", "::1"):
                return False
            # 必须**显式**等于本机端口：o.port is None 指的是「协议默认端口」(80/443)，
            # 那不是我们这道门听的随机端口
            if port != self.server.server_address[1]:
                return False
        return True

    # -- 路由 ----------------------------------------------------------
    def do_GET(self) -> None:
        path = unquote(urlsplit(self.path).path)
        rt, hub = App.runtime, App.hub

        if path in ("/", "/index.html"):
            return self._file(App.web / "index.html")
        if path.startswith("/web/"):
            target = (App.web / path[5:]).resolve()
            # 按路径分量判，不是字符串前缀：兄弟目录名只要以同一前缀开头
            # （web-evil / web2）就能骗过 startswith
            if not target.is_relative_to(App.web.resolve()):
                return self._json({"error": "forbidden"}, 403)
            return self._file(target)

        if path == "/aw.js":
            # 宿主服务脚本。**三个占位现场替换**：pid 留空（页面自己按地址认），
            # 外观填当前解析后的值 —— 插件页面因此拿到的就是宿主的答案，
            # 不必再问一次系统、也不会出现「一个身体两种颜色」。
            js = (App.web / "aw.js").read_text(encoding="utf-8")
            js = (js.replace("__AW_PID__", "")
                    .replace("__AW_THEME__", rt.theme_now())
                    .replace("__AW_PREF__", rt.theme_pref()))
            return self._send(200, js.encode("utf-8"),
                              "application/javascript; charset=utf-8")

        if path == "/aw.css":
            # 样式契约（docs/host-styles.md）。与 /aw.js 同一条思路：**同一个 URL、
            # 同一份字节** —— 宿主 index.html 引它，插件页（自己引的、宿主注入的）
            # 也引它。插件页在 iframe 里是另一份文档，自定义属性不跨文档继承，
            # 所以「插件像宿主」这件事只能靠同一个文件，靠抄一份必然漂移。
            return self._file(App.web / "aw.css")

        if path == "/api/theme":
            # 给原生窗口与插件页面用的一问一答（网页那两个值就在 /aw.js 里）。
            return self._json({"pref": rt.theme_pref(), "resolved": rt.theme_now()})

        if path == "/api/state":
            # 门只做搬运，**不重新发明** state 的形状：内核多一个字段（比如 devtools），
            # 界面就该收到它。这里曾经逐项挑着搬，于是新增字段静默消失。
            from . import pluginvcs

            vcs_ok, vcs_why = pluginvcs.available()
            st = rt.state()
            return self._json({**st,
                               "native": App.native.info() if App.native else {},
                               # 设置面板要的那两项：连哪个道 + 什么外观。
                               # 主题是「这台机器上这个工作台长什么样」，
                               # 与地址同级 —— 所以它跟着 state 一起下来。
                               "settings": {"url": hub.url if hub else "",
                                            "theme": settings.load_theme(
                                                hub.root if hub else rt.root)},
                               "agents": hub.status() if hub else {},
                               "vcs": {"enabled": vcs_ok, "reason": vcs_why}})
        if path == "/api/events":
            return self._sse()
        if path == "/api/settings":
            if hub is None:
                return self._json({"error": "智能体池未装配"}, 503)
            return self._json({"url": hub.url, "theme": settings.load_theme(hub.root),
                               "agents": hub.status()})

        if path == "/api/plugins/export":
            # 插件的「导出」：同一个入口，`dry=1` 只说「会装进什么」，否则回字节。
            # **data 默认 0** —— 「默认不导出数据」这条要求的判据就在这半行里，
            # 不在界面上那枚勾选框：勾选框只表达这一次的选择，而且它每次开面板
            # 都会被重置。判据放服务端，界面忘了重置也不会悄悄带上数据。
            from . import pluginpkg

            q = parse_qs(urlsplit(self.path).query)
            pid = (q.get("pid") or [""])[0]
            want_data = (q.get("data") or ["0"])[0] in ("1", "true", "yes")
            plugin = rt.plugins.get(pid)
            if plugin is None:
                why = ("内置程序没有插件目录，导不出" if rt.program(pid) is not None
                       else f"没有程序 {pid}")
                return self._json({"status": "bad", "error": why}, 404)
            try:
                if (q.get("dry") or [""])[0] == "1":
                    return self._json({"status": "ok", "dry": True,
                                       **pluginpkg.preview(plugin.path, pid=pid,
                                                           include_data=want_data)})
                blob, name = pluginpkg.export(rt, pid, include_data=want_data)
            except pluginpkg.BadPackage as exc:      # 消息给人看，原样回
                return self._json({"status": "bad", "error": str(exc)}, 400)
            # 文件名是 pid + 时间戳（纯 ASCII），所以能直接进 content-disposition；
            # 中文标题不进文件名 —— 那要走 RFC 5987，而这里没有值得为它加一层的
            # 信息（标题在包的 awp.json 里，装上去就是它）。
            return self._send(200, blob, "application/zip", {
                "content-disposition": f'attachment; filename="{name}"'})

        m = STORE_RE.match(path)
        if m:
            pid = m.group("pid")
            plugin = rt.plugins.get(pid)
            if plugin is None:
                return self._json({"error": f"没有程序 {pid}"}, 404)
            q = parse_qs(urlsplit(self.path).query)
            key = (q.get("key") or [""])[0]
            if not key:
                return self._json({"error": "缺 key"}, 400)
            from .kernel import Context

            return self._json({"key": key, "value": Context(rt, plugin).get(key)})

        m = VERSIONS_RE.match(path)
        if m:
            pid = m.group("pid")
            if rt.program(pid) is None:
                return self._json({"error": f"没有程序 {pid}"}, 404)
            from . import pluginvcs

            ok, why = pluginvcs.available()
            # 多取一个：多出来的那个只用来判断「还有更早的」，不上界面。
            # 面板不该让人以为「这个程序一共就这么多版」—— 静默截断比报错更坏。
            LIMIT = 30
            rows = (pluginvcs.versions(rt.root / "plugins" / pid, limit=LIMIT + 1)
                    if ok else [])
            more = len(rows) > LIMIT
            rows = rows[:LIMIT]
            return self._json({
                "pid": pid, "enabled": ok, "reason": "" if ok else why,
                "truncated": more, "shown": len(rows),
                "versions": [{k: v[k] for k in ("ver", "short", "sha", "msg", "when")} | {
                    "files": sorted(v["files"])} for v in rows]})

        # 会话列表。截断照抄 versions 的三件套：静默截断比报错更坏 ——
        # 人以为「就这么多」，于是以为旧对话丢了。
        m = SESSIONS_RE.match(path)
        if m:
            pid = m.group("pid")
            if rt.program(pid) is None:
                return self._json({"error": f"没有程序 {pid}"}, 404)
            if hub is None:
                return self._json({"error": "智能体池未装配"}, 503)
            LIMIT = 50
            out = hub.sessions(pid, limit=LIMIT)
            if out.get("status") == "missing":
                return self._json(out, 404)
            return self._json(out)

        # 单个会话的全部消息。limit 与 versions 同义：多取一条只用来判断还有没有。
        m = SESSION_ONE_RE.match(path)
        if m:
            pid, sid = m.group("pid"), m.group("sid")
            if rt.program(pid) is None:
                return self._json({"error": f"没有程序 {pid}"}, 404)
            if hub is None:
                return self._json({"error": "智能体池未装配"}, 503)
            l = hub.link(pid, create=True)
            if l is None or not l.store.exists(pid, sid):
                return self._json({"error": f"没有会话 {sid}"}, 404)
            q = parse_qs(urlsplit(self.path).query)
            try:
                LIMIT = max(1, min(1000, int((q.get("limit") or ["200"])[0])))
            except ValueError:
                LIMIT = 200
            rows = l.store.messages(pid, sid)
            meta = l.store.meta(pid, sid)
            stats = l.store.stats(pid, sid)
            total = len(rows)
            return self._json({
                "pid": pid, "sid": sid, "title": meta.get("title", ""),
                "active": sid == l.sid,
                "messages": rows[-LIMIT:], "total": total,
                "shown": min(total, LIMIT), "truncated": total > LIMIT,
                **{k: stats[k] for k in ("turns", "tokensIn", "tokensOut", "bad")},
            })

        # 会话里的一张图。**只发图，不列目录**：名字必须是那种形状，
        # 而 store.image_path 会再判一次（两处判据同一份，不是两份）。
        m = SESSION_IMG_RE.match(path)
        if m:
            pid, sid, name = m.group("pid"), m.group("sid"), m.group("name")
            if rt.program(pid) is None:
                return self._json({"error": f"没有程序 {pid}"}, 404)
            # 不用 hub.link(create=True)：那会顺手把一个智能体叫起来，
            # 而「看一眼历史里的图」不该有那个副作用。Store 只读盘。
            target = sessions.Store(rt.root).image_path(pid, sid, name)
            if target is None or not target.is_file():
                return self._json({"error": "没有这张图"}, 404)
            return self._file(target)

        m = PLUGIN_RE.match(path)
        if m:
            pid, rest = m.group("pid"), m.group("rest")
            plugin = rt.plugins.get(pid)
            if plugin is None:
                return self._json({"error": f"没有程序 {pid}"}, 404)
            # **界面入口只有一处判据：Plugin.ui** —— 它读的就是 plugin.json 的 ui。
            # 这道门从前自己拼 "ui/index.html"：插件把入口写成 "ui/tool.html" 时，
            # 内核认（hasUi=true、uiName 就是它），门不认（去开 ui/index.html），
            # 于是界面上是 iframe 里一段 {"error":"not found"}。白板，而且不报错。
            # 同一条事实算两遍，必然走岔。
            entry = plugin.ui
            if entry is None or plugin.ui_kind != "web":
                # 原生程序只有 .py 窗口脚本：它跑在自己的进程里，不是网页。
                # 这一条同时挡住「顺手请求 /plugin/<pid>/app.py 拿后端源码」。
                return self._json({"error": f"{pid} 没有网页界面"}, 404)
            if rest is None:
                # 少了尾斜杠：浏览器会把 /plugin/pid 当成**文件**，页内 ./a.js 于是
                # 解析到 /plugin/a.js（pid 变成 a），相对引用整片错位。302 补齐
                # 是唯一能让「相对 /plugin/<pid>/」继续成立的做法。
                self.send_response(302)
                self.send_header("location", f"/plugin/{pid}/")
                self.send_header("content-length", "0")
                self.end_headers()
                return
            root = plugin.path.resolve()
            if not entry.is_relative_to(root):
                return self._json({"error": "forbidden"}, 403)
            if rest in ("", "/"):
                return self._file(entry)
            target = (entry.parent / rest.lstrip("/")).resolve()
            # 上界是**整个插件目录**：页面可以放在 ui/sub/ 里引用同级的资源。
            # .py / .pyc 一律不发 —— 那是后端，从网页路由送出去就是送源码。
            if not target.is_relative_to(root) or target.suffix.lower() in (".py", ".pyc"):
                return self._json({"error": "forbidden"}, 403)
            return self._file(target)

        return self._json({"error": "not found", "path": path}, 404)

    def do_POST(self) -> None:
        if not self._origin_ok():
            # 还没读 body 就回绝：HTTP/1.1 是 keep-alive，不关连接的话这段未读的
            # body 会被当成下一个请求行解析（请求走私的经典形态）
            self.close_connection = True
            return self._json({"error": "只接受本机页面的请求"}, 403)
        path = unquote(urlsplit(self.path).path)
        rt, hub = App.runtime, App.hub
        length = int(self.headers.get("content-length") or 0)
        # 插件包这条路收的是**包的字节**，不是 JSON —— 分流必须在 json.loads 之前，
        # 读 body 的那一步也归它自己（上限要在读之前判，读完再判就太晚了）。
        if path == "/api/plugins/import":
            return self._import(length)
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw.decode("utf-8")) if raw else {}
        except ValueError:
            return self._json({"error": "body 不是合法 JSON"}, 400)

        # 人点的按钮 → 内核唯一入口（source="human"）
        m = CALL_RE.match(path)
        if m:
            res = rt.dispatch(m.group("pid"), m.group("fn"), body if isinstance(body, dict) else {},
                              source="human")
            return self._json(res.as_dict())

        # 网页与原生程序共用的两条：写自己的 store、往事件流里喊一声。
        # 与 CALL_RE 一样只认「这是一个已存在的程序」，越界的事它做不了：
        # 路径里的 pid 就是它能碰的唯一一份数据。
        m = STORE_RE.match(path)
        if m:
            pid = m.group("pid")
            plugin = rt.plugins.get(pid)
            if plugin is None:
                return self._json({"error": f"没有程序 {pid}"}, 404)
            body = body if isinstance(body, dict) else {}
            if "key" not in body:
                return self._json({"error": "缺 key"}, 400)
            from .kernel import Context

            Context(rt, plugin).set(str(body["key"]), body.get("value"))
            return self._json({"ok": True, "key": str(body["key"])})

        m = EMIT_RE.match(path)
        if m:
            pid = m.group("pid")
            plugin = rt.plugins.get(pid)
            if plugin is None:
                return self._json({"error": f"没有程序 {pid}"}, 404)
            body = body if isinstance(body, dict) else {}
            from .kernel import Context

            kind = str(body.get("kind") or "").strip()
            if not kind:
                return self._json({"error": "缺 kind"}, 400)
            Context(rt, plugin).emit(kind, body.get("data") or {})
            return self._json({"ok": True, "kind": kind})

        # 宿主页面把「auto 解析成了什么」报回来。**只有页面知道系统深浅**
        # （matchMedia 是页面的能力），所以这条路由是那个值进入宿主的唯一入口。
        if path == "/api/theme/resolved":
            body = body if isinstance(body, dict) else {}
            changed = rt.set_theme(resolved=str(body.get("resolved") or ""),
                                   pref=str(body.get("pref") or "") or None)
            return self._json({"status": "ok", "changed": changed,
                               "pref": rt.theme_pref(), "resolved": rt.theme_now()})

        # 原生程序：开/关它自己的窗口（网页程序不需要 —— 它就在宿主窗口里）
        m = OPEN_RE.match(path)
        if m:
            if App.native is None:
                return self._json({"status": "error", "message": "原生窗口宿主未装配"}, 503)
            fn = App.native.open if m.group("act") == "open" else App.native.close
            return self._json(fn(m.group("pid")))

        # 对话 / 取消 / 「把这个程序的智能体叫起来」
        m = AGENT_RE.match(path)
        if m:
            pid, act = m.group("pid"), m.group("act")
            if rt.program(pid) is None:
                return self._json({"error": f"没有程序 {pid}"}, 404)
            if act == "link":
                return self._json(hub.ensure(pid))
            if act == "cancel":
                # cancel 自己就是 {"status": 机器码, "message": 人话}：再包一层
                # 就成了 {"status": {"status": ...}}，前端读到的 status 是个对象。
                return self._json(hub.cancel(pid))
            text = str((body or {}).get("text", ""))
            images = (body or {}).get("images") or []
            if not isinstance(images, list):
                return self._json({"error": "images 必须是数组"}, 400)
            # 只有图、没有字是合法的一句话（「这张怎么样」的省略式）。真正的空
            # 消息是**两样都没有** —— 那才拒。
            if not text.strip() and not images:
                return self._json({"error": "空消息"}, 400)
            return self._json({"status": hub.send(pid, text, images=images), "pid": pid})

        # 会话的四个动作：新建 / 切换 / 改名 / 删除。
        # 它们都在 _origin_ok() 之下 —— 会话是「人这一侧」的东西，不需要另加防线。
        m = SESSION_ACT_RE.match(path)
        if m:
            pid, act = m.group("pid"), m.group("act")
            if rt.program(pid) is None:
                return self._json({"error": f"没有程序 {pid}"}, 404)
            if hub is None:
                return self._json({"error": "智能体池未装配"}, 503)
            body = body if isinstance(body, dict) else {}
            if act == "new":
                return self._json(hub.new_session(pid, str(body.get("title") or "")))
            sid = str(body.get("sid") or "")
            if not SESSION_SID_RE.match(sid):
                return self._json({"status": "bad", "message": "sid 形状不对"}, 400)
            if act == "switch":
                out = hub.switch(pid, sid)
            elif act == "rename":
                out = hub.rename(pid, sid, str(body.get("title") or ""))
            else:                                  # delete
                out = hub.drop(pid, sid)
            # busy / missing / bad 都是「人话 + 非 200」：界面要能把原因显示出来，
            # 而不是只看到一句「操作失败」。
            code = 200 if out.get("status") == "ok" else (
                409 if out.get("status") == "busy" else
                404 if out.get("status") == "missing" else 400)
            return self._json(out, code)

        # 版本回退：人点的按钮。语义是**再提交一版**（不是 reset）——
        # 所以它走的是与写入同一条落盘路径，四道校验照样挡在前面。
        m = VERSIONS_RE.match(path)
        if m:
            pid = m.group("pid")
            if rt.program(pid) is None:
                return self._json({"error": f"没有程序 {pid}"}, 404)
            from . import toolbox

            version = str((body or {}).get("version") or "").strip()
            paths = (body or {}).get("paths") or None
            try:
                out = toolbox._plugin_revert(pid, version, paths, by="human",
                                             why=str((body or {}).get("why") or "人点的回退"))
            except ValueError as exc:
                # 校验没过 / 没有这一版：消息本来就是给人看的一句话，原样回
                return self._json({"status": "error", "pid": pid, "message": str(exc)}, 400)
            return self._json({"status": "ok", "pid": pid, "message": out["text"]})

        # 开发者模式：人手拨的一枚开关。开着 = 这个程序的助手借来小助理的手
        m = DEV_RE.match(path)
        if m:
            pid = m.group("pid")
            if rt.program(pid) is None:
                return self._json({"error": f"没有程序 {pid}"}, 404)
            on = bool((body or {}).get("on"))
            msg = rt.set_dev(pid, on)
            return self._json({"status": "ok", "pid": pid, "dev": rt.dev_mode(pid),
                               "message": msg, "functions": sorted(rt.capabilities_of(pid))})

        # 设置：全系统唯一一条「改一下就改变内核之外那个东西」的路。
        # 它只属于人 —— 没有对应的工具，所以 agent 走不到这里。
        if path == "/api/settings":
            if hub is None:
                return self._json({"error": "智能体池未装配"}, 503)
            # 面板上有两项：地址与外观。两件都**先校验**再落盘 —— 一次保存
            # 不该只成一半（主题存进去了、地址报错，人就不知道该信哪个）。
            body = body or {}
            try:
                theme = (settings.normalize_theme(body.get("theme"))
                         if "theme" in body else settings.load_theme(hub.root))
                new_url = settings.normalize(body.get("url")) if "url" in body else None
            except (settings.BadURL, settings.BadTheme) as exc:   # 消息给人看的，原样回
                return self._json({"status": "bad", "error": str(exc)}, 400)
            settings.save_theme(hub.root, theme)
            # 人点的显式档位**不需要页面**就能解析：dark/light 就是答案本身。
            # auto 则要等页面把 matchMedia 的结果报回来（POST /api/theme/resolved）——
            # 宿主里没有第二个地方会问系统，因为那种「问法」只有页面有。
            if theme in ("dark", "light"):
                rt.set_theme(resolved=theme, pref=theme)
            pids = []
            if new_url is not None:
                settings.save_url(hub.root, new_url)
                if new_url != hub.url:     # 地址没变就别把每个程序都重连一遍
                    pids = hub.rebind(settings.hub_cfg(hub.root))
            return self._json({"status": "ok", "url": hub.url, "theme": theme,
                               "reconnected": pids, "agents": hub.status()})

        if path == "/api/rescan":
            msg = rt.scan()
            return self._json({"status": msg, "version": rt.version})

        return self._json({"error": "not found", "path": path}, 404)

    # -- 插件包 ---------------------------------------------------------
    IMPORT_MAX = 64 << 20            # 与 pluginpkg.MAX_TOTAL 同一个量级：
                                     # 压缩包本身比解包后小，这里只挡「别读进内存再说」

    def _import(self, length: int) -> None:
        """插件包的「导入」：收原始字节；``dry=1`` 只解析、不落盘。

        两条边界，都是「不替人做主」：
          * **不覆盖**：撞名就拒，一个已有程序的数据不该因为一次手滑而消失；
          * **不改名**：撞名时给一个空着的建议名，改不改由人在「导入为」里定。
        """
        from . import pluginpkg

        rt = App.runtime
        if length > self.IMPORT_MAX:
            # 还没读 body 就回绝：HTTP/1.1 是 keep-alive，未读的 body 会被当成
            # 下一个请求行解析（与 do_POST 开头那道门同一条理由）。
            self.close_connection = True
            return self._json({"status": "bad",
                               "error": f"包太大（{length} 字节 > {self.IMPORT_MAX}）"}, 413)
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return self._json({"status": "bad", "error": "没有收到文件"}, 400)
        q = parse_qs(urlsplit(self.path).query)
        name = (q.get("name") or [""])[0]          # 只是给人看的文件名，不进任何路径
        as_pid = ((q.get("as") or [""])[0] or "").strip() or None
        dry = (q.get("dry") or [""])[0] == "1"
        try:
            pkg = pluginpkg.read(raw, name=name)
        except pluginpkg.BadPackage as exc:
            return self._json({"status": "bad", "error": str(exc)}, 400)
        want = as_pid or pkg["pid"]
        if dry:
            # 「先看一眼」与真正导入走**同一个解析**（pluginpkg.read），所以预览里
            # 看到的东西与装上去的东西不可能不一致 —— 这一点比省一次读更重要。
            return self._json({
                "status": "ok", "dry": True, "pid": pkg["pid"], "title": pkg["title"],
                "files": len(pkg["files"]), "data": pkg["data"],
                "bytes": pkg["bytes"], "name": name,
                "conflict": (Path(rt.root) / "plugins" / want).exists(),
                "suggest": pluginpkg.free_pid(rt.root, want)})
        try:
            out = pluginpkg.install(rt, raw, as_pid=as_pid, name=name)
        except pluginpkg.BadPackage as exc:        # 校验没过 / 起不来 / 撞名：原样回人话
            return self._json({"status": "bad", "error": str(exc)}, 400)
        tail = "（含数据）" if out["data"] else "（没带数据：装上去是个干净的程序）"
        msg = f"已导入 {out['title']}，{out['files']} 个文件{tail}"
        if out["renamed"]:
            msg += f"；装成了 {out['pid']}（包里那个叫 {pkg['pid']}）"
        return self._json({**out, "status": "ok", "message": msg})

    # -- SSE -----------------------------------------------------------
    def _sse(self) -> None:
        q = App.runtime.subscribe()
        self.send_response(200)
        self.send_header("content-type", "text/event-stream; charset=utf-8")
        self.send_header("cache-control", "no-cache")
        self.send_header("connection", "keep-alive")
        self.end_headers()
        try:
            self.wfile.write(b": connected\n\n")
            self.wfile.flush()
            while True:
                try:
                    ev = q.get(timeout=15)
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")     # 心跳：别让浏览器把连接当死
                    self.wfile.flush()
                    continue
                self.wfile.write(b"data: " + json.dumps(ev, ensure_ascii=False,
                                                        default=str).encode("utf-8") + b"\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            App.runtime.unsubscribe(q)


class _Server(ThreadingHTTPServer):
    """客户端走掉不是错误。

    SSE 与原生窗口都会主动断开，而 socketserver 默认把任何异常打成整段 traceback。
    日志里天天躺着「ConnectionResetError: 远程主机强迫关闭了一个现有的连接」，
    真正的问题就没人看得见了 —— 所以这里只静默这一种。
    """

    daemon_threads = True
    # Windows 上 SO_REUSEADDR 允许**两个进程同时绑同一个端口**，请求随机落到
    # 其中一个：两个宿主各自以为自己是唯一的那一个。这不是「允许复用」，
    # 这是静默串台 —— 所以宁可在第二个宿主启动时就报「端口被占用」。
    allow_reuse_address = False

    def handle_error(self, request, client_address) -> None:
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)):
            return
        super().handle_error(request, client_address)


def serve(runtime, hub, web: Path, port: int = 8777) -> ThreadingHTTPServer:
    """装配「门」。原生程序也从这个门进出 —— 只是它开自己的窗口。"""
    from .native import NativeHost

    App.runtime, App.hub, App.web = runtime, hub, Path(web)
    httpd = _Server(("127.0.0.1", port), Handler)
    httpd.daemon_threads = True
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    App.native = NativeHost(runtime, base)          # 端口定了，原生程序才知道
    return httpd
