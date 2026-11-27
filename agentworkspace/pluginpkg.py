"""插件包（``.awp``）：把一个程序装进一个文件，再把它装回来。

这个文件只回答一个问题：**「一个程序」是什么。** 它不含任何 HTTP 知识、不含界面，
所以导出/导入这两件事可以脱离宿主单独验（selftest 就是这么验的）。

包是一枚 zip，根上只有两样东西 —— 这个形状不是审美，是校验成本::

    awp.json            包的身份（格式号 / pid / 标题 / 导出时间 / 带没带数据 / 文件数）
    plugin/**           程序本体：plugin.json、backend.py、ui/**，勾了数据才有 data/**

为什么让程序本体住在 ``plugin/`` 底下，而不是摊在 zip 根上：摊开以后「包的身份」与
「插件的内容」混在同一层，一个叫 ``awp.json`` 的插件文件就跟包身份撞名了 —— 那时校验
就得多写一套例外，而例外就是下一个 bug。现在判据只有一条：**条目要么是 ``awp.json``，
要么在 ``plugin/`` 底下，别的都不该存在。**

三处复用，都是有意的（同一件事只该有一份判据）：

    代码白名单   pluginvcs.tracked()      「有历史的那一份」就是「导出的那一份」，
                                          所以 .git / __pycache__ / data 天然不在包里
    pid 形状     toolbox.PID_RE          与「agent 写程序」同一条正则
    第四道校验   check_plugin + 子进程 import 探针（toolbox._import_probe）
                                          与 agent 写程序同一道门：装上去的程序必须
                                          真的能起来，否则整份回滚

**默认不导出数据不是界面上的一个默认值，是这里的签名**：``build(..., include_data=False)``。
界面上那枚勾选框只是表达「这一次的选择」，判据在服务端。
"""

from __future__ import annotations

import io
import json
import re
import shutil
import time
import zipfile
from pathlib import Path

SUFFIX = ".awp"
META = "awp.json"
BODY = "plugin"
FORMAT = 1                       # 包里那份 awp.json 的格式号；只认这一版

# 上限不是「安全沙箱」，是「别把一个手滑变成一次磁盘写满」：
# 插件本来就是本机单用户的东西，防的是 zip 炸弹与误操作，不是防攻击者。
MAX_FILES = 2000
MAX_ONE = 32 << 20               # 单个文件 32 MB
MAX_TOTAL = 64 << 20             # 解包后总量 64 MB
BOMB_RATIO = 120                 # 压缩比上限
BOMB_FLOOR = 1 << 20             # 小于 1 MB 的文件不看比值（小的天然比值高）

PID_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_SKIP_SUFFIX = (".pyc",)
_SKIP_PARTS = ("__pycache__",)


class BadPackage(ValueError):
    """这个包不能用 / 装不上。消息是**给人看的**，直接上界面。"""


# ------------------------------------------------------------------ 文件清单

def _walk(base: Path, rel_prefix: str) -> list[str]:
    """``base`` 下所有**该进包**的文件（相对 pdir 的正斜杠路径，已排序）。

    ``.pyc`` / ``__pycache__`` / 符号链接一律跳过：前两个是运行期垃圾（带上它们
    等于把一个 Python 版本绑进包里），符号链接则是「包里的路径可能指向包外」。
    """
    out: list[str] = []
    if not base.is_dir():
        return out
    for f in sorted(base.rglob("*")):
        if f.is_symlink() or not f.is_file():
            continue
        rel = f.relative_to(base)
        if rel.suffix in _SKIP_SUFFIX or any(p in _SKIP_PARTS for p in rel.parts):
            continue
        out.append(f"{rel_prefix}/{rel.as_posix()}" if rel_prefix else rel.as_posix())
    return out


def code_files(pdir: Path) -> list[str]:
    """程序本体（相对路径）。**复用版本化那份白名单**，不另写一份。"""
    from . import pluginvcs

    return pluginvcs.tracked(Path(pdir))


