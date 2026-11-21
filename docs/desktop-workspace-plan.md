# Agent Workspace 桌面端 · 方案（v2）

> 一句话：**pywebview 只当窗口；aaas-service 是唯一的脑；我们是一个 aaas 调用方，手里只有磁盘、能力清单和界面。**
>
> 从 openthree 借的是**设计哲学**（形状），不是代码。本项目对 openthree **零 import、零 fork、零共享模块**。

## v1 的错在哪

v1 把用户口中的「**你们的 agent-workspace 内核**」读成了「openthree 内核」，于是让桌面端去复用 openthree 的 `workspace.py` / `registry.py` / 上下文语义 —— 那等于把两个内核叠在一起，桌面端就有了自己的"半套 agent"。

这违背两条：
1. agent 能力**完全**在 aaas-service，本地不该有第二份推理/循环；
2. openthree 是**参照物**，不是依赖。

v2 把两条都钉死。

---

## 1. 三层分界（只说职责，不说复用）

```
┌──────────────────────────────────────────────────────┐
│ aaas-service        ws://localhost:8888              │
│ 模型 · 推理 · ReAct 循环 · 工具编排 · 取消 · 流式事件  │
│            ← 唯一的智能来源，没有第二个 ─────────────► │
└──────────────────────▲───────────────────────────────┘
                       │ 子协议 aaas.v1（我们只说 C→S 那半）
┌──────────────────────┴───────────────────────────────┐
│ 调用方 = 本应用（Desktop Host）                       │
│ 零智能：不会思考、不选工具、不决定下一步              │
│ 只做：持有状态 · 提供能力 · 执行被点名的那一个        │
└──────────────────────▲───────────────────────────────┘
                       │ 本地 HTTP + SSE
┌──────────────────────┴───────────────────────────────┐
│ pywebview 窗口：渲染事件、渲染 data part、放插件页面    │
└──────────────────────────────────────────────────────┘
```

| 层 | 有智能吗 | 拥有什么 |
|---|---|---|
| aaas-service | **有，且是唯一来源** | 对话、判断、调谁、怎么调、什么时候停 |
| 调用方（我们） | **没有** | 磁盘状态、能力清单与执行、history、界面、插件 |
| pywebview | 没有 | 一块 webview |

### 调用方的全部职责，就这么多

**C→S（我们发起的，只有四个）**
`aaas/connect` · `aaas/declareAgent` · `aaas/sendInput` · `aaas/cancelTurn`

**S→C（我们承接的，只有三类）**
`aaas/invokeFunction`（请求：执行并回结果）· `aaas/event`（通知：流式事件）· `aaas/cancelCall`（通知：放弃本次执行）

**协议规定没有第三个方向**（PLAN.md §2：没有一个操作在两个方向上都合法）。所以调用方的职责面是封闭的、可枚举的 —— 这就是「极简」的硬边界：**我们永远不需要写"下一步该干什么"的代码，那已经不在我们这一侧了。**

> 一个必须说清的区分：`invokeFunction` 让**执行**发生在我们进程里，这是协议规定的执行位置，不是「我们拥有 agent 能力」。我们执行的永远是**被点名的那个**函数，参数由 aaas 给。本地没有选择权。

---

## 2. 从 openthree 借的四句话（形状，不是代码）

| 哲学 | 在调用方落在哪 | 为什么成立 |
|---|---|---|
| **磁盘即状态** —— 每步重读，改一下下一步生效 | 工作区目录就是全部状态，没有内存中的"真相" | 内核重启/崩溃/换前端，状态不丢；同一目录可以被终端和桌面共用 |
| **提示词即身份** —— 身份可被自己改写 | `prompts/system.md` → `AgentDefinition.instructions` | 协议规定 `instructions` **MUST 不进入会话历史**、每次临时注入 —— 「改完即生效且不污染历史」是白拿的 |
| **函数即能力** —— 注解 → Schema → 工具 | `functions/*.py` → `FunctionDeclaration` | 一份类型注解同时长出「模型能调」和「界面能点」，同一执行路径 |
| **轮即栅栏** —— 演化在两个轮之间生效 | 写盘后 `reload()` → 下一轮 `declareAgent` | 协议本来就规定 `declareAgent` 只影响**后续轮次**，护栏是白拿的 |

