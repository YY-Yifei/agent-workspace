"""**开发者工具集** —— 一个程序「改自己」时借到的那双手。

它不是程序：没有 pid、没有界面、不在左侧栏；它是一套**能力**。
某个程序的人打开了开发者模式（``workspace/dev.json``），那个程序的智能体
就能看见它；关掉立刻收回（见 ``Runtime.capabilities_of``）。

能力面是**故意小**的，用户的原话就是判据：

    看：``dev_tree`` ``dev_read`` ``dev_search``
    史：``dev_versions`` ``dev_show`` ``dev_diff``
    写：``dev_write``         —— 工作台内的普通文件（不碰程序目录、不碰开关）
    改：``dev_plugin_write``  —— 改**自己**：**没有 pid 参数**，改别人在签名上就不可能
    退：``dev_revert``        —— 回到某一版：语义是「把那一版再提交一次」，不是 reset
    验：``dev_page_open`` ``dev_page_eval`` ``dev_page_shot`` ``dev_page_check``
        ``dev_page_close``    —— 在**真窗口、真引擎**里看自己的页面

「验」这一组为什么是五个而不是十个：``wait`` / ``click`` / ``scroll`` / ``read``
全是 ``dev_page_eval`` 的特例。每多一个动词，就要在提示词解释一次、schema 写
一遍、selftest 验一次 —— 而它们都只是「跑一段 JS」。万能动词 + 一条语义说明，
比十个别名准确。

为什么 ``dev_page_eval`` 可以跑任意 JS：**这个能力本来就在仓库里**。
``uiprobe._probe()`` 里有十余处 ``win.evaluate_js(...)``（读 DOM、点按钮、
派发键盘事件），它早就在对真窗口做任意 JS —— 缺的只是把 JS 从文件挪到参数里。
而那份写死的必然腐烂：``#setbtn`` / ``#setmsg`` 这些选择器，插件页面改一次名
就静默失效一次（它有 skip 分支，所以是**静默**）。

「eval 会读到 ``S.settings.token``」不是理由：``dev_read("config.json")``
今天就能拿到同一份 token，而 ``config.json`` 就在工作台根。真正的边界是
**开发者模式本身**（``_me()`` 拿不到 caller 就直接说这条路只给开发者模式用），
不是某个动词的粒度。用错的理由守对的事，守不住的从来不是那件事。

没有 ``plugin_list`` / ``plugin_call`` / ``plugin_delete`` / ``plugin_verify`` /
``shell`` —— 开发者模式是「给你一把改自己的螺丝刀」，不是「给你一整套造别人
程序的能力」，也不是「给你一个终端」。边界写在**能力面**上，比写在提示词里
劝模型「别乱来」可靠得多：模型看不见的能力，它连试都不会试。

为什么要有「史」这三个（第四轮补的）：自我演化缺的恰好是**记得住**与**退得回**。
四道校验只保证「合法」，不保证「不退化」——把 ``pong`` 删掉了校验照样过；改坏了
它唯一的手段是凭上下文记忆重写一遍，而上下文会被折叠，几轮之后它就不记得上一版
长什么样。``.backup/`` 只有一版，退一次可以、退两次就没了。这三个动词补的正是
这两件事：**一份不会忘的记忆，和一条能走回来的路**。

注意给的不是 git：没有 remote / fetch / push / branch / merge / tag / stash /
reset / clean / rebase，没有任意 sha，也不跨插件。跨机器同步是另一个功能，
不是「自我演化」；而能动历史的命令，任何一条都能把 agent 推到「历史里没有一版
能工作」的状态 —— 那时旁边没人救（装载发生在启动时）。

归属也不变：这些手写的是 ``dev_*``，工具卡上写「🛠 开发者 · write」——
用户一眼能分清「这是它自己的手」还是「小助理的手」。
"""

from __future__ import annotations

import base64
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

_RT = None                                     # 由 _install() 注入

SPILL_CHARS = 12000        # 超过这个字符就落盘返路径（§7.6 阈值表：20000 是给人的，12000 是给 agent 的）
SPILL_KEEP = 20            # 缓存目录里最多留几份
SPILL_TTL = 24 * 3600      # 超过一天就清掉
SHOW_LIMIT = 200           # dev_show / dev_diff 默认读多少行
SHOW_MAX = 500             # 再多就该分页要，别一次灌满上下文
DIFF_LIMIT = 300           # 一次 diff 最多给多少行 hunk


def _install(rt) -> None:
    global _RT
    _RT = rt


def _rt():
    if _RT is None:                            # 理论上不可达：装配先于使用
        raise RuntimeError("内核未装配")
    return _RT


def _me() -> str:
    """此刻动手的是**哪个程序**。内核的 caller 栈是唯一来源。

    为什么不用参数传 pid：一旦 pid 是参数，模型就能填别人的名字 ——
    那时「只能改自己」只能靠「拒绝」来守；而「改自己」这件事是这一层
    唯一的用途，它根本不需要知道名字。少一个参数，就少一条能走错的路。
    """
    me = _rt().caller()
    if not me or me not in _rt().plugins:
        raise ValueError("这条路只给打开了开发者模式的程序用：此刻不知道是哪个程序在动手")
    return me


def _pdir() -> Path:
    """自己的程序目录 —— 四个动词的**全部**作用范围。"""
    return _rt().root.resolve() / "plugins" / _me()


