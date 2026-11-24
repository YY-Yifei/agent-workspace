"""「选中复制」的浏览器端到端：真 Chrome、真鼠标、真键盘、真剪贴板、真页面。

## 它和 tests/e2e_copy.py 的分工

``e2e_copy.py`` 走**真 WebView2 窗口 + Win32 输入 + 系统剪贴板**，那是最外一环，
但它要求一台有交互桌面的机器。这一份走**真 Chrome（Playwright channel=chrome）**，
在 headless 下也能跑，于是 CI / 无桌面会话里也有话说。

两者都不做 ``Selection.addRange()`` 那种程序化选中 —— ``user-select:none`` 拦不住
程序化选中（这一点上探针已经验过：注入规则后 addRange 照样成功），
那样测出来的绿是假的。这里一律用 ``page.mouse`` 的真按下/移动/抬起。

## 为什么要把 pywebview 那条规则注入进来

被测的页面在**普通浏览器**里本来就能选中 —— 病不在页面，在宿主注入的
``body{-webkit-user-select:none;cursor:default}``（pywebview 的 customize.js 在
``text_select=False`` 时追加到 head 末尾）。不注入它，这整套测试等于什么都没测。
注入位置也照抄（head 末尾），因为「后写者赢」正是它当初压住别人的方式。

## 对照组不是装饰

``old`` 组把页面的 ``#thread,.dlg{user-select:text}`` 规则从 CSSOM 里删掉，
模拟「修复前」。如果 old 组也能选中，说明这套测试没打在病上 ——
那时 new 组的绿不说明任何问题。所以脚本会在 old 组**不红**时明确报 FAIL。

    python tests/e2e_browser_copy.py
"""
from __future__ import annotations

import json
import sys
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

MARK = "COPYE2E_ALPHA_9527"          # 断言必须出现在选中文字里的 ASCII 主干
BODY = (
    f"第一段：这句话里藏着标记 {MARK}，它必须能被鼠标拖出来。\n\n"
    "第二段：中文与 English 混排，用来确认跨行拖选也没问题。\n\n"
    "第三段：复制走的是页面真实的那条路（Ctrl+C → 剪贴板）。"
)
USER_TEXT = "你好，我是一句用户发言，选中我。"

ok = fail = 0


def check(name: str, cond, detail: str = "") -> None:
    global ok, fail
    if cond:
        ok += 1
        print(f"  [ok]   {name}")
    else:
        fail += 1
        print(f"  [FAIL] {name}" + (f" —— {detail}" if detail else ""))


# ------------------------------------------------------------------ 真服务
def start_server() -> tuple[ThreadingHTTPServer, str]:
    """起真的 agent-workspace 服务：页面、md.js、turn.js 都走真路由。

    只用 route 伪造 ``/api/state``（为的是让对话区里有内容可选）。
    其余 HTTP 一律是真的 —— 包括 404 与 MIME。
    """
    from agentworkspace import kernel
    from agentworkspace.server import serve

    ws = ROOT / ".tmp" / "e2e_browser_ws"
    ws.mkdir(parents=True, exist_ok=True)
    rt = kernel.Runtime(ws)
    httpd = serve(rt, None, Path(__file__).resolve().parents[1] / "agentworkspace" / "web",
                  port=0)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://127.0.0.1:{port}"


STATE = {
    "programs": [{
        "id": "assistant", "title": "小助理", "description": "造程序的程序",
        "icon": "", "uiKind": "none", "hasUi": False, "dev": False,
    }],
    "native": {},
    "devtools": None,
    "settings": {"url": ""},
    "vcs": {"enabled": False, "reason": "e2e"},
    "agents": {
        "assistant": {
            "status": "open", "error": None, "busy": False, "sid": "s_0000e2e0",
            "history": [
                {"role": "user", "text": USER_TEXT},
                {"role": "assistant", "text": BODY},
            ],
        },
    },
}

