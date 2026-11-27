"""**插件程序的版本化** —— 每个插件目录就地长成一个 git 仓。

为什么不外置（``workspace/.versions/<pid>`` + ``--git-work-tree``）：外置的代价是
**人打不开** —— ``git -C plugins/x log`` 失效，得记两个参数。就地 ``.git`` 换来的
正是不变量「人能用普通 git 客户端直接打开」（``docs/plugin-versioning.md`` §2）。

依赖 ``pure-python-git`` —— 它是**默认依赖**（``dependencies`` 里，不是 extra），
装上本包就有历史，不用另补一条命令。它缺失/坏掉时整个模块**静默退化**：
所有函数返回「没启用」，写路径一行都不改。这条不是可选优化：

    程序写不进去 = 用户的功能没了；历史上多一版少一版 = 可以接受的损失

所以本模块的公开函数**永不抛异常**（``commit_all`` 把失败包成返回值），并且启动时
跑一遍自检 —— 因为 pythongit 的标志位/行为与真 git 有出入（§6 那张表全是实跑结果），
光 pin 版本号挡不住。

四条施工规矩，每一条都对应一次踩坑：

1. **不用 CLI、不拼命令字符串**：走模块 API（``workdir.*`` / ``objects.*`` / ``refs.*``）。
   CLI 一半的标志位被 argparse 拒（``status --porcelain`` / ``ls-tree --name-only`` /
   ``rev-parse --short``），``reset --hard`` 更会 rc=128 且**输出为空**。
2. **不依赖 ignore**：``add_paths`` 只给显式路径。``add -A`` / ``add .`` 在 pythongit 里
   **不遵守 .gitignore**，会把 ``data/store.json`` 和 ``*.pyc`` 一起收进索引。
3. **不解析任何 rev 语法**：没有 ``HEAD~1``（``fatal: not a tree-ish``）。历史自己沿
   parent 链走，版本号在白名单里解析。
4. **回退按字节写回**：``read_at()`` 返回 ``bytes``，不是 ``str``。同一份文本用两种
   落盘约定会得到**两个不同 blob**（实测）；``decode('utf-8')`` 遇到 GBK 文件直接炸。
"""

from __future__ import annotations

import difflib
import shutil
import threading
import time
from pathlib import Path

# ---------------------------------------------------------------- 常量

TRACKED_FIXED = ("plugin.json", "backend.py")   # 顶层两个固定文件
TRACKED_DIR = "ui"                              # 界面目录，递归
SKIP_PARTS = frozenset({".git", "__pycache__", "data"})   # 永不入库
MAX_CHAIN = 500                                 # 数版本号时最多回走多少步

GITIGNORE = """\
# 这是**给人看的**注释，不是防线：pythongit 的 add 不读 ignore，
# 入库范围由 pluginvcs.tracked() 的显式白名单决定（见 docs/plugin-versioning.md §3）。
data/
__pycache__/
*.pyc
"""

_AVAILABLE: tuple[bool, str] | None = None      # 自检结论，一次就够
_CHECKING = False                                # 自检进行中（见 available）
_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


def _lock(pdir: Path) -> threading.RLock:
    """每个插件一把锁：HTTP 线程与智能体线程可能同时写同一个程序。"""
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(pdir.name, threading.RLock())


# ---------------------------------------------------------------- 可用性

def _import():
    """拿到 pythongit 的四个模块；没装就 (None, 原因)。"""
    try:
        from pythongit import Repository, objects, refs, workdir
    except Exception as exc:                     # ImportError 之外还有装坏了的情况
        return None, f"没有 pure-python-git（{type(exc).__name__}: {exc}）"
    return (Repository, objects, refs, workdir), ""


def _version_text() -> str:
    try:
        from importlib.metadata import version
        return version("pure-python-git")
    except Exception:
        return "?"


