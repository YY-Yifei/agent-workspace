"""入口 —— 装配三样东西，没有第四样。

    内核（kernel.Runtime）  一：目录约定 / 唯一 dispatch / 事件流 / 能力面
    智能体池（link.Hub）    道：每个程序一条 aaas 会话，用到才连
    人的门（server）        本地 HTTP + SSE

用法::

    python -m agentworkspace.main                       # 起桌面窗口（pywebview）
    python -m agentworkspace.main --browser             # 换成浏览器（pywebview 起不来时的形态）
    python -m agentworkspace.main --cli                 # 命令行：跟小助理说一句
    python -m agentworkspace.main --cli -m "做个番茄钟"    # 同上
    python -m agentworkspace.main --cli --as pomodoro -m "开始 25 分钟"   # 跟某个程序自己的助手说

``--cli`` 是验收形式：connect → declare → sendInput → invokeFunction →
本地执行 → functionResult → 模型续写 → turn.completed。这条不通，界面做得再漂亮
也是空壳；通了，后面每一步都有东西可验。
"""

from __future__ import annotations

import argparse
import json
import queue
import sys
import threading
import time
import webbrowser
from pathlib import Path

from . import kernel, settings, shell
from .link import ASSISTANT, Hub
from .server import App, serve

ROOT = Path(__file__).resolve().parent
try:                                   # 控制台多半是 GBK：不让一行日志带走一整轮
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
DEFAULT_WORKSPACE = ROOT.parent / "workspace"
ICON = ROOT / "assets" / "icon.ico"          # 窗口图标：我们自己画的，不是 python.exe 那个


