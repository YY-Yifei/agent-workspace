"""界面**行为**验收：在真窗口里点一下、拖一下，读回 DOM 的事实。

为什么必须有它：`selftest` 只能断言「文件里有这行字」。而
「工具默认是收起的」「拖一下对话真的变宽了」「⚙ 在左下角、点开能改地址」
是**渲染之后**的事实 —— CSS 优先级、事件绑定、重绘时机任何一处错了，静态断言都是绿的。

做法：起一个只开门的宿主（``--serve``），自己开一个 pywebview 窗口指过去，
用 ``evaluate_js`` 读 DOM、点按钮、派发事件 —— 全部是**真浏览器**在算。

**它自带工作区**（``.tmp/qa/ws-probe``）：因为这段验收会真的保存一次设置，
而「保存」是写 ``config.json`` 的动作。在别人真实的工作区里跑，就等于拿测试改用户的数据。

用法：
    python -m agentworkspace.uiprobe            # 会自动重投到交互式会话（Session 0 里跑不了）
    python -m agentworkspace.uiprobe --no-relaunch
"""

from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

from . import qa

# 全部同步：pywebview 的 evaluate_js 不 await Promise，异步会让结果永远拿不到。
# 所以「保存」这种带 fetch 的动作拆成三步：派发 → 由 Python 侧轮询结果 → 再读事实。

JS_MAIN = r"""
(() => {
  const out = {steps: []};
  const g = () => document.querySelector('#gutter');
  const chat = () => document.querySelector('.chat');
  const grp = () => document.querySelector('.tgroup');

  // 1) 两栏：有页面的程序 = 左页面 + 右对话，中间有抓手
  out.pid = S.pid;
  out.hasPane = !!document.querySelector('.pane');
  out.winW = window.innerWidth;
  out.docW = document.documentElement.scrollWidth;
  out.hasGutter = !!g();

  // 2) 拖一下：往左 120px → 对话变宽 120
  if (g()) {
    const w0 = Math.round(chat().getBoundingClientRect().width);
    const x0 = g().getBoundingClientRect().left + 3;
    const pe = (t, x) => g().dispatchEvent(new PointerEvent(t, {
      clientX: x, clientY: 300, pointerId: 1, bubbles: true, cancelable: true }));
    pe('pointerdown', x0); pe('pointermove', x0 - 120); pe('pointerup', x0 - 120);
    const w1 = Math.round(chat().getBoundingClientRect().width);
    out.drag = {before: w0, after: w1, delta: w1 - w0,
                saved: localStorage.getItem('ws.chatw.' + S.pid)};

    pe('pointerdown', x0); pe('pointermove', x0 + 5000); pe('pointerup', x0);
    out.dragMin = Math.round(chat().getBoundingClientRect().width);

    g().dispatchEvent(new MouseEvent('dblclick', {bubbles: true}));
    out.afterReset = Math.round(chat().getBoundingClientRect().width);
    out.chatRight = Math.round(chat().getBoundingClientRect().right);
  }

  // 3) 工具调用：默认折叠（一行摘要，正文不占地方）
  push(S.pid, {role: 'it', text: '', tools: [
    {label: '🛠 开发者 · read', status: '完成', text: '{"title":"样例程序"}'},
    {label: '🛠 开发者 · write', status: '完成', text: 'ok'}]});
  paintThread();
  out.rows = grp().querySelectorAll('.tool').length;
  out.groupOpen = grp().classList.contains('open');
  out.listDisplay = getComputedStyle(grp().querySelector('.tlist')).display;
  out.rowDisplay = getComputedStyle(document.querySelector('.tool .r')).display;
  out.summary = grp().querySelector('.tgh').textContent.replace(/\s+/g, ' ').trim();

  grp().querySelector('.tgh').click();
  out.groupOpenAfterClick = grp().classList.contains('open');
  out.listDisplayAfter = getComputedStyle(grp().querySelector('.tlist')).display;
  out.rowDisplayAfter = getComputedStyle(document.querySelector('.tool .r')).display;

  document.querySelector('.tool .th').click();
  out.toolOpen = document.querySelector('.tool').classList.contains('open');
  out.rowDisplayTool = getComputedStyle(document.querySelector('.tool .r')).display;

  paintThread();
  out.toolStillOpen = document.querySelector('.tool').classList.contains('open');

  // 4) 设置入口：在**左下角**，而且它不是程序
  const rail = document.querySelector('#rail');
  const setb = document.querySelector('#setbtn');
  out.gear = (() => {
    if (!setb) return null;
    const r = setb.getBoundingClientRect(), rr = rail.getBoundingClientRect();
    return {inRail: rail.contains(setb), isProgram: !!setb.dataset.pid,
            left: Math.round(r.left), bottom: Math.round(r.bottom),
            railBottom: Math.round(rr.bottom), railW: Math.round(rr.width),
            progCount: rail.querySelectorAll('.rbtn[data-pid]').length};
  })();

  // 5) 点它 → 面板打开，字段**预填当前地址**（不是空的）
  setb.click();
  const mask = () => document.querySelector('#mask');
  out.maskOpen = mask().classList.contains('on');
  out.maskDisplay = getComputedStyle(mask()).display;
  out.prefill = document.querySelector('#aaasurl').value;
  out.stateUrl = S.settings.url;
  out.focusOk = (document.activeElement || {}).id === 'aaasurl';
  return out;
})()
"""

