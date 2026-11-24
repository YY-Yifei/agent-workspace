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

from . import settings

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
# 会话：列表 / 单个 / 四个动作。sid 的形状在这里就钉死（s_ + 8 位 hex）——
# 它同时是路径分量，形状收紧了，越界就不必再靠 resolve 兜。
SESSIONS_RE = re.compile(r"^/api/p/(?P<pid>[a-z][a-z0-9_]*)/sessions$")
SESSION_ONE_RE = re.compile(r"^/api/p/(?P<pid>[a-z][a-z0-9_]*)/session/(?P<sid>s_[0-9a-f]{8})$")
SESSION_SID_RE = re.compile(r"^s_[0-9a-f]{8}$")
SESSION_ACT_RE = re.compile(
    r"^/api/p/(?P<pid>[a-z][a-z0-9_]*)/session/(?P<act>new|switch|rename|delete)$")


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
    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("content-type", ctype)
        self.send_header("content-length", str(len(body)))
        self.send_header("cache-control", "no-store")
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

        if path == "/api/state":
            # 门只做搬运，**不重新发明** state 的形状：内核多一个字段（比如 devtools），
            # 界面就该收到它。这里曾经逐项挑着搬，于是新增字段静默消失。
            from . import pluginvcs

            vcs_ok, vcs_why = pluginvcs.available()
            st = rt.state()
            return self._json({**st,
                               "native": App.native.info() if App.native else {},
                               "settings": {"url": hub.url} if hub else {},
                               "agents": hub.status() if hub else {},
                               "vcs": {"enabled": vcs_ok, "reason": vcs_why}})
        if path == "/api/events":
            return self._sse()
        if path == "/api/settings":
            if hub is None:
                return self._json({"error": "智能体池未装配"}, 503)
            return self._json({"url": hub.url, "agents": hub.status()})

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
            if not text.strip():
                return self._json({"error": "空消息"}, 400)
            return self._json({"status": hub.send(pid, text), "pid": pid})

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
            try:
                url = settings.save_url(hub.root, (body or {}).get("url"))
            except settings.BadURL as exc:      # 消息本来就是给人看的，原样回
                return self._json({"status": "bad", "error": str(exc)}, 400)
            pids = hub.rebind(settings.hub_cfg(hub.root))
            return self._json({"status": "ok", "url": hub.url, "reconnected": pids,
                               "agents": hub.status()})

        if path == "/api/rescan":
            msg = rt.scan()
            return self._json({"status": msg, "version": rt.version})

        return self._json({"error": "not found", "path": path}, 404)

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