def data_files(pdir: Path) -> list[str]:
    """``data/**`` —— 程序自己攒下来的东西（ctx.get/set 的 store.json 也在里面）。

    **对话不在这里**：``workspace/sessions/<pid>/`` 是宿主的会话库，与插件目录同级，
    不是插件的一部分。所以导出一个程序 = 导出这个程序，不是导出一段历史。
    """
    return _walk(Path(pdir) / "data", "data")


def _title(pdir: Path) -> str:
    try:
        man = json.loads((Path(pdir) / "plugin.json").read_text(encoding="utf-8"))
        return str(man.get("title") or Path(pdir).name)
    except (OSError, ValueError, AttributeError):
        return Path(pdir).name


def _bytes_of(pdir: Path, rels: list[str]) -> int:
    total = 0
    for rel in rels:
        try:
            total += (Path(pdir) / rel).stat().st_size
        except OSError:
            pass
    return total


def filename(pid: str, when: float | None = None) -> str:
    """导出的文件名：``<pid>-<年月日-时分>.awp``。

    只用 ASCII（pid 是 ASCII，时间戳也是）：文件名要经过 Content-Disposition，
    中文得走 RFC 5987 那一套，而这里没有非 ASCII 的信息值得为它加一层编码。
    """
    return f"{pid}-{time.strftime('%Y%m%d-%H%M', time.localtime(when or time.time()))}{SUFFIX}"


def preview(pdir: Path, *, include_data: bool = False, pid: str | None = None) -> dict:
    """「导出这一个会装进什么」——**不打包，只数**。界面用它把代价说出来。"""
    pdir = Path(pdir)
    pid = pid or pdir.name
    code = code_files(pdir)
    data = data_files(pdir) if include_data else []
    total = len(code) + len(data)
    return {
        "pid": pid, "title": _title(pdir), "name": filename(pid),
        "includeData": bool(include_data),
        # 两个数就叫 **文件数**（codeFiles / dataFiles）。上一版把它们叫 code / data，
        # 而导入那一路的 `data` 是**布尔**（这枚包带没带数据）—— 同一个名字两种意思，
        # 读的人迟早按错的那个理解。
        "codeFiles": len(code), "dataFiles": len(data),
        "dataBytes": _bytes_of(pdir, data),
        "bytes": _bytes_of(pdir, code) + _bytes_of(pdir, data),
        "names": (code + data)[:50], "more": max(0, total - 50),
    }


# ------------------------------------------------------------------ 打包

def build(pdir: Path, *, include_data: bool = False, pid: str | None = None,
          when: float | None = None) -> bytes:
    """把一个程序打成包（``bytes``）。

    ``include_data`` 默认 **False** —— 这是「默认不导出数据」那条要求在代码里的样子。
    一个程序的默认导出物因此是**干净的程序**：换台机器装上，它还是原来那个程序，
    只是没有你在这台机器上攒下的那点东西。
    """
    pdir = Path(pdir)
    pid = pid or pdir.name
    code = code_files(pdir)
    data = data_files(pdir) if include_data else []
    meta = {
        "awp": FORMAT,
        "pid": pid,
        "title": _title(pdir),
        "exported": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(when or time.time())),
        "data": bool(include_data),
        "files": len(code) + len(data),
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(META, json.dumps(meta, ensure_ascii=False, indent=2) + "\n")
        for rel in code + data:
            zf.write(pdir / rel, f"{BODY}/{rel}")
    return buf.getvalue()


# ------------------------------------------------------------------ 读包

def _clean(rel: str) -> str:
    """zip 里的一个名字 → 规整的相对路径；不像话就拒。

    这是 zip slip 的唯一判据处。**绝对路径、盘符、上跳都要在解包前就拦住**：
    等到写完再判，越界的那个文件已经在包外了。
    """
    p = str(rel or "").replace("\\", "/")
    if p.startswith("/") or re.match(r"^[A-Za-z]:", p):
        raise BadPackage(f"包里有绝对路径：{rel!r}")
    parts = [x for x in p.split("/") if x not in ("", ".")]
    if any(x == ".." for x in parts):
        raise BadPackage(f"包里有上跳路径：{rel!r}")
    if not parts:
        raise BadPackage("包里有一个空条目名")
    return "/".join(parts)