JS_BAD = r"""
(() => {
  document.querySelector('#aaasurl').value = 'ftp://nope/';
  document.querySelector('#setform').requestSubmit();
  return true;
})()
"""

JS_GOOD = r"""
(() => {
  document.querySelector('#aaasurl').value = ' ws://127.0.0.1:19999/ ';
  document.querySelector('#setform').requestSubmit();
  return true;
})()
"""

# ---------- 自己的小工作区：一个网页程序 + 一份自己的 config.json ----------
PLUGIN_JSON = {"title": "样例程序", "icon": "🧪", "description": "界面验收用的样例"}
BACKEND = '''"""界面验收用的样例程序：一个函数就够。"""


def ping(times: int = 1) -> str:
    """回一声（界面验收用）。"""
    return "pong" * max(1, min(int(times), 3))
'''
PAGE = """<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<title>样例程序</title>
<body style="font:14px system-ui;padding:24px">
<div id="probe-ui">样例程序的页面</div></body></html>
"""
START_URL = "ws://127.0.0.1:9/"          # 故意连不上：真跑时它就是连不上
NEW_URL = "ws://127.0.0.1:19999/"


def workspace() -> Path:
    """造一个**属于 uiprobe 自己**的工作区（幂等）。

    为什么不用真实工作区：这段验收会保存一次设置 —— 那是在写 config.json。
    测试改用户的配置，是最不该发生的那种「悄悄发生」。
    """
    ws = qa.OUT / "ws-probe"
    pdir = ws / "plugins" / "probe"
    (pdir / "ui").mkdir(parents=True, exist_ok=True)
    (pdir / "plugin.json").write_text(json.dumps(PLUGIN_JSON, ensure_ascii=False, indent=2),
                                      encoding="utf-8")
    (pdir / "backend.py").write_text(BACKEND, encoding="utf-8")
    (pdir / "ui" / "index.html").write_text(PAGE, encoding="utf-8")
    (ws / "config.json").write_text(json.dumps({
        "aaas": {"url": START_URL, "token": None},
        "agent": {"revision": "probe-v1"},
        "server": {"port": 8777},
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    return ws


def _wait(win, expr: str, timeout: float = 8.0) -> bool:
    """轮询一个同步表达式为真 —— fetch 的结果只能这样等（evaluate_js 不 await）。"""
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            if win.evaluate_js(expr):
                return True
        except Exception:
            pass
        time.sleep(0.15)
    return False


def _probe(port: int) -> dict:
    """开一个真窗口，把这几步跑一遍，返回它读到的事实。"""
    import webview

    url = f"http://127.0.0.1:{port}/#p/probe"
    win = webview.create_window("界面行为验收", url, width=1180, height=800)
    box: dict = {}

    def work() -> None:
        try:
            ready = _wait(win, "!!document.querySelector('#setbtn') && "
                               "!!document.querySelector('#stage .split')", 30)
            if not ready:
                box["error"] = "30 秒内页面没画出左边栏（含设置入口）与舞台"
                return
            box["result"] = win.evaluate_js(JS_MAIN)

            # 坏地址：必须当场报错，且不改任何东西
            win.evaluate_js(JS_BAD)
            box["badShown"] = _wait(
                win, "document.querySelector('#setmsg').className.indexOf('bad') >= 0", 6)
            box["badText"] = win.evaluate_js("document.querySelector('#setmsg').textContent")
            box["urlAfterBad"] = win.evaluate_js("S.settings.url")

            # 好地址：保存 + 重连
            win.evaluate_js(JS_GOOD)
            box["savedShown"] = _wait(
                win, "document.querySelector('#setmsg').className.indexOf('ok') >= 0", 10)
            box["savedText"] = win.evaluate_js("document.querySelector('#setmsg').textContent")
            box["statusText"] = win.evaluate_js("document.querySelector('#setstat').textContent")
            box["stateUrlAfter"] = win.evaluate_js("S.settings.url")

            # 关掉：Escape；再打开一次，字段应是**新**地址
            win.evaluate_js("window.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape'}))")
            box["closedByEsc"] = win.evaluate_js(
                "!document.querySelector('#mask').classList.contains('on')")
            win.evaluate_js("document.querySelector('#setbtn').click()")
            box["prefillAgain"] = win.evaluate_js("document.querySelector('#aaasurl').value")
        except Exception as exc:
            box["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            try:
                win.destroy()
            except Exception:
                pass

    def on_loaded() -> None:
        # loaded 事件是在 JS 线程上派发的：就地 evaluate_js 会等自己 → 死锁。
        threading.Thread(target=work, daemon=True).start()

    win.events.loaded += on_loaded
    webview.start()                            # 关掉窗口才返回
    return box


def _judge(r: dict, box: dict, disk: dict, api: dict) -> list[dict]:
    drag = r.get("drag") or {}
    gear = r.get("gear") or {}
    return [
        {"name": "有页面的程序是两栏，中缝有抓手",
         "ok": bool(r.get("hasPane") and r.get("hasGutter"))},
        {"name": "往左拖 120px，对话真的变宽 120px", "ok": drag.get("delta") == 120,
         "detail": str(drag)},
        {"name": "宽度记住了（localStorage，按程序分开）", "ok": str(drag.get("saved")) == "500",
         "detail": str(drag.get("saved"))},
        {"name": "拖过头被夹住（不许窄到读不了）", "ok": r.get("dragMin") == 300,
         "detail": str(r.get("dragMin"))},
        {"name": "双击复位到 380", "ok": r.get("afterReset") == 380,
         "detail": str(r.get("afterReset"))},
        {"name": "页面没有横向溢出（没有东西被挤出窗口）",
         "ok": (r.get("docW") or 0) <= (r.get("winW") or 0)
               and (r.get("chatRight") or 0) <= (r.get("winW") or 0),
         "detail": f"docW={r.get('docW')} winW={r.get('winW')} chatRight={r.get('chatRight')}"},
        {"name": "工具调用默认收起：整组没展开、列表不占地方",
         "ok": r.get("groupOpen") is False and r.get("listDisplay") == "none",
         "detail": f"{r.get('groupOpen')} / {r.get('listDisplay')}"},
        {"name": "收起时只留一行摘要（N 次工具调用）",
         "ok": "2 次工具调用" in str(r.get("summary")), "detail": str(r.get("summary"))},
        {"name": "收起时正文不占地方（display:none）", "ok": r.get("rowDisplay") == "none",
         "detail": str(r.get("rowDisplay"))},
        {"name": "点一下整组 → 列出每一次调用",
         "ok": r.get("groupOpenAfterClick") is True and r.get("listDisplayAfter") == "block",
         "detail": f"{r.get('groupOpenAfterClick')} / {r.get('listDisplayAfter')}"},
        {"name": "展开组之后，每一项的正文仍然收着（两段式，别一次全摊）",
         "ok": r.get("rowDisplayAfter") == "none", "detail": str(r.get("rowDisplayAfter"))},
        {"name": "再点某一项 → 那一项的正文才露出来",
         "ok": r.get("toolOpen") is True and r.get("rowDisplayTool") == "block",
         "detail": f"{r.get('toolOpen')} / {r.get('rowDisplayTool')}"},
        {"name": "重绘不丢：整段重画之后，用户点开的那一项还开着",
         "ok": r.get("toolStillOpen") is True},
        {"name": "两项都在（没有把别的调用弄丢）", "ok": r.get("rows") == 2,
         "detail": str(r.get("rows"))},

        # -- 设置入口 --------------------------------------------------
        {"name": "左栏里有 ⚙，而且它在**左下角**（贴着栏底）",
         "ok": bool(gear) and gear.get("inRail") and gear.get("bottom", 0)
               >= gear.get("railBottom", 0) - 60 and gear.get("left", 99) < gear.get("railW", 0),
         "detail": str(gear)},
        {"name": "⚙ 不是程序（没有 pid，不混进程序那一列）",
         "ok": bool(gear) and gear.get("isProgram") is False,
         "detail": str(gear.get("isProgram"))},
        {"name": "点 ⚙ → 设置面板真的打开（display 不是 none）",
         "ok": r.get("maskOpen") is True and r.get("maskDisplay") == "grid",
         "detail": f"{r.get('maskOpen')} / {r.get('maskDisplay')}"},
        {"name": "面板里只有一个地址字段，且预填当前值",
         "ok": r.get("prefill") == r.get("stateUrl") == START_URL,
         "detail": f"{r.get('prefill')} / {r.get('stateUrl')}"},
        {"name": "打开时焦点就在那个字段上",
         "ok": r.get("focusOk") is True},
        {"name": "坏地址当场报错（不是静悄悄吞掉）",
         "ok": box.get("badShown") is True and "只支持" in str(box.get("badText")),
         "detail": str(box.get("badText"))},
        {"name": "坏地址没有改动任何东西（界面里的地址还是旧的）",
         "ok": box.get("urlAfterBad") == START_URL, "detail": str(box.get("urlAfterBad"))},
        {"name": "好地址保存成功，并说清了「在重连」",
         "ok": box.get("savedShown") is True and "重连" in str(box.get("savedText")),
         "detail": str(box.get("savedText"))},
        {"name": "地址真的落盘了（config.json）", "ok": disk.get("url") == NEW_URL,
         "detail": str(disk.get("url"))},
        {"name": "GET /api/settings 与落盘一致", "ok": api.get("url") == NEW_URL,
         "detail": str(api.get("url"))},
        {"name": "界面里的地址跟着变了（不用刷新）",
         "ok": box.get("stateUrlAfter") == NEW_URL, "detail": str(box.get("stateUrlAfter"))},
        {"name": "再打开面板，预填的是**新**地址",
         "ok": box.get("prefillAgain") == NEW_URL, "detail": str(box.get("prefillAgain"))},
        {"name": "连不上时状态行如实说（不是假装已连上）",
         "ok": "连不上" in str(box.get("statusText")) or "正在连接" in str(box.get("statusText")),
         "detail": str(box.get("statusText"))},
        {"name": "Escape 能关掉面板",
         "ok": box.get("closedByEsc") is True},
    ]


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if "--no-relaunch" not in args:
        code = qa.relaunch_in_active_session(
            [sys.executable, "-X", "utf8", "-m", "agentworkspace.uiprobe", *args, "--no-relaunch"])
        if code is not None:
            return code

    name, visible = qa.station()
    print(f"[界面行为] 窗口站 {name}（可见={visible}）", flush=True)

    ws = workspace()
    host = qa.Host(["--serve", "--open", "probe", "--workspace", str(ws)])
    try:
        box = _probe(host.port)
        disk = json.loads((ws / "config.json").read_text(encoding="utf-8"))
        api = qa._get(host.port, "/api/settings") or {}
    finally:
        host.close()

    if box.get("error"):
        print(f"[界面行为] 没跑成：{box['error']}", flush=True)
        return 1

    r = box.get("result") or {}
    checks = _judge(r, box, disk.get("aaas") or {}, api or {})
    bad = 0
    for c in checks:
        if c["ok"]:
            print(f"  ok  {c['name']}", flush=True)
        else:
            bad += 1
            print(f"  !!  {c['name']}   {c.get('detail', '')}", flush=True)

    qa.OUT.mkdir(parents=True, exist_ok=True)
    (qa.OUT / "ui-probe.json").write_text(
        json.dumps({"checks": checks, "raw": r, "box": box, "disk": disk, "api": api},
                   ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n{'全绿' if bad == 0 else '有失败'}：{len(checks) - bad} 项过 / {bad} 项不过；"
          f"原始数据 {qa.OUT / 'ui-probe.json'}", flush=True)
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