四句话合起来就是「自演化」在调用方这一侧的完整形态：**模型写盘 → 我们重载 → 下一轮它就能用自己刚造的东西。** 我们不需要理解它在写什么，只需要把它写出来的东西**如实地变成下一轮的能力表**。

---

## 3. 工作区目录

```
my-workspace/
  .openthree/                  # 名字沿用，纯参照物，无代码依赖
    identity.json              # 名字 / 格言
    prompts/system.md          # 身份 → instructions 的唯一真源
    prompts/plan.md            # 长任务待办（模型自己写、自己读）
    prompts/user-prompts.md
    functions/*.py             # 裸能力：类型注解 → Schema
    plugins/<id>/              # 桌面端新增的一层
      plugin.json  page.html  functions.py  icon.svg
    memory/memory.jsonl        # 长期记忆（模型写，检索注入）
    history/<sid>.jsonl        # 会话历史（historyMode=stateless ⇒ 归我们）
    logs/sessions/<sid>/       # 事件流原始记录，可回放
    config.json                # aaas endpoint / token / 权限开关
  app/                         # shell.html / app.js / app.css（零构建）
```

`config.json` 里的 `aaas.endpoint` 默认 `ws://localhost:8888`。

### 关于「内核内置小助理」

开箱的「小助理」不是一段本地程序，而是**一份默认 `AgentDefinition`**：

- `instructions` = 内置的 `prompts/system.md`（讲清它是谁、这个工作区怎么用、怎么给自己造能力）；
- `functions` = 一组基础能力：读/写文件、列目录、检索、跑命令、`write_function`、`create_plugin`；
- **脑是 aaas-service 的脑**。所以第一次连上 8888，它就能干活；连不上，工作区就是一块空板子 —— 如实提示去配 `config.json`，**不做任何"假聊天"降级**。这是「能力完全基于 aaas」的代价，我们认。

---

## 4. 插件：一份能力，两个入口

插件不是功能模块，是**一处声明同时长出三种东西**：

```
functions.py（类型注解）──► 能力 Schema ──► ① 模型可调的 FunctionDeclaration
                                        ──► ② 页面可点的按钮/表单
                                        ──► ③ 返回 data part 时渲染成的界面块
```

- 模型调：`invokeFunction` → 执行 → `functionResult`；
- 页面点：`POST /api/capability/<name>` → **同一个执行器** → 结果回 HTTP，同时 SSE 广播「用户手动调用了 X」，下一轮模型自然知道。

`plugin.json`：

```json
{
  "id": "pomodoro",
  "title": "番茄钟",
  "entry": "page.html",
  "functions": "functions.py",
  "instructions": "用户提到专注、番茄、计时时，用 pomodoro_start / pomodoro_stop。",
  "ui": { "rail_icon": "🍅", "panels": ["main"] },
  "permissions": ["timer", "notify"]
}
```

三条硬约束（静态校验，不通过不许注册）：`id` 匹配 `^[a-z][a-z0-9_]{0,31}$`；能力名加插件前缀（协议对跨来源重名不保证行为，命名空间只能自己管）；`permissions` 声明式，执行前检查。

**agent 原生 UI 的协议级口子**：`Part.data` 只在 `FunctionResult` 里合法。所以能力可以返回 `Part(data={"view":"list","items":[…]})`，前端当 view-model 渲染 —— 一个协议字段都没多加，模型连「长什么样」都能决定。
反向（UI → 模型）：用户操作记成一条 `user` 文本进 history。协议禁止把工具痕迹塞进 history，但用户动作本来就是用户消息，合规且自然。

---

## 5. 数据流（一次输入）

```
1  前端 POST /api/turn {text, images[]}
2  host 读 prompts/system.md + 扫全量能力 → revision = sha8(prompt ‖ 能力名列表)
        revision 变了才 declareAgent（全量替换）
3  host sendInput(text, history=已折叠 history) → 立即得 turnId
4  event 流 → SSE → 前端：message.delta 流式、reasoning.delta 折叠区、functionCall.ready 卡片
5  invokeFunction → 线程池执行（timeoutMs 取声明里的值）→ 响应即结果
6  turn.completed → 本轮压成一条 agent 文本追加进 history → 落 logs
7  若本轮改动了 functions/ 或 plugins/ → reload() → 标记 dirty → 下一轮 declare 生效
```