def _selfcheck() -> tuple[bool, str]:
    """在临时目录里真跑一遍 init → add → commit → 读回。

    为什么值得多花 ~100ms：§6 那张表证明了「同名命令的行为随时会变」。
    自检不过就当没装 —— 宁可不记版本，也不能让写入跟着坏。
    """
    mods, why = _import()
    if mods is None:
        return False, why
    import shutil as _sh
    import tempfile as _tf

    d = Path(_tf.mkdtemp(prefix="ws-vcs-check-"))
    try:
        p = d / "p"
        p.mkdir()
        (p / "backend.py").write_text("def ping() -> dict:\n    return {}\n", encoding="utf-8")
        ok, detail = commit_all(p, "selfcheck", author=("selftest", "check@local"))
        if not ok:
            return False, f"自检失败：{detail}"
        vers = versions(p, limit=5)
        if not vers:
            return False, "自检失败：写完读不回版本"
        # 别断言 subject：首版会被 _compose 改写成 bootstrap 那句话。
        # 要验的是**内容能按字节读回**（revert 的全部依赖就在这一点上）。
        back = read_at(p, vers[0]["sha"], "backend.py") or b""
        if b"return {}" not in back:
            return False, "自检失败：读回的内容不对"
        if read_at(p, vers[0]["sha"], "data/x.json") is not None:
            return False, "自检失败：data/ 竟然进了库"
        return True, f"pure-python-git {_version_text()}"
    except Exception as exc:
        return False, f"自检异常：{type(exc).__name__}: {exc}"
    finally:
        _sh.rmtree(d, ignore_errors=True)


def available() -> tuple[bool, str]:
    """版本化现在能不能用（结论缓存进进程）。返回 (可用, 说明)。

    ``_CHECKING`` 是给自检自己开的门：自检要真的调一次 ``commit_all``，
    而 ``commit_all`` 第一句就问 ``available()`` —— 没有这个标记就是无限递归。
    """
    global _AVAILABLE, _CHECKING
    if _CHECKING:
        return True, "自检进行中"
    if _AVAILABLE is None:
        _CHECKING = True
        try:
            _AVAILABLE = _selfcheck()
        finally:
            _CHECKING = False
    return _AVAILABLE


# ---------------------------------------------------------------- 白名单

def tracked(pdir: Path) -> list[str]:
    """一个插件此刻**该入库**的文件（相对路径，正斜杠）。

    这是白名单的唯一定义处：写路径、读路径、回退路径都问它 ——
    于是「data/ 不能进历史」不会只在一半路径上成立。
    """
    out: list[str] = []
    for name in TRACKED_FIXED:
        if (pdir / name).is_file():
            out.append(name)
    base = pdir / TRACKED_DIR
    if base.is_dir():
        for f in sorted(base.rglob("*")):
            rel = f.relative_to(pdir)
            if not f.is_file() or rel.suffix == ".pyc":
                continue
            if any(part in SKIP_PARTS or part.startswith(".") for part in rel.parts):
                continue
            out.append(rel.as_posix())
    return out


def is_tracked_path(path: str) -> bool:
    """``data/`` 连只读都不给：与写入同一份白名单（§7.5 第 4 条）。"""
    p = str(path or "").replace("\\", "/").lstrip("/")
    if not p or p.startswith("../") or "/../" in p:
        return False
    parts = p.split("/")
    if any(part in SKIP_PARTS or part.startswith(".") for part in parts):
        return False
    if p in TRACKED_FIXED:
        return True
    return p.startswith(TRACKED_DIR + "/") and len(parts) > 1


# ---------------------------------------------------------------- 建仓 / 提交

def ensure(pdir: Path):
    """有仓就打开，没仓就建一个。返回 Repository 或 None（版本化不可用）。"""
    ok, _ = available()
    if not ok:
        return None
    mods, _ = _import()
    assert mods is not None
    Repository = mods[0]
    pdir = Path(pdir)
    try:
        if not (pdir / ".git").is_dir():
            Repository.init(pdir)
            ig = pdir / ".gitignore"
            if not ig.exists():
                ig.write_text(GITIGNORE, encoding="utf-8")
        return Repository(pdir)
    except Exception:
        return None


def _tree_of(repo, sha: str) -> str | None:
    """commit → tree（全在 objects 层，不做 rev 语法解析）。"""
    objects = _import()[0][1]
    try:
        return objects.parse_commit(objects.read_object(repo, sha)[1]).tree
    except Exception:
        return None


def _flatten(repo, tree: str | None) -> dict[str, str]:
    workdir = _import()[0][3]
    if not tree:
        return {}
    try:
        return workdir.flatten_tree(repo, tree)
    except Exception:
        return {}


