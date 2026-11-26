"""界面**行为**门（真 Chrome，headless 也能跑）—— 会话面板 / 图片气泡 / 开发者开关。

## 为什么有这一份
``uiprobe.py`` 走真 WebView2 窗口 + Win32 输入，最外一环，但要一台有交互桌面的机器。
这一份走**真 Chrome（Playwright channel=chrome）**：页面、CSS、md.js、turn.js 全是真的，
只有数据是喂的（``/api/state`` 与几条会话路由被 route 接住）。

## 门判的是「像素上成立的事实」，不是「代码里写了什么」
- 带图的那句话：``.body`` 与 ``.shots`` **竖着排、同时靠右**。上一版是横排
  （``justify-content:flex-end``），于是正文被挤成一根窄柱、图在右边一列 ——
  只看 CSS 源码看不出这个后果，只有量 bounding box 才知道。
- 开发者开关「开」：轨道的 ``opacity`` 必须是 1。上一版拿 ``opacity:.4`` 做淡，
  而滑块是轨道的 ``::after`` —— 透明度作用在整棵子树上，滑块跟着一起被削掉。
- 会话面板：Esc / 点窗外都得能关；每行只有两个动作；删除要按两下。

## 对照组不是装饰
``--negative`` 会把旧样式**注回去**（``add_style_tag``），断言那几条门确实变红。
不红就说明这套门没打在病上 —— 那时全绿什么也不说明。

    python tests/e2e_browser_ui.py
    python tests/e2e_browser_ui.py --negative
"""
from __future__ import annotations

import json
import re
import struct
import sys
import threading
import zlib
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

ok = fail = 0


def check(name: str, cond, detail: str = "") -> bool:
    global ok, fail
    if cond:
        ok += 1
        print(f"  [ok]   {name}")
    else:
        fail += 1
        print(f"  [FAIL] {name}" + (f" —— {detail}" if detail else ""))
    return bool(cond)


def _png(w: int, h: int) -> bytes:
    """一张真 PNG（不引第三方库）：只有它落下，<img> 才有真实的固有尺寸。"""
    raw = b"".join(b"\x00" + b"\x40\x80\xc0" * w for _ in range(h))

    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


PIC_A = "0123456789abcdef.png"
PIC_B = "fedcba9876543210.png"
SID = "s_0000abcd"

STATE = {
    "programs": [{
        "id": "pomodoro", "title": "番茄钟", "description": "专注 25 分钟",
        "icon": "🍅", "uiKind": "none", "hasUi": False, "dev": False,
    }],
    "native": {},
    "devtools": None,
    "settings": {"url": "", "theme": "auto"},
    "vcs": {"enabled": False, "reason": "e2e"},
    "agents": {
        "pomodoro": {
            "status": "ready", "error": None, "busy": False, "sid": SID,
            "history": [
                {"role": "user", "text": "这两个界面哪个更好看？",
                 "files": [{"name": PIC_A, "mime": "image/png", "title": "a.png"},
                           {"name": PIC_B, "mime": "image/png", "title": "b.png"}]},
                {"role": "assistant", "text": "第一张更清爽。"},
            ],
        },
    },
}

POSTED: dict = {}          # 页面对 /api/settings 的**请求体**，门要查它真交了什么
CHAT: dict = {"posted": []}          # 页面对 /chat 的请求体（门要查图真的随这一轮出去了）
NEW_SESSION: dict = {"empty": False, "sid": "s_0000feed"}   # 新建会话之后线程该是空的

#: [E] 停止=撤回那一节用的会话内容。``collapse`` 为真时，第一轮被撤回 ——
#: 这正是盘上 ``{"t":"retract","turn":"t-e2e-1"}`` 投影出来的那份 rows。
RETRACT_ROWS: dict = {"collapse": True, "rows": [
    {"role": "user", "text": "把那个配置改一下", "turn": "t-e2e-1"},
    {"role": "agent", "text": "好的，我先看看——", "turn": "t-e2e-1"},
    {"role": "user", "text": "第二个问题", "turn": "t-e2e-2"},
    {"role": "agent", "text": "第二个回答", "turn": "t-e2e-2"},
]}


SESSIONS = {
    "pid": "pomodoro", "sid": SID, "truncated": False, "shown": 3,
    "sessions": [
        {"sid": SID, "title": "番茄钟怎么用", "turns": 2, "tokensIn": 10, "tokensOut": 20,
         "size": 900, "created": 1764000000, "updated": 1764003600, "active": True},
        {"sid": "s_0000beef", "title": "提醒喝水", "turns": 1, "tokensIn": 5, "tokensOut": 8,
         "size": 400, "created": 1764000000, "updated": 1763917200, "active": False},
        {"sid": "s_0000cafe", "title": "（未命名）", "turns": 1, "tokensIn": 0, "tokensOut": 0,
         "size": 200, "created": 1763000000, "updated": 1763000000, "active": False},
    ],
}