第 6 步是协议逼出来的细节：`history` 只承载消息，不承载工具调用痕迹，所以每轮结束要把「这一轮怎么走的」压成一段文本。
第 2 步的 `revision` 是省流量的小手段：能力表没变就不重发（全量替换但不必每次都发）。

**上下文自管**：`historyMode: stateless` ⇒ 历史在我们手里 ⇒ 预算 / 折叠 / 丢弃由 host 的 `Session` 负责（形状照 openthree 的语义：预算字符数、自动折叠开关、已折次数），`compact_context` 作为一个**能力**仍然存在，只是它作用的对象从「内核的 steps」变成「host 的 history」。

---

## 6. 自演化闭环

用户：「给我做个番茄钟插件」

```
① 模型调 create_plugin(id, title, page_html, functions_py, manifest)   ← 这是我们声明给它的元能力
② host 静态校验（id 正则 / 前缀 / page.html 存在 / 无高危导入）
      → 写盘 → reload() → SSE plugins.changed
      → 返回文本：「已创建，pomodoro_start/pomodoro_stop 将于下一轮可见」
③ 前端 左栏出现 🍅
④ 下一轮 declareAgent → 模型真的能调 pomodoro_start
⑤ 用户点开页面 → 按钮 → POST 能力 → 同一个执行器
```

**演化后自动开接力轮**（`next_round` 的形状）：第 2 轮一开头，模型就能调自己刚造的工具、自己验证、自己修。这一步是「自演化」从「会写文件」升级为「能交付可用能力」的分界线。

三个闭环缺一不可：**能力**（写盘→reload→declare）/ **界面**（页面→路由→渲染）/ **记忆**（history 折叠 + memory.jsonl + plan.md）。

---

## 7. 里程碑

| 阶段 | 内容 | 估 |
|---|---|---|
| M0 | Host 骨架：workspace init · 能力扫描 · AaasCaller（connect/declare/sendInput + invokeFunction 回调 + event 分发）· 一轮真实对话跑通（CLI 先验，无 UI） | 1.5d |
| M1 | pywebview 窗口 + 本地 HTTP/SSE + 事件流式渲染（含 reasoning 折叠区、functionCall 卡片） | 2d |
| M2 | 基础能力面（文件/执行/write_function）+ history 持久化与折叠 + 中断/取消 | 2d |
| M3 | 插件模型：plugin.json 校验、页面路由、双入口执行器、data part → 视图渲染 | 3d |
| M4 | 演化接力：写盘 → reload → 下一轮 declare；`create_plugin` 元能力端到端 | 2d |
| M5 | 图片（part.file / 4MB 上限）、断连重连、多工作区切换 | 3d |

栈：Python 标准库 + pywebview + 单文件 HTML/JS，零构建零框架。本机 pywebview 未安装（`pip install pywebview`，Windows 需 WebView2，Win11 自带）。

---

## 8. 风险

| # | 风险 | 应对 |
|---|---|---|
| R1 | aaas-service 不可用 ⇒ 工作区零能力（没有本地兜底） | 启动探测 + 明确提示改 `config.json`；**不造假聊天** |
| R2 | 循环类旋钮（步数/轮数/温度）在服务端，本地改不动 | UI 里如实标「服务端管」，不摆假旋钮 |
| R3 | `declareAgent` 只在下一轮生效，模型写完看不到效果 | 演化后自动接力轮（§6） |
| R4 | 插件能跑 shell / 写盘 | `permissions` 声明 + 首次启用确认 + 总开关，默认禁网络与工作区外写入 |
| R5 | `maxFrameBytes=6MB`：能力面太大 declare 帧会超 | 能力控制在 ~80 个以内；插件按需加载 |
| R6 | 历史全量重发（stateless）导致 token 成本随会话增长 | host 侧折叠/摘要；这是调用方的活，不是 aaas 的 |