# 打桩：记录我们的兜底到底跑没跑（不能只凭「剪贴板里有字」就认定是自己写的）
STUB = """
window.__copies = [];
window.__legacy = 0;
try {
  const oc = navigator.clipboard.writeText.bind(navigator.clipboard);
  navigator.clipboard.writeText = function (t) {
    window.__copies.push(String(t));
    return oc(String(t));
  };
} catch (e) { window.__clipStubErr = String(e); }
const oe = document.execCommand.bind(document);
document.execCommand = function (c) {
  const r = oe(c);
  if (c === 'copy') window.__legacy++;
  return r;
};
"""

INJECT = """() => {
  const s = document.createElement('style');
  s.textContent = 'body{-webkit-user-select:none;cursor:default}';
  document.head.appendChild(s);          // 与 pywebview customize.js 一样：head 末尾
  return true;
}"""

DROP_FIX = """() => {
  let n = 0;
  for (const sh of document.styleSheets) {
    let rules; try { rules = [...sh.cssRules]; } catch (e) { continue; }
    for (let i = rules.length - 1; i >= 0; i--) {
      const r = rules[i];
      if (r.selectorText && r.selectorText.includes('#thread')
          && r.style && (r.style.userSelect || r.style.webkitUserSelect)) {
        sh.deleteRule(i); n++;
      }
    }
  }
  return n;
}"""


def drag_select(page, selector: str) -> None:
    """真鼠标：按下 → 分步移动 → 抬起。分步是必须的，一步跳终点 Chromium
    未必把它当一次拖选。"""
    b = page.locator(selector).first.bounding_box()
    if not b:
        return
    # 起点必须在**第一行**：取垂直中点会落在第二段，选出来的东西不含第一行。
    x1, y1 = b["x"] + 3, b["y"] + 4
    x2, y2 = b["x"] + b["width"] - 3, b["y"] + b["height"] - 2
    page.mouse.move(x1, y1)
    page.mouse.down()
    page.mouse.move(x2, y2, steps=14)
    page.mouse.up()
    page.wait_for_timeout(60)


def open_page(ctx, base: str, fix: bool):
    page = ctx.new_page()
    page.add_init_script(STUB)
    page.route("**/api/state", lambda r: r.fulfill(
        status=200, content_type="application/json", body=json.dumps(STATE)))
    page.route("**/api/p/**", lambda r: r.fulfill(
        status=200, content_type="application/json", body="{}"))
    page.goto(f"{base}/#p/assistant")
    page.wait_for_selector("#thread .msg", timeout=10000)
    page.evaluate(INJECT)
    dropped = page.evaluate(DROP_FIX) if not fix else 0
    page.wait_for_timeout(80)
    return page, dropped