def start_server() -> tuple[ThreadingHTTPServer, str]:
    from agentworkspace import kernel
    from agentworkspace.server import serve

    ws = ROOT / ".tmp" / "e2e_browser_ui_ws"
    ws.mkdir(parents=True, exist_ok=True)
    rt = kernel.Runtime(ws)
    httpd = serve(rt, None, ROOT / "agentworkspace" / "web", port=0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}"


def open_page(ctx, base: str):
    page = ctx.new_page()
    page.route("**/api/p/**", lambda r: r.fulfill(
        status=200, content_type="application/json", body="{}"))
    page.route("**/api/state", lambda r: r.fulfill(
        status=200, content_type="application/json", body=json.dumps(STATE)))
    page.route("**/api/p/pomodoro/sessions", lambda r: r.fulfill(
        status=200, content_type="application/json", body=json.dumps(SESSIONS)))
    def on_act(route, act):
        if act == "new":
            NEW_SESSION["empty"] = True
        return route.fulfill(
            status=200, content_type="application/json",
            body=json.dumps({"status": "ok",
                             "sid": NEW_SESSION["sid"] if act == "new" else SID,
                             "title": "新会话" if act == "new" else "番茄钟怎么用",
                             "message": f"{act} ok"}))

    # 闭包工厂，不用 `lambda r, a=act`：Playwright 按**参数个数**决定传什么，
    # 两个参数的 handler 收到的是 (route, request) —— 那个 `a` 会被 request 顶掉，
    # `act == "new"` 永远是假（门的红就变成路由的红，不是产品的红）。
    def _binder(act):
        def _handler(route):
            return on_act(route, act)
        return _handler

    for act in ("switch", "new", "delete", "rename"):
        page.route(f"**/api/p/pomodoro/session/{act}", _binder(act))
    def on_chat(route):
        """发一句话：受理。门要的是「受理之后气泡里的图还在不在」。"""
        CHAT["posted"].append(json.loads(route.request.post_data or "{}"))
        return route.fulfill(status=200, content_type="application/json",
                             body=json.dumps({"status": "sent", "pid": "pomodoro"}))

    def on_session(route):
        # 新建会话之后，那个会话是**空的** —— 门要证明「一次点击真的换了一份空历史」。
        if NEW_SESSION["empty"]:
            return route.fulfill(
                status=200, content_type="application/json",
                body=json.dumps({"pid": "pomodoro", "sid": NEW_SESSION["sid"],
                                 "title": "新会话", "active": True, "messages": [],
                                 "total": 0, "shown": 0, "truncated": False,
                                 "turns": 0, "tokensIn": 0, "tokensOut": 0, "bad": 0}))
        rows = [dict(r) for r in RETRACT_ROWS["rows"]]
        if RETRACT_ROWS.get("collapse"):
            for r in rows[:2]:                 # 第一轮整组被撤回
                r["retracted"] = True
        return route.fulfill(
            status=200, content_type="application/json",
            body=json.dumps({"pid": "pomodoro", "sid": SID, "title": "番茄钟怎么用",
                             "active": True, "messages": rows, "total": len(rows),
                             "shown": len(rows), "truncated": False,
                             "turns": 0, "tokensIn": 0, "tokensOut": 0, "bad": 0}))

    # 重载会话那一条：带上 ?limit=200（正则，别让 img 那些也落进来）。
    # sid 用形状而不是字面量：新建会话之后是**另一个** sid，钉死旧的就接不住它，
    # 请求会落到兜底 `{}` —— 那时门红的是路由，不是产品。
    page.route(re.compile(r"/session/s_[0-9a-f]{8}\?limit="), on_session)
    page.route(f"**/session/{SID}/img/*.png", lambda r: r.fulfill(
        status=200, content_type="image/png",
        body=_png(300, 200) if PIC_A in r.request.url else _png(200, 300)))
    def on_settings(route):
        if route.request.method != "POST":
            return route.fulfill(status=200, content_type="application/json",
                                 body=json.dumps({"url": "", "theme": "auto", "agents": {}}))
        POSTED.clear()
        POSTED.update(json.loads(route.request.post_data or "{}"))
        return route.fulfill(
            status=200, content_type="application/json",
            body=json.dumps({"status": "ok", "url": POSTED.get("url", ""),
                             "theme": POSTED.get("theme", "auto"),
                             "reconnected": [], "agents": {}}))

    page.route("**/api/p/pomodoro/chat", on_chat)
    page.route("**/api/settings", on_settings)
    page.goto(f"{base}/#p/pomodoro")
    page.wait_for_selector("#thread .msg", timeout=15000)
    page.wait_for_timeout(250)
    return page


# ---------------------------------------------------------------- 小工具
def box(page, sel: str) -> dict | None:
    return page.evaluate("""(sel) => {
      const el = document.querySelector(sel);
      if (!el) return null;
      const r = el.getBoundingClientRect();
      return {x:r.x, y:r.y, w:r.width, h:r.height, top:r.top, bottom:r.bottom,
              left:r.left, right:r.right};
    }""", sel)


