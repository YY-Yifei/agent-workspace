"""内核 —— 把「道」落到本机的**那一个**东西。

内核只有四个动词，除此别无其他：

    落盘        插件状态写进 plugins/<pid>/data/store.json（代码目录与数据目录分离）
    执行被点名的  ``Runtime.dispatch()`` 是全系统唯一执行入口
    广播        事件总线：谁做了什么、谁点的，一路流给所有前端
    传话        由 link 与 server 完成；内核只提供形状，不产生内容

**内核不含一克智能。** 它不选工具、不决定下一步、不重试、不兜底。
任何「模型调了 A，本地接着做 B」都是把智能从道里偷回来 —— 那会让链条断在「一」。
判据：本文件里出现的每个分支，都只回答「参数/状态是否合法」，不回答「接下来做什么」。

谱系（docs/vision.md §四）：
    道 = Agent（aaas-service，唯一智能，不在本进程）
    一 = 内核（本文件：一个目录约定、一条 dispatch、一条事件流、一份能力面）
    二 = 双入口（人走 HTTP，agent 走 invokeFunction，落到同一个 dispatch）
    三 = 插件三要素（backend.py 能力 / ui 页面 / data 状态）
    万物 = 工作区（用户攒出来的程序）

契约五条（docs/plugin-mechanism.md）：
    1. 公开函数 = 能力；``_`` 前缀 = 私有；wire name = ``<pid>_<fn>``
    2. 约束只写一处：``Annotated[int, {"minimum": 1}]`` → schema / 表单 / 校验同源
    3. 双面返回：dict 含 "text" → text 给模型，其余进 data 给界面
    4. 状态走 ctx.get/set → data/store.json
    5. 事件总线：call.ok / call.error / call.rejected / state.changed
"""

from __future__ import annotations

import enum
import importlib.util
import inspect
import json
import os
import queue
import re
import shutil
import threading
import time
import traceback
import types as _types
import typing
from dataclasses import dataclass, field
from pathlib import Path

PRIM = {str: "string", int: "integer", float: "number", bool: "boolean"}
_UNSET = object()          # 「这个默认值进不了 JSON，别写进 schema」


def _json_safe(value):
    """默认值 → 能进 JSON 的形态；转不了就 ``_UNSET``（调用方**省略**这个键）。

    为什么必须在这一层做：``parameters`` 最终要过 ``aaas.wire.dumps`` ——
    那是一次**不带** ``default=str`` 的 ``json.dumps``。一个 ``Path`` / ``Enum``
    默认值会让**整帧**序列化失败，而声明是全量的：这个程序的所有能力一条都
    声明不上去。现场离写注解的人隔了两层 —— 插件作者只写了 ``p: Path = Path(".")``。

    能转的就转，不丢信息（``Path(".")`` → ``"."``；枚举 → 它的值）；真转不了的
    才省略：写一个错的值进 schema，比不写更坏 —— 模型会照着它传参。
    """
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, enum.Enum):
        return _json_safe(value.value)
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (list, tuple)):
        out = [_json_safe(v) for v in value]
        return _UNSET if any(x is _UNSET for x in out) else out
    if isinstance(value, (set, frozenset)):
        return _json_safe(sorted(value, key=str))
    if isinstance(value, dict):
        out: dict = {}
        for k, v in value.items():
            sv = _json_safe(v)
            if sv is _UNSET:
                return _UNSET
            out[str(k)] = sv
        return out
    return _UNSET


WIRE_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")   # aaas §3.2 function 名约束
MAX_EVENT_LOG = 500
# 审计文件的**上界**。一个只 append 不轮转的日志会一直长 —— 真实工作区里
# events.jsonl 已经到 48MB，打开它的人要读 48MB 才看得到今天发生了什么。
LOG_MAX_BYTES = 5 * 1024 * 1024      # 单个文件 5MB
LOG_KEEP = 2                         # 保留 .1 / .2 两份（合计上限 ≈ 15MB）
DEV_PID = "dev"      # 开发者工具集的 wire 前缀。它**不是程序**：不进左侧栏、没有界面

# 不能当插件目录名的两个词。为什么必须在**装载处**拦，而不只在提示词里劝：
# 能力面按 wire name（``<pid>_<fn>``）索引，而 ``assistant`` 是内置程序的 pid、
# ``dev`` 是开发者工具集的 wire 前缀 —— 一个同名插件目录就能让「声明给模型的
# 那份」和「真正被执行的那份」指向两个不同的函数。让这个局面从源头不可能出现，
# 比在 dispatch 里猜「谁该赢」便宜得多。
RESERVED_PIDS = {"assistant", DEV_PID}


def reserved_reason(pid: str) -> str:
    """保留名的一句话理由（装载处、静态校验处共用同一句）。"""
    what = "内置程序小助理的 pid" if pid == "assistant" else "开发者工具集的 wire 前缀"
    return f"{pid!r} 是保留名（{what}）：换个目录名，否则声明与执行会指向两个不同的函数"


# --------------------------------------------------------------------- schema

def _schema_of(tp) -> dict:
    """类型注解 → JSON Schema 片段。``Annotated`` 里的 dict 直接并入。"""
    if tp is inspect.Parameter.empty or tp is None:
        return {}
    meta: dict = {}
    if typing.get_origin(tp) is typing.Annotated:
        args = typing.get_args(tp)
        tp, *rest = args
        for r in rest:
            if isinstance(r, dict):
                meta.update(r)
    if isinstance(tp, str):                                  # 字符串注解（未 resolve）
        tp = {"str": str, "int": int, "float": float, "bool": bool}.get(tp, tp)
        if isinstance(tp, str):
            return meta
    origin, args = typing.get_origin(tp), typing.get_args(tp)
    # **两种**联合：``typing.Optional[X]`` 的 origin 是 ``typing.Union``，
    # 而 PEP 604 的 ``X | None`` 是 ``types.UnionType`` —— 两个不同的对象。
    # 只认前者，``x: int | None`` 就掉到末尾那条 ``return meta``：属性里**连 type
    # 都没有**，模型不知道传什么，``validate()`` 也因为 want 为 None 直接跳过。
    # 而 ``X | None`` 正是模型/人写新函数时最常见的写法 —— 这类缺陷专伤自造工具。
    if origin is typing.Union or origin is _types.UnionType:
        rest = [a for a in args if a is not type(None)]
        if not rest:                                   # Optional[None] 这种退化写法
            return dict(meta)
        if len(rest) == 1:
            return {**_schema_of(rest[0]), **meta}
        # 多分支：anyOf 是协议里那枚校验器支持的关键字（aaas/_jsonschema），
        # 不会让声明被拒；单分支已在上面化开，绝不产出 anyOf 套 anyOf。
        return {**{"anyOf": [_schema_of(a) for a in rest]}, **meta}
    if origin is typing.Literal:
        out: dict = {"enum": list(args)}
        kinds = {PRIM[type(a)] for a in args if type(a) in PRIM}
        if len(kinds) == 1:                            # 同质字面量：把 type 也说出来
            out = {"type": kinds.pop(), **out}
        return {**out, **meta}
    if origin in (list, set, tuple):
        return {**{"type": "array", "items": _schema_of(args[0]) if args else {}}, **meta}
    if origin is dict:
        return {**{"type": "object"}, **meta}
    # 裸容器（``files: dict``）：``get_origin(dict)`` 是 None，上一条分支接不住，
    # 于是也会掉到末尾 —— 真实例子就是 assistant_plugin_write 的 files 参数。
    if tp in (dict,):
        return {**{"type": "object"}, **meta}
    if tp in (list, set, frozenset, tuple):
        return {**{"type": "array"}, **meta}
    if tp in PRIM:
        return {**{"type": PRIM[tp]}, **meta}
    return meta