def _vcs():
    """拿到版本化模块；不可用就明说（而不是让调用方看见一个空历史）。"""
    from . import pluginvcs

    ok, why = pluginvcs.available()
    if not ok:
        raise ValueError(f"版本化不可用：{why}")
    return pluginvcs


def _spill(text: str, slug: str) -> str:
    """超长结果落盘，返回**工作台内**的路径 —— 拿它去 ``dev_read(path, offset=…)`` 接着读。

    为什么落在 ``.dev-cache/<pid>/``：三个约束同时只允许这一个位置。
    ``_safe()`` 要求在工作台内（否则返回的路径 ``dev_read`` 根本打不开）；
    ``_snapshot()`` 会 ``rglob`` 整个插件目录且**不排除**隐藏目录，落进插件目录
    就会被拷进 ``.backup/``；``search()`` 跳过点开头的路径段，所以点开头不会被
    自己的检索扫到。缓存丢了不影响正确性：读不到就报错，agent 改走分页。
    """
    root = _rt().root.resolve()
    d = root / ".dev-cache" / _me()
    d.mkdir(parents=True, exist_ok=True)
    name = f"{time.strftime('%Y%m%d-%H%M%S')}-{slug}.txt"
    (d / name).write_text(text, encoding="utf-8")
    stale = sorted(d.glob("*.txt"), key=lambda p: p.stat().st_mtime, reverse=True)
    now = time.time()
    for old in stale:
        try:
            if stale.index(old) >= SPILL_KEEP or now - old.stat().st_mtime > SPILL_TTL:
                old.unlink()
        except OSError:
            pass
    return f".dev-cache/{_me()}/{name}"


def _page(lines: list[str], offset: int, limit: int) -> tuple[str, int, bool]:
    """切一页出来：(正文, 显示行数, 后面还有没有)。"""
    start = max(0, int(offset or 1) - 1)
    want = max(1, min(int(limit or SHOW_LIMIT), SHOW_MAX))
    body = lines[start: start + want]
    return "\n".join(body), len(body), start + len(body) < len(lines)


# ------------------------------------------------------------------ 看

def tree(max_depth: int = 3) -> dict:
    """看工作台结构（只读）；你的文件在 plugins/<你的 id>/ 下。

    max_depth: 递归深度
    """
    from . import toolbox

    out = toolbox.tree(max_depth)
    me = _rt().caller()
    if me in _rt().plugins:
        out["text"] += f"\n\n（你是 {me}：改自己用 dev_plugin_write，看历史用 dev_versions）"
    return out


def host_services() -> dict:
    """写程序之前先调我：宿主到底给了你什么能力（一份清单，三面同名）。

    为什么是「调一下」而不是「写进提示词」：写代码时才需要它，聊天时不需要 ——
    与「每一步都得看见的才进提示词」是同一条判据。清单**从活代码反射**，
    不另存一份文档（两份措辞必然漂移，然后有人照着旧的那份写）。
    """
    from .kernel import FACES, SERVICES, services_of

    lines = ["三个面（后端 backend.py 的模块全局 ``ctx`` · 网页 ui/index.html 的"
             "``window.aw`` · 原生 ui/app.py 的 ``sdk.Native``）名字**逐字相同**：", ""]
    for name, faces in SERVICES.items():
        where = " / ".join(faces)
        lines.append(f"  {name:<12} {where}")
    lines += [
        "",
        "明确没有：任何写 config.json / dev.json 的东西 —— 能写那两份就等于",
        "能给自己或别人拨权限。也没有 Runtime 本体（那能绕过 dispatch）。",
        "",
        "常用三步：",
        "  ctx.get('messages')                  # 你自己的 store，落盘、跨重启",
        "  ctx.set('messages', [...]); ctx.emit('dingtalk.sent', {'n': 1})",
        "  ctx.call('别的程序', '它的函数', {'x': 1})   # 走同一个 dispatch",
        "",
        "网页里（样式**借**宿主的，别抄一份 —— 插件页在 iframe 里是另一份文档，",
        "自定义属性不跨文档继承，抄了必然漂移。见 docs/host-styles.md）：",
        "  <link rel=\"stylesheet\" href=\"/aw.css\">   # 令牌 / 底座 / 滚动条",
        "  <script src=\"/aw.js\"></script>",
        "  aw.theme()            // \"dark\" | \"light\" —— 宿主解析过的，直接能画",
        "  aw.on_theme(t => paint(t))",
        "  aw.get('messages')    // 与后端 ctx.get 是同一份 store",
    ]
    return {"text": "\n".join(lines),
            "services": {n: list(f) for n, f in SERVICES.items()},
            "faces": {f: sorted(services_of(f)) for f in FACES}}


def read(path: str, offset: int = 1, limit: int = SHOW_LIMIT) -> dict:
    """读工作台内的文件，可分页。改自己前先读当前版。

    path: 相对工作台的路径
    offset: 起始行（1 = 开头）
    limit: 最多行数（硬顶 500）
    """
    from . import toolbox

    return toolbox.read(path, offset=offset, limit=min(int(limit or SHOW_LIMIT), SHOW_MAX))