def bg_lum(page) -> float:
    """页面底色有多亮（0 = 全黑，1 = 全白）。门判的是这个，不是 data-theme 属性
    —— 属性对而调色板没换，正是「假绿」的经典形态。"""
    return page.evaluate("""() => {
      const m = getComputedStyle(document.body).backgroundColor.match(/[\\d.]+/g) || [];
      const c = m.map(Number);
      if (c.length < 3) return -1;
      return (0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]) / 255;
    }""")


def theme_pref(page) -> str:
    return page.evaluate("() => (window.S && S.settings && S.settings.theme) || (document"
                         ".querySelector('#themeseg button.on') || {}).dataset?.themePref "
                         "|| ''")


def gates_theme(page) -> None:
    print("\n[F] 外观（深/浅）：在设置里，三档，点了就变，取消还原，真落盘")
    page.click("#setbtn")
    page.wait_for_selector("#mask.on", timeout=5000)
    labels = page.evaluate(
        "[...document.querySelectorAll('#themeseg button')].map(b=>b.textContent.trim())")
    check("设置面板里有「外观」三档", labels == ["跟随系统", "深色", "浅色"], str(labels))
    on = page.evaluate(
        "(document.querySelector('#themeseg button.on')||{}).dataset?.themePref")
    check("打开面板时选中的是当前那档（auto）", on == "auto", str(on))

    # 系统这台机器是 dark（见 main 里的 color_scheme），所以 auto 解析成 dark
    check("auto 档按系统解析：页面是深的", bg_lum(page) < 0.2, f"lum={bg_lum(page):.2f}")

    page.click("#themeseg button[data-theme-pref='light']")
    page.wait_for_timeout(120)
    check("点「浅色」→ html 上是 light",
          page.evaluate("document.documentElement.dataset.theme") == "light")
    check("点「浅色」→ 底色真的变亮了（不是只改了个属性）",
          bg_lum(page) > 0.7, f"lum={bg_lum(page):.2f}")

    page.click("#setcancel")
    page.wait_for_timeout(120)
    check("取消 → 面板关掉", not page.evaluate(
        "document.querySelector('#mask').classList.contains('on')"))
    check("取消 → 外观还原成打开前那档（预览不是既成事实）",
          bg_lum(page) < 0.2 and page.evaluate("document.documentElement.dataset.theme") == "dark",
          f"lum={bg_lum(page):.2f}")

    page.click("#setbtn")
    page.wait_for_selector("#mask.on", timeout=5000)
    page.click("#themeseg button[data-theme-pref='light']")
    page.click("#setsave")
    page.wait_for_timeout(250)
    check("保存时把外观一起交给了 /api/settings", POSTED.get("theme") == "light", str(POSTED))
    check("保存后仍然是浅色（不会被「关面板」撤回）",
          bg_lum(page) > 0.7, f"lum={bg_lum(page):.2f}")
    check("保存后那档记在 S 里（刷新/下一个窗口按它画）",
          theme_pref(page) == "light", theme_pref(page))
    page.keyboard.press("Escape")
    page.wait_for_timeout(120)
    check("已保存的外观：Esc 关面板也不会被还原", bg_lum(page) > 0.7, f"lum={bg_lum(page):.2f}")

    # auto 要实时跟随系统：CSS 已经不自己看 @media 了，靠的就是这条监听
    page.click("#setbtn")
    page.wait_for_selector("#mask.on", timeout=5000)
    page.click("#themeseg button[data-theme-pref='auto']")
    page.wait_for_timeout(120)
    check("选回 auto → 按当前系统（dark）解析成深色",
          page.evaluate("document.documentElement.dataset.theme") == "dark",
          page.evaluate("document.documentElement.dataset.theme"))
    page.emulate_media(color_scheme="light")
    page.wait_for_timeout(150)
    check("auto 档：系统变浅，页面跟着变浅（实时，不用刷新）",
          bg_lum(page) > 0.7 and theme_pref(page) == "auto",
          f"lum={bg_lum(page):.2f} pref={theme_pref(page)}")
    page.click("#themeseg button[data-theme-pref='dark']")
    page.emulate_media(color_scheme="dark")
    page.wait_for_timeout(120)
    page.emulate_media(color_scheme="light")
    page.wait_for_timeout(150)
    check("显式选了深色：系统变浅也**不动**它（这就是「设置里那一档」的意思）",
          page.evaluate("document.documentElement.dataset.theme") == "dark"
          and bg_lum(page) < 0.2,
          f"lum={bg_lum(page):.2f} theme={page.evaluate('document.documentElement.dataset.theme')}")
    page.click("#setcancel")
    page.wait_for_timeout(100)


