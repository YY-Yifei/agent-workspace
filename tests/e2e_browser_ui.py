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
    "settings": {"url": ""},
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
    for act in ("switch", "new", "delete", "rename"):
        page.route(f"**/api/p/pomodoro/session/{act}", lambda r, a=act: r.fulfill(
            status=200, content_type="application/json",
            body=json.dumps({"status": "ok", "sid": SID, "title": "番茄钟怎么用",
                             "message": f"{a} ok"})))
    page.route(f"**/session/{SID}/img/*.png", lambda r: r.fulfill(
        status=200, content_type="image/png",
        body=_png(300, 200) if PIC_A in r.request.url else _png(200, 300)))
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
    print("\n[F] 对照组：旧样式注回去，门必须红")
    page.add_style_tag(content=".msg.me{flex-direction:row;justify-content:flex-end}")
    page.wait_for_timeout(120)
    body = box(page, ".msg.me .body")
    shots = box(page, ".msg.me .shots")
    red = bool(body and shots) and body["bottom"] > shots["top"] + 2
    check("旧横排版：正文与图并排（「图在下面」这条会红）", red,
          f"body.bottom={body['bottom']:.1f} shots.top={shots['top']:.1f}" if body and shots else "")

    page.add_style_tag(content=".dev.on i{opacity:.4}")
    page.wait_for_timeout(120)
    op = page.evaluate("getComputedStyle(document.querySelector('.dev i')).opacity")
    check("旧 opacity 版：轨道真的被削到 0.4（那条门会红）", op == "0.4", f"opacity={op}")


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
        ctx = br.new_context(viewport={"width": 1180, "height": 760})
        page = open_page(ctx, base)
        gates_bubble(page)
        gates_dev(page)
        gates_sessions(page)
        gates_drag(page)
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
