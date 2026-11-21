# 插件机制 · 设计（v1）

> 一句话：**插件是一个「程序」——有目录、有页面、有状态、能独立运行；它同时把自己的一批函数
> 登记成 agent 能力。人走 HTTP 调它，agent 走 `invokeFunction` 调它，落到的是同一个执行入口。**

它不是「给 agent 加的工具栏」，而是「用户/agent 写出来的应用」。
agent 只是这个应用的**另一个用户**。

---

## 0. 反过来的先后顺序（这是整份设计的出发点）

绝大多数 agent 产品的插件是「工具 + UI 皮肤」：能力为模型而生，界面只是把结果画出来。
这里反过来：

| | 通常做法 | 本设计 |
|---|---|---|
| 一等公民 | 工具（给模型的函数） | **程序**（有状态、有页面、可独立运行） |
| 界面 | 工具结果的渲染器 | 程序的**主入口**，没有 agent 也能用 |
| agent | 唯一使用者 | **第二个客户端**，和人在同一层 |
| 没有 agent 时 | 什么都干不了 | 一切都照常，只是不能对话 |

所以判断某个功能该不该做成插件，标准不是「模型需不需要它」，而是
**「用户会不会想反复用它」**。会 → 插件；只是模型干活时顺手用一下 → 普通能力（写进 `functions/`）。

---

## 1. 一个插件的形状

```
workspace/plugins/pomodoro/
  plugin.json          # 清单：id / 名称 / 入口 / 超时 / 权限
  backend.py           # 能力实现：公开函数 = 能力（纯标准库，不 import 任何 agent 相关东西）
  ui/
    index.html         # 界面①：网页，活在宿主窗口里（默认）
    或 app.py          # 界面②：原生窗口（tkinter/PyQt），自己开一个 OS 窗口
  data/                # 插件自己的数据（store.json / sqlite / 用户文件）
```

**界面有两种形态，执行路径只有一条。** 网页程序活在宿主窗口里；原生程序是
独立进程、自己的系统窗口，但它同样只通过 `POST /api/p/<id>/call/<fn>` →
`Runtime.dispatch(..., source="human")` 回到内核 —— 它是「人的门」的另一种壳，
不是第二套执行路径。什么时候该写原生窗口、SDK 怎么用、踩过哪三个坑，
见 [shell-and-native.md](shell-and-native.md)。

`plugin.json`：

```json
{
  "id": "pomodoro",
  "name": "番茄钟",
  "version": "0.1.0",
  "description": "专注计时器",
  "entry": { "backend": "backend.py", "ui": "ui/index.html" },
  "timeoutMs": 5000,
  "permissions": ["store"]
}
```