def gates_bubble(page) -> None:
    print("\n[A] 一句话带几张图：竖着排、整块靠右")
    fd = page.evaluate(
        "getComputedStyle(document.querySelector('.msg.me')).flexDirection")
    check("带图的那条是竖排（旧版横排 → 正文被挤成窄柱）", fd == "column", f"flex-direction={fd}")
    body = box(page, ".msg.me .body")
    shots = box(page, ".msg.me .shots")
    check("正文与图都在（不是被谁挤没）", bool(body) and bool(shots),
          f"body={bool(body)} shots={bool(shots)}")
    if body and shots:
        check("图在正文**下面**，不是右边", body["bottom"] <= shots["top"] + 2,
              f"body.bottom={body['bottom']:.1f} shots.top={shots['top']:.1f}")
        check("两块都靠右（右边缘对齐，误差 < 2px）", abs(body["right"] - shots["right"]) < 2,
              f"body.right={body['right']:.1f} shots.right={shots['right']:.1f}")
        # 判据是**行数**而不是宽度：这句话 11 个字，178px 正是它该有的宽度；
        # 被挤成窄柱时它会折成两行 —— 高度从 ~38 变成 ~60。
        check("正文没有被迫折行（一行装得下 → 没被挤成窄柱）", body["h"] <= 50,
              f"h={body['h']:.1f}")
    sizes = page.evaluate("""() => [...document.querySelectorAll('.msg.me .shots img')]
      .map(i => [i.offsetWidth, i.offsetHeight])""")
    check("缩略图 ≤ 120px 且真的画出来了（两张）",
          len(sizes) == 2 and all(0 < w <= 120 and 0 < h <= 120 for w, h in sizes), str(sizes))

    print("\n[B] 点缩略图看大图；Esc 关掉")
    src = page.evaluate("document.querySelector('.msg.me .shots img').src")
    page.locator(".msg.me .shots img").first.click(force=True)
    page.wait_for_timeout(150)
    check("#lb 打开了", page.evaluate("document.querySelector('#lb').classList.contains('on')"))
    check("大图装的就是点的那一张",
          page.evaluate("document.querySelector('#lb img').src") == src)
    page.keyboard.press("Escape")
    page.wait_for_timeout(150)
    check("Esc 关掉大图", not page.evaluate(
        "document.querySelector('#lb').classList.contains('on')"))


def gates_dev(page) -> None:
    print("\n[C] 开发者开关：「开」不能靠 opacity 做淡")
    page.evaluate("""async () => {
      await fetch('/api/p/pomodoro/dev', {method: 'POST',
        headers: {'content-type': 'application/json'},
        body: JSON.stringify({on: true})});
    }""")
    page.evaluate("paintDev('pomodoro', true)")
    page.wait_for_timeout(120)
    st = page.evaluate("""() => {
      const i = document.querySelector('.dev i');
      const cs = getComputedStyle(i);
      const kn = getComputedStyle(i, '::after');
      return {opacity: cs.opacity, bg: cs.backgroundColor,
              knob: kn.transform, knobBg: kn.backgroundColor,
              on: document.querySelector('.dev').classList.contains('on')};
    }""")
    check("开关在「开」态", st.get("on") is True, str(st))
    check("轨道没有被 opacity 弄淡（旧版 .4 会把滑块一起削掉）",
          st.get("opacity") == "1", f"opacity={st.get('opacity')}")
    tx = page.evaluate("""() => {
      const t = getComputedStyle(document.querySelector('.dev i'), '::after').transform;
      const m = /matrix\\(([^)]*)\\)/.exec(t);
      return m ? Number(m[1].split(',')[4]) : NaN;
    }""")
    check("滑块真的滑到右边了（横移 ≈ 11px）", isinstance(tx, (int, float)) and tx > 9,
          f"translateX={tx} raw={st.get('knob')}")
    check("滑块与轨道不是同一个颜色（形状才读得出来）",
          st.get("knobBg") not in ("rgba(0, 0, 0, 0)", str(st.get("bg"))),
          f"knob={st.get('knobBg')} track={st.get('bg')}")


