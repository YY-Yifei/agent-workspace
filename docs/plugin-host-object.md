# 插件与宿主之间的那个对象

> **状态：0.4.4 已实现。** v2 里 A、B、C 三个阶段都落地了（P4 的 `"sdk": 1`
> 版本号故意没做 —— 等真有第二个绑定面要改的时候再说）。
>
> 落地在哪一目了然：
>
> | 这份设计里的东西 | 代码 |
> |---|---|
> | `SERVICES` 契约表（唯一真源） | `kernel.py` 的 `SERVICES` / `services_of` |
> | 后端面 `ctx` | `Context`（get/set/emit/call/theme/theme_pref/on_theme/on_event/state/dir/log） |
> | 网页面 `window.aw` | `web/aw.js`，由 `GET /aw.js` 现场替换 pid/theme/pref |
> | 原生面 `sdk.Native` | `sdk.py`（同名，`call` 多态：`call("fn")` / `call("别的","fn")`） |
> | 主题的值怎么流 | `Runtime._theme_now`；页面 `applyTheme` → `POST /api/theme/resolved` → `publish("ui.theme")` |
> | 订阅生命周期 | `Runtime.plugin_subscribe` / `drop_subs`，`scan()` 重装与删除都注销 |
> | 环检测 | `Runtime.dispatch` push 之前：`pid in chain → rejected` |
> | 桩消解 | `toolbox._PROBE` 直接用真的 `Context` + 一个假 Runtime |
> | 写之前的那一眼 | `devtools.host_services`（从活代码反射，不抄文档） |
>
> 下面留的是**设计时的判断过程**，不改。要核对实现是否还守得住，看
> `selftest.py` 的 G 段（九条门，双向比对三面的名字集合）。

---

## 0. 现状核实（源码，不是文档）

| 面 | 插件写在哪 | 它现在能拿到的「宿主」 | 证据 |
|---|---|---|---|
| 后端 | `backend.py` | **一个真对象**，模块全局 `ctx`，只有 3 个方法 | `kernel.py:754` `mod.__dict__["ctx"] = Context(self, p)`；`Context` 在 `kernel.py:300-358`：`get` / `set` / `emit` |
| 网页 | `ui/index.html` | **什么都没有**，自己拼 URL | `workspace/plugins/dingtalk/ui/index.html:102` `fetch(\`/api/p/${PID}/call/${fn}\`)`、`:240` `new EventSource('/api/events')` |
| 原生 | `ui/app.py` | 环境变量 + 一个 HTTP 客户端类 | `native.py:84-90` 注入 `AW_BASE/AW_PID/AW_PLUGIN_DIR`；`sdk.py:35` `class Native` |

**注入早就存在，只是只有三分之一，且三面形状互不认识。**

一处必须点名的分叉：`docs/plugin-mechanism.md:243` 承诺 `ctx.call(...)`，
而 **`Context` 上没有 `call`**。文档领先代码。

---

## 1. 判断：注入的是**门面**，不是内核

该注入：一个能力只写一次；`source`/`caller`/schema 校验/事件广播只在 `dispatch`
里有一份，插件自动继承；插件不硬编码端口。

**不该**注入 `Runtime` 本身：拿到它就能 `cap.handler(...)` 绕过 dispatch、
读别的插件的 store、改 `dev.json`。那是「唯一执行入口」这条地基的反面。

**判据：对象上的每个方法，必须能写成一条 HTTP 路由，或一次 `dispatch`。**

---

## 2. 一条原则：一份契约 · 两个时刻 · 三条门

这条语义（「宿主有什么能力」）在 v1 里被我写在了四个地方 ——
`plugin-dev.md` 的指针、`host-services.md` 的正文、`plugin_write` 的 docstring、
提交后的反馈。**同一语义存四份，必然漂移**（记忆里那条判据的原话）。

v2 只留两处，各自是一个不同的时刻：

| 时刻 | 载体 | 形态 |
|---|---|---|
| **写之前**：要知道有什么 | `dev_host_services()` | **从活代码反射**，不抄一份；调它才进上下文 |
| **写之后、错的时候**：用了不存在的 | 静态门（AST 扫 `ctx.<attr>`） | 校验失败，并列出真清单 |
| ~~每次提交都说一句~~ | ~~删掉~~ | 见 §3 |