**代码目录与数据目录分离**是一条硬规矩：`backend.py` / `ui/` 是代码，`data/` 是用户的数据。
agent 重写插件代码时**不动 data/** —— 于是「让 agent 帮我改进这个程序」不会清掉用户攒下来的东西。
这也是插件可以反复迭代的前提。

---

## 2. 三条通道，一个入口

```
        agent（aaas invokeFunction）        人（插件页面 / 能力面板）
                    │                                │
        wire_name = "pomodoro_start"     POST /api/p/pomodoro/call/start
                    │                                │
                    └────────────┬───────────────────┘
                                 ▼
                    Runtime.dispatch(pid, fn, args, source)
                                 │
                    ① 参数校验（schema）  ② 执行  ③ 广播事件
                                 ▼
                          backend.py 里的那个函数
                                 ▼
                     ctx.get/set  →  data/store.json（唯一状态）
```

一条铁律：**`dispatch()` 是唯一执行入口**。任何绕过它直接 `import backend` 调函数的代码
都是 bug——那样 agent 调用的效果和人点按钮的效果就会开始漂移，而这正是这类系统烂掉的起点。

`source` 只有三个值，它跟着事件一起广播出去：`human` / `agent` / `plugin`。
于是界面上永远能显示「这一步是人点的还是 agent 做的」——agent 的动作**可见**，也就**可接管**。

---

## 3. 契约五条（写插件的人只需要知道这五条）

### ① 公开函数 = 能力，`_` 前缀 = 私有

```python
def start(minutes: int = 25) -> dict:   # → 能力 pomodoro_start
    ...
def _bump_done() -> None:               # → 不暴露（与 openthree 的规则同源）
    ...
```

wire name 即 `<plugin_id>_<fn>`，天然满足 aaas 的 `^[a-z][a-z0-9_]{0,63}$`，
也天然避开插件之间的重名。

### ② 约束只写一处：`Annotated`

```python
Minutes = Annotated[int, {"minimum": 1, "maximum": 180}]

def start(minutes: Minutes = 25) -> dict: ...
```

这一份注解同时长出三样东西，**不许重复写**：

| 消费方 | 得到什么 |
|---|---|
| 模型 | `FunctionDeclaration.parameters`（含 `minimum`/`maximum`/描述） |
| 界面 | 输入框类型、默认值、范围提示、自动表单 |
| 执行 | `dispatch` 的入参校验（越界 → `rejected`，**不进函数体**） |

### ③ 双面返回：给模型一句话，给界面一份数据

```python
return {"text": "已开始 30 分钟专注，预计 11:24 结束",   # → FunctionResult.parts[0].text
        "running": True, "minutes": 30, "end_at": ...}  # → FunctionResult.parts[1].data
```

`data` 这一格在 aaas 的 `Part` 里**只在 `FunctionResult` 里合法** —— 也就是说，
「给界面的结构化数据」是协议里现成的一条通道，不需要造新字段。
模型只读 `text`，界面只读 `data`，两边各取所需。

### ④ 状态在 `ctx`，不在内存里

```python
ctx.set("end_at", t);  ctx.get("done_count", 0)
```

落到 `data/store.json`。于是：窗口关了再开还在；agent 重启后读到的还是同一份；
人点过的操作 agent 一问就知道。**没有内存中的真相**（openthree 的第一句话）。

### ⑤ 事件总线：谁改的都广播

`call.done` / `state.changed` / `call.failed` / `call.rejected` 全部进 SSE。
插件页面订阅自己的事件，于是 —— **agent 在对话里说「开始一个番茄钟」，旁边那个番茄钟页面上的数字就动起来了。**
这是「agent native」最直观的一种：不是给 agent 单独开一扇门，而是让它和人在同一个房间里。

---

## 4. 人这一侧：三条不依赖 agent 的用法

按投入从零到多排：

| 用法 | 需要写什么 | 适用 |
|---|---|---|
| **能力面板**（自动生成） | 啥都不写 | 任何插件都有：schema → 表单 → 按钮 → 结果。这是兜底，保证「插件没写 UI 也不是死的」 |
| **命令面板** | 啥都不写 | 打字搜 `pomodoro_start`，回车执行 |
| **自定义页面** | `ui/index.html` | 需要持续状态、图表、拖拽这类真界面时 |

能力面板是这套机制的关键保险：**agent 写出来的能力，天然就是人能点的按钮** ——
哪怕它一个 HTML 都没写。这也是「极简」能成立的原因：自动表单承担了 80% 的界面工作。

页面是普通静态页：同源、可 `fetch`、可用 `EventSource`、可用 ESM，**零构建零框架**。
直接 `http://127.0.0.1:<port>/p/pomodoro/ui/index.html` 也能在浏览器里打开。

---

## 5. agent 这一侧：聚合与栅栏

`declareAgent` 时把所有**已启用**插件的全部能力聚合进 `functions`：

```json
{ "name": "pomodoro_start",
  "description": "[番茄钟] 开始一个专注计时。",
  "parameters": { "type": "object", "properties": { "minutes": { ... } },
                  "required": [], "additionalProperties": false },
  "timeoutMs": 5000,
  "metadata": { "plugin": "pomodoro", "ui": "ui/index.html" } }
```

`metadata` 是协议里合法的**不透明字段**（服务端 MUST NOT 解释、MUST NOT 回传），
正好用来挂「这个能力属于哪个插件、对应哪个页面」—— UI 侧据此把工具调用显示成
「番茄钟 · 开始」，而不是一段 JSON。

**栅栏**：装/改/删插件后重载 runtime → 下一次 `declareAgent` 才生效。
协议本来就规定声明只影响后续轮次，所以 agent 不会在同一轮里对着刚写完的半成品反复试错。
演化后由宿主自动开一个接力轮 —— 于是「写完 → 下一轮自己调自己刚造的」这一环天然成立。

**帧大小**：能力全部随 declare 一次下发。按每个能力 ~250 字节估，6MB 上限下
**数百个能力**都安全；真正的约束是模型的注意力，不是帧大小。所以建议：插件默认只暴露
「值得模型调」的函数，纯界面用的辅助函数加 `_` 前缀。

---

## 6. agent 怎么「写出程序」

内置能力 `write_plugin(id, files)`：一次提交整个插件目录（`plugin.json` + `backend.py` + `ui/*`），
宿主做**四道静态校验**后才落盘：

1. `plugin.json` 合法、`id` 与目录一致、`entry` 指向的文件存在；
2. `backend.py` 语法通过，且有 ≥1 个公开函数；
3. 每个公开函数都有类型注解 + docstring 首行（否则模型看不到用途）；
4. 在**子进程**里真 import 一次（照抄内核 `verify_tool` 的做法：先证明能跑，再让它上岗）。

通过 → 写入 `plugins/<id>/` → `reload()` → 广播 `plugin.installed` → 界面出现新卡片。
下一轮 declare 里就有它。

于是用户的心智模型非常朴素：

> **「我跟它说：给我做个记账本。它做完了，侧边多出一个记账本 —— 我自己点着用。」**

而之后每一次「再加个分类统计吧」，agent 改的是代码，用户的账还在。

---

## 7. 失败与安全

**三种结局必须分得清**（对应 aaas 的 `FunctionResult.status`）：

| status | 何时 | 模型看到什么 | 界面看到什么 |
|---|---|---|---|
| `ok` | 正常 | `text` + `data` | 结果 + 状态刷新 |
| `rejected` | 参数不合 schema / 未知能力 / 越权字段 | reason（明确「你没按格式调」） | 输入框标红 |
| `error` | 函数体抛异常 | reason（堆栈摘要） | 错误提示 + 「让 agent 看看」按钮 |

把 `rejected` 和 `error` 分开很重要：前者是模型能自己修好的（改参数重调），
后者要人介入。糊成一个 `isError`，模型就会对着格式错误反复重试。

**信任模型要说实话**：插件是本机 Python 代码，`backend.py` 里能跑任意东西。
所以 v1 的立场是「插件 = 用户自己的程序」，提供：

- `plugin.json.permissions` 声明（`store` / `net` / `shell` / `fs`），**用于展示与审计**，不是沙箱；
- 首次启用一个由 agent 新写的插件时，界面提示「这个插件会在本机执行代码」并列出它 import 了什么；
- 执行放**子进程**（带超时与强杀）—— 不是为了防恶意，是为了「一个插件把宿主搞崩」这件事不该发生。

不做的事（明确不做）：不做 JS 沙箱、不做权限代理层、不做插件市场签名。
这些都要付出「极简」的代价，而收益在本地单用户场景里近乎为零。

---

## 8. 组合：插件调插件

插件要复用别人的能力时，走同一个入口：`ctx.call("notes_search", {...})` → `dispatch()`。
于是权限、校验、事件、循环依赖检测全都自动继承，不需要第二套机制。
（v1 只允许**单向**调用，A→B 时 B 不能回头调 A；检测到环直接 `rejected`。）

---

## 9. 原型已验证的事实（`proto/`，8 项断言全绿）

```
python proto/check.py
```

| # | 验证的事 | 结论 |
|---|---|---|
| 1 | 插件加载 | 4 个能力，坏插件不拖垮宿主 |
| 2 | 能力面符合 aaas | wire name 匹配 `^[a-z][a-z0-9_]{0,63}$`，参数含 description |
| 3 | 人通道 | `dispatch(source=human)` 返回标准 `FunctionResult` 形状 |
| 4 | agent 通道 | 人设的 30 分钟，agent 一问就答 30 分钟 —— 同一份状态 |
| 5 | 反向可见 | agent 调 `stop`，事件带 `source=agent` 广播到界面 |
| 6 | 私有隔离 | `_bump_done` 不出现在能力面 |
| 7 | rejected ≠ error | 越界/类型错/越权字段全部 `rejected` 且不进函数体 |
| 8 | 真 HTTP | `/api/plugins`、`POST call/start`、插件页面、`/api/declarations` 全通 |

原型文件：
- `proto/host.py` —— 极简宿主（schema 生成 / 校验 / dispatch / SSE / 能力面板）
- `proto/plugins/pomodoro/` —— 一个真插件（backend + 页面 + 状态）
- `proto/check.py` —— 上面那张表

---

## 10. 待定与取舍（写下来是为了以后能改）

| 点 | v1 的决定 | 什么时候要重看 |
|---|---|---|
| 执行隔离 | 原型 in-process，正式版子进程 | 插件数量 > 20，或出现卡死 |
| 页面容器 | 同源 iframe（可多开 tab） | 需要独立窗口/多显示器时 |
| 插件版本 | 目录即版本，无 git | 用户开始「回滚上一个版本」时 |
| 依赖 | 只许标准库 | 真需要第三方库时，先做 venv per plugin |
| 权限 | 声明式、非强制 | 出现第一个「从外面装来的插件」时 |