def gates_sessions(page) -> dict:
    print("\n[D] 会话面板：一屏读得懂，两条路都能关")
    page.click("#sessbtn")
    page.wait_for_selector("#smask.on")
    page.wait_for_selector("#slist .vrow", timeout=8000)
    page.wait_for_timeout(120)
    rows = page.evaluate("document.querySelectorAll('#slist .vrow').length")
    check("三个会话都列出来了", rows == 3, str(rows))
    btns = page.evaluate("""() => [...document.querySelectorAll('#slist .vrow')]
      .map(r => [...r.querySelectorAll('button')].map(b => b.textContent.trim()))""")
    check("每行只有两个动作（旧版是三个：切过去 / 改名 / 删除）",
          all(len(b) == 2 for b in btns), str(btns))
    check("没有「切过去」这个按钮（整行就是它）",
          not any("切过去" in t for row in btns for t in row), str(btns))
    check("当前那一行写着「当前」",
          page.evaluate("(document.querySelector('#slist .vrow.cur .scur')||{}).textContent")
          == "当前")
    meta = page.evaluate("(document.querySelector('#slist .vrow .vmeta')||{}).textContent")
    check("元信息是人话：没有 token、没有仓库路径",
          meta and "token" not in meta and ".trash" not in meta, repr(meta))
    hint = page.evaluate("document.querySelector('#smsg').textContent")
    check("提示语没漏出 sessions/.trash 路径", ".trash" not in hint, repr(hint))

    # 点行切换
    reqs = []
    page.on("request", lambda r: reqs.append(r.url))
    page.locator("#slist .vrow").nth(1).click()
    page.wait_for_timeout(250)
    check("点一行就切过去（发了 switch 请求）",
          any("/session/switch" in u for u in reqs), str(reqs[-3:]))
    reqs.clear()

    # 两步删除
    page.wait_for_selector("#slist .vrow", timeout=8000)
    page.wait_for_timeout(120)
    dele = page.locator("#slist .vrow").nth(2).locator("button.dele")
    dele.click()
    page.wait_for_timeout(120)
    check("第一下只把按钮变成「确认删除」", dele.text_content().strip() == "确认删除",
          dele.text_content().strip())
    check("第一下**没有**发出删除请求",
          not any("/session/delete" in u for u in reqs), str(reqs))
    dele.click()
    page.wait_for_timeout(250)
    check("第二下才真的删", any("/session/delete" in u for u in reqs), str(reqs))

    # Esc 关
    page.wait_for_selector("#slist .vrow", timeout=8000)
    page.wait_for_timeout(120)
    page.keyboard.press("Escape")
    page.wait_for_timeout(150)
    closed_by_esc = not page.evaluate(
        "document.querySelector('#smask').classList.contains('on')")
    check("Esc 能关掉会话面板", closed_by_esc)

    # 点窗外关
    page.click("#sessbtn")
    page.wait_for_selector("#smask.on")
    page.wait_for_timeout(120)
    page.evaluate("""() => {
      const m = document.querySelector('#smask');
      m.dispatchEvent(new MouseEvent('click', {bubbles: true}));   // target 就是遮罩本身
    }""")
    page.wait_for_timeout(150)
    check("点窗外也能关", not page.evaluate(
        "document.querySelector('#smask').classList.contains('on')"))
    return {"closedByEsc": closed_by_esc}


def gates_drag(page) -> None:
    print("\n[E] 拖文件进来要有落点反馈")
    page.evaluate("""() => {
      const f = new File([new Uint8Array([1,2,3])], 'a.png', {type: 'image/png'});
      const dt = new DataTransfer();
      dt.items.add(f);
      window.__dt = dt;
      document.querySelector('#composer').dispatchEvent(
        new DragEvent('dragenter', {dataTransfer: dt, bubbles: true}));
    }""")
    page.wait_for_timeout(80)
    check("拖进来时输入区亮了", page.evaluate(
        "document.querySelector('#composer').classList.contains('drag')"))
    page.evaluate("""() => {
      document.querySelector('#composer').dispatchEvent(
        new DragEvent('dragover', {dataTransfer: window.__dt, bubbles: true}));
    }""")
    page.wait_for_timeout(60)
    check("拖过去的时候还亮着（不在子元素之间闪）", page.evaluate(
        "document.querySelector('#composer').classList.contains('drag')"))
    page.evaluate("""() => {
      document.querySelector('#composer').dispatchEvent(
        new DragEvent('dragleave', {dataTransfer: window.__dt, bubbles: true}));
    }""")
    page.wait_for_timeout(80)
    check("离开就撤", not page.evaluate(
        "document.querySelector('#composer').classList.contains('drag')"))


def negative_controls(page) -> None:
    """把旧样式注回去 —— 这几条门必须红。不红就是没打在病上。"""
    print("\n[G] 对照组：旧样式注回去，门必须红")
    page.add_style_tag(content=".msg.me{flex-direction:row;justify-content:flex-end}")
    page.wait_for_timeout(120)
    body = box(page, ".msg.me .body")
    shots = box(page, ".msg.me .shots")
    red = bool(body and shots) and body["bottom"] > shots["top"] + 2
    check("旧横排版：正文与图并排（「图在下面」这条会红）", red,
          f"body.bottom={body['bottom']:.1f} shots.top={shots['top']:.1f}" if body and shots else "")

    # 旧做法：深浅交给 @media 决定 —— 那就等于「设置里那一档管不住系统」
    page.evaluate("() => applyTheme('dark')")
    page.emulate_media(color_scheme="light")
    page.wait_for_timeout(150)
    check("对照组前置：显式深色 + 系统浅色 → 页面仍是深的",
          bg_lum(page) < 0.2, f"lum={bg_lum(page):.2f}")
    page.add_style_tag(
        content="@media (prefers-color-scheme:light){:root{--bg:#f6f7f9}}")
    page.wait_for_timeout(150)
    check("旧 @media 写法注回去：系统一浅，显式深色就被盖掉（那条门会红）",
          bg_lum(page) > 0.7, f"lum={bg_lum(page):.2f}")

    page.add_style_tag(content=".dev.on i{opacity:.4}")
    page.wait_for_timeout(120)
    op = page.evaluate("getComputedStyle(document.querySelector('.dev i')).opacity")
    check("旧 opacity 版：轨道真的被削到 0.4（那条门会红）", op == "0.4", f"opacity={op}")


