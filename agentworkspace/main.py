"""入口 —— 装配三样东西，没有第四样。

    内核（kernel.Runtime）  一：目录约定 / 唯一 dispatch / 事件流 / 能力面
    智能体池（link.Hub）    道：每个程序一条 aaas 会话，用到才连
    人的门（server）        本地 HTTP + SSE

用法::

    python -m agentworkspace.main                       # 起桌面窗口（pywebview）
    # 工作区默认就是**你敲命令时所在的目录**；--workspace 可换，不存在会自动新建
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
# 默认工作区 = **启动时的当前目录**。
# 原来这里是 ROOT.parent / "workspace" —— 那是「从源码仓库里跑」的形状。
# 一旦装进 site-packages，它就等于 site-packages/workspace：一个谁都没有的地方，
# 对 PyPI 用户永远是错的默认值。而当前目录是「我现在要做的事」。
# 求值故意推迟到 main()：常量会在 import 那一刻把 cwd 冻住，而真正的启动目录
# 可能完全不同（编辑器、快捷方式、别的脚本)。
ICON = ROOT / "assets" / "icon.ico"          # 窗口图标：我们自己画的，不是 python.exe 那个


def _load_cfg(ws: Path) -> dict:
    """读工作区设置；**形状不对就当没有**。

    默认工作区可以是任意启动目录，所以这个文件可能根本不是我们的：解析成数组、
    ``"server"`` 是 null / 字符串 / 列表，都见过。这里不猜，一律降级 —— 让调用方
    永远能 ``cfg.get("server", {}).get(...)`` 而不炸。
    （settings.load() 也读这份文件，它的 isinstance 护栏必须与这里一致。）
    """
    try:
        cfg = json.loads((ws / "config.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(cfg, dict):
        return {}
    if "server" in cfg and not isinstance(cfg["server"], dict):
        cfg = {k: v for k, v in cfg.items() if k != "server"}
    return cfg


def _executable_plugins(ws: Path) -> list[str]:
    """启动时**真的会被执行**的程序名 —— 给「默认工作区 = 当前目录」那条提示用。

    判据必须与内核一致，宁可少说也不许多数：``Runtime.scan()`` 跳过 ``_`` / ``.``
    开头的目录；``Runtime._load()`` 还要求 ``plugin.json`` 能解析、``backend.py``
    存在，否则它只把 error 记在 Plugin 上，一行代码都不 exec。
    这是「我加载了哪些代码」的告知，不是装饰 —— 判据漂一毫米，它就是假话。
    """
    out: list[str] = []
    pdir = ws / "plugins"
    try:
        cands = [d for d in pdir.iterdir()
                 if d.is_dir() and not d.name.startswith(("_", "."))]
    except OSError:                      # 没有这个目录 / 没权限读：那就是一个都没有
        return out
    for d in sorted(cands, key=lambda p: p.name):
        try:
            if not (d / "backend.py").is_file():
                continue
            json.loads((d / "plugin.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):    # 与 _load() 一样：读不成就不执行
            continue
        out.append(d.name)
    return out


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
        hub.stop()                          # 早退也要收尸：否则 aaas 会话与线程都留着
        return 1

    q = rt.subscribe()
    deadline = time.time() + 30
    while time.time() < deadline and link.status not in ("ready", "error"):
        _drain(q, quiet=True)
        time.sleep(0.05)
    if link.status != "ready":
        # 超时也要算失败：idle / connecting 都不是「连上了」。原来只判 error，
        # 于是 30 秒没连上照样打印「已连接 session=None」，再发一句必然发不出去的话。
        why = link.error if link.status == "error" else f"30 秒内没连上（现在是 {link.status}）"
        print(f"[道] 连不上：{why}", flush=True)
        print("     → 检查 workspace/config.json 里的 aaas.url，或先启动 aaas-service", flush=True)
        hub.stop()
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
        hub.stop()
        return 2

    print(f"[你] {text}", flush=True)
    # send 的返回值是状态字符串：成功 "sent"，否则是一句人话原因。
    # 丢掉它的话，一次失败的发送只会变成 300 秒后的「超时」—— 原因全被抹平。
    sent = hub.send(pid, text)
    if sent != "sent":
        print(f"[道] 没送出去：{sent}", flush=True)
        hub.stop()
        return 4
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
            hub.stop()
            return 3
        _print_event(ev)
        if ev["type"] == "turn.started":
            done = False
        if ev["type"] == "turn.failed":
            # 失败也是终态：不认它，CLI 会接着干等 300 秒，然后报一个假的「超时」
            print("[轮] 这一轮失败了 —— 原因见上一条", flush=True)
            hub.stop()
            return 5
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
            # 四种结局必须区别对待：
            #   ok / blank / unavailable 都是「试过壳」的结果，
            #   browser 是「人明确不要壳」—— 此时说「pywebview 不可用」就是第二种谎，
            #   因为根本没人去试。所以它单独成一个态，不借道 unavailable。
            status = "browser" if browser else shell.run(
                url, width=size[0], height=size[1], storage=ws / ".shell", icon=ICON)
            if status == "browser":
                print("[壳] --browser：用浏览器打开（没有尝试桌面窗口）", flush=True)
                webbrowser.open(url)
                _wait_forever()
            elif status == "ok":
                print("[壳] 桌面窗口已关闭", flush=True)
            elif status == "blank":
                print("[壳] 窗口开了但页面没渲染出来（WebView2 初始化失败 / 无交互桌面）", flush=True)
                print("     → 回退浏览器；要修桌面窗口，检查 WebView2 Runtime 与图形会话", flush=True)
                webbrowser.open(url)
                _wait_forever()
            else:
                print("[壳] pywebview 不可用 → 回退浏览器", flush=True)
                # 走到这里一定是「试过壳、壳起不来」。两种因用 shell.available()
                # 分开（它只探 import），不靠猜：
                if shell.available():            # import 通 = 装上了，只是起不来
                    # available() 只证明 import 通了；run() 里任何异常都会返回
                    # unavailable。所以这里给的是「最常见原因」，不是结论。
                    print("     → pywebview 能 import，但窗口没起来。最常见是缺 GUI 后端"
                          "（Windows 缺 WebView2 Runtime；Linux/macOS 缺 GTK/Qt），"
                          "也可能是建窗口那一步就失败了",
                          flush=True)
                else:                            # import 不通 = 装坏了
                    print("     → pywebview 是默认依赖，却 import 不到，"
                          "说明装坏了：pip install --force-reinstall open-agent-workspace", flush=True)
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
    ap.add_argument("--workspace", default=None, metavar="DIR",
                    help="工作区目录（默认：启动时的当前目录；不存在则新建）")
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
    try:                                 # cwd 也可能取不到：启动目录被删、网络盘断开
        ws = Path(a.workspace).expanduser() if a.workspace else Path.cwd()
        ws = ws.resolve()
    except (OSError, RuntimeError, KeyError) as exc:
        # 除了 OSError（目录被删 / 网络盘断开），expanduser 在 POSIX 上还会抛
        # RuntimeError（`~nosuchuser`）与 KeyError（$HOME 未设且 uid 查不到）。
        # 三个都收：抛上去就是一个裸 traceback，而这行代码的存在意义正是不抛。
        print(f"[工作区] 定位不到启动目录：{exc}", flush=True)
        print("     → 用 --workspace 指定一个确定存在的路径", flush=True)
        return 2
    if not ws.is_dir():                  # 工作区就是一个目录：没有就建，
        print(f"[工作区] 新建：{ws}", flush=True)   # 别让人先 mkdir 再启动
        try:
            ws.mkdir(parents=True, exist_ok=True)
        except OSError as exc:           # 没权限 / 路径中段是个同名文件
            print(f"[工作区] 建不出来：{ws}（{exc}）", flush=True)
            return 2
    if not a.workspace:                  # 与上面「用真值挑工作区」同一个判据
        # 默认 = 启动时的当前目录，等于把「我在哪个目录」当成信任边界。
        # 这件事必须说出来：build() 里的 scan() 会 exec 这里每一个
        # plugins/*/backend.py，dev.json 也从同一个根读。别让它悄悄成立。
        found = _executable_plugins(ws)
        # 同一个信任前提还包括「读写」：config.json 里的 server.port / openBrowser
        # 会被采用，而 /api/settings 保存时是 os.replace 覆盖它（settings.load() 对
        # 解析失败的 JSON 返回 {} —— 也就是说，一份不是我们格式的 config.json，
        # 会在人点一次「保存设置」时被整份抹掉）。
        # 这不是本轮新增的行为，但默认 cwd 把它从「你挑的目录」变成「你恰好所在的目录」。
        stray = [n for n in ("config.json", "dev.json") if (ws / n).is_file()]
        # 「是不是我们的工作区」要看**内容与痕迹**，不能看目录名：
        # plugins/ 太常见（别人的项目也常有），而只有一份 config.json 的工作区
        # 恰恰是本程序自己写出来的 —— 按目录名判会把两种情况都判反。
        ours = bool({"aaas", "agent", "server"} & set(_load_cfg(ws))) \
            or (ws / ".shell").is_dir() \
            or (ws / "logs" / "events.jsonl").is_file()
        if stray and not ours:
            print(f"[工作区] {ws} 里已有 {'、'.join(stray)}：本程序会把它当自己的设置读取，"
                  f"保存设置时就地改写它", flush=True)
            print("     → 这看起来更像别人的项目而不是工作区；"
                  "不是你的就用 --workspace 换一处", flush=True)
        if found:
            shown = "、".join(found[:8]) + ("…" if len(found) > 8 else "")
            print(f"[工作区] {ws}（= 启动时的当前目录）里有 {len(found)} 个程序：{shown}",
                  flush=True)
            print("     → 它们的 backend.py 会被加载执行；只在你信任这个目录时启动，"
                  "否则用 --workspace 换一处", flush=True)
    if a.cli:
        return cli(ws, a.message, a.pid)
    cfg = _load_cfg(ws)
    port = a.port if a.port is not None else cfg.get("server", {}).get("port", 8777)
    return gui(ws, port,
               not a.no_browser and cfg.get("server", {}).get("openBrowser", True),
               browser=a.browser, open_pid=a.open_pid, serve_only=a.serve)


if __name__ == "__main__":
    raise SystemExit(main())