def search(pattern: str, path: str = ".", limit: int = 30, regex: bool = False,
           ignore_case: bool = False, glob: str = "", context: int = 0) -> dict:
    """检索工作台，返回「文件:行号: 内容」。

    pattern: 要找的文本
    path: 检索起点，可给目录或单个文件
    limit: 最多几条命中
    regex: 按正则解释 pattern
    ignore_case: 忽略大小写
    glob: 只看匹配的文件名，如 *.py
    context: 每条命中带几行上下文（0~5）

    和 ``dev_read`` 一样是**只读**的；返回里的 truncated 说明「到 limit 就停了」，
    别把「给了 30 条」当成「一共 30 条」。
    """
    from . import toolbox

    return toolbox.search(pattern, path, limit, regex, ignore_case, glob, context)


# ------------------------------------------------------------------ 史

def versions(limit: int = 20) -> dict:
    """看你自己的历史版本（最新在前），回退前先看这里。

    每一版一行：版本号、短号、时间、摘要、改了哪几个文件。
    ``v1`` 是最早的那版，号越大越新 —— 回退时用 ``dev_revert(version="v3")``。

    limit: 最多列几版
    """
    vcs = _vcs()
    me = _me()
    # 多取一个：多出来的那个只用来判断「还有更早的」。让 agent 以为
    # 「一共就这 20 版」是错的 —— 它可能正需要更早那一版来对照。
    want = max(1, min(int(limit or 20), 100))
    rows = vcs.versions(_pdir(), limit=want + 1)
    # 名字别叫 more：下面循环体里已经有一个 more（「文件太多」的省略号），
    # 同名会把这里的标志覆盖掉 —— 然后 truncated 永远是空串，前端永远 falsy，
    # 「更早的还有」永远不会显示，而没有任何东西会报错。
    truncated = len(rows) > want
    rows = rows[:want]
    if not rows:
        return {"text": f"{me} 还没有任何版本：下一次 dev_plugin_write 成功时会自动记第 1 版。",
                "count": 0}
    lines = [f"{me} 的历史（最新在前）："]
    for v in rows:
        files = ", ".join(f"{p}({how})" for p, how in list(v["files"].items())[:4]) or "—"
        more = "" if v["count"] <= 4 else f" …共 {v['count']} 个"
        lines.append(f"- v{v['ver']}  {v['short']}  {v['when']}  {v['msg']}\n"
                     f"    {files}{more}")
    if truncated:
        lines.append(f"\n…更早的还有（这里只列最新 {want} 版）："
                     f"dev_versions(limit={min(want * 2, 100)})")
    lines.append("\n看某一版：dev_show(version)。看它和现在的差别：dev_diff(version)。"
                 "退回去：dev_revert(version)。")
    return {"text": "\n".join(lines), "count": len(rows), "truncated": truncated,
            "versions": [{k: v[k] for k in ("ver", "short", "sha", "msg", "when")} for v in rows]}


def show(version: str, path: str = "", offset: int = 1, limit: int = SHOW_LIMIT) -> dict:
    """看某一版：不带 path 看文件清单，带 path 看那个文件。

    先看清单再要片段，比一次要全文省得多 —— 你通常只想知道「那时候 backend.py
    有几个函数」，而不是把 300 行全搬进上下文。

    version: 版本号（v1 / v2 …）或 8 位短号
    path: 相对你目录的路径，留空看清单
    offset: 起始行（1 = 开头）
    limit: 最多行数（硬顶 500）
    """
    vcs = _vcs()
    me = _me()
    pdir = _pdir()
    v = vcs.resolve(pdir, version)
    if v is None:
        known = ", ".join(f"v{x['ver']}" for x in vcs.versions(pdir, limit=5)) or "（还没有版本）"
        raise ValueError(f"没有这一版：{version}。有的是：{known}")

    if not str(path or "").strip():
        rows = []
        files = vcs.files_at(pdir, v["sha"])
        for p in sorted(files):
            data = vcs.read_at(pdir, v["sha"], p) or b""
            n = data.count(b"\n") + (1 if data and not data.endswith(b"\n") else 0)
            rows.append(f"- {p}  {n} 行  {len(data)} 字节")
        changed = vcs.changed_since(pdir, v["sha"])
        if changed:
            tail = "与你**现在**不同的文件：" + ", ".join(
                {"new": "现在没有", "mod": "改过", "del": "现在也没了"}[how] + f" · {p}"
                for p, how in changed.items())
        else:
            tail = "与你现在的文件一模一样（无一改动）。"
        body = (f"v{v['ver']} {v['short']} {v['when']} — {v['msg']}\n"
                + "\n".join(rows) + f"\n\n{tail}\n\n"
                + "要某个文件的全文：dev_show(version, path=\"backend.py\")；"
                  "要看差别：dev_diff(version)。")
        return {"text": body, "version": v["ver"], "short": v["short"], "path": "",
                "files": sorted(files), "changed": changed}

    data = vcs.read_at(pdir, v["sha"], path)
    if data is None:
        raise ValueError(f"v{v['ver']} 里没有 {path}（只有 plugin.json / backend.py / ui/** 有历史）")
    text = data.decode("utf-8", errors="replace")
    lines = text.splitlines()
    body, shown, more = _page(lines, offset, limit)
    head = f"v{v['ver']} {v['short']} · {path}（共 {len(lines)} 行）\n"
    out = {"version": v["ver"], "short": v["short"], "path": path,
           "lines": len(lines), "shown_lines": shown, "more": more}
    if len(body) + len(head) > SPILL_CHARS:
        rel = _spill(text, f"v{v['ver']}-{Path(path).name}")
        out.update({"text": f"{head}**这一版整份内容太长，已落盘**：{rel}\n"
                            f"用 dev_read(path=\"{rel}\", offset=1, limit=200) 分页读。",
                    "spilled": rel})
        return out
    if more:
        out["next_offset"] = int(offset or 1) + shown
        body += f"\n…（还有 {len(lines) - int(offset or 1) + 1 - shown} 行：offset={out['next_offset']}）"
    out["text"] = head + body
    return out


