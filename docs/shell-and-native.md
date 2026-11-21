# 外壳与原生程序（v1）

这份文档只回答两个问题：

1. **窗口是谁开的**（外壳）
2. **程序可以长什么样**（界面形态：网页 / 原生 Python）

两件事都不碰执行路径 —— 它们只是「人的门」的壳。

---

## 一、外壳：一个窗口，不是一条新路径

宿主本来就是本地 HTTP 服务。所以「外壳」这件事只剩下一个变量：

```
用什么把 http://127.0.0.1:<port>/ 装起来？
```

| 选项 | 形态 | 何时用 |
|---|---|---|
| `pywebview` | 真桌面窗口：无地址栏、无标签、有图标、能最小化 | 默认 |
| `webbrowser` | 一个浏览器标签 | 兜底 |

**边界只有一条：外壳不认识内核。** `agentworkspace/shell.py` 里不出现 `kernel` / `dispatch`，
它只收一个 URL。所以将来换 Electron、Tauri、系统托盘，只动这一个文件。

### 三种结局，必须分清

`webview.start()` 在 WebView2 初始化失败时**照样正常返回**：窗口开着、里面是白的、
没有任何异常。把它当成成功，用户看到的是一个白窗口，而日志里一片祥和 ——
这是最坏的一种谎。

于是外壳等 `window.events.loaded`：

| 返回值 | 含义 | 宿主怎么办 |
|---|---|---|
| `ok` | 窗口起来了，页面渲染完了 | 收摊（用户关了窗口） |
| `blank` | 窗口开了，页面没渲染（WebView2 缺失 / 无交互桌面） | **关掉它**，回退浏览器，打印原因 |
| `unavailable` | pywebview 装不上 / 后端起不来 | 回退浏览器 |

实测（本机，2026-11-21）。第一次跑 `python -m agentworkspace.main`，三种 pywebview 配置
全部 `loaded=false`，WebView2 报 `E_ABORT (0x80004004)`。当时的结论
「WebView2 缺失 / 没有交互式桌面」**说对了现象、说错了原因**。查清楚之后：

| 事实 | 怎么查的 | 结果 |
|---|---|---|
| 进程在哪个会话 | `ProcessIdToSessionId` + `GetProcessWindowStation` | **Session 0**，窗口站 `Service-0x0-3e7$` |
| 那个窗口站可见吗 | `GetUserObjectInformation(UOI_FLAGS)` | `WSF_VISIBLE = 0` —— 不可见 |
| WebView2 装了吗 | 注册表 EdgeUpdate Clients + 安装目录 | **已装**（158.0.4370.33） |
| 活跃控制台会话是几 | `WTSGetActiveConsoleSessionId` | **2** |

所以 `E_ABORT` 不是「缺 Runtime」，是 **Chromium 拒绝在非交互窗口站里建合成器**。
注意这跟「窗口能不能创建」是两件事：`Service-0x0-3e7$` 里 tkinter 照样能建窗口、
`EnumWindows` 也数得到 —— 它只是**永远不可见**。所以「窗口存在」不能当作
「页面渲染了」的证据。

而这条路走得通：以 SYSTEM 身份（`SeTcbPrivilege`）用
`WTSQueryUserToken(2)` + `CreateProcessAsUserW` 把进程投进会话 2，
得到 `WinSta0 / WSF_VISIBLE=1 / 1440x900`，同一个 `python -m agentworkspace.main` 就走 `ok`。

`agentworkspace/qa.py` 把这件事固化了：检测到非交互窗口站就自动重投，然后拿
`EnumWindows` 与像素当判据。用法：`python -m agentworkspace.qa`（`desktop` / `native` / 全部）。
在 Session 0 里加 `--no-relaunch`，它会**如实报 blank 并判四项失败**——
一个验收工具最重要的性质，是它拒绝说谎。

> 一个坑：pywebview 的事件是 `event += handler`，**不是装饰器**。
> 写成 `@window.events.loaded` 会抛 `TypeError`，被外层当成「不可用」——
> 于是回退的理由都是错的（真相是「能开、只是没渲染」）。

---

## 二、程序可以长什么样：两种界面，一个入口

`plugin.json` 的 `"ui"` 决定形态，**默认选网页**：

| `"ui":` | 形态 | 生命周期 |
|---|---|---|
| `ui/index.html` | 网页，活在宿主窗口里 | 宿主管它 |
| `ui/app.py` | 原生窗口（tkinter/PyQt），自己开 OS 窗口 | 独立进程，宿主只拉起/记账/收尸 |

### 什么时候才该写原生窗口

判据很严 —— 满足任一条才选它，否则写网页：

- 程序要**常驻桌面**（便签、悬浮计时、桌面挂件），不该被关进一个标签页；
- 要用**本地资源**（剪贴板、全局快捷键、打印、串口、托盘图标）；
- 要**自己控制窗口**（置顶、无边框、自定义标题栏、贴边）。

### 为什么必须是独立进程

tkinter 的窗口塞不进 webview，两套事件循环也不能共处一条线程。
这是**宿主能力**的边界，不是偷懒。

所以：`agentworkspace/native.py` 负责 `Popen`、记账、收尸；`native-memo.log` 记日志；
起不来时把日志尾巴交回去（而不是假装成功）。

