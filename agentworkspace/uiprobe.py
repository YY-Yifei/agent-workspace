"""界面**行为**验收：在真窗口里点一下、拖一下，读回 DOM 的事实。

为什么必须有它：`selftest` 只能断言「文件里有这行字」。而
「工具默认是收起的」「拖一下对话真的变宽了」「⚙ 在左下角、点开能改地址」
是**渲染之后**的事实 —— CSS 优先级、事件绑定、重绘时机任何一处错了，静态断言都是绿的。

做法：起一个只开门的宿主（``--serve``），自己开一个 pywebview 窗口指过去，
用 ``evaluate_js`` 读 DOM、点按钮、派发事件 —— 全部是**真浏览器**在算。

**它自带工作台**（``.tmp/qa/ws-probe``）：因为这段验收会真的保存一次设置，
而「保存」是写 ``config.json`` 的动作。在别人真实的工作台里跑，就等于拿测试改用户的数据。

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

# 对话闸门：忙碌 / 停止 / 终态。全部**同步**驱动 —— evaluate_js 不 await，
# 所以不真发请求（那样结果永远拿不到），而是拦下 fetch 看它到底打了哪个地址。
JS_GATE = r"""
(() => {
  /* 注释要写在字符串里面：写到 Python 三引号外面就成了 Python 代码（上一版就这么错的）。 */
  const out = {};
  const pid = S.pid;
  const btn = document.querySelector('#sendb');
  const ta = document.querySelector('#ta');
  const form = document.querySelector('#composer');
  const snap = () => ({label: btn.textContent, cls: btn.className,
                       disabled: !!btn.disabled, ta: ta.value});

  // 0) 起点：空闲，输入框可编辑
  S.phase[pid] = 'idle'; paintComposer(pid); paintThread();
  out.idle = snap();
  out.taDisabled = !!ta.disabled;

  // 1) 按下发送（直接把相位推过去，不真发请求）
  setPhase(pid, 'submit');
  out.sending = snap();

  // 2) 忙的时候按回车、按发送、再按一次发送：都不许提交、不许清空、不许插换行
  ta.value = '这句要留在框里';
  const n0 = msgs(pid).length;
  ta.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', bubbles: true, cancelable: true}));
  form.requestSubmit();
  btn.click();
  out.busy = snap();
  out.busyAdded = msgs(pid).length - n0;      // 0 = 一条都没发出去
  out.taEditable = !ta.readOnly && !ta.disabled;

  // 3) 受理：按钮原地变「停止」（同一个位置，不是多出一个按钮）
  setPhase(pid, 'started');
  out.running = snap();
  out.btnIsSame = document.querySelectorAll('#composer button').length;

  // 4) 点停止 → 前端**真的**打了 /cancel（拦下 fetch 看地址）
  const real = window.fetch;
  const seen = [];
  window.fetch = (u) => { seen.push(String(u));
                          return Promise.resolve({json: () => Promise.resolve(
                            {status: 'cancelling', message: '已请求取消'})}); };
  btn.click();
  out.cancelUrls = seen.slice();
  out.stopping = snap();
  window.fetch = real;

  // 5) 终态 cancelled：已产出的内容保留，尾巴上注明「已停止」
  S.cur[pid] = null;
  const m = push(pid, {role: 'it', text: '已经跑了一半的回答',
                       tools: [{label: '🛠 工具 · x', status: '完成', text: 'ok'}]});
  S.cur[pid] = msgs(pid).indexOf(m);
  on({type: 'turn.completed', pid, data: {status: 'cancelled', text: ''}});
  paintThread();
  out.afterCancel = snap();
  out.keptText = document.querySelector('#thread').textContent.indexOf('已经跑了一半的回答') >= 0;
  const note = document.querySelector('.stopnote');
  out.stopNote = note ? note.textContent : null;

  // 6) 一字未产出的那一轮：不留空气泡，换成一行「已停止」小字
  S.cur[pid] = null;
  curIt(pid);                                  // turn.started 时就会建的那条空气泡
  on({type: 'turn.completed', pid, data: {status: 'cancelled'}});
  paintThread();
  out.emptyBubbles = msgs(pid).filter(
    (x) => x.role === 'it' && !x.text && !(x.tools || []).length).length;
  out.lastText = (msgs(pid)[msgs(pid).length - 1] || {}).text || '';

  /* 6b) 中途才在场的页面（刚打开窗口 / SSE 重连）：没见过 turn.started，
     也没收到过任何 delta —— 终态事件里的 d.text 是这一轮**唯一**的产出。
     不接住，屏幕上就是「跑完了一轮，什么都不显示」，看起来像答案被吞了。 */
  S.cur[pid] = null;
  const nMid = msgs(pid).length;
  on({type: 'turn.completed', pid,
      data: {status: 'completed', text: '中途进来也接得住的那段回答'}});
  paintThread();
  out.midJoinText = document.querySelector('#thread').textContent
    .indexOf('中途进来也接得住的那段回答') >= 0;
  out.midJoinAdded = msgs(pid).length - nMid;

  /* 6c) 删掉一条消息，后面的全前移一位：S.cur 不跟着挪，流式文本就会灌进
     别人的气泡。被拒时正是这个形状 —— 人的话在 i、空气泡在 i+1。 */
  S.cur[pid] = null;
  const mMe = push(pid, {role: 'me', text: '这句要撤回'});
  const mIt = push(pid, {role: 'it', text: '正在写的回答', tools: []});
  S.cur[pid] = msgs(pid).indexOf(mIt);
  dropMsg(pid, mMe);
  out.curAfterDrop = (msgs(pid)[S.cur[pid]] || {}).text || null;

  /* 6d) 一张卡都还挂在「运行中」就被停掉：没有任何操作撤回，
     「已经跑完的操作不会撤回」在那时是**假话**，不许写。 */
  S.cur[pid] = null;
  push(pid, {role: 'it', text: '半句',
             tools: [{label: '🛠 工具 · y', status: '运行中', text: ''}]});
  S.cur[pid] = msgs(pid).length - 1;
  on({type: 'turn.completed', pid, data: {status: 'cancelled'}});
  paintThread();
  const notes = document.querySelectorAll('.stopnote');
  out.stopNoteNoTool = notes.length ? notes[notes.length - 1].textContent : null;

  // 7) 连接断了：闸门必须开回去（否则界面永久「运行中」，只能重启）
  setPhase(pid, 'started');
  on({type: 'link.status', pid, data: {status: 'error', error: 'boom'}});
  out.afterLinkDown = snap();

  // 收尾：回到干净状态，不影响后面的设置那一段
  S.phase[pid] = 'idle'; paintComposer(pid); ta.value = ''; paintThread();
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

# ---------- 自己的小工作台：一个网页程序 + 一份自己的 config.json ----------
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
    """造一个**属于 uiprobe 自己**的工作台（幂等）。

    为什么不用真实工作台：这段验收会保存一次设置 —— 那是在写 config.json。
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

            # 对话闸门：面板关掉之后再跑（遮罩开着时不该假装点得到东西）
            win.evaluate_js("window.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape'}))")
            box["gate"] = win.evaluate_js(JS_GATE)
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

        # -- 对话闸门（忙时禁发 / 按得动停止）------------------------------
        *[{"name": n, "ok": ok, "detail": d} for n, ok, d in _judge_gate(box.get("gate") or {})],
    ]


def _judge_gate(g: dict) -> list[tuple[str, bool, str]]:
    """对话闸门的事实判据。全是渲染之后才成立的东西：按钮文字、disabled、
    输入框里的字、真发出去的请求、气泡里留下什么。"""
    idle = g.get("idle") or {}
    sending = g.get("sending") or {}
    busy = g.get("busy") or {}
    running = g.get("running") or {}
    stopping = g.get("stopping") or {}
    cancel = g.get("afterCancel") or {}
    down = g.get("afterLinkDown") or {}
    return [
        ("空闲时按钮是「发送」且可点",
         idle.get("label") == "发送" and idle.get("disabled") is False, str(idle)),
        ("忙碌时输入框仍可编辑（字是人的东西，收走的只是「现在能送出去」）",
         g.get("taEditable") is True and g.get("taDisabled") is False,
         f"editable={g.get('taEditable')} disabled={g.get('taDisabled')}"),
        ("按下发送后按钮变「发送中…」且不可点",
         sending.get("label") == "发送中…" and sending.get("disabled") is True, str(sending)),
        ("忙时按回车/点发送：一个字都不提交（没多出一条消息）",
         g.get("busyAdded") == 0, f"新增 {g.get('busyAdded')} 条"),
        ("忙时也不清空输入框（被拒的那句不会凭空消失）",
         busy.get("ta") == "这句要留在框里", str(busy.get("ta"))),
        # 原来这里有一条「回车没在框里插进一个换行」——**它永远为真**：
        # dispatchEvent 造出来的 KeyboardEvent 是 untrusted 的，Chromium 不会为它
        # 执行默认的文本插入，`ta.value` 根本不可能多出一个换行，所以这条断言
        # 无论代码怎么写都绿。真正能证伪的是上一条「一个字都不提交」，那条在。
        ("服务端受理后按钮**原地**变「停止」（没多出按钮）",
         running.get("label") == "停止" and running.get("disabled") is False
         and g.get("btnIsSame") == 1, f"{running} / 按钮数={g.get('btnIsSame')}"),
        ("停止键是红的（.stop）", "stop" in str(running.get("cls")), str(running.get("cls"))),
        ("点停止 → 前端真的打了 /cancel（原来全文件调用过 0 次）",
         any("/cancel" in u for u in (g.get("cancelUrls") or [])), str(g.get("cancelUrls"))),
        ("正在停止时按钮不可点（防连点）",
         stopping.get("label") == "正在停止…" and stopping.get("disabled") is True, str(stopping)),
        ("被停止的那一轮：已产出的内容**保留**（删掉等于撒谎）",
         g.get("keptText") is True and cancel.get("label") == "发送", str(cancel)),
        ("尾巴上注明「已停止」，并说清操作不会撤回",
         "已停止" in str(g.get("stopNote")) and "不会撤回" in str(g.get("stopNote")),
         str(g.get("stopNote"))),
        ("一字未产出的那一轮不留空气泡（否则像「答了但内容丢了」）",
         g.get("emptyBubbles") == 0, f"空气泡 {g.get('emptyBubbles')} 个"),
        ("一字未产出时换成一行「已停止」小字", g.get("lastText") == "已停止",
         str(g.get("lastText"))),
        ("连接断了自动解锁（否则界面永久「运行中」，只能重启）",
         down.get("label") == "发送" and down.get("disabled") is False, str(down)),
        # ocr 第一轮评审补的三条（每条都是一个「屏幕上看不见但确实丢东西」的形状）
        ("中途进页面（没见过 turn.started）也接住终态里的那段回答",
         g.get("midJoinText") is True and g.get("midJoinAdded") == 1,
         f"接住={g.get('midJoinText')} 新增={g.get('midJoinAdded')} 条"),
        ("删掉一条消息后，流式写入仍指向原来那一条（索引要跟着挪）",
         g.get("curAfterDrop") == "正在写的回答", str(g.get("curAfterDrop"))),
        ("一张卡都没跑完就被停：不写「已经跑完的操作不会撤回」",
         str(g.get("stopNoteNoTool") or "").strip() == "已停止", str(g.get("stopNoteNoTool"))),
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