def diff(version: str, path: str = "") -> dict:
    """看某一版到你现在的逐行差别（定位是哪行改坏的）。

    不带 path：先给统计头（几个文件、各 +/- 多少行），再给改动最多的那个文件的 hunk。

    version: 版本号（v1 / v2 …）或 8 位短号
    path: 只看某个文件，留空看全部
    """
    vcs = _vcs()
    pdir = _pdir()
    v = vcs.resolve(pdir, version)
    if v is None:
        known = ", ".join(f"v{x['ver']}" for x in vcs.versions(pdir, limit=5)) or "（还没有版本）"
        raise ValueError(f"没有这一版：{version}。有的是：{known}")

    targets = [str(path).strip()] if str(path or "").strip() else sorted(
        vcs.changed_since(pdir, v["sha"]))
    if not targets:
        return {"text": f"v{v['ver']} 与你现在的文件一模一样，没有差别。", "version": v["ver"]}

    stats, hunks = [], []
    for p in targets:
        text = vcs.diff_text(pdir, v["sha"], p)
        plus = sum(1 for ln in text.splitlines() if ln.startswith("+") and not ln.startswith("+++"))
        minus = sum(1 for ln in text.splitlines() if ln.startswith("-") and not ln.startswith("---"))
        stats.append(f"- {p}  +{plus} −{minus}")
        hunks.append((plus + minus, p, text))

    head = f"v{v['ver']} {v['short']} → 你现在（共 {len(targets)} 个文件）\n" + "\n".join(stats)
    hunks.sort(reverse=True)
    body = ""
    for weight, p, text in hunks[:3]:
        lines = text.splitlines()
        body += f"\n\n### {p}\n" + "\n".join(lines[:DIFF_LIMIT])
        if len(lines) > DIFF_LIMIT:
            body += f"\n…（还有 {len(lines) - DIFF_LIMIT} 行没显示：dev_diff(version, path=\"{p}\") 单独看）"

    out = {"version": v["ver"], "short": v["short"], "files": targets}
    if len(head) + len(body) > SPILL_CHARS:
        rel = _spill(head + body, f"diff-v{v['ver']}")
        out.update({"text": f"{head}\n\n**差别太长，已落盘**：{rel}\n"
                            f"用 dev_read(path=\"{rel}\") 分页读。", "spilled": rel})
        return out
    out["text"] = head + body
    return out


# ------------------------------------------------------------------ 写

def write(path: str, content: str) -> dict:
    """写工作台内的文本文件。改自己用 dev_plugin_write。

    path: 相对工作台的路径（data/、plugins/**、dev.json、config.json 拒写）
    content: 完整内容
    """
    from . import toolbox

    return toolbox.write(path, content)


def plugin_write(files: dict, why: str = "") -> dict:
    """提交你自己的新版本（整份一次提交，四道校验后落盘）。

    提交的是整份文件，不是补丁：backend.py 里原有的函数、注解、docstring
    都得带上，否则那个能力（和用户用的那个按钮）就没了。

    files: {文件名: 内容}，只收 plugin.json / backend.py / ui/**（data/ 不许碰）
    why: 为什么改（进历史，下一轮的你会读到）

    宿主给了你什么（ctx.get / ctx.call / ctx.theme …）：dev_host_services。

    校验不过会**整份回滚**到提交前那一版，你什么也没改成。
    成功后会记一版历史：dev_versions 看得见，dev_revert 退得回。
    """
    me = _me()
    from . import toolbox

    caps, kind = toolbox._commit(me, files, by="self", why=why)   # 与小助理同一条落盘路径
    how = ("你的界面是原生窗口（ui/app.py）—— 用户点开就弹那个窗口。"
           if kind == "native" else
           "你的界面是网页（ui/index.html）—— 用户刷新一下就是新的。")
    return {"text": f"**改的是你自己**：{me} 现在是这一版，能力 {caps}。\n"
                    f"{how}\n下一轮你的上下文里就是这套能力。\n"
                    f"（历史已记一版：dev_versions 看；退回去用 dev_revert。"
                    f"上一版另有一份在 workspace/.backup/{me}/。）",
            "plugin": me, "functions": caps, "self": True}