def _load_cfg(ws: Path) -> dict:
    try:
        return json.loads((ws / "config.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def build(ws: Path) -> tuple[kernel.Runtime, Hub]:
    """装配：内核 + 内置的小助理 + 智能体池。没有第四样东西。"""
    rt = kernel.Runtime(ws)
    from . import assistant
    mounted = assistant.install(rt)          # 内核只内置一个程序：小助理（带手）
    msg = rt.scan()
    # 地址只从 settings 翻译一次（设置面板改的也是这一份）—— 两处各读一遍就会漂
    hub = Hub(rt, ws, settings.hub_cfg(ws))
    print(f"[内核] {mounted}；{msg}", flush=True)
    print(f"[道] {hub.url}", flush=True)
    return rt, hub


# ------------------------------------------------------------------ 命令行
def cli(ws: Path, message: str | None, pid: str = ASSISTANT) -> int:
    rt, hub = build(ws)
    caps = rt.capabilities_of(pid)
    print(f"[程序] {pid}｜能力 {len(caps)} 项：{sorted(caps)}", flush=True)
    link = hub.link(pid, create=True)
    if link is None:
        print(f"[程序] 没有 {pid} 这个程序", flush=True)
        return 1

    q = rt.subscribe()
    deadline = time.time() + 30
    while time.time() < deadline and link.status not in ("ready", "error"):
        _drain(q, quiet=True)
        time.sleep(0.05)
    if link.status == "error":
        print(f"[道] 连不上：{link.error}", flush=True)
        print("     → 检查 workspace/config.json 里的 aaas.url，或先启动 aaas-service", flush=True)
        return 1
    print(f"[道] 已连接 session={link.session_id}", flush=True)

    text = message
    if text is None:
        if not sys.stdin.isatty():
            text = sys.stdin.read().strip()
        else:
            text = input("[你] ").strip()
    if not text:
        print("没有输入", flush=True)
        return 2

    print(f"[你] {text}", flush=True)
    hub.send(pid, text)
    done = False
    while True:
        try:
            # 收轮之后给一小段静默窗口：接力轮（能力面变了）会紧接着开，
            # 立刻退出就会把「模型用上自己刚造的东西」这一幕漏掉。
            ev = q.get(timeout=1.2 if done else 300)
        except queue.Empty:
            if done:
                break
            print("[超时] 300 秒没有事件", flush=True)
            return 3
        _print_event(ev)
        if ev["type"] == "turn.started":
            done = False
        if ev["type"] == "turn.completed":
            done = True
    hub.stop()
    return 0


def _drain(q: queue.Queue, quiet: bool = False) -> None:
    while True:
        try:
            ev = q.get_nowait()
        except queue.Empty:
            return
        if not quiet:
            _print_event(ev)


def _print_event(ev: dict) -> None:
    t = ev["type"]
    d = ev.get("data") or {}
    who = ev.get("pid") or ""
    tag = f"{who}" if who else ""
    if t == "message.delta":
        sys.stdout.write(d.get("text") or "")
        sys.stdout.flush()
        return
    if t == "reasoning.delta":
        return
    label = {
        "link.status": "道", "link.declared": "声明", "plugin.loaded": "程序",
        "turn.started": "开轮", "turn.handoff": "接力", "call.started": "点名",
        "call.ready": "调用", "call.result": "执行", "call.ok": "内核", "call.rejected": "拒绝",
        "call.error": "出错", "turn.completed": "收轮", "turn.failed": "失败",
    }.get(t, t)
    if t == "call.ready":
        print(f"\n[{label}] {tag} {d.get('name')}({json.dumps(d.get('args'), ensure_ascii=False)})",
              flush=True)
    elif t == "call.ok":
        print(f"[{label}] {ev.get('source')} → {d.get('text')}", flush=True)
    elif t == "call.result":
        print(f"[{label}] status={d.get('status')} {d.get('text')}", flush=True)
    elif t == "turn.completed":
        usage = d.get("usage") or {}
        print(f"\n[{label}] {tag} status={d.get('status')} finish={d.get('finishReason')} "
              f"usage={usage.get('totalTokens', usage)}", flush=True)
    elif t == "link.declared":
        print(f"[{label}] {tag} revision={d.get('revision')} functions={d.get('functions')}",
              flush=True)
    else:
        print(f"[{label}] {tag} {json.dumps(d, ensure_ascii=False)[:300]}", flush=True)


# ------------------------------------------------------------------ 带界面
def gui(ws: Path, port: int, open_browser: bool, browser: bool = False,
        size: tuple[int, int] = (1180, 800), open_pid: str | None = None,
        serve_only: bool = False) -> int:
    """起界面。外壳只是一个窗口，里面永远是同一个本地 URL。

    ``browser=False`` 时先试 pywebview（真桌面窗口）；起不来就**如实说**，
    然后回退浏览器 —— 一个壳起不来，不该让整个宿主起不来。
    """
    rt, hub = build(ws)
    try:
        httpd = serve(rt, hub, ROOT / "web", port=port)
    except OSError as exc:                  # 端口被占：说人话，别丢一个 traceback
        print(f"[门] 起不来：{port} 端口被占用（{exc}）", flush=True)
        print("     → 也许已经有一个 Agent Workspace 在跑；换一个端口：--port 8778", flush=True)
        hub.stop()
        return 4
    url = f"http://127.0.0.1:{httpd.server_address[1]}/"
    if open_pid:                       # 深链接：直接打开某个程序（验收/复现用）
        url += f"#p/{open_pid}"
    print(f"[门] {url}", flush=True)
    threading.Thread(target=httpd.serve_forever, name="http", daemon=True).start()

    print("[就绪] 关掉窗口即退出", flush=True)
    code = 0
    try:
        if serve_only:                     # 只起门：留给自动验收，或让别的进程接壳
            print("[就绪] 只起门，不开窗口（Ctrl+C 结束）", flush=True)
            _wait_forever()
        else:
            status = "unavailable" if browser else shell.run(
                url, width=size[0], height=size[1], storage=ws / ".shell", icon=ICON)
            # 三种结局必须区别对待：把"窗口开着但白屏"当成成功，是最坏的一种谎
            if status == "ok":
                print("[壳] 桌面窗口已关闭", flush=True)
            elif status == "blank":
                print("[壳] 窗口开了但页面没渲染出来（WebView2 初始化失败 / 无交互桌面）", flush=True)
                print("     → 回退浏览器；要修桌面窗口，检查 WebView2 Runtime 与图形会话", flush=True)
                webbrowser.open(url)
                _wait_forever()
            else:
                print("[壳] pywebview 不可用 → 回退浏览器", flush=True)
                webbrowser.open(url)
                _wait_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if rt_host := getattr(App, "native", None):
            rt_host.shutdown()                 # 收尸：不留孤儿原生窗口
        hub.stop()
        httpd.shutdown()
    return code


def _wait_forever() -> None:
    while True:
        time.sleep(1)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="agentworkspace.main", description="Agent Workspace")
    ap.add_argument("--workspace", default=str(DEFAULT_WORKSPACE))
    # None = 读 config.json；0 = 随机空闲端口（验收用它，绝不和别的宿主抢）
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--cli", action="store_true", help="不开界面，跑一条真链路")
    ap.add_argument("-m", "--message", default=None)
    ap.add_argument("--as", dest="pid", default=ASSISTANT,
                    help="跟哪个程序的助手说话（默认 assistant）")
    ap.add_argument("--open", dest="open_pid", default=None,
                    help="直接打开某个程序（pid）")
    ap.add_argument("--browser", action="store_true", help="用浏览器当外壳，不开桌面窗口")
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--serve", action="store_true",
                    help="只起门、不开任何窗口（自动验收 / 自己接壳时用）")
    a = ap.parse_args(argv)
    ws = Path(a.workspace).resolve()
    if not ws.is_dir():
        print(f"工作区不存在：{ws}", flush=True)
        return 2
    if a.cli:
        return cli(ws, a.message, a.pid)
    cfg = _load_cfg(ws)
    port = a.port if a.port is not None else cfg.get("server", {}).get("port", 8777)
    return gui(ws, port,
               not a.no_browser and cfg.get("server", {}).get("openBrowser", True),
               browser=a.browser, open_pid=a.open_pid, serve_only=a.serve)


if __name__ == "__main__":
    raise SystemExit(main())