def _arg_docs(doc: str | None) -> dict[str, str]:
    """docstring 里的 ``name: 说明`` → {name: 说明}（就地重用，不引额外解析库）。"""
    out: dict[str, str] = {}
    for ln in (doc or "").splitlines():
        m = re.match(r"^\s*(\w+)\s*(?:\([^)]*\))?\s*[:：]\s*(.+)$", ln)
        if m:
            out[m.group(1)] = m.group(2).strip()
    return out


def build_schema(fn) -> dict:
    """函数 → JSON Schema（模型看到的、界面表单用的、dispatch 校验用的，同一份）。"""
    sig = inspect.signature(fn)
    try:
        hints = typing.get_type_hints(fn, include_extras=True)
    except Exception:                                        # 未定义的前向引用等
        hints = {}
    docs = _arg_docs(inspect.getdoc(fn))
    props, required = {}, []
    for name, p in sig.parameters.items():
        if name in ("self", "cls", "ctx"):
            continue
        sch = _schema_of(hints.get(name, p.annotation))
        if name in docs:
            sch["description"] = docs[name]
        if p.default is not inspect.Parameter.empty and "default" not in sch:
            safe = _json_safe(p.default)
            if safe is not _UNSET:                     # 转不了就**不写**这一格
                sch["default"] = safe
        props[name] = sch
        if p.default is inspect.Parameter.empty:
            required.append(name)
    out: dict = {"type": "object", "properties": props, "additionalProperties": False}
    if required:
        out["required"] = required
    return out


def validate(schema: dict, args) -> str | None:
    """最小校验器：只覆盖我们自己生成的 schema 形状。返回错误说明，或 None 表示通过。"""
    if not isinstance(args, dict):
        return "参数必须是 JSON 对象"
    props = schema.get("properties", {})
    for r in schema.get("required", []):
        if r not in args or args[r] is None:
            return f"缺少必需参数 {r}"
    for k, v in args.items():
        if k not in props:
            return f"未知参数 {k}（不接受未声明的字段）"
        spec = props[k] or {}
        if spec.get("anyOf"):                          # 多分支联合：任一支通过即可
            branches = [b for b in spec["anyOf"] if isinstance(b, dict)]
            if branches and not any(_matches(b, v) for b in branches):
                kinds = " | ".join(str(b.get("type") or "any") for b in branches)
                return f"{k} 应为 {kinds} 之一，收到 {type(v).__name__}"
            continue                                   # 联合已判完，别再按 type 走
        want = spec.get("type")
        if want and v is None:
            continue
        if want == "integer" and (not isinstance(v, int) or isinstance(v, bool)):
            return f"{k} 应为整数，收到 {type(v).__name__}"
        if want == "number" and (not isinstance(v, (int, float)) or isinstance(v, bool)):
            return f"{k} 应为数字，收到 {type(v).__name__}"
        if want == "string" and not isinstance(v, str):
            return f"{k} 应为字符串，收到 {type(v).__name__}"
        if want == "boolean" and not isinstance(v, bool):
            return f"{k} 应为布尔值，收到 {type(v).__name__}"
        if want == "array" and not isinstance(v, list):
            return f"{k} 应为数组，收到 {type(v).__name__}"
        if want == "object" and not isinstance(v, dict):
            return f"{k} 应为对象，收到 {type(v).__name__}"
        if "enum" in spec and v not in spec["enum"]:
            return f"{k} 只能取 {spec['enum']}，收到 {v!r}"
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            lo, hi = spec.get("minimum"), spec.get("maximum")
            if lo is not None and v < lo:
                return f"{k} 不能小于 {lo}，收到 {v}"
            if hi is not None and v > hi:
                return f"{k} 不能大于 {hi}，收到 {v}"
    return None


def _matches(spec: dict, value) -> bool:
    """值是否满足一条 schema 片段（只给 anyOf 的分支判定用）。"""
    return validate({"type": "object", "properties": {"x": spec}}, {"x": value}) is None


# --------------------------------------------------------------------- 数据

@dataclass
class Result:
    """一次执行的结局。三态，别糊成一个 isError —— 糊了模型就会对着格式错误反复重试。"""

    status: str                       # ok | rejected | error
    text: str = ""
    data: dict = field(default_factory=dict)
    reason: str | None = None
    elapsed_ms: int = 0

    def as_dict(self) -> dict:
        return {"status": self.status, "text": self.text, "data": self.data,
                "reason": self.reason, "elapsed_ms": self.elapsed_ms}


@dataclass
class Capability:
    pid: str
    fn: str
    wire: str
    description: str
    parameters: dict
    timeout_ms: int = 15_000
    handler: typing.Callable = None

    def as_decl(self) -> dict:
        return {"name": self.wire, "description": self.description,
                "parameters": self.parameters, "timeoutMs": self.timeout_ms,
                "metadata": {"plugin": self.pid, "fn": self.fn}}