def revert(version: str, paths: list[str] | None = None, why: str = "") -> dict:
    """回到某一版：把那一版再提交一次（不是 reset，历史留着）。

    为什么不是 reset：回退本身也得可逆。这样退回去之后还能再退回来，
    而且四道校验仍然挡在前面 —— 退到一版坏代码会被拒，并告诉你为什么。

    version: 版本号（v1 / v2 …）或 8 位短号
    paths: 只退这几个文件，留空 = 整份回退
    why: 为什么退（进历史）
    """
    me = _me()
    from . import toolbox

    out = toolbox._plugin_revert(me, version, paths, why=why or f"回到 {version}", by="self")
    vcs = _vcs()
    v = vcs.resolve(_pdir(), version)
    remain = vcs.changed_since(_pdir(), (v or {}).get("sha", "")) if v else {}
    out["text"] = out["text"] + (
        "\n（与那一版还有差别的文件：" + ", ".join(f"{p}({h})" for p, h in remain.items())
        + " —— 那是校验挡住或你只要了部分文件）" if remain else
        "\n（工作树已与那一版一致。）")
    return out


# ------------------------------------------------------------------ 验
# 一个页面会话 = 一个真窗口（pywebview / WebView2）+ 一条 CDP 观察通道。
# 常驻的理由不是省事：一次性批跑做不了「看一眼 → 改一处 → 再看一眼」，
# 而那正是页面调试的全部内容。

_PAGE: dict[str, dict] = {}        # sid → 会话
_PAGE_SEQ = 0                      # sid 只增不减
# 日志路径**另存一份**：会话收掉之后仍要读得到。
# 排在 _PAGE 里的话，每一条错误路径都会先 _page_kill（第一件事就是 pop），
# 再读日志 —— 读到的永远是空串。真正的原因被代码自己删掉了。
_PAGE_LOG: dict[str, Path] = {}
_PAGE_LOG_KEEP = 20
PAGE_IDLE = 300                    # 没有命令这么久就自己收尸
PAGE_READY = 150                   # 起窗口 + 连 CDP 的上限（秒）
PAGE_CMD = 180                     # 单条命令的上限（截图/逐面检查会慢）


def _page_new_sid() -> str:
    """一个新的、**保证**没在用的 sid。

    别用 ``len(_PAGE) + 1``：开着 page1、page2，关掉 page1 之后 len 又回到 1，
    下一次就又叫 page2 —— 把那个还活着的会话整条覆盖掉，旧窗口、旧子进程、
    旧句柄一起变成没人认领的孤儿（而新会话看起来一切正常）。
    """
    global _PAGE_SEQ
    _PAGE_SEQ += 1
    while f"page{_PAGE_SEQ}" in _PAGE:
        _PAGE_SEQ += 1
    return f"page{_PAGE_SEQ}"


def _page_reap() -> None:
    """把闲置太久的会话收掉。**每次调用都做** —— 没有后台线程，就没有「忘了关」。"""
    now = time.time()
    for sid, s in list(_PAGE.items()):
        if now - s["at"] > PAGE_IDLE:
            _page_kill(sid)


def _page_kill(sid: str) -> None:
    s = _PAGE.pop(sid, None)
    if not s:
        return
    for key in ("sock", "fh"):
        try:
            s[key].close()
        except Exception:
            pass
    proc = s.get("proc")
    if proc is not None and proc.poll() is None:
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           capture_output=True, timeout=20)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass


def _page_pick(sid: str) -> str:
    """没点名就用唯一那个会话；有多个就**如实说**，别猜。"""
    if sid:
        if sid not in _PAGE:
            raise ValueError(f"没有这个页面会话：{sid!r}（现有：{sorted(_PAGE) or '无'}）")
        return sid
    if len(_PAGE) == 1:
        return next(iter(_PAGE))
    if not _PAGE:
        raise ValueError("还没有页面会话：先 dev_page_open()")
    raise ValueError(f"有 {len(_PAGE)} 个页面会话，得点名：{sorted(_PAGE)}")


def _page_cmd(sid: str, req: dict, timeout: float = PAGE_CMD) -> dict:
    s = _PAGE[sid]
    s["at"] = time.time()
    s["sock"].settimeout(timeout)
    s["sock"].sendall((json.dumps(req, ensure_ascii=False) + "\n").encode("utf-8"))
    buf = s["buf"]
    while b"\n" not in buf:
        try:
            chunk = s["sock"].recv(1 << 20)
        except socket.timeout:
            # 超时之后这条通道就不可信了：迟到的那条回答还排在队列里，
            # 下一条命令会先读到它 —— 从此每条结果都错一位，而每条都
            # 「看起来正常」。宁可丢会话，也不要一个悄悄错位的通道。
            _page_kill(sid)
            raise ValueError(f"页面会话 {sid} 在 {timeout:.0f}s 内没回话，已收掉；"
                             "重新 dev_page_open() 一个") from None
        if not chunk:
            raise ValueError("页面会话断了（窗口被关掉了？）")
        buf += chunk
    line, s["buf"] = buf.split(b"\n", 1)
    return json.loads(line.decode("utf-8"))


def _page_log_tail(sid: str, n: int = 900) -> str:
    """日志尾部。**会话收掉之后仍然读得到** —— 要排的正是失败那一刻。"""
    log = _PAGE_LOG.get(sid) or (_PAGE.get(sid) or {}).get("log")
    try:
        return Path(log).read_text(encoding="utf-8", errors="replace")[-n:] if log else ""
    except Exception:
        return ""