def gates_retract(page) -> dict:
    """[E] 停止 = 撤回：被撤回的那一轮塌成一枚记号。

    走的是**真重载路径**（reloadThread → /session/<sid>?limit= → groupRetracted
    → DOM），不是直接往 S.msgs 里塞数据：塞数据只能证明「画得出来」，证明不了
    「盘上那条 retract 记录能一路走到屏幕上」。
    """
    print("\n[E] 停止 = 撤回：塌成一枚记号，不是正文 + 一句尾注")
    page.evaluate("reloadThread('pomodoro', '%s')" % SID)
    page.wait_for_selector("#thread .retract", timeout=8000)
    page.wait_for_timeout(150)
    st = page.evaluate("""() => {
      const r = document.querySelectorAll('#thread .retract');
      const cs = r.length ? getComputedStyle(r[0]) : null;
      return {
        n: r.length,
        me: document.querySelectorAll('#thread .msg.me').length,
        meText: [...document.querySelectorAll('#thread .msg.me')]
                  .map(e => e.textContent).join(' | '),
        q: (r[0] && r[0].querySelector('.rq') || {}).textContent || '',
        text: document.querySelector('#thread').textContent,
        border: cs ? cs.borderTopStyle : '',
        dim: cs ? cs.borderTopColor : '',
        body: document.querySelectorAll('#thread .msg.it .body:not(:empty)').length,
        open: document.querySelectorAll('#thread .retract .rb').length,
        btn: [...document.querySelectorAll('#thread .retract button')]
               .map(b => b.textContent.trim()),
      };
    }""")
    check("撤回的一轮变成**恰好一枚**记号", st["n"] == 1, str(st["n"]))
    # 原话**该在记号里出现一次**（那是给人看重发用的），但不该再是一句正文气泡：
    # 判据是「哪一类节点里有没有它」，不是「整页有没有它」。整页扫字符串的话，
    # 这条门连正确的实现也会判红 —— 假红和假绿是同一个毛病：门没打在病上。
    check("被撤回的那句话不再是正文气泡（只剩 1 条 .msg.me：未撤回的那一句）",
          st["me"] == 1 and "把那个配置改一下" not in st["meText"],
          f"me={st['me']} {st['meText']}"[:90])
    check("原话整页只出现一次（没有同时留在正文和记号里）",
          st["text"].count("把那个配置改一下") == 1,
          str(st["text"].count("把那个配置改一下")))
    check("记号上留着原话（不知道刚才问了什么，就没法重发）",
          "把那个配置改一下" in st["q"], repr(st["q"]))
    check("未撤回的那一轮照常在正文里（门没把工具砸到所有气泡上）",
          "第二个回答" in st["text"], "")
    check("记号是虚线的（视觉上「不属于对话」）", st["border"] == "dashed", st["border"])
    check("半句回答默认收着（不占正文）", st["open"] == 0, str(st["open"]))
    check("记号上只有两个动作：展开 / 重新发送",
          st["btn"] == ["展开", "重新发送"], str(st["btn"]))

    # 展开：那半句露出来，而且带删除线（存在过，但不算话）
    page.click("#thread .retract .rx")
    page.wait_for_timeout(120)
    ex = page.evaluate("""() => {
      const b = document.querySelector('#thread .retract .rb .body');
      const t = document.querySelector('#thread .retract .rx');
      return {has: !!b, line: b ? getComputedStyle(b).textDecorationLine : '',
              txt: b ? b.textContent : '', label: t ? t.textContent.trim() : ''};
    }""")
    check("点「展开」露回那半句", ex["has"] and "我先看看" in ex["txt"], str(ex))
    check("展开的那半句带删除线", "line-through" in ex["line"], ex["line"])
    check("按钮就地变成「收起」", ex["label"] == "收起", ex["label"])

    # 重新发送：只把原话填回输入框，**不替人发**
    reqs = []
    page.on("request", lambda r: reqs.append(r.url))
    page.click("#thread .retract .ry")
    page.wait_for_timeout(150)
    box = page.evaluate("(document.querySelector('#ta')||{}).value || ''")
    check("点「重新发送」把原话填回输入框", box == "把那个配置改一下", repr(box))
    check("重新发送**不**自己发出去（人按发送才算一句话）",
          not any("/send" in u for u in reqs), str(reqs[-3:]))

    # 反面对照：同一份数据、只是没被撤回 —— 必须**一枚记号都没有**。
    # 少了这一半，一个「把所有气泡都塌成记号」的实现也会让上面全绿。
    RETRACT_ROWS["collapse"] = False
    page.evaluate("reloadThread('pomodoro', '%s')" % SID)
    page.wait_for_timeout(250)
    n2 = page.evaluate("document.querySelectorAll('#thread .retract').length")
    check("没被撤回就**一枚记号都没有**（门判的是 retract 记录，不是「有对话就塌」）",
          n2 == 0, str(n2))