class Context:
    """注入进插件模块的宿主服务。极简：store + emit + 元信息。"""

    def __init__(self, runtime: "Runtime", plugin: "Plugin") -> None:
        self._rt = runtime
        self._pid = plugin.pid

    # -- 状态：落盘，且**代码目录与数据目录分离**（agent 改代码不动用户数据）
    @property
    def _store(self) -> Path:
        return self._rt.plugins_dir / self._pid / "data" / "store.json"

    def _read(self) -> dict:
        try:
            return json.loads(self._store.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _store_lock(self) -> threading.Lock:
        """这个程序的数据文件锁（唯一一把，按 pid）。"""
        with self._rt._lock:
            lk = self._rt._store_locks.get(self._pid)
            if lk is None:
                lk = self._rt._store_locks[self._pid] = threading.Lock()
            return lk

    def get(self, key: str, default=None):
        # 读也要进锁，不只是为了「读到一致的整份」：Windows 上 os.replace 撞上
        # 目标文件正被别的线程打开时会直接 PermissionError —— 读侧不加锁，写侧就
        # 会随机失败。POSIX 上原子替换本来就保证读到的不会半截，但代价一样地小。
        with self._store_lock():
            return self._read().get(key, default)

    def set(self, key: str, value) -> None:
        # 插件同时活在三条路径上：_SerialExecutor 线程（agent 的工具调用）、
        # HTTP 线程（人在界面上点）、原生窗口进程（走 HTTP 回来）。所以
        # 读-改-写必须是**一次临界区**，否则后写的那份把先写的那份整体覆盖。
        #
        # 临时文件名也必须唯一：写死 store.tmp 时两个线程在同一条路径上互踩
        # os.replace —— Windows 抛 PermissionError，POSIX 静默丢更新。
        p = self._store
        with self._store_lock():
            data = self._read()
            data[key] = value
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.parent / f".{p.name}.{os.getpid()}.{threading.get_ident()}.tmp"
            try:
                tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                               encoding="utf-8")
                tmp.replace(p)                             # 原子替换：半截文件不该被读到
            finally:
                try:
                    tmp.unlink()                           # 替换成功后它已不存在；失败时别留垃圾
                except OSError:
                    pass

    # -- 广播：插件自己也能喊一声
    def emit(self, kind: str, payload: dict | None = None) -> None:
        self._rt.publish(kind, pid=self._pid, data=payload or {}, source="plugin")


class Plugin:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.pid = path.name
        # 插件目录的绝对形态，只算一次：Plugin.ui 每次取值都要拿它做越界判断，
        # 而 as_dict() 每个程序每份 state 都会走一遍 ui。
        self._root = path.resolve()
        self.manifest: dict = {}
        self.caps: dict[str, Capability] = {}
        self.module = None
        self.error: str | None = None

    @property
    def ui(self) -> Path | None:
        """这个程序的界面入口文件（网页或原生窗口脚本）。

        ``ui`` 是**插件自己写的 json 字段**，不可信：写成 ``../../config.json``
        时它必须变成「没有界面」，而不是把宿主上任意一个文件交给页面。界一越，
        ``hasUi`` / ``uiKind`` / ``/plugin/<pid>/`` 读到的就是同一个「没有」——
        不会出现「清单说有页面，点开是 403」那种自相矛盾。
        """
        p = (self.path / (self.manifest.get("ui") or "ui/index.html")).resolve()
        if not p.is_relative_to(self._root):
            return None
        return p if p.is_file() else None

    @property
    def ui_kind(self) -> str:
        """``web`` | ``native`` | ``none`` —— 界面长在哪儿，全系统只有这一处判据。

        ``.py`` 是原生窗口：它跑在自己的进程里，宿主只能拉起它，不能把它塞进
        webview（两套事件循环不能共处一线程）。其余是网页，活在宿主窗口里。
        """
        ui = self.ui
        if ui is None:
            return "none"
        return "native" if ui.suffix.lower() == ".py" else "web"

    @property
    def data_dir(self) -> Path:
        return self.path / "data"

    @property
    def title(self) -> str:
        return self.manifest.get("title") or self.pid

    def as_dict(self) -> dict:
        return {"id": self.pid, "title": self.title,
                "description": self.manifest.get("description", ""),
                "icon": self.manifest.get("icon") or "",
                "hasUi": self.ui is not None, "uiKind": self.ui_kind,
                "uiName": (self.manifest.get("ui") or "ui/index.html") if self.ui else "",
                "error": self.error,
                "functions": [{"name": c.wire, "fn": c.fn,
                               "description": c.description,
                               "parameters": c.parameters} for c in self.caps.values()]}


class BuiltinProgram:
    """内核自带的一个程序 —— 不来自 plugins/ 目录。目前只有小助理。

    它和插件在**所有对外的地方长得一样**（state 里一项、能力面一份、走同一个
    dispatch），差别只有两处：没有目录（所以没有 ui、没有 data），代码在内核这一侧。
    """

    def __init__(self, pid: str, meta: dict) -> None:
        self.pid = pid
        self.meta = dict(meta or {})
        self.caps: dict[str, Capability] = {}
        self.error: str | None = None

    @property
    def title(self) -> str:
        return self.meta.get("title") or self.pid

    def as_dict(self) -> dict:
        return {"id": self.pid, "title": self.title,
                "description": self.meta.get("description", ""),
                "icon": self.meta.get("icon") or "",
                "hasUi": False, "uiKind": "none", "uiName": "",
                "builtin": True, "error": self.error,
                "functions": [{"name": c.wire, "fn": c.fn,
                               "description": c.description,
                               "parameters": c.parameters} for c in self.caps.values()]}


# --------------------------------------------------------------------- 运行时

class _Sub(queue.Queue):
    """一条订阅者的队列。``dropped`` 记着**它自己**丢过多少条。

    为什么要这个类：``publish`` 在队列满时丢最老的、保最新的（见 ``_offer``），
    而「丢了几条」如果只活在总线上，就永远说不清是哪个订阅者慢 —— 那又是静默。
    """

    def __init__(self, maxsize: int = 0) -> None:
        super().__init__(maxsize)
        self.dropped = 0