def _child_env() -> dict:
    """给子进程的环境：把**包根**钉进 PYTHONPATH，其余原样继承。"""
    import agentworkspace as pkg

    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    root = str(Path(pkg.__file__).resolve().parent.parent)
    old = env.get("PYTHONPATH") or ""
    env["PYTHONPATH"] = root if not old else (root + os.pathsep + old)
    return env


def page_open(viewport: str = "pane") -> dict:
    """开一个真窗口看自己的页面，之后一直用它。

    引擎是 pywebview / WebView2 —— 生产里插件页面就是它渲染的，不是 Chrome。
    窗口开在屏幕外（x=-3000）：既合成帧（能截图），又不挡你。

    看的是**真页面**：真工作台、真插件、真会话。有正在跑的宿主就直接连它；
    没有就自己起一个（仍是真工作台）。窗口是新的，不碰你正在看的那个。

    viewport: pane（默认，1180 宽，与生产一致）或 desktop（1600 宽）
    """
    _page_reap()
    me = _me()
    root = _rt().root.resolve()
    from . import pagecheck

    port = pagecheck.free_port()
    argv = [sys.executable, "-X", "utf8", "-m", "agentworkspace.pagecheck",
            "--worker", "--port", str(port), "--pid", me,
            "--workspace", str(root), "--viewport", viewport]
    hp = int(getattr(_rt(), "port", 0) or 0)
    if hp:                              # 有现成宿主就连它；没有时 pagecheck 自己起一个
        argv += ["--host-port", str(hp)]

    log = root / ".dev-cache" / me / "worker.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    fh = open(log, "w", encoding="utf-8", buffering=1)
    # cwd 是工作台（不是包所在的仓库根），所以 `-m agentworkspace.pagecheck`
    # 得靠 PYTHONPATH 才 import 得到。生产装了包时这条是多余的，开发时是必需的 ——
    # 而开发者模式主要在开发时用。
    proc = subprocess.Popen(argv, cwd=str(root), stdout=fh, stderr=subprocess.STDOUT,
                            env=_child_env())

    sid = _page_new_sid()
    _PAGE[sid] = {"proc": proc, "fh": fh, "log": log, "at": time.time(),
                  "buf": b"", "sock": None, "viewport": viewport}
    _PAGE_LOG[sid] = log
    for old in list(_PAGE_LOG)[:-_PAGE_LOG_KEEP]:
        _PAGE_LOG.pop(old, None)

    # 连上去：worker 先 bind 再干活，所以这一步很快（真正的等待在 ready 上）
    t0 = time.time()
    sock = None
    while time.time() - t0 < 60:
        try:
            sock = socket.create_connection(("127.0.0.1", port), 2)
            break
        except Exception:
            if proc.poll() is not None:
                tail = _page_log_tail(sid)      # 先读日志：_page_kill 会摘掉会话
                _page_kill(sid)
                return {"text": f"页面自查进程起不来（退出码 {proc.returncode}）：\n{tail}"}
            time.sleep(0.2)
    if sock is None:
        tail = _page_log_tail(sid)
        _page_kill(sid)
        return {"text": "60 秒内没连上页面自查进程（它可能卡在起窗口那一步）：\n"
                        f"{tail}"}
    _PAGE[sid]["sock"] = sock

    # 等 ready：起宿主 + 起窗口 + 连 CDP + reload，全在这一步里
    sock.settimeout(PAGE_READY)
    buf = b""
    try:
        while b"\n" not in buf:
            chunk = sock.recv(65536)
            if not chunk:
                break
            buf += chunk
    except socket.timeout:
        tail = _page_log_tail(sid)
        _page_kill(sid)
        return {"text": f"{PAGE_READY} 秒内窗口没就绪：\n{tail}"}
    if b"\n" not in buf:
        tail = _page_log_tail(sid)
        _page_kill(sid)
        return {"text": f"页面自查进程没回话就退了：\n{tail}"}
    line, rest = buf.split(b"\n", 1)
    _PAGE[sid]["buf"] = rest
    hello = json.loads(line.decode("utf-8"))
    if not hello.get("ok"):
        why = hello.get("error") or "窗口没起来"
        tail = _page_log_tail(sid)
        _page_kill(sid)
        return {"text": f"窗口没起来：{why}\n\n{tail}"}

    plugin = hello.get("plugin") or {}
    sizes = hello.get("sizes") or {}
    lines = [f"页面会话 {sid} 开好了（{me} · 真窗口 · WebView2 {hello.get('engine')}）",
             f"  窗口 {sizes.get('requested')} → 实测 {sizes.get('inner')}"
             f"（dpr {sizes.get('dpr')}）；页面区 {sizes.get('frame')}"]
    if not plugin.get("has_ui"):
        lines.append(f"  ⚠ {plugin.get('why') or '这个程序没有页面'}")
    else:
        lines.append(f"  页面：{plugin.get('entry')}")
    lines += ["", "接着用：dev_page_eval(js) / dev_page_shot() / dev_page_check() / "
                   "dev_page_close()"]
    return {"text": "\n".join(lines), "sid": sid, "engine": hello.get("engine"),
            "plugin": plugin, "sizes": sizes}


