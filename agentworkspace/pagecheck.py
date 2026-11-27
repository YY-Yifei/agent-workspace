"""页面自查 —— 在**真窗口、真引擎**里把自己的页面看一遍。

为什么要它：``selftest`` 只能断言「文件里有这行字」，``uiprobe`` 的断言写死在
验收脚本里。而插件作者（那个 agent）需要的是**问页面一句真话**：这段 CSS 到底
渲染成什么样、刚才那次改动有没有把按钮挤出屏幕、页面加载时那个
``TypeError: S.pid is undefined`` 还在不在。

引擎只有一个：**pywebview / WebView2**。这不是选择，是事实 —— 生产里插件页面
就是它渲染的（``web/index.html`` 的 ``<iframe id="appframe">``，外面是 pywebview
窗口）。换 Chrome 来验等于验另一台机器：字体、行高、滚动条宽度、以及 pywebview
在 ``text_select=False`` 时往 ``<head>`` 末尾注入的 ``user-select:none``，全不一样。

CDP 只是**挂在同一个窗口上的观察通道**，不是第二个引擎：

    WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS=--remote-debugging-port=<随机>
      → Playwright ``connect_over_cdp`` 连上**那一个** WebView2 实例
      → console / pageerror / requestfailed / Page.screenshot / evaluate 全都有

三条实测约束（v1/v2 探针，不是推演）：

  1. ``hidden=True`` 截不到图 —— 从没显示过的窗口不合成帧，``Page.screenshot``
     会一直等到超时。所以窗口必须**可见**，但可以放在屏幕外
     （``x=-3000, y=-3000``）：既合成帧，又不挡用户。
  2. ``reload`` 会**再次**派发 pywebview 的 ``loaded`` 事件 —— 用 ``events.loaded``
     起工作线程会让流程跑两遍，第二遍端口已经关了。所以用 ``webview.start(func)``。
  3. attach 必须在**导航之前**：``Runtime.consoleAPICalled`` 是事件、不重放，
     加载完再 attach 就一条也拿不到。而加载期恰恰最容易出错。这里的做法是
     attach 之后 ``reload`` 一次 —— 同一批事件，页面一个字不动（不像 v2 那样
     用 ``add_init_script`` 抢在导航前注脚本：那是**为了观察而改变观察对象**）。
     代价写进报告：页面加载两次，第一次的 console 拿不到。

验证对象只有一个：**真页面** —— 真工作台、真插件、真会话、你此刻正在跑的那个宿主。
没有「副本工作台」这一档：副本能截图、能点按钮，但它不回答「我的页面在真实数据下
长什么样」。要看，就看真的。

进程只有两个（第二个只在没有现成宿主时才起）：

    宿主进程（agent 住的那一个）—— 已经有 HTTP 端口，直接连它
      └─ 本文件 --worker  ← ``webview.start()`` 一个进程只能调一次，所以必须独立
            ├─ 也许再 spawn 一个 agentworkspace.main --serve（宿主没在跑时）
            └─ 真窗口 → #p/<pid> → #appframe → /plugin/<pid>/

用法（人）：
    python -m agentworkspace.pagecheck --pid memo --check
    python -m agentworkspace.pagecheck --pid memo --eval "document.title"
    python -m agentworkspace.pagecheck --pid memo --shot out.png --eval "1+1"
    python -m agentworkspace.pagecheck --worker --port 9000 --pid memo   # 给 devtools

用法（agent）：``dev_page_open`` / ``dev_page_eval`` / ``dev_page_shot`` /
``dev_page_check`` / ``dev_page_close`` —— 会话常驻，同一窗口连续问。
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# 缓存**不在这里**：它必须落在工作台里，见 Worker.cache。
OFFSCREEN = -3000                  # 屏幕外坐标：可见（能合成帧）但不挡人
MAX_VALUE = 4000                   # eval 返回值超过这个字符就落盘
IDLE = 300                         # 没有命令这么久就自己收尸
VIEWPORTS = {"pane": (1180, 800), "desktop": (1600, 900)}
NARROW = 820                       # 窄屏：窗口这么宽时 pane ≈ 420

# 值的序列化：在**页面里**做完再回来。
# 为什么不能直接把值交给 Playwright：DOM 节点回来是 ``"ref: <Node>"``（不可用），
# 循环引用直接抛错。而抛错会让 agent 去改**没写错的那一行** —— 比不给答案更坏。
SERIALIZER = """(async (v) => {
  v = await v;                       // 传进来的是 Promise 就先等它的值。
                                     // 不等的话 `(async()=>42)()` 会序列化成
                                     // `{}` —— 而 await 正是这套 eval 的卖点。
  if (v === undefined) return {__t: 'undefined'};
  if (v === null) return null;
  const t = typeof v;
  if (t === 'number' || t === 'string' || t === 'boolean') return v;
  if (t === 'function') return {__t: 'function', name: v.name || ''};
  if (typeof Node !== 'undefined' && v instanceof Node) {
    return {__t: 'node', tag: v.tagName, id: v.id || '',
            cls: String(v.className || '').slice(0, 120),
            text: String(v.textContent || '').slice(0, 200),
            html: String(v.outerHTML || '').slice(0, 300)};
  }
  try { return JSON.parse(JSON.stringify(v)); }
  catch (e) { return {__t: 'unserializable', desc: String(v).slice(0, 200)}; }
})((%s))"""

FRAME_JS = """(() => {
  const f = document.querySelector('#appframe');
  if (!f) return {__no_frame__: true};
  const d = f.contentDocument;
  if (!d) return {__no_doc__: true};
  return {frameW: f.clientWidth, frameH: f.clientHeight,
          ready: d.readyState,
          nodes: d.body ? d.body.querySelectorAll('*').length : 0,
          scrollW: d.documentElement ? d.documentElement.scrollWidth : 0,
          clientW: d.documentElement ? d.documentElement.clientWidth : 0,
          scrollH: d.documentElement ? d.documentElement.scrollHeight : 0,
          bg: d.body ? (function eff(el) {
                 // 透明的 body（默认就是 rgba(0,0,0,0)）不等于黑色：往上找到
                 // 第一个真的画了颜色的祖先，那才是眼睛看到的那个底。
                 while (el) {
                   const c = getComputedStyle(el).backgroundColor || '';
                   if (c && c !== 'transparent'
                       && !/^rgba\\(0,\\s*0,\\s*0,\\s*0\\)$/.test(c)) return c;
                   el = el.parentElement;
                 }
                 return 'rgb(255, 255, 255)';
               })(d.body) : null,
          fg: d.body ? getComputedStyle(d.body).color : null,
          title: d.title || ''};
})()"""

STYLE_JS = """(() => {
  const f = document.querySelector('#appframe');
  if (!f) return {__no_frame__: true};
  const d = f.contentDocument;
  if (!d) return {__no_doc__: true};
  const isAw = (h) => (h || '').split('?')[0] === '/aw.css';
  const links = [...d.querySelectorAll('link[rel~="stylesheet"]')];
  const scripts = [...d.querySelectorAll('script[src]')];
  let hex = 0, tok = 0, sb = 0, sheets = 0, unreadable = 0;
  for (const sh of [...d.styleSheets]) {
    const href = sh.href || '';
    if (isAw(href)) continue;              // 契约那一份**不算抄**：它正是"借"的那份
    let rules;
    // 跨源表读不到 cssRules（SecurityError）。读不到就记一笔，不当成 0 ——
    // 「查不了」与「查了是干净」是两件事，混成一个就会假绿。
    try { rules = sh.cssRules; } catch (e) { unreadable++; continue; }
    sheets++;
    let text = '';
    for (const r of rules) text += (r.cssText || '') + '\\n';
    // 只数**声明值**里的 hex：`#id{...}` 的选择器在冒号之*前*，不该算成颜色。
    for (const decl of (text.match(/:[^;{}]*/g) || [])) {
      const m = decl.match(/#[0-9a-fA-F]{3,8}\\b/g);
      if (m) hex += m.length;
    }
    tok += (text.match(/var\\(\\s*--aw-/g) || []).length;
    sb += (text.match(/::-webkit-scrollbar/g) || []).length;
  }
  const cs = d.defaultView
      ? d.defaultView.getComputedStyle(d.documentElement) : null;
  return {aw_css: links.some((l) => isAw(l.getAttribute('href'))),
          aw_js: scripts.some((s) => (s.getAttribute('src') || '').split('?')[0] === '/aw.js'),
          sheets: sheets, unreadable: unreadable,
          hex: hex, tok: tok, sb: sb,
          aw_bg: cs ? cs.getPropertyValue('--aw-bg').trim() : '',
          theme: d.documentElement.getAttribute('data-theme') || '',
          scheme: cs ? String(cs.colorScheme || '') : ''};
})()"""

CLICK_JS = """(() => {
  const f = document.querySelector('#appframe');
  const d = f && f.contentDocument;
  if (!d) return [];
  const els = [...d.querySelectorAll('button, [role="button"], input[type="button"]')]
      .slice(0, 8);
  return els.map((el) => {
    const label = (el.textContent || el.value || '').trim().slice(0, 40);
    try { el.click(); return {tag: el.tagName, label, ok: true}; }
    catch (e) { return {tag: el.tagName, label, ok: false, err: String(e)}; }
  });
})()"""

DESKTOP_JS = """(() => {
  const f = document.querySelector('#appframe');
  const chat = document.querySelector('.chat');
  return {winW: innerWidth, winH: innerHeight, dpr: devicePixelRatio,
          frameW: f ? f.clientWidth : 0, frameH: f ? f.clientHeight : 0,
          chatW: chat ? Math.round(chat.getBoundingClientRect().width) : 0,
          docW: document.documentElement.scrollWidth};
})()"""

DEFAULT_AREAS = ("load", "console", "network", "overflow", "contrast", "narrow")
KNOWN_AREAS = DEFAULT_AREAS + ("interactive", "style")


def unknown_areas(picks) -> list[str]:
    """点名了却**不存在**的面。

    为什么要有这个函数：``check()`` 的装饰器只匹配已注册的名字，
    所以 ``areas="consle"`` 会得到「一行不跑、一条跳过也不报」——
    ``ok=false`` 配一份空报告，一个字的解释都没有。这与
    「没跑的东西必须出现在跳过里」正好相反。提成纯函数，才能被门钉住。
    """
    return [n for n in (picks or []) if n not in KNOWN_AREAS]


# ------------------------------------------------------------------ 小工具

def free_port() -> int:
    """要一个当前空闲的端口。**不落盘、不写死** —— 它只活这一次会话。"""
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


def _engine_name(ua: str, version: str = "") -> str:
    """从 UA 里认出引擎。``browser.version`` 只有版本号，认不出是谁 ——
    而「报告里不许出现 HeadlessChrome」这条门，靠的正是这个前缀。"""
    import re
    ua = ua or ""
    for pat in (r"Edg/([\d.]+)", r"HeadlessChrome/([\d.]+)", r"Chrome/([\d.]+)"):
        m = re.search(pat, ua)
        if m:
            name = pat.split("/")[0]
            return f"{name}/{m.group(1)}"
    return version or "unknown"


def plugin_of(ws: Path, pid: str) -> dict:
    """这个程序有没有页面。没有不是错误 —— 是**没有**，报告里要如实说。"""
    pdir = Path(ws) / "plugins" / pid
    man = pdir / "plugin.json"
    out = {"pid": pid, "dir": str(pdir), "exists": pdir.is_dir(), "has_ui": False,
           "why": ""}
    if not out["exists"]:
        out["why"] = f"工作台里没有这个程序：{pdir}"
        return out
    try:
        meta = json.loads(man.read_text(encoding="utf-8")) if man.exists() else {}
    except Exception as exc:
        out["why"] = f"plugin.json 读不动：{type(exc).__name__}: {exc}"
        return out
    entry = meta.get("ui") or ("ui/index.html" if (pdir / "ui" / "index.html").exists() else None)
    if not entry:
        out["why"] = "这个程序没有页面（plugin.json 没有 ui，也没有 ui/index.html）"
        return out
    index = pdir / str(entry)
    if not index.exists():
        out["why"] = f"plugin.json 指向的页面不存在：{entry}"
        return out
    out.update(has_ui=True, entry=str(entry), index=str(index))
    return out


def spawn_host(ws: Path, pid: str, log: Path) -> tuple[subprocess.Popen, int]:
    """起一个只开门、不开窗的宿主，读到它的端口就返回。

    ``ws`` 是**真工作台**：只有在没有现成宿主可连时才走到这里。

    为什么不让本进程直接 serve：窗口要加载一个 URL，而 ``webview.start()`` 会
    占死主线程 —— 同一个进程里既当服务器又当 GUI 会互相等。用 CLI 还顺带复用了
    它已经调好的全部装配（插件扫描、事件流、深链接）。
    """
    log.parent.mkdir(parents=True, exist_ok=True)
    fh = open(log, "w", encoding="utf-8", buffering=1)
    argv = [sys.executable, "-X", "utf8", "-m", "agentworkspace.main",
            "--serve", "--port", "0", "--no-browser",
            "--workspace", str(ws), "--open", pid]
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    old = env.get("PYTHONPATH") or ""
    env["PYTHONPATH"] = str(ROOT) if not old else (str(ROOT) + os.pathsep + old)
    proc = subprocess.Popen(argv, cwd=str(ROOT), stdout=fh, stderr=subprocess.STDOUT,
                            env=env)
    t0 = time.time()
    import re
    while time.time() - t0 < 60:
        txt = log.read_text(encoding="utf-8", errors="replace") if log.exists() else ""
        m = re.search(r"\[门\] http://127\.0\.0\.1:(\d+)/", txt)
        if m:
            port = int(m.group(1))
            if _http_ok(port):
                return proc, port
        if proc.poll() is not None:
            raise RuntimeError("宿主退出了：" + txt[-600:])
        time.sleep(0.25)
    raise RuntimeError("宿主 60 秒内没有就绪：" + (log.read_text(encoding="utf-8")[-600:]
                                              if log.exists() else ""))


def _http_ok(port: int, path: str = "/api/state", timeout: float = 1.5) -> bool:
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=timeout) as r:
            r.read(64)
        return True
    except Exception:
        return False


def kill_tree(proc: subprocess.Popen | None, timeout: float = 20) -> None:
    if proc is None or proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=timeout)
    except Exception:
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           capture_output=True, timeout=15)
        except Exception:
            pass


# ------------------------------------------------------------------ 工作者

class Worker:
    """一个窗口、一个会话。命令从 socket 或脚本进来，事实从页面出去。"""

    def __init__(self, pid: str, ws: Path, host_port: int, cdp_port: int,
                 preset: str = "pane", width: int | None = None) -> None:
        self.pid = pid
        self.ws = Path(ws)
        self.host_port = host_port
        self.cdp_port = cdp_port
        self.preset = preset
        w, h = VIEWPORTS.get(preset, VIEWPORTS["pane"])
        self.width = int(width or w)
        self.height = h
        self.plugin = plugin_of(self.ws, pid)
        self.win = None
        self.page = None
        self.console: list[dict] = []
        self.errors: list[str] = []
        self.network: list[str] = []
        self.aborted: list[str] = []          # 被导航/reload 打断的请求：不是页面的错
        self.engine = ""
        self.host: subprocess.Popen | None = None
        self._pw = None
        self._br = None
        self._ready = False
        self._closing = False
        self._idle_at = time.time()
        self._last = ""
        self.ua = ""
        self.gen = 0                      # 第几「代」页面：导航一次 +1
        # 插件自己的运行时数据：真页面会写它。记下来，收尾时如实说。
        self.data0 = self._data_snap()

    @property
    def cache(self) -> Path:
        """``<工作台>/.dev-cache/<pid>/`` —— 缓存只能落在这里。

        返回给 agent 的路径（eval 落盘、截图）要拿 ``dev_read`` 去打开，
        而 ``toolbox._safe()`` 拒绝工作台外的一切；落在包目录旁边时，
        那些路径一个都打不开。装成 wheel 之后那个目录还常常是只读的
        —— 于是 ``eval`` 抛 PermissionError，而它不在这条路的 try 里。
        """
        return self.ws / ".dev-cache" / self.pid

    # -- 起 ---------------------------------------------------------
    def url(self) -> str:
        return f"http://127.0.0.1:{self.host_port}/#p/{self.pid}"

    def run(self, serve=None) -> None:
        """起窗口 → 跑工作线程 → 等窗口关。``serve`` 就是工作线程里的那个循环。"""
        import webview

        self.win = webview.create_window(
            f"页面自查 · {self.pid}", self.url(),
            width=self.width, height=self.height,
            x=OFFSCREEN, y=OFFSCREEN)
        # func 只在 GUI 起来之后被调用**一次**；而 events.loaded 每次导航都会派发
        # （reload 会再派一次 → 流程跑两遍，第二遍什么都连不上）。
        webview.start(lambda: self._work(serve))

    def _work(self, serve) -> None:
        try:
            self._attach()
            self._ready = True
        except Exception as exc:
            self._last = f"{type(exc).__name__}: {exc}"
            self._ready = False
        if serve is not None:
            try:
                serve(self)
            except Exception as exc:
                self._last = f"{type(exc).__name__}: {exc}"
        try:
            self.destroy()
        except Exception:
            pass

    def _wait_cdp(self, timeout: float = 40) -> None:
        t0 = time.time()
        while time.time() - t0 < timeout:
            try:
                with socket.create_connection(("127.0.0.1", self.cdp_port), 1):
                    return
            except Exception:
                time.sleep(0.2)
        raise RuntimeError(f"这段时间里没有出现 CDP 端口 {self.cdp_port}"
                           "（窗口没起来？或者 WebView2 没起来）")

    def _attach(self) -> None:
        from playwright.sync_api import sync_playwright

        self._wait_cdp()
        self._pw = sync_playwright().start()
        self._br = self._pw.chromium.connect_over_cdp(
            f"http://127.0.0.1:{self.cdp_port}")
        self.engine = self._br.version
        ctx = self._br.contexts[0]
        pages = [p for p in ctx.pages if f"127.0.0.1:{self.host_port}" in p.url] or ctx.pages
        if not pages:
            raise RuntimeError("CDP 连上了，但一个 page target 都没有")
        self.page = pages[0]
        self.page.on("console", self._on_console)
        self.page.on("pageerror", lambda e: self.errors.append(str(e)))
        self.page.on("requestfailed", self._on_failed)
        self.page.on("response", self._on_response)
        self.page.on("framenavigated", self._on_nav)
        # attach 之后 reload：加载期的事件（更容易出错的那一批）现在收得到。
        # 页面本身一个字不动 —— 代价是它加载了两次，第一次的 console 拿不到。
        self.page.reload(wait_until="load")
        self._settle()
        # UA 在这里读，不是刚连上时：那时 self.page 还没赋值 / 执行上下文还没建好，
        # evaluate 一抛就只剩版本号 —— 而「报告里不许出现 HeadlessChrome」这条门
        # 靠的正是 UA 里的 Edg/ 前缀。
        try:
            self.ua = str(self.page.evaluate("navigator.userAgent") or "")
        except Exception:
            self.ua = ""
        self.engine = _engine_name(self.ua, self._br.version)

    def _on_failed(self, r) -> None:
        """请求失败。**原因要记下来** —— 不然 `net::ERR_ABORTED`（我们自己 reload
        打断了加载期正在飞的请求）会和真失败混成一栏，把页面判成有 bug。"""
        try:
            why = str(getattr(r, "failure", "") or "")
        except Exception:
            why = ""
        if "ERR_ABORTED" in why:
            self.aborted.append(f"{why} {r.url}")
        else:
            self.network.append(f"{why or 'failed'} {r.url}")

    def _on_console(self, msg) -> None:
        self.console.append({"type": msg.type, "text": msg.text})

    def _on_response(self, r) -> None:
        try:
            if r.status >= 400:
                self.network.append(f"{r.status} {r.url}")
        except Exception:
            pass

    def _on_nav(self, frame) -> None:
        """页面重新加载 = 新的一代，旧记录不再属于「现在」。

        不清空的话，「看一眼 → 改一处 → 再看一眼」这个循环永远走不完：
        第二次 check 报的还是修复前的 console 错误，而 agent 会去改一段
        **已经改好**的代码 —— 这比不给答案更坏。
        """
        try:
            if frame is self.page or f"plugin/{self.pid}" in (frame.url or ""):
                self._reset()
        except Exception:
            pass

    def _reset(self) -> None:
        self.console.clear()
        self.errors.clear()
        self.network.clear()
        self.aborted.clear()
        self.gen += 1

    def reload(self) -> dict:
        """重新加载页面并等它画好；顺带清掉上一代的记录。"""
        if self.page is None:
            return {"ok": False, "error": self._why_not_ready()}
        try:
            self.page.reload(wait_until="load")
        except Exception as exc:
            return {"ok": False, "error": f"重新加载没成：{type(exc).__name__}: {exc}"}
        self._settle()
        return {"ok": True, "gen": self.gen, "sizes": self._facts()}

    def _settle(self, ms: int = 900) -> None:
        """等页面把该画的画完。Iframe 的加载不一定落在主 frame 的 load 之后。"""
        t0 = time.time()
        while time.time() - t0 < ms / 1000:
            try:
                r = self.page.evaluate(FRAME_JS)
                if isinstance(r, dict) and r.get("nodes", 0) > 0 and r.get("ready") == "complete":
                    return
            except Exception:
                pass
            time.sleep(0.1)
        self.page.wait_for_timeout(ms)

    # -- 命令 -------------------------------------------------------
    def _target(self, which: str = "page"):
        """求值的对象：``page`` = 插件页面的那个 iframe，``host`` = 外壳。

        为什么默认是 iframe：agent 要验的是**自己的页面**。让它每次都自己写
        ``contentDocument`` 前缀，等于把「我在看哪一层」压在记忆里 —— 十个调用
        写错一个，就会得到一个看起来很有道理、其实在说壳的结论。
        """
        if self.page is None:
            return None
        if which == "host":
            return self.page
        for f in self.page.frames:
            if f is self.page.main_frame:
                continue
            if f"plugin/{self.pid}" in (f.url or ""):
                return f
        for f in self.page.frames:          # 兜底：本页确实只有一个子 frame
            if f is not self.page.main_frame:
                return f
        return self.page

    def eval(self, js: str, frame: str = "page") -> dict:
        """跑一段 JS。表达式优先，语句兜底 —— 两条路都返回「值或为什么没有值」。"""
        if self.page is None:
            return {"ok": False, "error": self._why_not_ready()}
        target = self._target(frame)
        if target is None:
            return {"ok": False, "error": self._why_not_ready()}
        wrapped = False
        try:
            value = target.evaluate(SERIALIZER % js)
        except Exception as exc:
            if "SyntaxError" not in str(exc) and "Unexpected" not in str(exc):
                return {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:400]}"}
            wrapped = True
            try:
                value = target.evaluate(js)
            except Exception as exc2:
                return {"ok": False, "wrapped": True,
                        "error": f"{type(exc2).__name__}: {str(exc2)[:400]}"}
        out = {"ok": True, "value": value, "wrapped": wrapped, "frame": frame}
        if isinstance(value, str) and len(value) > MAX_VALUE:
            out["value"] = value[:MAX_VALUE]
            out["truncated"] = len(value)
            out["spilled"] = self._spill(value, "eval")
        return out

    def _spill(self, text: str, slug: str) -> str:
        d = self.cache / "spill"
        d.mkdir(parents=True, exist_ok=True)
        name = f"{time.strftime('%Y%m%d-%H%M%S')}-{slug}.txt"
        (d / name).write_text(text, encoding="utf-8")
        return str(d / name)

    def shot(self, full: bool = False) -> dict:
        if self.page is None:
            return {"ok": False, "error": self._why_not_ready()}
        png = self.page.screenshot(full_page=bool(full))
        d = self.cache / "shots"
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"{time.strftime('%Y%m%d-%H%M%S')}.png"
        path.write_bytes(png)
        facts = self._facts()
        # ``name`` 一律是**真的那个文件名**：这张图在盘上叫 20261127-090650.png，
        # 而 ``<pid>-page.png`` 是一个从未存在过的模板名。拿模板名去报错，人就会
        # 去一个不存在的路径找一个其实好好的文件。
        return {"ok": True, "path": str(path), "bytes": len(png),
                "b64": base64.b64encode(png).decode(), "mime": "image/png",
                "name": path.name, "sizes": facts}

    def _facts(self) -> dict:
        try:
            d = self.page.evaluate(DESKTOP_JS)
        except Exception:
            d = {}
        d = d or {}
        return {"inner": [d.get("winW"), d.get("winH")],
                "dpr": d.get("dpr"),
                "frame": [d.get("frameW"), d.get("frameH")],
                "chatW": d.get("chatW"),
                "requested": [self.width, self.height]}

    def _data_snap(self) -> dict:
        d = self.ws / "plugins" / self.pid / "data"
        out: dict = {}
        if not d.is_dir():
            return out
        for f in d.rglob("*"):
            try:
                if f.is_file():
                    st = f.stat()
                    out[str(f.relative_to(d))] = [st.st_size, int(st.st_mtime)]
            except Exception:
                continue
        return out

    def _data_delta(self) -> dict:
        """真页面写了自己的数据 —— 如实列出来，不当成缺陷，也不假装没发生。"""
        now = self._data_snap()
        before = getattr(self, "data0", {}) or {}
        added = sorted(set(now) - set(before))
        changed = sorted(k for k in (set(before) & set(now)) if before[k] != now[k])
        return {"added": added, "changed": changed,
                "dir": str(self.ws / "plugins" / self.pid / "data")}

    def _why_not_ready(self) -> str:
        return ("窗口没起来：" + self._last) if self._last else "窗口还没就绪"

    def resize(self, width: int) -> dict:
        if self.win is None:
            return {"ok": False, "error": self._why_not_ready()}
        try:
            self.win.resize(int(width), self.height)
            self.page.wait_for_timeout(400)
        except Exception as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        return {"ok": True, "sizes": self._facts()}

    # -- 样式：引没引宿主那份（只读，不改页面一个字节）------------------
    def _style_facts(self) -> dict:
        """页面里「借没借宿主样式」的一组事实。**不判对错。**

        为什么要有它：撤掉宿主注入之后，一个没引 /aw.css 的插件页什么都不会发生 ——
        滚动条是系统默认那条 17px，颜色是浏览器给的。这件事以前被注入藏起来了，
        作者永远看不见。现在把它摆到报告里一行，判不判是作者的事。

        判据（写死在这里，免得下次又要从零想一遍）：
          * 没引 /aw.css        → 不算错，只说明它拿的是系统默认；
          * 引了但有裸 hex      → 点名（宿主换色它不会跟着变，那正是"抄了一份"）；
          * 引了但自写滚动条    → 点名（未分层赢过分层，WCAG 那两档从此归它维护）；
          * 引了且干净          → 说干净，并报用了几处令牌。
        """
        try:
            f = self.page.evaluate(STYLE_JS) or {}
        except Exception as exc:
            return {"ok": False, "why": f"这一面自己炸了：{type(exc).__name__}: {exc}"}
        if f.get("__no_frame__"):
            return {"ok": False, "why": "宿主里没有 #appframe（没开页面）"}
        if f.get("__no_doc__"):
            return {"ok": False, "why": "iframe 里读不到文档（跨源？）"}
        aw, tok, hexes, sb = bool(f.get("aw_css")), int(f.get("tok") or 0), \
            int(f.get("hex") or 0), int(f.get("sb") or 0)
        bits: list[str] = []
        if not aw:
            bits.append("没引 /aw.css —— 这页的滚动条、深浅、颜色都是系统默认"
                        "（宿主**不会**替你注入）。想借宿主那份：调 dev_style_guide()")
        else:
            bits.append(f"已引 /aw.css（页面里用了 {tok} 处 var(--aw-*)）")
            if hexes:
                bits.append(f"但还有 {hexes} 处裸 hex 颜色 —— 宿主换一次色，那些不会跟着变")
            if sb:
                bits.append(f"并且自己写了 {sb} 处 ::-webkit-scrollbar（未分层赢过分层，"
                            f"aw.css 那两档 WCAG 对比度就失效了）")
            if not hexes and not sb:
                bits.append("没有裸 hex、没有自写滚动条")
        if f.get("unreadable"):
            bits.append(f"另有 {f['unreadable']} 张样式表读不到规则（跨源），那些没数进去")
        return {"ok": True, "aw_css": aw, "aw_js": bool(f.get("aw_js")),
                "tokens_used": tok, "bare_hex": hexes, "own_scrollbar": sb,
                "aw_bg": f.get("aw_bg") or "", "theme": f.get("theme") or "",
                "color_scheme": f.get("scheme") or "",
                "sheets": int(f.get("sheets") or 0),
                "unreadable": int(f.get("unreadable") or 0),
                "hint": "；".join(bits) + "。"}

    # -- 逐面看 -----------------------------------------------------
    def check(self, areas=None, want_shot: bool = False) -> dict:
        if self.page is None:
            return {"ok": False, "error": self._why_not_ready()}
        # sync API 的事件是「下一次调用时 pump」的，不是后台线程派发的。
        # 不 pump 这一下，只跑 network / console 那些**不碰 Playwright** 的面时，
        # 队列里攒着的 console.error 与 404 一条都取不出来 —— 而报告会显示「全绿」。
        try:
            self.page.wait_for_timeout(80)
        except Exception:
            pass
        picks = list(areas) if areas else list(DEFAULT_AREAS)
        rows: list[dict] = []
        skipped: list[dict] = []
        # 名字点错了不能静默：没跑的东西必须出现在「跳过」里。
        # 少了这一段，``dev_page_check(areas="consle")`` 会给出 ok=false +
        # 一份空报告，一个字的解释都没有 —— 与「缺口可见」正好相反。
        for name in unknown_areas(picks):
            skipped.append({"name": name,
                            "why": "没有这一面（可选：" + "/".join(KNOWN_AREAS) + "）"})
        if not self.plugin.get("has_ui"):
            skipped.append({"name": "全部页面面", "why": self.plugin.get("why") or "没有页面"})
            return {"ok": True, "pid": self.pid, "plugin": self.plugin,
                    "engine": {"browser": self.engine, "engine": "webview2"},
                    "sizes": self._facts(), "areas": rows, "skipped": skipped,
                    "console": self.console, "network": self.network,
                    "pageerrors": self.errors, "shot": None, "style": None,
                    "data_changed": self._data_delta()}

        frame_open = bool(self.plugin.get("has_ui"))

        def area(name: str):
            def deco(fn):
                if name in picks and frame_open:
                    try:
                        rows.append(fn())
                    except Exception as exc:
                        rows.append({"name": name, "ok": False,
                                     "detail": f"这一面自己炸了：{type(exc).__name__}: {exc}"})
                elif name not in picks:
                    skipped.append({"name": name, "why": "这次没要求跑它"})
                else:
                    skipped.append({"name": name, "why": "这个程序没有页面"})
                return fn
            return deco

        # 样式复用是**事实**，不是分数：没引 /aw.css 不是坏页面，只是没借。
        # 所以它不进 ok（那份报告是"能不能用"），只作为一行事实摆在那里 ——
        # 宿主不替插件做决定，但也不替它隐瞒。算一次，面与字段共用这一份。
        sty = self._style_facts()

        @area("style")
        def _style() -> dict:
            return {"name": "样式：借了宿主那份，还是自己抄了一份", "ok": True,
                    "detail": sty.get("hint") or ""}

        @area("load")
        def _load() -> dict:
            f = self.page.evaluate(FRAME_JS) or {}
            ok = (not f.get("__no_frame__") and not f.get("__no_doc__")
                  and (f.get("nodes") or 0) > 0 and (f.get("frameW") or 0) > 100)
            return {"name": "页面真的画出来了（iframe 里有节点、有宽度）", "ok": bool(ok),
                    "detail": json.dumps(f, ensure_ascii=False)[:400]}

        @area("console")
        def _console() -> dict:
            bad = [c for c in self.console if c["type"] in ("error",)]
            ok = not bad and not self.errors
            return {"name": "页面没有 console.error、没有未捕获异常", "ok": ok,
                    "detail": json.dumps({"console": bad[-6:], "pageerror": self.errors[-6:]},
                                         ensure_ascii=False)[:600]}

        @area("network")
        def _network() -> dict:
            detail = json.dumps(self.network[-8:], ensure_ascii=False)[:600]
            if self.aborted:
                detail += f"（另有 {len(self.aborted)} 个请求被 reload 打断，不算失败）"
            return {"name": "没有失败 / 4xx / 5xx 的请求", "ok": not self.network,
                    "detail": detail}

        @area("overflow")
        def _overflow() -> dict:
            f = self.page.evaluate(FRAME_JS) or {}
            sw, cw = f.get("scrollW") or 0, f.get("clientW") or 0
            ok = cw > 0 and sw <= cw + 1
            return {"name": "页面没有横向溢出（没有东西被挤出可视区）", "ok": ok,
                    "detail": f"scrollWidth={sw} clientWidth={cw}"}

        @area("contrast")
        def _contrast() -> dict:
            f = self.page.evaluate(FRAME_JS) or {}
            ratio = _contrast_ratio(f.get("bg"), f.get("fg"))
            return {"name": "文字与背景有对比度（不是同色、不是看不见）",
                    "ok": ratio is None or ratio >= 2.0,
                    "detail": f"bg={f.get('bg')} fg={f.get('fg')} 对比度={ratio}"}

        @area("narrow")
        def _narrow() -> dict:
            before = self._facts()
            self.resize(NARROW)
            f = self.page.evaluate(FRAME_JS) or {}
            homes = self.page.evaluate(DESKTOP_JS) or {}
            self.resize(self.width)
            self.page.wait_for_timeout(250)
            sw, cw = f.get("scrollW") or 0, f.get("clientW") or 0
            ok = cw >= 300 and sw <= cw + 1 and (homes.get("chatW") or 0) >= 240
            return {"name": f"窄到 {NARROW}px 时不塌（页面与对话都还在，没横向溢出）",
                    "ok": bool(ok),
                    "detail": f"窄时 frameW={cw} scrollW={sw} chatW={homes.get('chatW')}"
                              f"（原宽 {before.get('frame')}）"}

        @area("interactive")
        def _interactive() -> dict:
            # 默认清单里没有这一面，所以它不会自己点。要就显式点名 —— 点了就真点了：
            # 这些按钮发的是真消息。
            n0 = len(self.errors)
            clicked = self.page.evaluate(CLICK_JS) or []
            self.page.wait_for_timeout(500)
            fresh = self.errors[n0:]
            bad = [c for c in clicked if not c.get("ok")]
            ok = not fresh and not bad
            return {"name": "点一遍可点控件之后没有新报错", "ok": bool(ok),
                    "detail": f"点了 {len(clicked)} 个；新报错 {fresh[:3]}；点击失败 {bad[:3]}"}

        out = {"ok": True, "pid": self.pid, "plugin": self.plugin,
               "engine": {"browser": self.engine, "engine": "webview2"},
               "gen": self.gen,
               "sizes": self._facts(), "areas": rows, "skipped": skipped,
               "console": self.console, "network": self.network,
               "aborted": self.aborted,
               "pageerrors": self.errors, "shot": None, "style": sty,
               "data_changed": self._data_delta()}
        if want_shot:
            s = self.shot()
            if s.get("ok"):
                out["shot"] = s["path"]
                out["_png"] = s["b64"]
        return out

    def destroy(self) -> None:
        self._closing = True
        try:
            if self._br is not None:
                self._br.close()
        except Exception:
            pass
        try:
            if self._pw is not None:
                self._pw.stop()
        except Exception:
            pass
        self._br = self._pw = None
        try:
            if self.win is not None:
                self.win.destroy()
        except Exception:
            pass
        self.win = None
        kill_tree(self.host)
        self.host = None


def _contrast_ratio(bg: str | None, fg: str | None) -> float | None:
    """两个 CSS 颜色的对比度。算不出来就说算不出来（None），不猜。"""
    def lum(c: str) -> float | None:
        if not c:
            return None
        nums = [float(x) for x in c.replace("rgba(", "").replace("rgb(", "")
                .replace(")", "").split(",")[:4]]
        if len(nums) < 3:
            return None
        if len(nums) >= 4 and nums[3] < 0.9:
            # 半透明 / 全透明：底下是什么颜色，这一层不知道 —— 不猜。
            # 把 rgba(0,0,0,0) 当黑，一个完全正常的页面会被判成对比度 1.0，
            # 然后 agent 去「修」一段本来没问题的代码。
            return None
        def f(v: float) -> float:
            v = v / 255
            return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
        r, g, b = (f(v) for v in nums[:3])
        return 0.2126 * r + 0.7152 * g + 0.0722 * b
    try:
        a, b = lum(bg), lum(fg)
    except Exception:
        return None
    if a is None or b is None:
        return None
    hi, lo = max(a, b), min(a, b)
    return round((hi + 0.05) / (lo + 0.05), 2)


# ------------------------------------------------------------------ 两种入口

def serve_socket(w: Worker, conn: socket.socket) -> None:
    """给 devtools 用：一行一个 JSON 命令，一行一个 JSON 回答。"""
    def send(obj: dict) -> None:
        conn.sendall((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))

    send({"ok": bool(w._ready), "event": "ready", "engine": w.engine,
          "error": None if w._ready else w._why_not_ready(),
          "plugin": w.plugin, "sizes": w._facts() if w._ready else {}})
    if not w._ready:
        return
    buf = b""
    conn.settimeout(IDLE)
    while not w._closing:
        try:
            chunk = conn.recv(65536)
        except socket.timeout:
            break
        except Exception:
            break
        if not chunk:
            break
        buf += chunk
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            if not line.strip():
                continue
            try:
                req = json.loads(line.decode("utf-8"))
            except Exception as exc:
                send({"ok": False, "error": f"请求不是 JSON：{exc}"})
                continue
            op = req.get("op")
            # 一条命令炸了不能连带把窗口拆掉：异常冒出去会被 _work 里那个
            # except 接住，然后整个会话被销毁 —— devtools 只看到「会话断了」，
            # 真正的原因（截图抢跑、求值期间导航）全丢了，而那个会话本来还能用。
            try:
                if op == "eval":
                    send(w.eval(req.get("js") or "", req.get("frame") or "page"))
                elif op == "shot":
                    send(w.shot(bool(req.get("full"))))
                elif op == "check":
                    send(w.check(req.get("areas"), bool(req.get("shot"))))
                elif op == "resize":
                    send(w.resize(int(req.get("width") or NARROW)))
                elif op == "reload":
                    send(w.reload())
                elif op == "info":
                    send({"ok": True, "engine": w.engine, "plugin": w.plugin,
                          "sizes": w._facts(), "url": w.url(), "gen": w.gen})
                elif op == "close":
                    send({"ok": True, "closed": True, "data_changed": w._data_delta()})
                    w._closing = True
                else:
                    send({"ok": False, "error": f"不认识的 op：{op!r}"})
            except Exception as exc:                       # noqa: BLE001
                send({"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:400]}"})


def serve_script(w: Worker, box: dict) -> None:
    """给人用：把命令行要求的那几步跑完，结果放 box。"""
    box["engine"] = w.engine
    box["plugin"] = w.plugin
    if box.get("check"):
        box["report"] = w.check(box.get("areas"), bool(box.get("shot_out")))
        # 脚本入口只给路径：base64 进终端是 200KB 噪声，而图已经落盘了
        box["report"].pop("_png", None)
    if box.get("js"):
        box["eval"] = w.eval(box["js"], box.get("frame") or "page")
    if box.get("shot_out"):
        s = w.shot()
        if s.get("ok"):
            Path(box["shot_out"]).write_bytes(base64.b64decode(s["b64"]))
            box["shot"] = s["path"]
        else:
            box["shot_error"] = s.get("error")
    box["sizes"] = w._facts()


def exit_code(box: dict) -> int:
    """命令行模式的退出码。报告里有 ✗ 就是 1 —— 流水线只看这个数。

    为什么它值得单独一个函数：不看 ``report.ok`` 的后果不是「少一条信息」，
    而是「一页全红、退出 0」—— 调用方从 0 只能得出一个结论：页面是好的。
    这种错在测试里读不出来，只能靠一条把判据钉住的断言。
    """
    if box.get("eval") and not box["eval"].get("ok"):
        return 1
    if box.get("shot_error"):
        return 1
    rep = box.get("report") or {}
    if isinstance(rep, dict) and rep.get("areas"):
        if not all(a.get("ok") for a in rep["areas"]):
            return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="agentworkspace.pagecheck",
                                 description="在真窗口真引擎里看自己的页面")
    ap.add_argument("--pid", required=True, help="哪个程序的页面")
    ap.add_argument("--workspace", default=None, help="真工作台路径（默认仓库 workspace/）")
    ap.add_argument("--host-port", type=int, default=0,
                    help="正在运行的那个宿主的端口（不给就自己起一个，仍用真工作台）")
    ap.add_argument("--viewport", default="pane", choices=tuple(VIEWPORTS))
    ap.add_argument("--width", type=int, default=0, help="窗口宽度（覆盖 viewport）")
    ap.add_argument("--eval", dest="js", default="", help="跑一段 JS，打印结果")
    ap.add_argument("--frame", default="page", choices=("page", "host"),
                    help="在哪一层求值：page=插件页面（默认） / host=外壳")
    ap.add_argument("--check", action="store_true", help="逐面检查并打印报告")
    ap.add_argument("--areas", default="", help="只跑这几面，逗号分隔")
    ap.add_argument("--shot", dest="shot_out", default="", help="截一张图存到这个路径")
    # --json 删掉了：默认输出就是 JSON，一个从不被读的参数只会让人以为它在起作用。
    ap.add_argument("--worker", action="store_true", help="常驻会话模式（给 devtools）")
    ap.add_argument("--port", type=int, default=0, help="worker 模式：命令端口")
    ap.add_argument("--no-relaunch", action="store_true", help="不要重投到交互式会话")
    args = ap.parse_args(argv)

    # 会话 0（服务/无窗口站）起不了 WebView2 → 重投到交互式桌面。
    # 这条判断必须在**起窗口之前**做，而且 --no-relaunch 之后不再重投（否则无限套娃）。
    if not args.no_relaunch:
        from . import qa
        _, visible = qa.station()
        if not visible:
            again = [x for x in (argv if argv is not None else sys.argv[1:])]
            code = qa.relaunch_in_active_session(
                [sys.executable, "-X", "utf8", "-m", "agentworkspace.pagecheck",
                 *again, "--no-relaunch"])
            if code is not None:
                return code

    box: dict = {}
    listener = None
    conn = None
    # 端口先占住：之后**任何一步**失败，都能用同一份协议把原因说出来。
    # 反过来的次序（先把活干完再 bind）在失败时只剩「退出码 1」可看 ——
    # 而重投到交互式会话之后，stdout 已经不在 devtools 手里了。
    if args.worker:
        listener = socket.socket()
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", args.port))
        listener.listen(1)
        listener.settimeout(90)

    src = Path(args.workspace) if args.workspace else (ROOT / "workspace")
    host = None
    host_port = args.host_port
    w = None
    try:
        ws = src
        cdp = free_port()
        # 必须在 import webview **之前**设：WebView2 在创建环境时读它。
        os.environ["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"] = \
            f"--remote-debugging-port={cdp}"
        if not host_port:
            # 没有现成宿主可连（宿主不是带界面启动的）：自己起一个，但仍是**真工作台**。
            # 真数据 + 新窗口 —— 不是复制品，也不跟你抢你正在看的那个窗口。
            host, host_port = spawn_host(ws, args.pid,
                                         ws / ".dev-cache" / args.pid / "host.log")

        w = Worker(args.pid, ws, host_port, cdp, args.viewport, args.width or None)
        w.host = host
        if listener is not None:
            conn, _ = listener.accept()        # 先握手，让对面知道进程活着
            w.run(lambda wk: serve_socket(wk, conn))
        else:
            box.update(check=args.check, js=args.js, shot_out=args.shot_out,
                       frame=args.frame,
                       areas=[a for a in args.areas.split(",") if a.strip()] or None)
            w.run(lambda wk: serve_script(wk, box))
    except Exception as exc:                   # noqa: BLE001
        why = f"{type(exc).__name__}: {exc}"
        if listener is not None:
            try:
                if conn is None:
                    listener.settimeout(20)
                    conn, _ = listener.accept()
                conn.sendall((json.dumps({"ok": False, "event": "ready",
                                          "error": why}, ensure_ascii=False)
                              + "\n").encode("utf-8"))
            except Exception:
                pass
            return 1
        raise
    finally:
        for s in (conn, listener):
            try:
                if s is not None:
                    s.close()
            except Exception:
                pass
        kill_tree(host)

    if listener is not None:
        return 0 if (w is not None and w._ready) else 1
    print(json.dumps(box, ensure_ascii=False, indent=2, default=str), flush=True)
    return exit_code(box)


def __main__(argv: list[str] | None = None) -> int:
    """命令行入口。"""
    return main(argv)


if __name__ == "__main__":
    raise SystemExit(__main__())