def gates_ui_fixes(page) -> None:
    """[G] 三处界面问题：新建会话一次点击 / 图片发出去还是图 / 开关垂直居中。

    三条都走**真浏览器**：判据是「屏幕上画成了什么」（naturalWidth、几何中心、
    请求次数），不是「源码里有没有那个字符串」。
    """
    print("\n[G] 三处界面问题")

    # ---- G1 图片：发出去之后，气泡里还得是**图**，不是「破图标 + 文件名」
    # 病根：待发图的 blob URL 被预览条和气泡**两个主人**共用，预览条清空时
    # 把它吊销了 —— 已经解码的那张还看得见，可下一次重绘（每个 delta 都会重绘）
    # 新建的 <img> 再去取同一个 URL 就取不到了，浏览器画成破图标 + alt 文本。
    # 所以这条门必须**在发送之后重绘一次**：不重绘的话，旧实现也是绿的。
    page.evaluate("clearPics('pomodoro')")
    page.set_input_files("#pickf", {"name": "image.png", "mimeType": "image/png",
                                    "buffer": _png(40, 30)})
    page.wait_for_timeout(200)
    pend = page.evaluate("""() => {
      const im = document.querySelector('.pends img');
      return im ? {w: im.naturalWidth, h: im.naturalHeight} : null;
    }""")
    check("贴进来的图先出现在预览条上（这一步本来就对）",
          bool(pend) and pend["w"] > 0, str(pend))

    CHAT["posted"].clear()
    page.click("#sendb")
    page.wait_for_timeout(400)
    check("图随这一轮真的发出去了（不是只画在屏幕上）",
          len(CHAT["posted"]) == 1 and len(CHAT["posted"][0].get("images") or []) == 1,
          str([{"images": len(p.get("images") or [])} for p in CHAT["posted"]]))

    page.evaluate("paintThread()")          # 生产里每个 delta 都会重绘一次
    page.wait_for_timeout(200)
    shot = page.evaluate("""() => {
      const im = [...document.querySelectorAll('.msg.me .shots img')].pop();
      if (!im) return null;
      return {w: im.naturalWidth, h: im.naturalHeight, alt: im.alt,
              blob: im.src.startsWith('blob:'), complete: im.complete};
    }""")
    check("重绘之后气泡里的图**还是图**（旧版在这里变成破图标 + 文件名）",
          bool(shot) and shot["w"] > 0 and shot["h"] > 0, str(shot))
    check("那张图不是靠 alt 文本撑着的（alt 只是兜底，不是内容）",
          bool(shot) and shot["alt"] == "image.png", str(shot))

    # 反面对照：把同一个 URL 吊销掉再重绘 —— 必须**变红**。
    # 少了这一半，一个「永远不 revoke」的实现也会让上面全绿，而那是内存泄漏。
    page.evaluate("""() => {
      const im = [...document.querySelectorAll('.msg.me .shots img')].pop();
      if (im) URL.revokeObjectURL(im.src);
      paintThread();
    }""")
    page.wait_for_timeout(200)
    dead = page.evaluate("""() => {
      const im = [...document.querySelectorAll('.msg.me .shots img')].pop();
      return im ? {w: im.naturalWidth, complete: im.complete} : null;
    }""")
    check("反面对照：URL 被吊销之后这张图确实画不出来（门打在病上）",
          bool(dead) and dead["w"] == 0, str(dead))

    # ---- G2 开发者开关：与同排东西的垂直中心对齐
    page.evaluate("paintDev('pomodoro', true)")
    page.wait_for_timeout(120)
    geo = page.evaluate("""() => {
      const c = (el) => { const r = el.getBoundingClientRect();
        return r.top + r.height / 2; };
      return {align: getComputedStyle(document.querySelector('.chat header')).alignItems,
              sw: c(document.querySelector('.dev i')),
              lab: c(document.querySelector('.dev')),
              btn: c(document.querySelector('#sessbtn')),
              title: c(document.querySelector('.chat header b'))};
    }""")
    check("开关的垂直中心与同排按钮重合（误差 ≤ 1px）",
          abs(geo["sw"] - geo["btn"]) <= 1, str(geo))
    check("开关的垂直中心与标题那一行重合（误差 ≤ 1px）",
          abs(geo["sw"] - geo["title"]) <= 1, str(geo))
    check("开关在自己的标签里也是居中的（不是被文字基线顶上去）",
          abs(geo["sw"] - geo["lab"]) <= 1, str(geo))

    # 反面对照：把 baseline 注回去 —— 必须**变红**
    page.evaluate("""() => {
      const h = document.querySelector('.chat header');
      h.dataset.align = h.style.alignItems;
      h.style.alignItems = 'baseline';
    }""")
    page.wait_for_timeout(120)
    bad = page.evaluate("""() => {
      const c = (el) => { const r = el.getBoundingClientRect();
        return r.top + r.height / 2; };
      return {sw: c(document.querySelector('.dev i')),
              btn: c(document.querySelector('#sessbtn'))};
    }""")
    check("反面对照：baseline 对齐时开关确实偏了（门打在病上）",
          abs(bad["sw"] - bad["btn"]) > 1, str(bad))
    page.evaluate("""() => {
      const h = document.querySelector('.chat header');
      h.style.alignItems = h.dataset.align || '';
    }""")

    # ---- G3 新建会话：**一次**点击
    # 判据是「点了几下」：一次点击之后必须已经换到一份空历史，且面板没被打开
    # （打开面板再点「新建会话」是两次 —— 那正是要修掉的那一步）。
    page.evaluate("reloadThread('pomodoro', '%s')" % SID)
    page.wait_for_selector("#thread .msg", timeout=8000)
    page.wait_for_timeout(150)
    before = page.evaluate("document.querySelectorAll('#thread .msg').length")
    check("先有对话（否则「换空了」证明不了什么）", before > 0, str(before))

    reqs = []
    page.on("request", lambda r: reqs.append(r.url))
    page.click("#newbtn")                    # ← 就这一下
    page.wait_for_timeout(500)
    check("点一下「＋」就发出了新建会话的请求",
          any("/session/new" in u for u in reqs), str(reqs[-3:]))
    check("**没有**顺手把会话面板打开（那一步是多余的）",
          not page.evaluate("document.querySelector('#smask').classList.contains('on')"))
    after = page.evaluate("""() => ({
      me: document.querySelectorAll('#thread .msg.me').length,
      text: document.querySelector('#thread').textContent,
      sid: (typeof S === 'object' && S.sid && S.sid['pomodoro']) || '',
    })""")
    # 判据是「旧对话的话一个字都不在了」，不是「一个节点都没有」：新会话那句
    # 注脚自己就是一个 .msg.it .body，数节点会把正确的实现判红。
    check("一次点击之后线程换成了空的（旧对话不再挂在屏幕上）",
          after["me"] == 0 and "第二个回答" not in after["text"], str(after)[:120])
    check("当前会话号跟着换了（不是「屏幕空了、其实还在旧会话里」）",
          after["sid"] == NEW_SESSION["sid"], str(after["sid"]))
    check("空线程里有一句人话说明旧对话还在（不是凭空消失）",
          "还在" in after["text"], repr(after["text"])[:80])

    # 反面对照：只点「会话」—— 必须**不**新建（否则「一次点击」这条门是假的）
    NEW_SESSION["empty"] = False
    page.evaluate("reloadThread('pomodoro', '%s')" % SID)
    page.wait_for_timeout(200)
    reqs.clear()
    page.click("#sessbtn")
    page.wait_for_timeout(300)
    check("反面对照：只点「会话」不会新建（它只是打开面板）",
          not any("/session/new" in u for u in reqs), str(reqs[-3:]))
    page.keyboard.press("Escape")
    page.wait_for_timeout(150)

    # ---- G4 窄窗口：多一颗按钮不许把 header 挤坏
    # 判据是「还在一行里」+「每个控件都完整落在 header 之内」，不是「没报错」。
    page.set_viewport_size({"width": 420, "height": 720})
    page.wait_for_timeout(300)
    nar = page.evaluate("""() => {
      const hd = document.querySelector('.chat header');
      const r = hd.getBoundingClientRect();
      const kids = [...hd.children].map((e) => {
        const b = e.getBoundingClientRect();
        return {id: e.id || e.tagName, w: b.width,
                insideX: b.left >= r.left - 1 && b.right <= r.right + 1,
                insideY: b.top >= r.top - 1 && b.bottom <= r.bottom + 1};
      });
      return {h: r.height, w: r.width, kids};
    }""")
    check("窗口窄到 420px 时 header 仍是**一行**（没有被挤成两行）",
          nar["h"] <= 56, f"h={nar['h']} w={nar['w']}")
    bad = [k["id"] for k in nar["kids"] if not (k["insideX"] and k["insideY"])]
    check("窄屏时每个控件都完整落在 header 里（没有被切掉 / 溢出）",
          not bad, f"溢出的：{bad}")
    nb = next((k for k in nar["kids"] if k["id"] == "newbtn"), None)
    check("窄屏时那颗「＋」还在（宽度 > 8px，没被挤没）",
          bool(nb) and nb["w"] > 8, str(nb))
    page.set_viewport_size({"width": 1180, "height": 760})
    page.wait_for_timeout(200)


def main() -> int:
    neg = "--negative" in sys.argv
    httpd, base = start_server()
    print(f"真服务：{base}\n")

    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        try:
            br = p.chromium.launch(headless=True, channel="chrome")
        except Exception as e:                       # pragma: no cover
            print(f"[跳过] 这台机器起不了真 Chrome：{type(e).__name__}: {e}")
            httpd.shutdown()
            return 0
        print(f"真浏览器：{br.version}")
        # 系统主题钉成 dark：auto 档才有一个确定的答案可断言
        ctx = br.new_context(viewport={"width": 1180, "height": 760},
                             color_scheme="dark")
        page = open_page(ctx, base)
        gates_bubble(page)
        gates_dev(page)
        gates_sessions(page)
        gates_drag(page)
        gates_theme(page)
        gates_retract(page)
        gates_ui_fixes(page)
        if neg:
            negative_controls(page)
        ctx.close()
        br.close()

    httpd.shutdown()
    print(f"\n{'全绿' if fail == 0 else '有失败'}：{ok} 项过 / {fail} 项不过")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