def page_eval(js: str, sid: str = "", frame: str = "page") -> dict:
    """在自己的页面里跑一段 JS，把结果拿回来。

    语义（每条都是会踩的坑）：
      * **表达式优先**：``1+1`` → 2，``document.title`` → 字符串。
      * **语句兜底**：``let a=1; a*2`` 会先当表达式跑（SyntaxError），再包成
        函数重跑，返回里带 ``wrapped: true`` 告诉你发生过什么。
      * **await 直接可用**：``await fetch('/api/state').then(r=>r.json())``。
        （``uiprobe`` 里那套轮询是「没得 await 时的妥协」，不是方法。）
      * **DOM 节点不报错**：返回 ``{__t:'node', tag, id, cls, text, html}``。
        返回 ``{ok:false}`` 只会让你去改**没写错的那一行**。
      * **导航会毁掉执行上下文**：``location.reload()`` 之后返回
        ``navigated: true`` —— 那是事实，不是错误。
      * 返回值超过 4000 字符会落盘，返回里给路径。

    默认在**你自己的页面**（那个 iframe）里求值 —— 所以 ``document.title``
    拿到的是你页面的标题，``document.querySelector('#btn')`` 找的是你页面上的按钮。
    要看外壳（左侧栏、对话面板）时传 ``frame="host"``。

    js: 要跑的 JS
    sid: 页面会话（只有一个时可以不给）
    frame: page（默认，自己的页面）或 host（外壳）
    """
    try:
        sid = _page_pick(sid)
        r = _page_cmd(sid, {"op": "eval", "js": js, "frame": frame})
    except Exception as exc:
        return {"text": f"{type(exc).__name__}: {exc}"}
    if not r.get("ok"):
        err = str(r.get("error") or "")
        if "Execution context was destroyed" in err or "navigat" in err.lower():
            return {"text": "这段 JS 让页面导航走了（执行上下文被销毁）—— 这是事实，"
                            "不是错误。窗口还在，接着 eval 就行。", "navigated": True}
        return {"text": f"这段 JS 没跑成：{err}"}
    value = r.get("value")
    text = json.dumps(value, ensure_ascii=False, default=str)
    if r.get("truncated"):
        text += f"\n（原值 {r['truncated']} 字符，已截断；全文：{r.get('spilled')}）"
    if r.get("wrapped"):
        text += "\n（这是一条**语句**，不是表达式：我把它包成函数跑了一遍）"
    return {"text": text, "value": value, "wrapped": bool(r.get("wrapped")),
            "frame": r.get("frame") or frame}


def _shot_name(path: str, fallback: str = "page.png") -> str:
    """图的名字 = **真的那个文件名**。

    上一版这里是 ``f"{pid}-page.png"`` —— 一个从未存在过的模板名。它唯一的
    后果是把排查引向一个不存在的文件（真那张在 ``shots/<时间戳>.png``），
    并且让「图没送到」看起来像「文件是坏的」。
    """
    if path:
        got = Path(path).name
        if got:
            return got
    return fallback


def _shot_bytes(r: dict) -> tuple[bytes, str]:
    """这次截图的字节：``（字节, 备注）``。**优先读盘上那一份**。

    为什么先读盘：发出去的字节必须就是**写下去**的那张，否则「agent 看见的」
    与「你在硬盘上点开的那张」可以是两回事。读不动才退回返回里带的 base64，
    并且把这件事说出来 —— 一次静默的换源就是下一次「我明明看到了」的伏笔。
    """
    path = str(r.get("path") or "")
    note = ""
    if path:
        try:
            raw = Path(path).read_bytes()
            if raw:
                return raw, ""
            note = f"{path} 是 0 字节"
        except OSError as exc:
            note = f"{path} 读不动：{type(exc).__name__}: {exc}"
    else:
        note = "返回里没有落盘路径"
    b64 = r.get("b64") or ""
    if b64:
        try:
            raw = base64.b64decode(b64)
        except (ValueError, TypeError) as exc:
            return b"", f"{note}；base64 也解不开：{type(exc).__name__}: {exc}"
        if raw:
            return raw, f"{note}（改用返回值里的 base64）"
        return b"", f"{note}；返回值里的 base64 解出来是 0 字节"
    return b"", note


def page_shot(sid: str = "", full: bool = False) -> dict:
    """截一张页面图，图会进你的上下文（你能真的看见它）。

    为什么单独一个动词而不是 ``page_check(shot=True)``：一张 1180×800 的 PNG
    base64 之后约 270KB，8 个面就是 2MB —— 上下文会直接溢出。想「看」就单独取
    一张；判断一个面长得对不对，一张就够。

    sid: 页面会话（只有一个时可以不给）
    full: 截整页（含滚动出去的部分）
    """
    try:
        sid = _page_pick(sid)
        r = _page_cmd(sid, {"op": "shot", "full": bool(full)})
    except Exception as exc:
        return {"text": f"{type(exc).__name__}: {exc}"}
    if not r.get("ok"):
        return {"text": f"截图没成：{r.get('error')}"}
    path = str(r.get("path") or "")
    raw, note = _shot_bytes(r)
    if not raw:
        # 「拍到了但没读出来」必须是一个**独立的**结局，并且报真实路径：
        # 它和「没拍到」是两件事，能被查的方向也完全不同。
        return {"text": f"截图拍到了，但我没读出来：{note or '原因不明'}"
                        f"（文件：{path or '没给路径'}）", "path": path}
    sizes = r.get("sizes") or {}
    text = (f"截了一张 {len(raw)} 字节的图（窗口实测 {sizes.get('inner')}，"
            f"页面区 {sizes.get('frame')}）；文件：{path or '（没落盘）'}")
    if r.get("bytes") and int(r["bytes"]) != len(raw):
        text += f"（注意：报告说 {r['bytes']} 字节，实际发的是 {len(raw)}）"
    if note:
        text += f"\n（{note}）"
    return {"text": text,
            "images": [{"raw": raw, "mime": "image/png",
                        "name": _shot_name(path), "path": path}],
            "path": path, "bytes": len(raw)}