指针只写一处：`devtools.plugin_write` 的 docstring 里一句
「宿主能力清单：`dev_host_services`」。工具描述每轮常驻，恰好就是「我要写插件」
那一刻被读到的位置 —— 不再往 `plugin-dev.md` 抄第二份。

---

## 3. 为什么删掉「提交后报告 ctx 用量」

三条，任何一条都成立：

1. **时机错**。它在成功路径上说，而那时代码已落盘、四道校验已过。
   这句话说出口时**不能改变任何一个决定**。
2. **频率错**。无条件每次都说的字会变成背景噪音（habituation）。
   这条仓库最贵的教训是「假绿」——永远绿的门等于没有门，永远说的话等于没有话。
3. **它数的不是错**。`ctx.get ×2` 只是个次数；真正该挡的是 `ctx.call`
   （一个不存在的属性）。**计数对这件事一声不响。** 它把一个「该是门的事」
   做成了「该是报告的事」。

还有一条更深的：`kernel.py:12` 写明「本文件里每个分支只回答参数/状态是否合法，
不回答接下来做什么」。成功路径上多一句「你没用宿主状态哦」，是宿主在替 agent
做「你该反思一下」的判断 —— 那是把智能从道里偷回来的一种。

---

## 4. 契约表（唯一真源，`kernel.py`）

```python
SERVICES = {                         # 名字 → 哪些面必须真的有它
    "get":      ("backend", "web", "native"),
    "set":      ("backend", "web", "native"),
    "emit":     ("backend", "web", "native"),
    "call":     ("backend", "web", "native"),
    "theme":    ("backend", "web", "native"),
    "on_theme": ("backend", "web", "native"),
    "state":    ("backend", "web", "native"),
    "dir":      ("backend", "web", "native"),
    "on_event": ("backend",),        # 订阅只在宿主进程里有意义
    "log":      ("backend",),
}
```

**明确不给**：任何写 `config.json` / `dev.json` 的东西。能写这两份就等于能给自己
或别人拨权限（`settings.py` 那条边界）。这条不从「更体面的入口」上被旁路。

---

## 5. 三个绑定

| 面 | 绑定 | 为什么只能这样 |
|---|---|---|
| `backend.py` | 模块全局 `ctx`（**已有**，扩展） | 在宿主进程内，`exec_module` 前注入是现成的 |
| `ui/index.html` | `<script src="/aw.js">` → `window.aw` | 同源 iframe，JS 只能走 HTTP；且页面必须能单独打开 |
| `ui/app.py` | `from agentworkspace.sdk import Native`（**已有**，扩展） | 独立进程，只有环境变量 + HTTP |

两个不做：**不用 postMessage**（同源时多一条无鉴权通道，还让页面离不开 iframe）；
**不重写插件 HTML 去注入 script**（不引 `/aw.js` 也不坏）。

---

## 6. 桩消解：契约的第二份由**构造**消除，不是靠门去追

今天 `toolbox._PROBE` 里有一个手写的 `class Ctx`（`toolbox.py:823-827`），
它是契约的第二份。它与 `Context` 的方法集合**今天恰好相等，纯属巧合**。

改法（不是加断言，是让它不存在）：

```python
# 探针子进程里：用真的 Context，配一个假 Runtime
sys.path.insert(0, sys.argv[2])                 # 宿主包根，由 toolbox 自己算
from agentworkspace.kernel import Context       # 顶层只有 stdlib，已核实
class _RT:                                      # Context 只要这四样 + plugin.pid
    def __init__(self, d): self.plugins_dir=d; self._lock=threading.Lock(); self._store_locks={}
    def publish(self, *a, **k): pass
```

顺带升级第四道校验：探针原本只 `exec_module`，现在可以真跑一次
`ctx.set("k",1); assert ctx.get("k")==1`（写临时目录）。于是「import 不炸」
变成「import + 状态读写真的通」，失败信息也具体了。

---

## 7. 主题：第一个实例（也是唯一能被像素门卡住的）

```
theme()          → "dark" | "light"     # 解析后的，直接能画
theme_pref()     → "auto"|"dark"|"light"# 人点的那一档，插件不该拿它去画
on_theme(cb)     → 取消函数
```

**解析权只在宿主**：`auto` 要问系统，而唯一免费跨平台准确的问法是
`matchMedia`（`web/index.html:1787` 已在用）—— 那是页面的能力。让每个插件
自己解析，就是又长出三份判据。