class Runtime:
    """内核本体：扫描插件、生成能力面、唯一 dispatch、广播事件。

    线程模型：扫描与 dispatch 都可能来自 HTTP 线程或 aaas 事件循环线程；
    状态只在磁盘上，因此这里唯一的共享物是事件订阅者列表 —— 一把锁保护它。
    """

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.plugins_dir = self.root / "plugins"
        self.plugins: dict[str, Plugin] = {}
        self.builtins: dict[tuple[str, str], Capability] = {}   # (pid, fn) → 内核自带的能力
        self.programs: dict[str, BuiltinProgram] = {}      # 内置程序（小助理）
        # 开发者工具集（fn → 能力）。**不属于任何程序**：只在某个程序的开发者模式
        # 打开时，临时并进那个程序的 capabilities_of（见 devtools.py）。
        self.devtools: dict[str, Capability] = {}
        self.version = 0                                   # 能力面版本：变了就该重新 declare
        self._subs: list[queue.Queue] = []
        self._dropped = 0                                  # 总线一共丢过多少条事件
        self._lock = threading.Lock()
        self.events: list[dict] = []                       # 最近事件（新前端进场先补一份）
        self.log_path = self.root / "logs" / "events.jsonl"
        # 轮转与写入必须共用**同一把锁**：换文件那一刻不能有人在写，否则行会写进
        # 一个已经被 rename 走的文件（谁也不知道它去哪了），或者写出半个事件。
        # log_max / log_keep 做成实例属性而不是直接用常量：门要能用小阈值验证
        # 「真的会轮转」，而不是靠读源码相信它。
        self.log_max = LOG_MAX_BYTES
        self.log_keep = LOG_KEEP
        self._log_lock = threading.Lock()
        self._recover_staging()   # 上次崩在轮转中途 → 先把那段审计并回来，别当垃圾
        try:
            self._log_size = self.log_path.stat().st_size     # 接着上次的大小算
        except OSError:
            self._log_size = 0
        # 开发者模式开关。**故意不放在插件目录里**：一个开了开发者模式的程序
        # 能改自己的文件，如果开关跟着它一起被改坏，人就再也没法把它关掉。
        self.dev_path = self.root / "dev.json"
        self._callers = threading.local()                  # 一次 dispatch 的调用者栈
        # 每个程序一把数据文件锁（pid → Lock）。**按程序**而不是全局：两个程序
        # 各写各的 store.json 本来互不相干，一把全局锁会让「甲在写」变成「乙也得等」。
        self._store_locks: dict[str, threading.Lock] = {}
        self._rejected: dict[str, str] = {}                # 目录名 → 被拒的理由

    # -- 事件 ---------------------------------------------------------
    def subscribe(self) -> queue.Queue:
        q: queue.Queue = _Sub(1000)
        with self._lock:
            self._subs.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self._subs:
                self._subs.remove(q)

    def publish(self, kind: str, *, pid: str = "", fn: str = "", source: str = "system",
                data: dict | None = None) -> None:
        ev = {"type": kind, "pid": pid, "fn": fn, "source": source,
              "ts": time.time(), "data": data or {}}
        self._audit(ev)
        with self._lock:
            subs = list(self._subs)
        for q in subs:
            self._offer(q, ev)

    def _audit(self, ev: dict) -> None:
        """落审计：内存里最近 MAX_EVENT_LOG 条 + events.jsonl。**不扇出**。

        单独拆出来是给「总线自己」用的：``_offer`` 丢事件时要留痕，而那条痕不能再走
        publish —— 那会递归回一个已经满了的队列。审计是给人事后查的，不是给界面画的。
        """
        self.events.append(ev)
        if len(self.events) > MAX_EVENT_LOG:
            del self.events[: len(self.events) - MAX_EVENT_LOG]
        try:                                               # 审计：谁点的、做了什么
            # default=str：事件里可能带 Path 之类的非 JSON 值。审计写不进去
            # 只该丢日志 —— 而 TypeError 不是 OSError，它会在 handler 成功之后
            # 冲出 publish，让调用方拿不到 Result。
            line = json.dumps(ev, ensure_ascii=False, default=str) + "\n"
            data = line.encode("utf-8")
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self._log_lock:
                if self._log_size > 0 and self._log_size + len(data) > self.log_max:
                    self._rotate_log()
                # **二进制追加**，不用 text 模式：Windows 上 text 会把 "\n" 翻成
                # "\r\n"，写进去的字节比 len(data) 多一行一个 —— 而 _log_size 是按
                # len(data) 累加的，于是它从此一直偏小（实测每行差 1 字节），
                # 阈值判断跟着失真：行数越多偏得越多，上界变成「差不多」。
                with self.log_path.open("ab") as fh:
                    fh.write(data)
                self._log_size += len(data)
        except (OSError, TypeError, ValueError):
            pass

    def _staging_path(self) -> Path:
        """轮转的中转站：当前文件先去这里，归档链走完了才改名成 ``.1``。"""
        return self.log_path.parent / (self.log_path.name + ".staging")

    def _log_bytes(self) -> int:
        """当前文件的真实字节数（在不在都给一个数）。"""
        try:
            return self.log_path.stat().st_size
        except OSError:
            return 0

    def _recover_staging(self) -> bool:
        """收拾上一次轮转崩在中途留下的现场。返回**是否收拾干净**。

        崩在「当前文件已经挪开、归档链还没走完」之间时，事件全在那份 .staging
        里。把它当垃圾删掉就是**悄悄吃掉一段审计** —— 宁可这一次超阈值，也不丢。

        返回值不是装饰：收拾不干净时上层**不许继续轮转** —— 否则 cur→.staging
        这一步会把那段还没并回来的审计直接覆盖掉（「为了不丢」丢在这）。

        两条已知的粗糙处（都是低危，记在这里而不是假装没有）：
          * 并回去是**追加到当前文件末尾**，所以顺序不是严格按时间 —— 崩溃之后
            同一份审计里会有两段各自有序的内容。读的人应当按每条事件自己的 ``ts``
            排序，而不是按文件行序。
          * copy 与 unlink 不是原子的：若在两者之间崩掉，下次恢复会把同一段
            再并一遍，那几行会重复。宁可重复，不可丢失。
        """
        stage = self._staging_path()
        try:
            if not stage.exists():
                return True
            if not self.log_path.exists():
                stage.replace(self.log_path)
                return True
            with stage.open("rb") as src, self.log_path.open("ab") as dst:
                shutil.copyfileobj(src, dst)
            stage.unlink()
            return True
        except OSError:
            return False

    def _rotate_log(self) -> None:
        """把 events.jsonl 顶掉一格：``.N`` → ``.N+1``（最老的删掉），当前 → ``.1``。

        调用者必须持有 ``_log_lock``。只保留 ``log_keep`` 份：审计是给人查
        「最近发生了什么」的，不是归档 —— 要么这里给上界，要么这个文件没有上界。

        **顺序是有意的**：先把当前文件挪到 .staging，成了之后才动归档。
        旧实现反着来（先删 .2 → 再把 .1 顶成 .2 → 最后才挪当前文件），于是当前
        文件挪不动的时候（Windows 上被别的进程打开着 → WinError 32），归档已经
        被吃掉：第一次失败丢 .2，第二次失败连 .1 也没了 —— 实测**两次就把两份
        归档清空**，而当前文件一个字节没动。审计是故障之后唯一能回查的东西，
        偏偏它最容易死在故障上。
        """
        logs = self.log_path.parent
        stage = self._staging_path()
        if not self._recover_staging():
            # 上一次崩在中途的那段还没并回来：此刻轮转 = cur→.staging 把它覆盖掉。
            self._log_size = self._log_bytes()
            return
        try:
            self.log_path.replace(stage)        # ① 先把自己挪开
        except OSError:
            self._log_size = self._log_bytes()  # 挪不动 = 本次不轮转，归档一根手指都不碰
            return
        try:                                    # ② 自己安全了，才有资格动归档
            # **这里不先删 .log_keep**：下面第一次 replace（n = log_keep-1 → log_keep）
            # 就会把它覆盖掉。多一次 unlink 只是多开一个「删了还没挪」的窗口 ——
            # .1 挪不动时（被别的进程打开着 → WinError 32）那一步已经白删了一代归档，
            # 恢复又把当前文件放回来，等于这次轮转没发生、却少了一份归档。
            for n in range(self.log_keep - 1, 0, -1):
                src = logs / f"{self.log_path.name}.{n}"
                if src.exists():
                    src.replace(logs / f"{self.log_path.name}.{n + 1}")
            stage.replace(logs / f"{self.log_path.name}.1")
        except OSError:
            try:                                # 归档链走不下去：把当前文件扶回去
                stage.replace(self.log_path)
            except OSError:
                pass                            # 双重失败：内容还在 .staging，下次进来先收拾
            self._log_size = self._log_bytes()
            return
        self._log_size = 0

    def _offer(self, q: queue.Queue, ev: dict) -> None:
        """投一条事件。队列满时**丢最老的、留最新的**，并且把丢了几条记下来。

        为什么保最新：界面要的是「现在什么样」，不是「三分钟前什么样」。中间丢掉的
        帧由 ``turn.completed``（带全文）兜底，所以丢老的最不伤。旧实现在 Full 处直接
        ``pass`` —— 少画几帧、少几条状态，而没有任何一处说得出来。
        """
        try:
            q.put_nowait(ev)
            return
        except queue.Full:
            pass
        try:
            q.get_nowait()                                   # 丢最老的那一条
        except queue.Empty:
            pass                                             # 消费者刚好腾了一格
        try:
            q.put_nowait(ev)
        except queue.Full:
            pass                                             # 塞不进也算丢（下面记账）
        first = getattr(q, "dropped", 0) == 0
        try:
            q.dropped += 1
        except AttributeError:                               # 不是 _Sub：只记总线总数
            pass
        with self._lock:
            self._dropped += 1
            total = self._dropped
            subs = len(self._subs)
        # 留痕：第一条 + 每 100 条。每条都记会把 events.jsonl 自己撑爆；
        # 一条不记就等于没发生。
        if first or total % 100 == 0:
            self._audit({"type": "bus.dropped", "pid": "", "fn": "", "source": "system",
                         "ts": time.time(),
                         "data": {"dropped": 1, "total": total, "subscribers": subs,
                                  "note": "队列满：已丢最老的一条，保住最新的"}})

    # -- 扫描与装载 ---------------------------------------------------
    def scan(self) -> str:
        """重扫插件目录。返回一行人话。**不思考，只落盘。**"""
        changed = False
        seen: set[str] = set()
        # **不原地改** self.plugins：扫描可能在 HTTP 线程里跑，而能力面 / state()
        # 在别的线程上遍历它。原地增删会让读侧抛
        # RuntimeError: dictionary changed size during iteration，
        # 或者拿到半新半旧的能力面（version 与实际能力不一致）。
        # 先在副本上做完，最后整体换掉 —— 读侧要么看到旧的整份，要么看到新的整份。
        plugins = dict(self.plugins)
        # wire 重名必须在装载时判（理由见 _collect_caps）。注意 self.plugins
        # 在整轮扫描里是不动的（要整体换掉），所以「本次扫描里先装好的那个」
        # 只能从这张局部表里看见 —— 少了它，两个新插件互相撞名谁都判不出来。
        taken: dict[str, Capability] = {c.wire: c for c in self.builtins.values()}
        for q in plugins.values():
            for c in q.caps.values():
                taken[c.wire] = c
        rejected: dict[str, str] = {}
        if self.plugins_dir.is_dir():
            for d in sorted(self.plugins_dir.iterdir()):
                if not d.is_dir() or d.name.startswith(("_", ".")):
                    continue
                if d.name in RESERVED_PIDS:
                    # 不装载，也不放进 self.plugins：program() 先查 plugins，
                    # 放进去就等于把内置程序劫持成了这个目录。
                    rejected[d.name] = reserved_reason(d.name)
                    if self._rejected.get(d.name) != rejected[d.name]:
                        self.publish("plugin.error", pid=d.name,
                                     data={"error": rejected[d.name]})
                    continue
                seen.add(d.name)
                old = plugins.get(d.name)
                stamp = self._stamp(d)
                if old is not None and getattr(old, "_stamp", None) == stamp:
                    continue
                p = Plugin(d)
                self._load(p, taken)
                p._stamp = stamp                            # type: ignore[attr-defined]
                plugins[d.name] = p
                taken.update({c.wire: c for c in p.caps.values()})
                changed = True
                self.publish("plugin.loaded", pid=d.name,
                             data={"title": p.title, "functions": [c.wire for c in p.caps.values()],
                                   "error": p.error})
        for gone in [k for k in plugins if k not in seen]:
            del plugins[gone]
            changed = True
            self.publish("plugin.removed", pid=gone)
        self.plugins = plugins                      # 整体换掉：读侧永远看不到中间态
        self._rejected = rejected
        if changed:
            self.version += 1
        return f"插件 {len(self.plugins)} 个，能力 {len(self.capabilities())} 项，版本 v{self.version}"

    @staticmethod
    def _stamp(d: Path) -> tuple:
        files = sorted(d.glob("backend.py")) + sorted(d.glob("plugin.json"))
        return tuple((f.name, f.stat().st_mtime_ns) for f in files if f.exists())

    def _load(self, p: Plugin, taken: dict[str, Capability] | None = None) -> None:
        if p.pid in RESERVED_PIDS:
            p.error = reserved_reason(p.pid)
            self.publish("plugin.error", pid=p.pid, data={"error": p.error})
            return
        backend = p.path / "backend.py"
        try:
            p.manifest = json.loads((p.path / "plugin.json").read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            p.error = f"plugin.json 读取失败：{exc}"
            return
        if not backend.is_file():
            p.error = "没有 backend.py"
            return
        name = f"aw_plugin_{p.pid}"
        try:
            spec = importlib.util.spec_from_file_location(name, backend)
            mod = importlib.util.module_from_spec(spec)      # type: ignore[arg-type]
            mod.__dict__["ctx"] = Context(self, p)
            spec.loader.exec_module(mod)                     # type: ignore[union-attr]
        except Exception as exc:                             # 插件炸了不许带崩宿主
            p.error = f"{type(exc).__name__}: {exc}"
            self.publish("plugin.error", pid=p.pid, data={"error": p.error})
            return
        p.module = mod
        p.caps, problems = self._collect_caps(
            mod, p.pid, int(p.manifest.get("timeoutMs", 15_000)), taken)
        if problems:
            p.error = ((p.error + "；") if p.error else "") + "；".join(problems)
            # 与保留名那条同形：拒收要发事件，而不是只在 p.error 里留一句
            # 等着谁翻 plugin.loaded 的 data。人看不见的错误等于没发生。
            self.publish("plugin.error", pid=p.pid, data={"error": p.error})

    # -- 能力面 -------------------------------------------------------
    def _registry(self) -> dict[str, Capability]:
        """**全系统唯一的能力表**：wire name → 能力（内置 + 插件 + 开发者工具集）。

        为什么必须有它：以前有两份，而且优先级方向相反 —— ``capabilities()`` 用
        dict 覆盖（插件盖内置），``dispatch()`` 用短路（内置 → devtools → 插件）。
        于是「声明给模型的能力」与「真正被执行的那份」会分叉：插件目录一旦叫
        assistant，模型看到的 docstring 来自插件、跑起来的函数来自内置。

        现在次序只在这里定**一次**：内置 → 插件覆盖 → 开发者工具集。声明
        （``capabilities`` / ``capabilities_of``）与执行（``dispatch``）都从它派生。
        保留名在装载处就被拒，所以「谁该赢」这个问题不再需要回答。
        """
        out: dict[str, Capability] = {c.wire: c for c in self.builtins.values()}
        for p in self.plugins.values():
            for c in p.caps.values():
                out[c.wire] = c
        for c in self.devtools.values():
            out[c.wire] = c
        return out

    def capabilities(self) -> dict[str, Capability]:
        """全部能力 = 内置程序 + 所有插件。**这是模型和人都看到的同一张表。**

        开发者工具集**不在其中**：它不是程序、平时不借给任何人，只在
        ``capabilities_of`` 里对一个开了开发者模式的程序临时出现。
        """
        return {w: c for w, c in self._registry().items() if c.pid != DEV_PID}

    def capabilities_of(self, pid: str) -> dict[str, Capability]:
        """**一个程序的智能体只能看见自己那部分能力。**

        这是全系统「上下文隔离」的唯一实现处，而不是一条写在提示词里的规矩：
        小助理的上下文里是「手」，pomodoro 的上下文里只有 pomodoro_*。
        别处不许再按 pid 过滤 —— 想改隔离程度，只改这里。

        唯一的例外也是在这里给的：人给某个程序打开了**开发者模式**
        （``dev.json``），它就能额外看见**开发者工具集**（``dev_*``）——
        看/写文件 + 改自己，仅此。不是小助理那双手：那里有 plugin_list、
        plugin_call、plugin_delete、shell，一个都不给（见 devtools.py）。
        开关本身是人给的，所以这不是「隔离失效」，是「人临时把手借出去」；
        关掉立刻收回（``dispatch`` 里还有一次复核，不等下一轮重声明）。
        """
        own = {w: c for w, c in self._registry().items()
               if c.pid == pid and c.pid != DEV_PID}
        if pid in self.plugins and self.dev_mode(pid):
            own.update({c.wire: c for c in self.devtools.values()})
        return own

    # -- 开发者模式 ---------------------------------------------------
    def dev_mode(self, pid: str) -> bool:
        """这个程序的助手现在是否开着开发者模式。

        磁盘即状态：每次问都重新读 ``workspace/dev.json`` —— 人手改了它、
        另一个进程改了它，下一步就生效，不需要谁去通知谁。
        """
        try:
            data = json.loads(self.dev_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        return bool(isinstance(data, dict) and data.get(pid))

    def set_dev(self, pid: str, on: bool) -> str:
        """打开/关闭一个程序的开发者模式。返回一句人话。"""
        if pid in self.programs:
            return f"「{self.programs[pid].title}」本来就有全部能力，不需要这个开关"
        if pid not in self.plugins:
            return f"没有程序 {pid}"
        try:
            data = json.loads(self.dev_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        if on:
            data[pid] = True
        else:
            # dev.json 只记「**开着的**」：关掉就把条目删了。
            # 为什么不是写成 false：删掉一个程序之后，那个条目没有任何东西会再清理它
            # —— 它会以 "habit": false 的形状永远留在文件里（0.3.0 的工作区里就有一枚
            # "habit": true，而 habit 这个程序早删了）。一个说不清来历的条目，比没有
            # 条目更坏：下一次同名插件出现时，人是照着它做判断的。
            data.pop(pid, None)
        tmp = self.dev_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.dev_path)                     # 原子换名：别留半份 JSON
        self.version += 1
        caps = sorted(self.capabilities_of(pid))
        self.publish("dev.changed", pid=pid, source="human",
                     data={"dev": bool(on), "functions": caps})
        return ("开发者模式已打开：能读写工作区里的文件，也能改自己的代码"
                "（只有这些 —— 碰不到别的程序，也没有终端）" if on
                else "开发者模式已关闭：手收回，回到只能动自己程序的状态")

    # -- 谁在调 -------------------------------------------------------
    def caller(self) -> str | None:
        """此刻是谁在动手（程序的 pid）；人手点的、或没有上下文时是 None。

        为什么要有这一层：开发者模式让一个程序**借来**了小助理的手，于是
        dispatch 里的 pid 是「手的主人」，不是「正在动手的那个程序」。
        没有它，「不许用手删掉自己」这种事根本没法判 —— 只能靠提示词劝。
        """
        stack = getattr(self._callers, "stack", None)
        return stack[-1] if stack else None

    def _push_caller(self, pid: str | None) -> None:
        stack = getattr(self._callers, "stack", None)
        if stack is None:
            stack = self._callers.stack = []
        stack.append(pid)

    def _pop_caller(self) -> None:
        stack = getattr(self._callers, "stack", [])
        if stack:
            stack.pop()

    def program(self, pid: str):
        """按 pid 找程序：插件或内置程序，都没有就 None。"""
        return self.plugins.get(pid) or self.programs.get(pid)

    @staticmethod
    def _collect_caps(mod, pid: str, timeout_ms: int,
                      taken: dict[str, Capability] | None = None
                      ) -> tuple[dict[str, Capability], list[str]]:
        """一个模块 → 一套能力，以及**没能变成能力的原因**。

        **三处装载入口共用这一份**：插件目录（``_load``）、内置程序（``mount``）、
        开发者工具集（``mount_devtools``）。以前是三段复制粘贴的循环：判据一样，
        失败处理却分了叉 —— 插件路径把「wire 名不合法」记进 ``p.error``，另两条
        静默 ``continue``。于是同一份代码搬个地方，一个会告诉你为什么，
        另一个只会回报「0 项能力」/「0 项：」。

        判据（四条，缺一条就会把不是能力的东西当能力）：
          * 不以下划线开头（``_`` = 私有）
          * 是函数（模块里的常量、类、导入的对象都不是）
          * ``__module__`` 就是这个模块（``import time`` 进来的不是能力）
          * ``<pid>_<fn>`` 匹配 aaas 的 function 名约束（大写开头会掉队）
          * 这个名字**还没有主**（见 ``taken``）

        最后一条是后补的：pid 与函数名是**两段独立取值**，拼起来的 wire 会撞 ——
        插件目录叫 ``assistant_plugin``、函数叫 ``delete``，就与内置小助理的
        ``plugin_delete`` 撞成同一个 ``assistant_plugin_delete``。而 ``_registry()``
        里插件是覆盖内置的，于是模型看到的声明来自内置、跑起来的函数来自插件，
        调用还会因为 pid 不匹配被拒（"「assistant」没有能力 plugin_delete"）——
        一条能力凭空消失，三处都说不出来是谁干的。所以判在装载处：
        ``taken`` 里有主就拒收这一个函数，别的函数照常装。
        """
        caps: dict[str, Capability] = {}
        problems: list[str] = []
        mod_name = getattr(mod, "__name__", None)
        for fn_name, obj in vars(mod).items():
            if fn_name.startswith("_") or not callable(obj):
                continue
            if not inspect.isfunction(obj) or getattr(obj, "__module__", None) != mod_name:
                continue
            wire = f"{pid}_{fn_name}"
            if not WIRE_NAME_RE.match(wire):
                problems.append(f"{wire} 不是合法能力名")
                continue
            owner = (taken or {}).get(wire)
            if owner is not None and owner.pid != pid:      # 自己重装自己不算撞
                problems.append(f"{wire} 已被「{owner.pid}」的 {owner.fn} 占用，本次不装载")
                continue
            doc = inspect.getdoc(obj) or ""
            caps[fn_name] = Capability(
                pid=pid, fn=fn_name, wire=wire,
                description=(doc.splitlines()[0].strip() if doc else wire),
                parameters=build_schema(obj), handler=obj, timeout_ms=timeout_ms,
            )
        return caps, problems

    def mount(self, mod, pid: str = "assistant", program: dict | None = None,
              timeout_ms: int = 120_000) -> str:
        """把一组函数挂成**一个内置程序**的能力（不走插件目录）。

        ``mod`` 里的公开函数 = 能力，规则与插件**完全一样**：一份注解同时长出
        schema、表单、校验。内核因此没有第二套规则。
        """
        prog = self.programs.get(pid) or BuiltinProgram(pid, program or {})
        if program:
            prog.meta.update(program)
        caps, problems = self._collect_caps(mod, pid, timeout_ms, self._registry())
        for fn_name, cap in caps.items():
            prog.caps[fn_name] = cap
            self.builtins[(pid, fn_name)] = cap
        added = [c.wire for c in caps.values()]
        if problems:
            # 以前这里静默 continue：一个函数叫 Read（大写）时 mount 只回报
            # 「0 项能力」，而同一份代码放进 plugins/ 会被告知原因。
            self.publish("plugin.error", pid=pid,
                         data={"error": "；".join(problems), "builtin": True})
        self.programs[pid] = prog
        self.version += 1
        self.publish("plugin.loaded", pid=pid,
                     data={"title": prog.title, "functions": added, "builtin": True})
        return f"内置程序「{prog.title}」：{len(added)} 项能力"

    def mount_devtools(self, mod, timeout_ms: int = 120_000) -> str:
        """把一组函数挂成**开发者工具集** —— 它不属于任何程序。

        与 ``mount`` 的差别只有一处，但很关键：不建程序、不进 ``programs``、
        不进 ``capabilities()``。它只在某个程序的开发者模式打开时，临时并进
        那个程序的 ``capabilities_of``。

        为什么不做成「程序」：程序有 pid、有界面、在左边栏占一格。开发者模式是
        多出来的一双手，不是用户多出来的一个应用 —— 摆进左侧栏就是在说
        「你还有个叫开发者的程序」，那是谎话。
        """
        caps, problems = self._collect_caps(mod, DEV_PID, timeout_ms, self._registry())
        self.devtools.update(caps)
        added = [c.wire for c in caps.values()]
        if problems:
            self.publish("plugin.error", pid=DEV_PID,
                         data={"error": "；".join(problems)})
        self.version += 1
        return f"开发者工具集 {len(added)} 项：{'、'.join(sorted(added))}"

    def declarations(self, pid: str | None = None) -> list[dict]:
        caps = self.capabilities() if pid is None else self.capabilities_of(pid)
        return [c.as_decl() for c in caps.values()]

    def state(self) -> dict:
        """给界面的一份：**程序清单**。

        没有「内核」这一项 —— 人不需要看见手；小助理就是手的主人。
        """
        progs = []
        for p in list(self.programs.values()) + list(self.plugins.values()):
            d = p.as_dict()
            d["dev"] = self.dev_mode(d["id"])      # 界面上那枚开关的当前状态
            progs.append(d)
        # 开发者工具集也报给界面 —— 但**只为了工具卡写清「谁在动手」**
        #（labelOf 靠它把 dev_read 显示成「🛠 开发者 · read」）。
        # 它不是程序：不进 programs，所以左边栏没有它。
        dev = {"id": DEV_PID, "title": "开发者", "icon": "🛠", "internal": True,
               "description": "打开开发者模式时借到的那双手：读写文件 + 改自己",
               "hasUi": False, "uiKind": "none", "uiName": "", "error": None,
               "functions": [{"name": c.wire, "fn": c.fn,
                              "description": c.description, "parameters": c.parameters}
                             for c in self.devtools.values()]}
        with self._lock:
            bus = {"dropped": self._dropped, "subscribers": len(self._subs)}
        return {"version": self.version, "programs": progs, "devtools": dev,
                "events": bus}

    # -- 唯一执行入口 -------------------------------------------------
    def dispatch(self, pid: str, fn: str, args: dict | None, source: str = "human",
                 caller: str | None = None) -> Result:
        """**全系统唯一执行入口。** 人、agent、插件彼此调用，都到这里。

        绕开它直接 import backend 的代码都是 bug：那样两条路径会开始漂移，
        而「人能用、agent 也能用，完全同一件事」是这个产品的地基。
        """
        started = time.perf_counter()
        cap = self._registry().get(f"{pid}_{fn}")    # 与声明同一张表
        if cap is not None and (cap.pid != pid or cap.fn != fn):
            # pid 自己带下划线时（dingtalk 那种），拼出来的 wire 可能恰好撞上
            # 另一个 (pid, fn) 的组合。单表的键是 wire（声明要用），所以这里
            # 必须核对回去，否则 dispatch("a", "b_c") 会点到 (a_b, c)。
            cap = None
        if cap is None:
            return self._reject(pid, fn, source, self._why_missing(pid, fn), started)
        if cap.pid == DEV_PID:                       # 开发者工具集（不属于任何程序）
            who = caller if caller is not None else self.caller()
            if not (who and self.dev_mode(who)):
                # 开关关掉的那一刻就收回。光靠能力面不够：声明在**这一轮开始时**
                # 已经发出去了，人可以在半路把它关掉，而这一轮还会继续调。
                return self._reject(pid, fn, source, "开发者模式没有对这个程序打开", started)
        err = validate(cap.parameters, args if args is not None else {})
        if err:
            # rejected 而不是 error：模型自己能改好参数再调一次
            return self._reject(pid, fn, source, err, started)

        # 没点名谁在动手，就**继承**上一层 —— plugin_call 引起的那层嵌套 dispatch
        # 正是这样。否则「谁在调」会在嵌套处退化成 source 字符串（'plugin'）。
        self._push_caller(caller if caller is not None else (self.caller() or source))
        try:
            value = cap.handler(**(args or {}))
        except Exception as exc:                            # 函数体抛错：要人介入
            self.publish("call.error", pid=pid, fn=fn, source=source,
                         data={"reason": type(exc).__name__, "caller": caller})
            return Result(status="error", reason=type(exc).__name__,
                          text=f"{type(exc).__name__}: {exc}",
                          elapsed_ms=int((time.perf_counter() - started) * 1000))
        finally:
            self._pop_caller()

        text, data = self._split(value)
        res = Result(status="ok", text=text, data=data,
                     elapsed_ms=int((time.perf_counter() - started) * 1000))
        self.publish("call.ok", pid=pid, fn=fn, source=source,
                     data={"text": text, "result": data, "caller": caller})
        return res

    def _why_missing(self, pid: str, fn: str) -> str:
        """找不到能力时的那句话。模型靠它决定下一步，所以别混成一句「未知」。"""
        if pid == DEV_PID:
            return f"开发者工具集里没有 {fn}"
        if pid in self.plugins:
            return f"插件 {pid} 没有能力 {fn}"
        prog = self.programs.get(pid)
        if prog is not None:
            return f"「{prog.title}」没有能力 {fn}"
        return f"未知程序 {pid}"

    @staticmethod
    def _split(value) -> tuple[str, dict]:
        """双面返回：dict 里的 "text" 给模型，其余给界面。"""
        if value is None:
            return "", {}
        if isinstance(value, str):
            return value, {}
        if isinstance(value, dict):
            text = value.get("text")
            rest = {k: v for k, v in value.items() if k != "text"}
            if text is None:
                return json.dumps(rest, ensure_ascii=False, default=str), rest
            return str(text), rest
        return json.dumps(value, ensure_ascii=False, default=str), {"value": value}

    def _reject(self, pid: str, fn: str, source: str, why: str, started: float,
                caller: str | None = None) -> Result:
        self.publish("call.rejected", pid=pid, fn=fn, source=source,
                     data={"reason": why, "caller": caller})
        return Result(status="rejected", reason=why, text=why,
                      elapsed_ms=int((time.perf_counter() - started) * 1000))

    # -- 给 agent 侧的一条便利：wire name 直接落到 dispatch
    def call_wire(self, wire: str, args: dict, source: str = "agent",
                  caller: str | None = None) -> Result:
        cap = self.capabilities().get(wire)
        if cap is None:
            return self._reject("", wire, source, f"未知能力 {wire}", time.perf_counter())
        return self.dispatch(cap.pid, cap.fn, args, source, caller=caller)


# --------------------------------------------------------------------- 插件体检
# 写插件这件事可以被静态体检：只在需要时才调它，不必每步都看。

def check_plugin(path: Path) -> dict:
    """对一个插件目录做四道静态校验（不执行、不 import）。

    返回 {"ok": bool, "errors": [...], "functions": [...]}。
    """
    path = Path(path)
    errors: list[str] = []
    manifest: dict = {}
    if path.name in RESERVED_PIDS:
        errors.append(f"目录名{reserved_reason(path.name)}")
    mf = path / "plugin.json"
    if not mf.is_file():
        errors.append("缺 plugin.json")
    else:
        try:
            manifest = json.loads(mf.read_text(encoding="utf-8"))
            if not re.match(r"^[a-z][a-z0-9_]{0,31}$", path.name):
                errors.append(f"目录名 {path.name!r} 应匹配 ^[a-z][a-z0-9_]{{0,31}}$")
        except ValueError as exc:
            errors.append(f"plugin.json 不是合法 JSON：{exc}")
    # 界面入口：网页（.html）或原生窗口（.py）。写错的入口 = 用户点开是白板，
    # 所以它和 backend.py 一样要过静态检查 —— 不执行，只解析。
    ui_name = (manifest.get("ui") or "ui/index.html")
    ui_path = path / ui_name
    if ui_path.suffix.lower() == ".py":
        if not ui_path.is_file():
            errors.append(f"缺原生窗口入口 {ui_name}")
        else:
            import ast
            try:
                ast.parse(ui_path.read_text(encoding="utf-8"))
            except SyntaxError as exc:
                errors.append(f"{ui_name} 语法错误：第 {exc.lineno} 行 {exc.msg}")
    elif not ui_path.is_file():
        errors.append(f"缺界面 {ui_name}（没有页面的插件算没做完）")

    backend = path / "backend.py"
    fns: list[str] = []
    if not backend.is_file():
        errors.append("缺 backend.py")
    else:
        import ast
        try:
            tree = ast.parse(backend.read_text(encoding="utf-8"))
        except SyntaxError as exc:
            errors.append(f"backend.py 语法错误：第 {exc.lineno} 行 {exc.msg}")
            tree = None
        if tree is not None:
            import builtins
            for node in tree.body:
                if not isinstance(node, ast.FunctionDef) or node.name.startswith("_"):
                    continue
                fns.append(node.name)
                # 能力名撞内置名：一旦定义 list()，本模块里所有 list(...) 都变成
                # 调它自己 —— 报错信息是「list() takes 0 positional arguments」，
                # 谁看都不知道发生了什么。所以在这里就拦住。
                if hasattr(builtins, node.name):
                    errors.append(f"{node.name}() 撞了 Python 内置名 —— "
                                  f"改个名字（如 {node.name}s / all_{node.name}），"
                                  f"否则模块内所有 {node.name}(...) 都会调到它自己")
                if ast.get_docstring(node) is None:
                    errors.append(f"{node.name}() 没有 docstring（模型只靠首行认识它）")
                for a in list(node.args.args):
                    if a.annotation is None and a.arg not in ("ctx",):
                        errors.append(f"{node.name}() 的参数 {a.arg} 没有类型注解")
            if not fns:
                errors.append("没有公开函数 —— 这个插件不提供任何能力")
    ui_kind = "native" if ui_name.endswith(".py") else "web"
    return {"ok": not errors, "errors": errors, "functions": fns, "uiKind": ui_kind,
            "uiName": ui_name, "title": manifest.get("title", path.name)}