def page_check(sid: str = "", areas: str = "", shot: bool = False) -> dict:
    """逐面看一遍自己的页面，给一份能判对错的报告。

    默认六面（``load`` / ``console`` / ``network`` / ``overflow`` / ``contrast`` /
    ``narrow``）。``console`` 与 ``network`` 必须放在最前面：**页面白屏时截图
    出来是一张干净的空白**，DOM 里什么都不缺 —— ``TypeError: S.pid is undefined``
    只在这两条通道里留痕。那是最坏的一种假通过。

    没跑的、跑不了的，都会**显式**出现在「跳过」里 —— 缺口可见，不能假装完整。

    sid: 页面会话（只有一个时可以不给）
    areas: 只跑这几面，逗号分隔（空 = 默认六面）
    shot: 顺带截一张图（会进上下文，慎用）
    """
    try:
        sid = _page_pick(sid)
        r = _page_cmd(sid, {"op": "check",
                            "areas": [a.strip() for a in areas.split(",") if a.strip()] or None,
                            "shot": bool(shot)},
                      timeout=PAGE_CMD * 2)
    except Exception as exc:
        return {"text": f"{type(exc).__name__}: {exc}"}
    if not r.get("ok"):
        return {"text": f"检查没跑成：{r.get('error')}"}

    return _page_report(r)


def _page_report(r: dict) -> dict:
    """pagecheck 的报告 → 给 agent 的形状（文字 + 图）。纯函数，好上门。

    ``_png`` 必须在这里摘掉：它是「给 agent 那一张」的 base64 备份，
    不是报告内容 —— 留着它每份报告都多出 ~270KB 的 data，
    而界面收到的那坨字符串它一个字节都不会用。
    """
    png = r.pop("_png", "")
    shot_path = str(r.get("shot") or "")
    eng = r.get("engine") or {}
    sizes = r.get("sizes") or {}
    plugin = r.get("plugin") or {}
    lines = [f"页面自查 · {r.get('pid')}（真窗口 · WebView2 {eng.get('browser')}）",
             f"窗口 {sizes.get('requested')} → 实测 {sizes.get('inner')}"
             f"（dpr {sizes.get('dpr')}）；页面区 {sizes.get('frame')}"]
    if not plugin.get("has_ui"):
        lines.append(f"⚠ {plugin.get('why') or '这个程序没有页面'}")
    rows = r.get("areas") or []
    if rows:
        lines.append("")
    for a in rows:
        mark = "✓" if a.get("ok") else "✗"
        lines.append(f"{mark} {a.get('name')}")
        if a.get("detail") and not a.get("ok"):
            lines.append(f"    {a.get('detail')}")
    skipped = r.get("skipped") or []
    if skipped:
        lines.append("")
        lines.append("跳过：" + "；".join(f"{s.get('name')}（{s.get('why')}）"
                                        for s in skipped))
    bad = [c for c in (r.get("console") or []) if c.get("type") == "error"]
    lines.append("")
    lines.append(f"console 错误 {len(bad)} 条 · 未捕获异常 {len(r.get('pageerrors') or [])} 条"
                 f" · 请求失败 {len(r.get('network') or [])} 条")
    for c in bad[-5:]:
        lines.append(f"  [console] {c.get('text')}")
    for e in (r.get("pageerrors") or [])[-5:]:
        lines.append(f"  [异常] {e}")
    for n in (r.get("network") or [])[-5:]:
        lines.append(f"  [请求] {n}")
    out = {"text": "\n".join(lines), "report": r,
           "ok": all(a.get("ok") for a in rows) if rows else False}
    if png:
        raw, note = _shot_bytes({"path": shot_path, "b64": png})
        if raw:
            out["images"] = [{"raw": raw, "mime": "image/png",
                              "name": _shot_name(shot_path, f"{r.get('pid')}-page.png"),
                              "path": shot_path}]
            if note:
                out["text"] = f"{out['text']}\n（{note}）"
        else:
            # 报告照发，但**明说**图没带上 —— 静默少一张，agent 会以为自己看过了。
            out["text"] = f"{out['text']}\n（图没能带上：{note or '原因不明'}；" \
                          f"文件：{shot_path or '没给路径'}）"
    return out


def page_close(sid: str = "") -> dict:
    """关掉页面会话（窗口、宿主、CDP 端口一起收）。

    sid: 页面会话（只有一个时可以不给）
    """
    _page_reap()
    try:
        sid = _page_pick(sid)
    except Exception as exc:
        return {"text": str(exc)}
    try:
        _page_cmd(sid, {"op": "close"}, timeout=30)
    except Exception:
        pass
    _page_kill(sid)
    return {"text": f"页面会话 {sid} 关了（窗口、宿主、CDP 端口一起收）。"
                    f"还剩 {len(_PAGE)} 个。"}