def main() -> int:
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
        print(f"真浏览器：{br.version}\n")
        ctx = br.new_context(viewport={"width": 1180, "height": 760})
        ctx.grant_permissions(["clipboard-read", "clipboard-write"], origin=base)

        print("[A] 病在：注入 pywebview 的 none，且没有页面那条修复规则 → 应该选不中")
        old, dropped = open_page(ctx, base, fix=False)
        check("确实删掉了页面的 user-select 修复规则", dropped >= 1, f"dropped={dropped}")
        check("注入后 #thread 的计算值确实是 none",
              old.evaluate("getComputedStyle(document.getElementById('thread')).webkitUserSelect")
              == "none")
        drag_select(old, "#thread .msg:not(.me) .body")
        got = old.evaluate("String(window.getSelection())")
        check("拖不动（对照组必须红，否则这套测试没打在病上）", got.strip() == "", repr(got))
        old.evaluate("window.getSelection().removeAllRanges()")
        old.keyboard.press("Control+c")
        old.wait_for_timeout(150)
        check("对照组 Ctrl+C 也没东西可复制",
              old.evaluate("navigator.clipboard.readText()").strip() == "")
        old.close()

        print("\n[B] 修在：同一条注入，页面规则还在 → 应该选得中")
        new, _ = open_page(ctx, base, fix=True)
        check("注入后 #thread 的计算值是 text（id 选择器压住了 body）",
              new.evaluate("getComputedStyle(document.getElementById('thread')).webkitUserSelect")
              == "text")
        drag_select(new, "#thread .msg:not(.me) .body")
        sel = new.evaluate("String(window.getSelection())")
        check("真鼠标拖出了文字", bool(sel.strip()), repr(sel))
        check(f"选中的就是要的那段（含 {MARK}）", MARK in sel, repr(sel[:80]))

        print("\n[C] 复制：Ctrl+C → 真剪贴板")
        new.keyboard.press("Control+c")
        new.wait_for_timeout(250)
        clip = new.evaluate("navigator.clipboard.readText()")
        # 行尾不同是**对的**：Windows 剪贴板把换行规范成 CRLF，Selection 给的是 LF。
        # 规范化后比较；原值留在失败信息里，免得这点差异把真问题藏起来。
        norm = lambda t: t.replace("\r\n", "\n").strip()
        check("剪贴板里就是选中的那段", norm(clip) == norm(sel),
              f"clip={clip[:60]!r} sel={sel[:60]!r}")
        copies = new.evaluate("window.__copies")
        check("页面自己的兜底真的跑了（不是「原生碰巧能复制」）", len(copies) >= 1,
              f"__copies={copies!r}")
        check("兜底写进剪贴板的正是选中文字",
              bool(copies) and copies[-1].strip() == sel.strip(),
              repr(copies[-1][:60]) if copies else "no call")

        check("原生复制没有被我们拦掉（剪贴板是 CRLF，兜底写的是 LF）",
              "\r\n" in clip or "\n" not in sel,
              f"clip={clip[:40]!r}")

        print("\n[D] 流式输出不抹选区")
        new.evaluate("""() => {
            const m = S.msgs['assistant'].find(x => x.role === 'it');
            m.text += '\\n\\n新来的一段 delta。';
            paintThread();                        // 模拟一个 delta 到达
        }""")
        still = new.evaluate("String(window.getSelection())")
        check("delta 到达后选区还在", still.strip() == sel.strip(), repr(still[:60]))
        check("那一帧被记成「欠着」（threadDirty）",
              new.evaluate("threadDirty") is True)
        check("DOM 上仍是被保护住的那份（没有白重画）",
              new.evaluate("document.getElementById('thread').innerText").find("新来的一段 delta") < 0)
        new.evaluate("window.getSelection().removeAllRanges()")
        new.wait_for_timeout(80)
        check("松手后欠的帧补上了", new.evaluate("threadDirty") is False)
        check("补画之后新内容到位",
              new.evaluate("document.getElementById('thread').innerText").find("新来的一段 delta") >= 0)

        print("\n[E] 输入框里的 Ctrl+C 不被接管")
        new.fill("#ta", "输入框里的一句话 INPUT_ONLY_777")
        new.focus("#ta")
        new.keyboard.press("Control+a")
        n0 = len(new.evaluate("window.__copies"))
        new.keyboard.press("Control+c")
        new.wait_for_timeout(250)
        ta_clip = new.evaluate("navigator.clipboard.readText()")
        check("剪贴板里是输入框的内容", "INPUT_ONLY_777" in ta_clip, repr(ta_clip[:60]))
        check("页面兜底没有插手输入框", len(new.evaluate("window.__copies")) == n0,
              f"{n0} → {len(new.evaluate('window.__copies'))}")

        print("\n[F] 没被顺手放开的地方")
        check(".dev 开关仍然不可选（没有为了复制把整页放开）",
              new.evaluate("getComputedStyle(document.querySelector('.chat header')).userSelect")
              != "text")
        new.close()
        br.close()

    httpd.shutdown()
    print(f"\n通过 {ok} / 失败 {fail}")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
