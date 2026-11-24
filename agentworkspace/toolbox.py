"""**小助理的手**：看、读、写、搜、跑命令，以及造程序。

它们挂在**内置程序** ``assistant`` 名下 —— wire name 是 ``assistant_tree`` /
``assistant_plugin_write`` …… 注意这是结构性的：能力面按 pid 切开
（``Runtime.capabilities_of``），别的程序的智能体**拿不到**这些手，
所以「谁能造程序」不是提示词里劝出来的，是它们压根没这项能力。

它们和插件能力走**同一个** schema 生成器、同一个 dispatch、同一条事件流。

为什么要内置这八件：模型要能「造东西」，它得先有手。手本身就是能力，
而能力必须声明出来（否则模型不知道自己有手）。这也正是「最大化模型能力」的
正确做法：不是给它脚手架，是给它**准确的能力面 + 看得懂的错误**。

安全边界（本机单用户，只做「不该发生的事」不让它发生，不做沙箱）：
    - 一切路径锁在 workspace 内；插件只能写 plugin.json / backend.py / ui/**
    - **永远不许写 data/** —— 那是用户攒下来的东西，agent 改代码不许碰它
    - 跑命令限 cwd=workspace、30 秒、输出截断
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import threading
import time
import sys
from pathlib import Path

_RT = None                                     # 由 install() 注入，函数内部用 _rt() 取
PID_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
WRITABLE = re.compile(r"^(plugin\.json|backend\.py|ui/[A-Za-z0-9_.\-/]+)$")


def _install(rt) -> None:
    global _RT
    _RT = rt


def _rt():
    if _RT is None:                            # 理论上不可达：install 先于 mount
        raise RuntimeError("内核未装配")
    return _RT


def _ws() -> Path:
    return _rt().root.resolve()


def _safe(path: str, *, must_exist: bool = False) -> Path:
    """把相对路径解析成 workspace 内的绝对路径；越界一律拒。"""
    root = _ws()
    p = (root / str(path or ".")).resolve()
    if p != root and root not in p.parents:
        raise ValueError(f"路径越界：{path}（只能操作工作区内的文件）")
    if must_exist and not p.exists():
        raise ValueError(f"不存在：{path}")
    return p


# ------------------------------------------------------------------ 看

def tree(max_depth: int = 3) -> dict:
    """看一眼工作区结构：有哪些目录、插件、提示词。

    max_depth: 递归深度，默认 3
    """
    root = _ws()
    lines: list[str] = []

    def walk(d: Path, depth: int) -> None:
        if depth > max_depth:
            return
        for child in sorted(d.iterdir()):
            if child.name.startswith((".", "__")) or child.name in ("logs", ".tmp"):
                continue
            if child.is_symlink():
                # 只显示、不跟进去：跟进去就绕开了「工作区边界」这句话
                lines.append("  " * (depth - 1) + child.name + "@")
                continue
            shown = child.name + ("/" if child.is_dir() else "")
            lines.append("  " * (depth - 1) + shown)
            if child.is_dir():
                walk(child, depth + 1)

    walk(root, 1)
    body = "\n".join(lines) or "（空工作区）"
    return {"text": f"工作区 {root.name}/：\n{body}", "root": str(root)}


MAX_READ_CHARS = 20000          # 一次读进上下文的硬顶（字符）


def read(path: str, offset: int = 1, limit: int = 0) -> dict:
    """读一个文件的内容（工作区内，可**分页**）。

    path: 相对工作区的路径，如 plugins/pomodoro/backend.py
    offset: 从第几行开始（1 = 文件开头）
    limit: 最多读多少行（0 = 尽可能多，仍受 20000 字符硬顶）

    为什么现在要分页：老版本是 ``text[:20000]`` —— 截了却不**说**，于是 agent 看见
    ``lines: 3000``、实际只拿到 800 行，却以为自己读完了整份文件，然后基于半份文件
    去改程序。报错会让它换个办法，静默会让它写错。所以返回值里带上
    ``truncated`` / ``shown_lines`` / ``more`` / ``next_offset``：**能不能自证完整**，
    比多给几千字符重要得多。
    """
    p = _safe(path, must_exist=True)
    if p.is_dir():
        raise ValueError(f"{path} 是目录，用 tree 看结构")
    text = p.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines(keepends=True)
    start = max(0, int(offset or 1) - 1)
    want = int(limit or 0)
    chunk = lines[start: start + want] if want > 0 else lines[start:]
    body = "".join(chunk)
    cut = len(body) > MAX_READ_CHARS
    if cut:
        body = body[:MAX_READ_CHARS]
    shown = body.count("\n") + (1 if body and not body.endswith("\n") else 0)
    consumed = start + shown
    more = consumed < len(lines)
    out = {"text": body, "path": p.relative_to(_ws()).as_posix(),
           "lines": len(lines), "shown_lines": shown,
           "truncated": bool(more or cut), "more": more}
    if more:
        out["next_offset"] = consumed + 1
        out["text"] += (f"\n…（还有 {len(lines) - consumed} 行没读，"
                        f"接着读用 offset={consumed + 1}）")
    elif cut:
        out["text"] += "\n…（这一页已到 20000 字符硬顶，请用 offset 分页读）"
    return out


def search(pattern: str, path: str = ".", limit: int = 30) -> dict:
    """在工作区里按文本检索，返回「文件:行号: 内容」。

    pattern: 要找的文本
    path: 检索起点（相对工作区），默认整个工作区
    limit: 最多返回多少条
    """
    base = _safe(path, must_exist=True)
    hits: list[str] = []
    files = [base] if base.is_file() else sorted(base.rglob("*"))
    for f in files:
        if not f.is_file() or f.stat().st_size > 400_000:
            continue
        if any(part.startswith((".", "__")) or part == "data" for part in f.parts[-4:]):
            continue
        try:
            for i, ln in enumerate(f.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
                if pattern in ln:
                    hits.append(f"{f.relative_to(_ws()).as_posix()}:{i}: {ln.strip()[:200]}")
                    if len(hits) >= limit:
                        break
        except OSError:
            continue
        if len(hits) >= limit:
            break
    return {"text": "\n".join(hits) or "没有命中", "count": len(hits)}


# ------------------------------------------------------------------ 写

RESERVED = ("dev.json", "config.json")   # 归人的东西：开关与宿主配置


def _write_target(path: str) -> Path:
    """文件写能力的**唯一**判据：能写哪儿、不能写哪儿。

    小助理的 ``write`` 与开发者模式的 ``dev_write`` 都走这里 —— 于是「不许碰
    什么」只有一处，不会两条路径各说各话。

        工作区内         可以
        data/           不行（用户攒下的东西，删了没法退）
        plugins/**      不行（改程序要走 plugin_write：四道校验 + 整份回滚）
        dev.json        不行（**开关必须归人**：程序能写它，就等于自己能把自己
                         ——甚至别的程序——的权限拨开。这是整套边界的最后一道门）
        config.json     不行（宿主配置；写坏了下次起不来，而那时没人能改）

    第三条是这一轮补的真洞：开发者模式让程序能写文件，而开关本身也是一个文件。
    不堵它，「只能改自己」就有一扇后门直接通向「改别人的权限」。
    """
    p = _safe(path)
    parts = p.relative_to(_ws()).parts
    rel = "/".join(parts)
    if "data" in parts:
        # 按**路径分量**判，不是只看末两段：``data/sub/x.txt`` 的末两段是
        # ``sub``/``x.txt``，剥两段的写法会放它过去 —— 防护等于没有。
        raise ValueError(f"不许写 data/ —— 那是用户的数据（{rel}）")
    if rel.startswith("plugins/"):
        raise ValueError(
            f"改程序不走 write：{rel} 在程序目录里。用 plugin_write —— "
            f"它会先跑四道校验、失败整份回滚；write 是裸写，写坏了下一次装载直接起不来。")
    if rel in RESERVED:
        raise ValueError(
            f"{rel} 归人：它管着开发者模式开关 / 宿主配置。程序能写它，就等于 "
            f"自己能把自己（或别的程序）的权限拨开 —— 这把钥匙只该在人手里。")
    return p


def write(path: str, content: str) -> dict:
    """写一个文本文件（工作区内，自动建父目录）。不要用来改插件，改插件用 plugin_write。

    path: 相对工作区的路径
    content: 完整内容
    """
    p = _write_target(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return {"text": f"已写入 {p.relative_to(_ws()).as_posix()}（{len(content)} 字符）",
            "path": p.relative_to(_ws()).as_posix()}


def shell(command: str) -> dict:
    """在工作区目录里跑一条命令（超时 30 秒），用来看测试结果、列目录等。

    command: 要执行的命令
    """
    # 边读边限：`capture_output` 把整份输出攒进内存，落临时文件又会把磁盘写满
    # （`yes` 在 30 秒里能做几个 GB）。所以起一个读线程，超过 60000 字符就杀掉进程。
    LIMIT = 60000
    buf: list[str] = []
    total = 0
    proc = subprocess.Popen(command, shell=True, cwd=str(_ws()),
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding="utf-8", errors="replace")
    t0 = time.time()

    def _reader() -> None:
        nonlocal total
        assert proc.stdout is not None
        while True:
            chunk = proc.stdout.read(8192)
            if not chunk:
                return
            if total < LIMIT:
                buf.append(chunk[: LIMIT - total])
                total += len(chunk)
            if total >= LIMIT and proc.poll() is None:
                proc.kill()                    # 话太多就不听了：别让它继续占着管道
                return

    th = threading.Thread(target=_reader, name="toolbox-shell", daemon=True)
    th.start()
    try:
        proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        raise ValueError("命令超过 30 秒未结束，已终止")
    th.join(timeout=5)
    out = "".join(buf).strip()
    return {"text": out[:6000] or f"（无输出，退出码 {proc.returncode}）",
            "exit": proc.returncode, "seconds": round(time.time() - t0, 2)}


# ------------------------------------------------------------------ 造

def plugin_write(pid: str, files: dict) -> dict:
    """创建或重写一个插件的文件 —— 一次提交整个目录，四道校验后才落盘。

    这是「agent 写程序」的唯一入口。写成功后插件立刻生效，
    下一轮你就能调用它自己的函数了。

    pid: 插件 id（小写字母开头，只能是 a-z0-9_，如 ledger）
    files: {文件名: 内容}，只接受 plugin.json / backend.py / ui/**；
           data/ 永远不许碰

    界面有两种形态，由 plugin.json 的 "ui" 决定：
        "ui": "ui/index.html"  → 网页，活在宿主窗口里（**默认选这个**）
        "ui": "ui/app.py"      → 原生窗口，它自己弹一个 OS 窗口（tkinter）
    原生窗口只在"网页做不出来"时才用（要常驻、要贴着桌面、要调本地资源）。
    两种形态的执行路径完全一样：都落到同一个 dispatch。
    """
    caps, kind = _commit(pid, files)
    how = ("它有自己的原生窗口（ui/app.py）—— 人在左边栏点它，就弹出那个窗口。"
           if kind == "native" else
           "它的界面是网页（ui/index.html）—— 在左边栏点开就能用。")
    me = _rt().caller()
    if me == pid:
        return {"text": f"**改的是你自己**：{pid} 现在是这一版，能力 {caps}。\n"
                        f"下一轮你的上下文里就是这套能力；浏览器里的页面刷新一下就是新的。"
                        f"\n（上一版留在 workspace/.backup/{pid}/，要退回去用 plugin_write 提交它。）",
                "plugin": pid, "functions": caps, "self": True}
    return {"text": f"程序 {pid} 已就绪，{how}\n它的能力：{caps}。\n"
                    f"这些能力属于**它自己的助手**（那个助手的上下文里只有这些，"
                    f"没有你的手）。你要用它们，走 plugin_call(\"{pid}\", \"<函数>\", {{...}})"
                    f"——同一个 dispatch，和人点按钮完全一样，**现在就能调**。",
            "plugin": pid, "functions": caps}


def _commit(pid: str, files: dict, *, by: str = "agent", why: str = "") -> tuple[list[str], str]:
    """把 files 提交成插件 pid 的当前版本；四道校验不过 → 整份回滚并抛错。

    返回 (能力清单, uiKind)。这是**改程序的唯一落盘路径**：小助理的
    ``plugin_write``、开发者模式的 ``dev_plugin_write``、以及两者的**回退**
    都走它 —— 于是「会回滚」和「会记一版」都不会只对一半路径成立。

    ``files`` 的值可以是 ``str``（正常写入）或 ``bytes``（回退：旧版内容必须
    **按字节**写回，decode 一次再 encode 回去，CRLF 会变 LF、GBK 文件直接炸）。
    ``by`` / ``why`` 只进提交消息：``self`` / ``agent`` / ``human``，以及一句人话。

    为什么回滚是硬要求：开发者模式让一个程序能改自己，而「改坏了」最坏的结局
    不是这一次失败，是**下次开机起不来**（装载发生在启动时，旁边没人能救）。
    """
    if not PID_RE.match(pid):
        raise ValueError(f"插件 id {pid!r} 不合法：须匹配 {PID_RE.pattern}")
    if not isinstance(files, dict) or not files:
        raise ValueError("files 必须是非空对象：{'plugin.json': ..., 'backend.py': ...}")

    root = _ws()
    pdir = (root / "plugins" / pid).resolve()
    if root not in pdir.parents:
        raise ValueError("插件路径越界")

    for name in files:
        if not WRITABLE.match(name):
            raise ValueError(f"不允许写 {name}：只接受 plugin.json、backend.py、ui/**（data/ 是用户的数据）")

    # 先留一份「提交前」的（也就是后悔药）
    snap = _snapshot(pdir)
    try:
        for name, content in files.items():
            if not isinstance(content, (str, bytes)):
                raise ValueError(f"{name} 的内容必须是字符串或字节")
            target = (pdir / name).resolve()
            if pdir not in target.parents and target != pdir:
                raise ValueError(f"{name} 路径越界")
            target.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(content, bytes):
                target.write_bytes(content)          # 回退：原样写回，一个字节都不改
            else:
                target.write_text(content, encoding="utf-8")

        report = _verify(pid)
    except Exception:                       # 写到一半就炸：也退回原样
        _restore(pdir, snap)
        raise
    if not report["ok"]:
        n = _restore(pdir, snap)
        raise ValueError(
            f"插件未通过校验，**已整份回滚**到提交前的版本（{n} 个文件复原，data/ 没动）。"
            "要改的是这些：\n" + "\n".join(f"- {e}" for e in report["errors"]))

    _rt().scan()                               # 继承：内核只落盘；变更由事件广播出去
    caps = [c.wire for c in _rt().plugins[pid].caps.values()] if pid in _rt().plugins else []
    kind = _rt().plugins[pid].ui_kind if pid in _rt().plugins else "web"
    _vcs_commit(pid, pdir, caps=caps, by=by, why=why)   # 旁路：失败只留痕，绝不影响写入
    return caps, kind


def _vcs_commit(pid: str, pdir: Path, *, caps: list[str], by: str, why: str) -> None:
    """成功写入之后记一版。**这里抛出的任何东西都不许冒泡**（不变量 1）。

    为什么挂在 ``_commit`` 而不是各个调用方：小助理、插件自己、人的回退按钮
    最终都落到这一处；挂在这里，「一版」的语义才只有一种解释
    ——「一次被四道校验接受的改动」。
    """
    try:
        from . import pluginvcs

        ok, detail = pluginvcs.available()
        if not ok:
            return                                 # 没装 / 自检不过：静默退化
        prog = _rt().plugins.get(pid)
        title = (getattr(prog, "title", "") or pid) if prog is not None else pid
        done, detail = pluginvcs.commit_all(
            pdir, author=(title, f"{pid}@plugin.local"),
            notes={"Caps": ", ".join(caps), "By": by, "Why": why, "Check": "verify=ok, import=ok"})
        if done:
            v = pluginvcs.versions(pdir, limit=1)
            _rt().publish("plugin.commit", pid=pid, source="system",
                          data={"sha": detail[:8], "ver": (v[0]["ver"] if v else 0),
                                "msg": (v[0]["msg"] if v else "")})
        elif detail != "nothing to commit":
            _rt().publish("plugin.commit", pid=pid, source="system",
                          data={"sha": "", "error": detail})
    except Exception as exc:                       # 版本化是旁路，写入已经成功了
        try:
            _rt().publish("plugin.commit", pid=pid, source="system",
                          data={"sha": "", "error": f"{type(exc).__name__}: {exc}"})
        except Exception:
            pass


def _plugin_revert(pid: str, version: str, paths: list[str] | None = None, *,
                   why: str = "", by: str = "human") -> dict:
    """把某一版的内容**再提交一次** —— 回退不是 reset，历史只增不改。

    为什么必须回到 ``_commit`` 里走一遍：回退到一版坏代码时，四道校验要能拦住它，
    并说出原因。绕过校验的回退等于给「改坏了」开一扇后门，而坏事总是从那扇门进来。
    """
    from . import pluginvcs

    ok, detail = pluginvcs.available()
    if not ok:
        raise ValueError(f"版本化不可用：{detail}")
    root = _ws()
    pdir = (root / "plugins" / pid).resolve()
    if pdir.parent != (root / "plugins").resolve() or not pdir.is_dir():
        raise ValueError(f"没有插件 {pid}")
    files, drops, err = pluginvcs.revert_files(pdir, version, paths)
    if files is None:
        raise ValueError(err)
    for name in drops:                             # 那一版没有、现在却有 → 删掉才是「回到那一版」
        drop = (pdir / name).resolve()
        if pdir in drop.parents and drop.is_file():
            drop.unlink()
    caps, kind = _commit(pid, files, by=by, why=why)
    v = pluginvcs.resolve(pdir, version)
    return {"text": f"已回到 v{(v or {}).get('ver', '?')}（{(v or {}).get('short', '')}）："
                    f"内容按字节写回，再提交成一版新版本。能力 {caps}。",
            "plugin": pid, "functions": caps, "version": version}


def plugin_delete(pid: str, keep_data: bool = True) -> dict:
    """删除一个插件。默认保留 data/（用户攒下的数据）。

    pid: 插件 id
    keep_data: 是否保留数据目录，默认保留
    """
    if _rt().caller() == pid:
        raise ValueError(
            f"不能用手删掉自己：{pid} 一删，它自己的界面和那枚开发者模式开关就一起没了"
            f"（开关在 workspace/dev.json，这个目录之外）。要删就让用户删、或让小助理删。")
    if not PID_RE.match(pid):
        raise ValueError(f"插件 id {pid!r} 不合法：须匹配 {PID_RE.pattern}")
    root = _ws()
    pdir = (root / "plugins" / pid).resolve()
    # 判据是「pdir 的父目录**就是** plugins」，不是「root 在它的祖先里」：
    # 后者会让 pid="../data" 解析到 <ws>/data 而通过，接着 rmtree 掉整个数据目录。
    if pdir.parent != (root / "plugins").resolve() or not pdir.is_dir():
        raise ValueError(f"没有插件 {pid}")
    # 开发者模式开关是**目录之外**的状态（workspace/dev.json）：程序删了，那个
    # true 还留在文件里，下一个同名插件一出现就悄悄带着开发者模式上岗。删程序的
    # 时候一并收掉。顺序要紧：set_dev 对「不在 plugins 里」的 pid 会拒绝，所以
    # 必须赶在 rmtree 之前。
    try:
        if _rt().dev_mode(pid):
            _rt().set_dev(pid, False)
    except Exception:                   # 开关收不掉也不许拦住删除本身
        pass
    backup = None
    if keep_data and (pdir / "data").is_dir():
        backup = pdir.parent / f".trash-{pid}-data"
        if backup.exists():
            shutil.rmtree(backup)
        shutil.move(str(pdir / "data"), str(backup))
    arch = None
    try:
        from . import pluginvcs

        if pluginvcs.available()[0]:
            arch = pluginvcs.archive(pdir, _ws() / BACKUP)   # 删程序 ≠ 扔历史
    except Exception:
        arch = None
    shutil.rmtree(pdir)
    _rt().scan()
    tail = "".join([f"，数据备份在 {backup.name}" if backup else "",
                    f"，历史归档在 {BACKUP}/{arch.name}" if arch else ""])
    return {"text": f"已删除插件 {pid}" + tail, "plugin": pid}


def plugin_list() -> dict:
    """列出工作区里所有插件、它们的能力，以及装载错误。"""
    rt = _rt()
    rows = []
    for p in rt.plugins.values():
        caps = ", ".join(c.wire for c in p.caps.values()) or "（无能力）"
        rows.append(f"- {p.pid}（{p.title}）：{caps}" + (f"  ⚠ {p.error}" if p.error else ""))
    return {"text": "\n".join(rows) or "还没有插件", "count": len(rows)}


def plugin_verify(pid: str) -> dict:
    """对一个插件做静态体检（不落盘、不执行）—— 提交前先自查。

    pid: 插件 id
    """
    return _verify(pid)


def plugin_call(pid: str, fn: str, args: dict | None = None) -> dict:
    """替用户操作**别的程序**：调用 pid 这个程序的 fn 能力。

    用户的程序不止一个，而只有你能操作它们 —— 「开始专注」「记一笔」「喝了杯水」
    这种话落在哪个程序上，就调哪个程序的函数。不确定有哪些能力就先 plugin_list。

    pid: 程序 id（如 pomodoro）
    fn: 那个程序里的函数名（如 start）
    args: 参数对象，省略等于 {}
    """
    rt = _rt()
    if pid == "assistant":
        raise ValueError("小助理不能通过这条路调用自己")
    if rt.program(pid) is None:
        raise ValueError(f"没有程序 {pid}")
    res = rt.dispatch(pid, fn, dict(args or {}), source="agent")
    body = json.dumps(res.data, ensure_ascii=False) if res.data else ""
    return {"text": f"[{res.status}] {res.text}" + (f"\n{body}" if body else ""),
            "status": res.status, "data": res.data or {}}


# ------------------------------------------------------------------ 四道校验

BACKUP = ".backup"          # workspace/.backup/<pid>/ = 上一次提交前的版本（也是后悔药）


def _snapshot(pdir: Path) -> Path | None:
    """提交前把插件的非 data/ 文件拷一份。目录本来不存在 → None（没有可退的）。"""
    if not pdir.is_dir():
        return None
    dst = _ws() / BACKUP / pdir.name
    if dst.exists():
        shutil.rmtree(dst)
    for f in pdir.rglob("*"):
        rel = f.relative_to(pdir)
        # .git 也要跳过：就地长出一个仓之后，不跳的话每写一次就把整个仓库
        # 拷进 .backup/<pid>/ —— 几十毫秒变几秒。
        if not f.is_file() or rel.parts[0] in ("data", ".git"):
            continue
        t = dst / rel
        t.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(f, t)
    return dst if dst.exists() else None


def _restore(pdir: Path, snap: Path | None) -> int:
    """把插件退回快照。**data/ 一个字节都不碰**。返回复原的文件数。"""
    n = 0
    for f in sorted(pdir.rglob("*"), key=lambda x: len(x.parts), reverse=True):
        rel = f.relative_to(pdir)
        # **最致命的一处**：不跳过 .git，一次失败的回滚会把整个历史删掉 ——
        # 而那时正是最需要「退回去」的时候。
        if rel.parts[0] in ("data", ".git"):
            continue
        if f.is_file():
            f.unlink()
            n += 1
        elif f.is_dir() and not any(f.iterdir()):
            f.rmdir()
    if snap is not None:
        for f in snap.rglob("*"):
            if f.is_file():
                t = pdir / f.relative_to(snap)
                t.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(f, t)
    elif pdir.is_dir() and not any(pdir.iterdir()):
        pdir.rmdir()                        # 本来没有这个程序：连空目录也别留
    return n


def _verify(pid: str) -> dict:
    """manifest 合法 → backend 语法且有公开函数 → 每个函数有注解与 docstring → 子进程 import。

    前三道是静态的（kernel.check_plugin），第四道是**真的跑一次 import**：
    「语法没错但一 import 就炸」只有跑起来才能发现，而那时内核已经被污染了。
    """
    from .kernel import check_plugin

    pdir = _ws() / "plugins" / pid
    static = check_plugin(pdir)
    errors = list(static["errors"])
    if static["ok"]:
        errors += _import_probe(pdir)
    return {"ok": not errors, "errors": errors, "functions": static["functions"],
            "uiKind": static.get("uiKind", "web"), "uiName": static.get("uiName", ""),
            "title": static.get("title", pid)}


_PROBE = r"""
import importlib.util, sys, types
class Ctx:
    def __init__(self): self.d = {}
    def get(self, k, default=None): return self.d.get(k, default)
    def set(self, k, v): self.d[k] = v
    def emit(self, *a, **k): pass
spec = importlib.util.spec_from_file_location("probe", sys.argv[1])
mod = importlib.util.module_from_spec(spec)
mod.__dict__["ctx"] = Ctx()
spec.loader.exec_module(mod)
names = [n for n, o in vars(mod).items()
         if not n.startswith("_") and callable(o)
         and getattr(o, "__module__", None) == "probe"]
print("FUNCS:" + ",".join(sorted(names)))
"""


def _import_probe(pdir: Path) -> list[str]:
    """在**子进程**里真正 import 一次：死了也不影响内核。"""
    try:
        proc = subprocess.run([sys.executable, "-c", _PROBE, str(pdir / "backend.py")],
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=20, cwd=str(pdir))
    except subprocess.TimeoutExpired:
        return ["backend.py 顶层执行超过 20 秒（import 时不要做重活）"]
    if proc.returncode != 0:
        tail = (proc.stderr or "").strip().splitlines()
        return [f"backend.py 一 import 就失败：{tail[-1] if tail else '未知错误'}"]
    return []