def _signature(author: tuple[str, str], when: float | None = None) -> str:
    objects = _import()[0][1]
    tz = -(time.timezone if not time.daylight else time.altzone) // 60
    return objects.format_signature(author[0] or "agent", author[1] or "agent@local",
                                    when=int(when or time.time()), tz_minutes=tz)


def _prev_caps(repo, sha: str | None) -> list[str]:
    """上一版自己写的 ``Caps:`` 行 —— 历史就是记忆，不用另外存状态。"""
    if not sha:
        return []
    objects = _import()[0][1]
    try:
        msg = objects.parse_commit(objects.read_object(repo, sha)[1]).message
    except Exception:
        return []
    for line in msg.splitlines():
        if line.startswith("Caps:"):
            body = line[5:].strip()
            return [c.strip() for c in body.split(",") if c.strip() and c.strip() != "（无能力）"]
    return []


def commit_all(pdir: Path, message: str = "", *, author: tuple[str, str] = ("agent", "agent@local"),
               notes: dict[str, str] | None = None) -> tuple[bool, str]:
    """把插件此刻的工作树提交成一版。**永不抛**；无差异/失败都返回 False + 原因。

    ``message`` 为空时由**真实差异**推出来（改了哪些文件、能力增删），而不是让调用方
    编一句 —— 消息是给人看的，就不能靠猜。
    """
    ok, why = available()
    if not ok:
        return False, why
    pdir = Path(pdir)
    Repository, objects, refs, workdir = _import()[0]
    with _lock(pdir):
        try:
            repo = ensure(pdir)
            if repo is None:
                return False, "建仓失败"
            paths = tracked(pdir)
            if not paths:
                return False, "没有可入库的文件"

            head_sym, head_sha = refs.read_head(repo)
            head_tree = _tree_of(repo, head_sha) if head_sha else None
            before = _flatten(repo, head_tree)

            workdir.add_paths(repo, paths)               # 显式路径：不靠 ignore
            gone = sorted(set(before) - set(paths))
            if gone:
                workdir.rm_paths(repo, gone, cached=True)  # 只清索引，别删磁盘上的文件
            tree = workdir.write_tree(repo)

            if head_sha and tree == head_tree:
                return False, "nothing to commit"

            after = _flatten(repo, tree)
            changed = sorted(p for p in set(before) | set(after)
                             if before.get(p) != after.get(p))
            body = _compose(message, changed, before, after,
                            _prev_caps(repo, head_sha), notes or {})

            sig = _signature(author)
            commit = objects.Commit(tree=tree, parents=[head_sha] if head_sha else [],
                                    author=sig, committer=sig, message=body)
            sha = objects.write_object(repo, "commit", commit.encode())
            refs.update_ref(repo, head_sym or "refs/heads/main", sha, message=body)
            return True, sha
        except Exception as exc:                         # 版本化不许把写入拖下水
            return False, f"{type(exc).__name__}: {exc}"


def _compose(message: str, changed: list[str], before: dict, after: dict,
             prev_caps: list[str], notes: dict[str, str]) -> str:
    """拼提交消息：subject 由差异推出，body 给人看。"""
    first = not before
    caps_new = [c.strip() for c in (notes.get("Caps") or "").split(",") if c.strip()]
    subject = (message or "").strip().splitlines()[0] if message else ""

    if first:
        subject = "bootstrap: 接管既有版本"
    elif not subject:
        ui_only = all(p.startswith(TRACKED_DIR + "/") for p in changed)
        added = [p for p in changed if p not in before]
        removed = [p for p in changed if p not in after]
        gained = [c for c in caps_new if c not in prev_caps]
        lost = [c for c in prev_caps if c not in caps_new]
        if gained:
            subject = "feat: +" + ", ".join(gained[:4])
        elif lost:
            subject = "refactor: -" + ", ".join(lost[:4])
        elif ui_only:
            subject = f"format: 只动界面（{len(changed)} 个文件）"
        elif added or removed:
            bits = []
            if added:
                bits.append("+" + ", ".join(added[:3]))
            if removed:
                bits.append("-" + ", ".join(removed[:3]))
            subject = "edit: " + "; ".join(bits)
        else:
            subject = "edit: " + ", ".join(changed[:3])

    lines = [subject, "", "Files: " + (", ".join(changed[:12]) or "（无）")]
    if len(changed) > 12:
        lines[-1] += f" … 共 {len(changed)} 个"
    lines.append("Caps: " + (", ".join(caps_new) or "（无能力）"))
    for key in ("By", "Why", "Check"):
        val = (notes.get(key) or "").strip()
        if val:
            lines.append(f"{key}: {' '.join(val.split())[:300]}")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- 读历史