### 但它**不改执行路径**

原生程序拿到的是同一个本地 HTTP 地址：

```
原生窗口点一下 → POST /api/p/<pid>/call/<fn> → Runtime.dispatch(..., source="human")
```

和网页、和 agent 是**同一条路**。审计里它们只差 `source` 的值。

### 写原生程序的 SDK

```python
from agentworkspace.sdk import Native

ui = Native()                      # 宿主注入 AW_BASE / AW_PID / AW_PLUGIN_DIR
res = ui.call("add", text="记一句")  # → {"status","text","data"}
ui.on_event(lambda ev: ...)        # 订阅：agent 动过的数据，窗口要自己跟着变
```

三条纪律（违反任何一条都会让它变成死界面）：

1. **界面不存业务状态** —— 只存 `data/store.json`，人和 agent 看同一份；
2. **每次操作后重新问后端** —— 自己往列表里塞，就看不见 agent 改的数据；
3. **订阅事件** —— agent 加了东西窗口要自己变，否则用户以为它卡住了。

### 三个已经踩过的坑

1. **入口文件叫 `app.py` 会撞包名。** Python 把脚本目录插到 `sys.path[0]`，
   于是 `import agentworkspace.sdk` 解析到 `ui/app.py` 自己 → `'app' is not a package`。
   修法：`agentworkspace/native.py` 用 bootstrap 启动，项目根排最前、脚本目录排最后。
2. **宿主被强杀会留孤儿窗口。** 子进程收不到任何通知，窗口飘在屏幕上，点它每次都失败。
   修法：`sdk.py` 起一个看门线程，两次连不上宿主就 `os._exit(0)`。
3. **能力名撞 Python 内置名。** 定义 `list()` 之后，模块内所有 `list(...)` 都调到它自己，
   报错是「list() takes 0 positional arguments」——没人看得懂。
   修法：`check_plugin` 把撞内置名直接判为错误，并在 `system.md` 写进契约。

---

## 三、实测证据（2026-11-21，真桌面，不是设计稿）

| 断言 | 怎么验的 | 结果 |
|---|---|---|
| **桌面窗口真的渲染出来了** | 会话 2 里 `EnumWindows` 找标题 + 全屏截图 | `WindowsForms10.Window`，`1180x800`，`壳的结局 ok`，退出码 0 |
| 壳在白屏会话里不撒谎 | Session 0 里 `python -m agentworkspace.qa desktop --no-relaunch` | 报 `blank`，**四项全失败** |
| 原生窗口真是 OS 窗口 | `EnumWindows` 数标题含「便签」的顶层窗口 | 开 → **1**（`TkTopLevel` 446x509），关 → **0** |
| 原生窗口走同一个 dispatch | 窗口写一条后 `dispatch("memo","notes")` | 读到同一条 |
| 网页程序不会被当成原生拉起 | `POST /api/pomodoro/open` | `status=error` |
| 内核认得两种界面 | `Plugin.ui_kind` | memo=`native`，其余=`web` |
| **GUI 里真造出一个程序** | 界面上说「做个喝水提醒」→ SSE → 首页 | 新增 `water`（5 能力 + `ui/index.html`）；`plugin.loaded` 1 次、`turn.handoff` 1 次；接力轮里模型调用 `water_today` / `water_history`；首页 5 → **6** 张卡片 |
| 连上后能再发一条 | `selftest [9]`：一个永不结束的 `Turn` 下再 `send()` | 返回「上一轮还没结束」，不抛异常 |
| 整套不回归 | `python -m agentworkspace.selftest` | **29/29 全绿** |
| 真窗口验收可重复 | `python -m agentworkspace.qa` | desktop 4/4、native 3/3，退出码 0 |

### 这次测试抓到的两个真 bug（27/27 全绿时它们都在）

1. **`aaas.Turn.done` 是 property，`link.py` 却写 `.done()`。**
   `TypeError: 'bool' object is not callable`。触发条件是「**连上之后再发第二条**」——
   首轮跑通的测试永远看不见它。selftest `[9]` 就是为它加的。
2. **`send()` 有一个并发窗口。** `_turn` 是在 asyncio 循环里赋值的，而 `send()` 由
   HTTP 线程调用；在这个间隙里第二句能穿过去，**两轮并发**。不报错、只是行为错——
   比崩溃更难发现。修法是把「已受理」在 `send()` 里同步置位（`_busy` + 锁），
   `finally` 保证释放。

## 四、还没做到的（如实记）

- **原生窗口不跟随系统主题。** 宿主跟了 `prefers-color-scheme`（深色），
  `memo` 的 tkinter 窗口却硬编码浅色 `#f7f6f4`，并排放在一起很割裂。
  需要给原生程序一个「读主题」的契约（`plan.md` 已记，未做）。
- 原生程序之间仍然不能互相调用（插件调插件只在网页侧验证过）。
- 原生窗口的**窗口状态**（位置/大小/置顶）不持久化 —— 每次打开都是默认尺寸。
- 没有托盘、没有单实例聚焦（同一个程序开着时再点，只会告诉你「已经开着了」）。
- 验收脚本踩过的坑（已修进 `agentworkspace/qa.py`）：**宿主「HTTP 就绪」≠「窗口已建好」**。
  就绪后立刻找窗口会扑空，必须单独等。
