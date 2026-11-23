"""**开发者工具集** —— 一个程序「改自己」时借到的那双手。

它不是程序：没有 pid、没有界面、不在左侧栏；它是一套**能力**。
某个程序的人打开了开发者模式（``workspace/dev.json``），那个程序的智能体
就能看见它；关掉立刻收回（见 ``Runtime.capabilities_of``）。

能力面是**故意小**的，用户的原话就是判据：

    看：``dev_tree`` ``dev_read`` ``dev_search``
    史：``dev_versions`` ``dev_show`` ``dev_diff``
    写：``dev_write``         —— 工作区内的普通文件（不碰程序目录、不碰开关）
    改：``dev_plugin_write``  —— 改**自己**：**没有 pid 参数**，改别人在签名上就不可能
    退：``dev_revert``        —— 回到某一版：语义是「把那一版再提交一次」，不是 reset

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
    """超长结果落盘，返回**工作区内**的路径 —— 拿它去 ``dev_read(path, offset=…)`` 接着读。

    为什么落在 ``.dev-cache/<pid>/``：三个约束同时只允许这一个位置。
    ``_safe()`` 要求在工作区内（否则返回的路径 ``dev_read`` 根本打不开）；
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
    """看工作区结构（只读）。你自己的文件在 plugins/<你的 id>/ 下。

    max_depth: 递归深度，默认 3
    """
    from . import toolbox

    out = toolbox.tree(max_depth)
    me = _rt().caller()
    if me in _rt().plugins:
        out["text"] += f"\n\n（你是 {me}：改自己用 dev_plugin_write，看历史用 dev_versions）"
    return out


def read(path: str, offset: int = 1, limit: int = SHOW_LIMIT) -> dict:
    """读一个文件（工作区内，可**分页**）。改自己之前，先把当前那一版读出来。

    path: 相对工作区的路径，如 plugins/<你的 id>/backend.py
    offset: 从第几行开始（1 = 文件开头）
    limit: 最多读多少行（默认 200，硬顶 500）
    """
    from . import toolbox

    return toolbox.read(path, offset=offset, limit=min(int(limit or SHOW_LIMIT), SHOW_MAX))


def search(pattern: str, path: str = ".", limit: int = 30) -> dict:
    """在工作区里按文本检索，返回「文件:行号: 内容」。

    pattern: 要找的文本
    path: 检索起点（相对工作区），默认整个工作区
    limit: 最多返回多少条
    """
    from . import toolbox

    return toolbox.search(pattern, path, limit)


# ------------------------------------------------------------------ 史

def versions(limit: int = 20) -> dict:
    """看**你自己**的历史版本（最新在前）。改坏了想退回去，先看这里。

    每一版一行：版本号、短号、时间、摘要、改了哪几个文件。
    ``v1`` 是最早的那版，号越大越新 —— 回退时用 ``dev_revert(version="v3")``。

    limit: 最多列多少版，默认 20
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
    """看某一版：**不带 path 看清单**（有哪些文件、各多少行、与你现在的差别），带 path 看那个文件。

    先看清单再要片段，比一次要全文省得多 —— 你通常只想知道「那时候 backend.py
    有几个函数」，而不是把 300 行全搬进上下文。

    version: 版本号（v1 / v2 …）或短号（8 位），取 dev_versions 里出现过的
    path: 相对你自己目录的路径（plugin.json / backend.py / ui/**），留空看清单
    offset: 从第几行开始（1 = 文件开头）
    limit: 最多多少行（默认 200，硬顶 500）
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
    """看某一版**到你现在**的逐行差别。定位「是哪一行改坏的」，比整份回退准得多。

    不带 path：先给统计头（几个文件、各 +/- 多少行），再给改动最多的那个文件的 hunk。

    version: 版本号（v1 / v2 …）或短号
    path: 只看某个文件（plugin.json / backend.py / ui/**），留空看全部
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
    """写一个普通文本文件（工作区内）。**不要用来改自己** —— 改自己用 dev_plugin_write。

    path: 相对工作区的路径（plugins/**、dev.json、config.json 会被拒）
    content: 完整内容
    """
    from . import toolbox

    return toolbox.write(path, content)


def plugin_write(files: dict, why: str = "") -> dict:
    """提交**你自己**的新版本：一次提交整份文件，四道校验后才落盘。

    提交的是整份文件，不是补丁：backend.py 里原有的函数、注解、docstring
    都得带上，否则那个能力（和用户用的那个按钮）就没了。

    files: {文件名: 内容}，只接受 plugin.json / backend.py / ui/**；
           data/（用户攒下的数据）永远不许碰
    why: 一句话说明**为什么**改（进历史，下一轮的你会读到它）

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
    """回到某一版：把那一版的内容**再提交一次**（不是 reset，历史照旧留着）。

    为什么不是 reset：回退本身也得可逆。这样退回去之后还能再退回来，
    而且四道校验仍然挡在前面 —— 退到一版坏代码会被拒，并告诉你为什么。

    version: 版本号（v1 / v2 …）或短号，取 dev_versions 里出现过的
    paths: 只退某几个文件（如 ["backend.py"]），留空 = 整份回到那一版
    why: 一句话说明为什么退（进历史）
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