def _walk(repo, limit: int = MAX_CHAIN) -> list[tuple[str, object]]:
    """从 HEAD 沿 first-parent 往回走（最新在前）。"""
    objects, refs = _import()[0][1], _import()[0][2]
    _, sha = refs.read_head(repo)
    out: list[tuple[str, object]] = []
    while sha and len(out) < limit:
        try:
            commit = objects.parse_commit(objects.read_object(repo, sha)[1])
        except Exception:
            break
        out.append((sha, commit))
        sha = commit.parents[0] if commit.parents else None
    return out


def _epoch(commit) -> int:
    try:
        return int(commit.author.rsplit(" ", 2)[1])
    except Exception:
        return 0


def _changed_between(repo, old_tree: str | None, new_tree: str | None) -> dict[str, str]:
    """两棵树之间的变化：path → new / mod / del。"""
    before, after = _flatten(repo, old_tree), _flatten(repo, new_tree)
    out: dict[str, str] = {}
    for p in set(before) | set(after):
        if p not in before:
            out[p] = "new"
        elif p not in after:
            out[p] = "del"
        elif before[p] != after[p]:
            out[p] = "mod"
    return out


def versions(pdir: Path, limit: int = 20) -> list[dict]:
    """版本链（最新在前）。``ver`` 是**从第一版起数**的编号：v1 = 最早那版。"""
    ok, _ = available()
    if not ok:
        return []
    pdir = Path(pdir)
    if not (pdir / ".git").is_dir():
        return []
    Repository, objects, refs, workdir = _import()[0]
    try:
        repo = Repository(pdir)
        chain = _walk(repo)
    except Exception:
        return []
    total = len(chain)
    out: list[dict] = []
    for i, (sha, commit) in enumerate(chain[:max(1, limit)]):
        parent_tree = _tree_of(repo, chain[i + 1][0]) if i + 1 < len(chain) else None
        try:
            changed = _changed_between(repo, parent_tree, commit.tree)
        except Exception:
            changed = {}
        msg = commit.message or ""
        subject = msg.splitlines()[0] if msg.splitlines() else ""
        epoch = _epoch(commit)
        out.append({
            "sha": sha, "short": sha[:8], "ver": total - i,
            "msg": subject, "body": msg.strip(), "epoch": epoch,
            "when": time.strftime("%m-%d %H:%M", time.localtime(epoch)) if epoch else "",
            "author": (commit.author.rsplit(" <", 1)[0] if " <" in commit.author else commit.author),
            "files": changed, "count": len(changed),
        })
    return out


def resolve(pdir: Path, version: str) -> dict | None:
    """把 ``v3`` / 短号 / 全号解析成链上的一版。**白名单**：链上没有就是 None。

    不做任何 rev 语法解析（``HEAD~1`` / ``rev:path`` 在 pythongit 里全都不成立）。
    """
    key = str(version or "").strip().lower().lstrip("v")
    if not key:
        return None
    for v in versions(pdir, limit=MAX_CHAIN):
        if key == str(v["ver"]) or (len(key) >= 4 and v["sha"].startswith(key)):
            return v
    return None


def files_at(pdir: Path, sha: str) -> dict[str, str]:
    """某一版的文件 → blob 号。"""
    ok, _ = available()
    if not ok:
        return {}
    try:
        repo = _import()[0][0](Path(pdir))
        return _flatten(repo, _tree_of(repo, sha))
    except Exception:
        return {}


def read_at(pdir: Path, sha: str, path: str) -> bytes | None:
    """某一版的某个文件的**原始字节**。

    返回 bytes 不是风格问题，是契约：``revert`` 要把它按字节写回磁盘。
    decode 一次再 encode 回去，CRLF 会变 LF、GBK 文件会直接炸（§7.5）。
    """
    ok, _ = available()
    if not ok:
        return None
    rel = str(path or "").replace("\\", "/").lstrip("/")
    if not is_tracked_path(rel):
        return None
    try:
        repo = _import()[0][0](Path(pdir))
        files = _flatten(repo, _tree_of(repo, sha))
        blob = files.get(rel)
        if not blob:
            return None
        return _import()[0][1].read_object(repo, blob)[1]
    except Exception:
        return None