值怎么流：

```
matchMedia → 宿主页面 applyTheme() ← config.json ui.theme
                    ↓ POST /api/theme/resolved（新增）
             Runtime._theme_now = "light"   ← 后端要能随时答 ctx.theme()
                    ↓ publish("ui.theme", {pref, resolved})
        ├→ ctx.theme() / ctx.on_theme()     （宿主进程内）
        ├→ /api/events SSE → aw.onTheme()   （iframe 里）
        └→ Native().theme() / on_event      （独立进程）
```

今天 `publish` 只有 `call.ok / call.rejected / bus.dropped`，**没有一条是
「设置变了」**。这条事件一旦有了，`on_theme` 就是**第一个真实订阅** —— 于是
订阅的生命周期必须与它同时落地（见 §8 的幽灵插件）。

---

## 8. 两个机制问题（必须与使用者同时上线）

1. **环检测**（`call` 一出现就成立）。A→B→A 是无限递归，而 `caller()`
   （`kernel.py:864`）**只看栈顶**，判不出「A 已在栈里」。判据加在 `dispatch`
   push 之前：`pid in chain → rejected`。**加在 dispatch 而不是 `ctx.call`** ——
   agent 的 `plugin_call` 走同一条路，判据只该有一份。

2. **订阅生命周期**。`on_theme` / `on_event` 在宿主进程挂回调，而 `scan()` 会
   **整套换掉** `Plugin`、丢弃旧模块 —— 旧回调闭包攥着旧 runtime。
   结果：收事件、占内存、`ctx` 指向已不在的 runtime 的**幽灵插件**。
   订阅按 pid 记在 `Runtime`，删插件时一并注销。

---

## 9. 分期：按「哪条门能红」切

| 阶段 | 内容 | 门 |
|---|---|---|
| **A** | `SERVICES` 表 + 三个绑定的集合门 + 桩消解（§6） | 面门 · 桩门（**今天就能红**：`aw`/`Native` 缺位） |
| **B** | `theme` 三面打通 + 订阅按 pid 记 + `scan()` 注销 | 像素门 · 三面同值门 · 幽灵插件门 |
| **C** | `call` + `dispatch` 里的环检测 | 经 dispatch 门 · 环门（带超时，不 hang） |
| ~~P4~~ | `plugin.json` 的 `"sdk": 1` + `ctx.sdk_version` | 留到有第二个绑定面要改的时候 |

顺序理由：A 先做，因为它把「三面漂移」变成一条能红的门 —— 之后每个新能力
都必须先在表里登记、再在三面实现。B 是第一个真值，且**订阅清理属于 B**
（`on_theme` 就是第一个订阅；先上一条会漏的回调、再补一次清理，是错的）。
C 是唯一需要新机制的能力。

---

## 10. 门

| # | 门 | 挡的病 |
|---|---|---|
| 1 | **面门**：`SERVICES` 声明的面集合 == 三面实际导出的集合（双向） | 三面漂移、顺手加方法 |
| 2 | **桩门**：`_PROBE` 源码里不再有 `class Ctx` | 契约第二份被加回来 |
| 3 | **未知属性门**：`backend.py` 里 `ctx.<attr>` 不在契约里 → 校验失败 | **文档写了没实现的能力**（`ctx.call` 那个病） |
| 4 | **假阳性门**：插件自己定义了局部 `ctx`（参数/赋值）时不报 | 门变成误报机器 |
| 5 | **像素门**：宿主设浅色 → 插件 iframe 底色真的变亮（量 `bg_lum`）；把 `@media` 注回去必须变红 | 「一个身体两种颜色」 |
| 6 | **幽灵插件门**：卸载后旧 `on_theme` 回调计数不增长 | 订阅泄漏 |
| 7 | **三面同值门**：同一时刻 `ctx.theme()` / `aw.theme()` / `Native().theme()` 必须相等 | 三份实现漂移 |

每条都先红后绿；第 4、5 条自带反面对照。

---

## 11. 明确不做

- 提交后的 ctx 用量报告（§3）；
- `workspace/prompts/host-services.md`（会漂移的副本，被 `dev_host_services` 取代）；
- 在 `plugin-dev.md` 里重复 API（指针只留 `plugin_write` 的 docstring 一处）；
- postMessage；
- 任何写 `config.json` / `dev.json` 的入口。