def _role(rel: str) -> str:
    """条目的身份：``meta`` / ``body`` / 不该存在（抛）。"""
    if rel == META:
        return "meta"
    if rel.startswith(BODY + "/") and len(rel) > len(BODY) + 1:
        return "body"
    raise BadPackage(f"包里有一个不该存在的条目：{rel!r}"
                     f"（只收 {META} 与 {BODY}/**）")


def read(blob: bytes, *, name: str = "") -> dict:
    """把一枚包读成 ``{pid, title, data, files, names, bytes}``；不像话就抛 :class:`BadPackage`。

    **只读不落盘**：导入前的「先看一眼」与真正导入走的是同一个解析 ——
    预览里看到的东西与装上去的东西因此不可能不一致。
    """
    try:
        zf = zipfile.ZipFile(io.BytesIO(blob))
    except (zipfile.BadZipFile, ValueError, OSError) as exc:
        raise BadPackage(f"这不是一个 .awp 包（zip 打不开：{exc}）")
    files: dict[str, bytes] = {}
    total = 0
    with zf:
        names = [i for i in zf.infolist() if not i.is_dir()]
        if len(names) > MAX_FILES:
            raise BadPackage(f"包里文件太多（{len(names)} > {MAX_FILES}）")
        seen: set[str] = set()
        for info in names:
            rel = _clean(info.filename)
            if rel in seen:
                raise BadPackage(f"包里有重名条目：{rel}（解出来是哪一份说不清）")
            seen.add(rel)
            _role(rel)
            if info.file_size > MAX_ONE:
                raise BadPackage(f"{rel} 太大（{info.file_size} 字节 > {MAX_ONE}）")
            total += info.file_size
            if total > MAX_TOTAL:
                raise BadPackage(f"解包后总量超了（> {MAX_TOTAL} 字节）")
        total = 0                                     # 真的读完一遍再数一次
        for info in names:
            rel = _clean(info.filename)
            try:
                raw = zf.read(info)
            except (RuntimeError, zipfile.BadZipFile, OSError) as exc:
                raise BadPackage(f"{rel} 读不出来：{exc}")
            # zip 头里的 file_size 是**别人写的**，读完再看真字节 —— 谎报尺寸的包
            # 只有在解压那一刻才露馅（zip 炸弹就是这个形状）。
            if len(raw) > MAX_ONE:
                raise BadPackage(f"{rel} 解出来比头里说的更大（{len(raw)} 字节）")
            if (info.compress_size and len(raw) > BOMB_FLOOR
                    and len(raw) > BOMB_RATIO * info.compress_size):
                raise BadPackage(f"{rel} 的压缩比太离谱（{len(raw)}/"
                                 f"{info.compress_size}），不像是正常的包")
            total += len(raw)
            if total > MAX_TOTAL:
                raise BadPackage(f"解包后总量超了（> {MAX_TOTAL} 字节）")
            files[rel] = raw

    if META not in files:
        raise BadPackage(f"包里没有 {META}（这不是这个产品导出的包）")
    try:
        meta = json.loads(files[META].decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise BadPackage(f"{META} 不是合法 JSON：{exc}")
    if not isinstance(meta, dict):
        raise BadPackage(f"{META} 不是对象")
    fmt = meta.get("awp")
    if fmt != FORMAT:
        raise BadPackage(f"这个包的格式是 awp {fmt!r}，本版本只认 {FORMAT}")
    pid = str(meta.get("pid") or "")
    if not PID_RE.match(pid):
        raise BadPackage(f"包里的 pid {pid!r} 不合法（须匹配 {PID_RE.pattern}）")
    body = {k[len(BODY) + 1:]: v for k, v in files.items() if _role(k) == "body"}
    if "plugin.json" not in body:
        raise BadPackage(f"{BODY}/ 里没有 plugin.json —— 这个包不完整")
    if "backend.py" not in body:
        raise BadPackage(f"{BODY}/ 里没有 backend.py —— 这个包不完整")
    # 包说「没带数据」却塞了 data/：这是包在说谎，而说谎的包不该被信任。
    has_data = any(k.startswith("data/") for k in body)
    if has_data and not meta.get("data"):
        raise BadPackage(f"包里带了 data/，{META} 却说没带 —— 这份包自相矛盾")
    return {"pid": pid, "title": str(meta.get("title") or pid),
            "data": bool(meta.get("data")), "files": body,
            "names": sorted(f"{BODY}/{k}" for k in body) + [META],
            "bytes": total, "name": name, "exported": str(meta.get("exported") or "")}


# ------------------------------------------------------------------ 装回去

def free_pid(root: Path, pid: str) -> str:
    """撞名时给一个空着的名字（``pomodoro`` → ``pomodoro_2``）；没有就空串。

    **只建议，不替人决定**：界面上那个「导入为」可以改，预填的就是这个值。
    """
    base = Path(root) / "plugins"
    if PID_RE.match(pid) and not (base / pid).exists():
        return pid
    for i in range(2, 100):
        cand = f"{pid[:28]}_{i}"
        if PID_RE.match(cand) and not (base / cand).exists():
            return cand
    return ""


def install(rt, blob: bytes, *, as_pid: str | None = None, name: str = "") -> dict:
    """把包装成 ``plugins/<pid>``；装不上就**整份回滚**（不留下半个程序）。

    校验与「agent 写程序」同一道门：静态（``kernel.check_plugin``）过了再在子进程里
    import 一次。为什么必须跑那一次 —— 语法没错但一 import 就炸的程序，装上去的结果
    是「开机少一个程序」，而那时旁边没人能救。

    ``as_pid`` 是「导入为」：撞名时人可以把包装成另一个名字（pid 就是目录名，也是
    能力名的前缀）。默认用包里那个名字 —— 不悄悄改名，改名要人看见。
    """
    from .kernel import RESERVED_PIDS, check_plugin, reserved_reason
    from .toolbox import _import_probe

    pkg = read(blob, name=name)
    pid = str(as_pid or pkg["pid"]).strip()
    if not PID_RE.match(pid):
        raise BadPackage(f"程序 id {pid!r} 不合法：须匹配 {PID_RE.pattern}")
    if pid in RESERVED_PIDS:
        raise BadPackage(reserved_reason(pid))
    root = Path(rt.root)
    pdir = (root / "plugins" / pid)
    if pdir.exists():
        raise BadPackage(f"已经有一个叫 {pid} 的程序了 —— 导入不覆盖它"
                         f"（先删掉它，或者在「导入为」里换个名字）")

    pdir.parent.mkdir(parents=True, exist_ok=True)
    try:
        for rel, raw in pkg["files"].items():
            target = (pdir / rel).resolve()
            if pdir.resolve() not in target.parents:      # 与 _clean 两道判据，同一条事实
                raise BadPackage(f"{rel} 解析到了包外")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)
        report = check_plugin(pdir)
        errs = list(report["errors"])
        if report["ok"]:
            errs += _import_probe(pdir)
        if errs:
            raise BadPackage("这个包装上了，但**起不来**，已整份回滚：\n- "
                             + "\n- ".join(errs))
    except BaseException:
        shutil.rmtree(pdir, ignore_errors=True)           # 只删**这次新建**的那个目录
        raise

    rt.scan()                                             # 继承：只落盘，变更走事件
    prog = rt.plugins.get(pid)
    return {
        "pid": pid, "title": getattr(prog, "title", pkg["title"]),
        "files": len(pkg["files"]), "data": pkg["data"],
        "renamed": pid != pkg["pid"],
        "uiKind": getattr(prog, "ui_kind", "none"),
        "functions": sorted(prog.caps) if prog is not None else report["functions"],
        "error": getattr(prog, "error", None),
    }


def export(rt, pid: str, *, include_data: bool = False) -> tuple[bytes, str]:
    """导出：返回 ``(包字节, 文件名)``。没有这个程序就抛 :class:`BadPackage`。"""
    pdir = Path(rt.root) / "plugins" / pid
    if not pdir.is_dir():
        raise BadPackage(f"没有程序 {pid}")
    return build(pdir, pid=pid, include_data=include_data), filename(pid)


def __main__(argv=None) -> int:
    """命令行入口（导出 / 看一眼一枚包）。"""
    from openthree.cli_tool import run_cli

    return run_cli(globals(), argv=argv)


if __name__ == "__main__":
    raise SystemExit(__main__())