def diff_text(pdir: Path, sha: str, path: str, context: int = 3) -> str:
    """那一版 → 当前工作树 的行级 diff（给人和 agent 看）。"""
    rel = str(path or "").replace("\\", "/").lstrip("/")
    if not is_tracked_path(rel):
        return f"（{rel} 不在版本库里：只有 plugin.json / backend.py / ui/** 有历史）"
    old = read_at(pdir, sha, rel)
    cur_path = Path(pdir) / rel
    new = cur_path.read_bytes() if cur_path.is_file() else b""
    old_lines = (old or b"").decode("utf-8", errors="replace").splitlines()
    new_lines = new.decode("utf-8", errors="replace").splitlines()
    if old_lines == new_lines:
        return f"（{rel} 与那一版一模一样）"
    out = list(difflib.unified_diff(old_lines, new_lines,
                                    fromfile=f"{rel} @ {sha[:8]}", tofile=f"{rel} @ 工作树",
                                    lineterm="", n=context))
    return "\n".join(out)


def changed_since(pdir: Path, sha: str) -> dict[str, str]:
    """那一版 → 当前工作树，哪些跟踪文件变了（不读内容）。"""
    old = files_at(pdir, sha)
    now = {p: "" for p in tracked(Path(pdir))}
    out: dict[str, str] = {}
    for p in set(old) | set(now):
        if p not in old:
            out[p] = "new"
        elif p not in now:
            out[p] = "del"
        else:
            cur = Path(pdir) / p
            if not cur.is_file():
                out[p] = "del"
            elif read_at(pdir, sha, p) != cur.read_bytes():
                out[p] = "mod"
    return out


# ---------------------------------------------------------------- 回退

def revert_files(pdir: Path, version: str, paths: list[str] | None = None
                 ) -> tuple[dict[str, bytes] | None, list[str], str]:
    """把某一版的内容取出来，交给**写入路径**去落盘 + 四道校验。

    为什么不在这里落盘：四道校验 + 整份回滚只有一处（``toolbox._commit``），
    回退必须是它的**调用者**，不是绕过它。回退到一版坏代码会被拒，并说明原因。

    返回 ``(files, drops, err)``：``files`` 是 {路径: 字节}；``drops`` 是「那一版没有、
    现在却有」的跟踪文件（回退要把它们删掉，否则就不是「回到那一版」）。
    """
    if not Path(pdir).is_dir():
        return None, [], "插件目录不存在"
    v = resolve(Path(pdir), version)
    if v is None:
        return None, [], f"没有这一版：{version}（用 versions 看有哪些）"
    want = [str(p).replace("\\", "/").lstrip("/") for p in (paths or [])]
    if paths:
        for p in want:
            if not is_tracked_path(p):
                return None, [], f"{p} 不在版本库里（只有 plugin.json / backend.py / ui/**）"
        have = files_at(Path(pdir), v["sha"])
        missing = [p for p in want if p not in have]
        if missing:
            return None, [], f"那一版里没有：{', '.join(missing)}"
        targets = want
    else:
        targets = sorted(files_at(Path(pdir), v["sha"]))

    files: dict[str, bytes] = {}
    for p in targets:
        data = read_at(Path(pdir), v["sha"], p)
        if data is None:
            return None, [], f"读不出 {p} @ {v['short']}"
        files[p] = data
    if not files:
        return None, [], f"v{v['ver']} 里没有可跟踪的文件"

    drops = sorted(set(tracked(Path(pdir))) - set(files)) if not paths else []
    return files, drops, ""


# ---------------------------------------------------------------- 删除

def archive(pdir: Path, dest_dir: Path) -> Path | None:
    """删程序之前，先把 ``.git`` 挪走 —— 「删了想找回上一版」正是最需要历史的时候。"""
    git = Path(pdir) / ".git"
    if not git.is_dir():
        return None
    dest_dir.mkdir(parents=True, exist_ok=True)
    target = dest_dir / f"{Path(pdir).name}.git-{time.strftime('%Y%m%d-%H%M%S')}"
    try:
        shutil.move(str(git), str(target))
        return target
    except Exception:
        return None
