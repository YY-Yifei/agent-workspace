# Agent Workspace

桌面上的 **Agent 工作区**：用户攒程序（番茄钟、心情日记、记账…），每个程序都有自己的助手；
智能全在远端 `aaas-service`，本进程只做四件事 —— **落盘、执行被点名的那一个、广播、传话**。

> **内核不含一克智能。** 它不选工具、不决定下一步、不重试、不兜底。
> 判据：本仓库里出现的每个分支，都只回答「参数/状态是否合法」，不回答「接下来做什么」。

## 谱系

```
道      = Agent（aaas-service，唯一的智能，不在本进程）
一      = 内核（本仓库 agentworkspace/：一个目录约定、一条 dispatch、一条事件流、一份能力面）
二      = 双入口（人走本地 HTTP，agent 走 invokeFunction，落到同一个 dispatch）
三      = 程序三要素（backend.py 能力 / ui 页面 / data 状态）
万物    = 工作区（用户攒出来的程序）
```

七个模块，各管一件事：

| 文件 | 它是谁 |
|---|---|
| `kernel.py` | **一**：目录约定 / 唯一 `dispatch()` / 事件总线 / 能力面（按 pid 隔离） |
| `link.py` | **道**：aaas 接线（connect / declare / send / 转发被点名的那一个），每个程序一条会话 |
| `server.py` | **二之一**：人的门（本地 HTTP + SSE） |
| `shell.py` | 窗口壳：pywebview（`--browser` 是它的退化形态） |
| `native.py` | 原生程序宿主：不是第二条执行路径，是「人的门」的第三种壳 |
| `main.py` | 入口：装配，没有第四样东西 |
| `web/index.html` | 工作区视图 + 对话 + 能力面板（单文件，无构建步骤） |

配套：`settings.py`（唯一一项设置：aaas-service 地址）、`devtools.py`（开发者模式那双手）、
`assistant.py`（内核唯一内置的程序：小助理）、`toolbox.py`（小助理的手）、
`selftest.py` / `qa.py` / `uiprobe.py`（三层的自检：逻辑 / 真窗口 / 真浏览器）。

## 装

```bash
pip install agent-workspace          # 内核 + aaas 客户端
pip install "agent-workspace[gui]"   # 再带上桌面窗口（pywebview）
```

需要 Python 3.10+，以及一个能连上的 `aaas-service`（地址在设置里改，默认 `ws://localhost:8888`）。

## 跑

```bash
agent-workspace                     # 桌面窗口
agent-workspace --browser           # 换成浏览器（pywebview 起不来时的形态）
agent-workspace --cli               # 命令行：跟小助理说一句
agent-workspace --cli -m "做个番茄钟"        # 同上
agent-workspace --cli --as pomodoro -m "开始 25 分钟"   # 跟某个程序自己的助手说
```

`--cli` 是验收形态：`connect → declare → sendInput → invokeFunction → 本地执行 →
functionResult → 模型续写 → turn.completed`。这条不通，界面做得再漂亮也是空壳。

## 工作区长什么样

工作区（默认 `./workspace`，可用 `--workspace` 换）是**运行时**，不在本仓库里：

```
workspace/
  plugins/<pid>/         程序：plugin.json + backend.py + ui/index.html + data/store.json
  logs/events.jsonl      谁做了什么、谁点的（事件流）
  prompts/               每个程序的指令
  config.json            设置（只有一项：aaas-service 地址）
  dev.json               开发者模式开关（**只属于人**：程序写它一律被拒）
```

内核**没有任何内置的领域能力**：番茄钟、日记、记账都是工作区里长出来的程序，
所以它们**不在这个仓库里** —— 这里只有内核。

## 契约（写程序的人关心这几条）

1. 公开函数 = 能力；`_` 前缀 = 私有；wire name = `<pid>_<fn>`
2. 约束只写一处：`Annotated[int, {"minimum": 1}]` → schema / 表单 / 校验同源
3. 双面返回：dict 含 `"text"` → 文本给模型，其余进 `data` 给界面
4. 状态走 `ctx.get/set` → `data/store.json`（代码目录与数据目录分离）
5. 事件总线：`call.ok` / `call.error` / `call.rejected` / `state.changed`

隔离只写一处：`Runtime.capabilities_of(pid)`。别的程序在结构上就**拿不到** `plugin_write`。

## 开发者模式

一个开关（设置面板里拨），打开后当前程序多拿到一把**只够改自己**的手：

| 有 | 没有 |
|---|---|
| `dev_tree` / `dev_read` / `dev_search` | `plugin_list`（「有哪些程序」本身就是权限地图） |
| `dev_write`（普通文件） | `plugin_call`（能绕开别人的隔离） |
| `dev_plugin_write`（**没有 pid 参数**） | `plugin_delete` / `shell` |

想改别人，在**签名**上就无从下手；`dev.json` 由人掌握，程序写它一律被拒。
细节见 [`docs/dev-mode.md`](docs/dev-mode.md)。

## 自检

```bash
python -m agentworkspace.selftest     # 逻辑层（内存里跑完整链路）
python -m agentworkspace.qa all       # 真窗口
python -m agentworkspace.uiprobe      # 真浏览器（拖动 / 折叠 / 设置 / 图标）
```

三层缺一不可：`selftest` 只能断言「文件里有这行字」，CSS 优先级、事件绑定、
重绘时机任何一处错了都拦不住 —— 那要靠后两层。

## 文档

- [`docs/vision.md`](docs/vision.md) —— 道 / 一 / 二 / 三 / 万物
- [`docs/desktop-workspace-plan.md`](docs/desktop-workspace-plan.md) —— 方案与三层分界
- [`docs/plugin-mechanism.md`](docs/plugin-mechanism.md) —— 程序三要素与契约五条
- [`docs/agent-per-program.md`](docs/agent-per-program.md) —— 每个程序一个智能体
- [`docs/dev-mode.md`](docs/dev-mode.md) —— 开发者模式的边界
- [`docs/settings-and-icon.md`](docs/settings-and-icon.md) —— 设置与窗口图标
- [`docs/shell-and-native.md`](docs/shell-and-native.md) —— 窗口壳与原生程序

## License

MIT
