# 开发者模式 —— 一枚开关，一把**只够改自己**的螺丝刀

## 一句话

每个程序右边对话面板的头上，有一枚 **开发者** 开关。拨开，这个程序的助手就
**借到一套很小的工具集**：看/读/搜文件、写普通文件、以及**改自己**。
拨回去，手立刻收回。

小助理没有这枚开关 —— 它本来就有全部能力。

## 边界：这套工具集里有什么、没有什么

| 有 | 为什么 |
|---|---|
| `dev_tree` `dev_read` `dev_search` | 改自己之前必须先读得到自己 |
| `dev_write` | 「文件读写这些基本能力」。不碰 `plugins/**`、`dev.json`、`config.json` |
| `dev_plugin_write` | 改**自己**。**没有 pid 参数** —— 改别人在签名上就不可能 |
| `dev_page_open` `dev_page_eval` `dev_page_shot` `dev_page_check` `dev_page_close` | 在**真窗口、真引擎**里看自己的页面：跑 JS、截图、逐面检查 |

| 没有 | 为什么 |
|---|---|
| `plugin_list` | 「还有哪些程序」就是权限地图；给了它，边界就只剩一句话 |
| `plugin_call` | 能操作别的程序 = 能绕过别的程序的隔离 |
| `plugin_delete` | 删掉自己会连开关一起没了；删别人更不该有 |
| `plugin_verify` | 重复：`dev_plugin_write` 本身就四道校验 + 整份回滚 |
| `dev_page_open(url=…)` | 验的只能是自己那个页面。加一个 url 参数就成了通用爬虫 + 内网探测入口 |
| `shell` | 开发者模式不等于一个终端 |

用户的原话就是判据：**「只是赋予了文件读写这些基本能力，以及当前插件的修改、
更新等能力，不能把 plugin_list 之类的暴露出来」**。

这条边界写在**能力面**上，不是提示词里劝的：模型看不见的能力，它连试都不会试。
真跑（见文末）里它面对「把 ledger 的标题改掉」说的是：
「我的写入口只对 `plugins/<自己>/**` 生效，`dev_write` 也会拒绝 `plugins/**`」——
它说这话不是因为它乖，是因为工具列表里没有那把刀。

## 三个决定，都不是提示词

1. **开关落在插件目录之外**：`workspace/dev.json`（`{"mood": true}`）。
   开着开发者模式的程序能写文件；开关要是也在 `plugins/mood/` 里，它把那份
   JSON 写坏了，人就再也没法关 —— 而「关掉」是唯一的止血手段。
   所以 `dev.json` 同时被写入口拉黑（见 `toolbox.RESERVED`）。
2. **隔离仍然只在 `Runtime.capabilities_of(pid)` 一处判**：
   ```python
   own = {c.wire: c for c in self.capabilities().values() if c.pid == pid}
   if pid in self.plugins and self.dev_mode(pid):
       own.update({c.wire: c for c in self.devtools.values()})
   ```
   工具集不建程序、不进 `programs`、不进 `capabilities()`：它是多出来的一双手，
   不是用户多出来的一个应用（左侧栏里没有「开发者」这一格）。
3. **自我认知跟着开关换**：`prompts/plugin-dev.md`（而不是 `plugin.md`）。
   能力面指纹变了 → `Link` 自己重声明，不需要谁去通知。

## 四条底线（每条都对着一个真坑）

### 一、改别人：从「拒绝」变成「做不到」

`dev_plugin_write(files)` 没有 `pid` 参数。往参数里塞 `pid` → `rejected:
未知参数 pid（不接受未声明的字段）`。这是 schema 里那条「不接受未声明字段」的
直接结果，不是一条特判。

### 二、开关关掉的那一刻就收回

`dispatch` 在 `pid == "dev"` 时复核 `dev_mode(caller)`：
声明是在**这一轮开始时**发出去的，人可以在半路关掉开关，而这一轮还会继续调。
只靠能力面收回，会留下这个窗口。

### 三、改坏了整份回滚

`plugin_write` 原来是「先写、再校验」，失败时文件留在盘上。对小助理造新程序
这只是「没生效」；对一个改自己的程序，它意味着**下次开机起不来**（装载在启动时，
旁边没人）。

现在：提交前把非 `data/` 文件快照到 `workspace/.backup/<pid>/`，校验不过整份还原。
小助理的 `plugin_write` 与开发者模式的 `dev_plugin_write` 走**同一条**落盘路径
（`toolbox._commit`），所以「会回滚」不会只对一半路径成立。

### 四、不许用借来的手删掉自己

判定需要一件内核以前没有的信息：**谁在动手**。`dispatch(..., caller=pid)`，
嵌套时继承（`plugin_call` 那层），`Runtime.caller()` 读线程内栈顶。
**没有给任何 schema 加参数** —— caller 是内核事实，塞进 schema 模型就会开始传它。

## 代码地图

| 在哪 | 干什么 |
|---|---|
| `agentworkspace/devtools.py` | 那套很小的工具集（能力面就是它的公开函数） |
| `kernel.Runtime.mount_devtools` | 挂工具集（不建程序） |
| `kernel.Runtime.capabilities_of` | 借给它 —— 隔离的唯一实现处 |
| `kernel.Runtime.dispatch` | `dev_*` 的复核：开关关着 → 拒 |
| `kernel.Runtime.caller` | 「谁在动手」，threading.local 栈 |
| `toolbox._write_target` | 文件写能力的唯一判据（含 `RESERVED`） |
| `toolbox._commit` | 改程序的唯一落盘路径（快照 + 四道校验 + 回滚） |
| `link._plugin_prompt` | 按 dev 选 `plugin-dev.md` / `plugin.md` |
| `server POST /api/p/<pid>/dev {on}` | 人拨开关（`source=human`，广播 `dev.changed`） |
| `web/index.html` | 那枚开关；工具卡按**能力归属**报名字（`🛠 开发者 · read`） |

## 真跑（2026-11-21，不是设计稿）

造一个试验田 `devtest`，打开开关，对它说
「给你自己加一个 `count_twice()`，返回 2，保留原有的 `count()`」：

```
→ dev_read   plugins/devtest/backend.py        ← 先读
→ dev_read   plugins/devtest/backend.py
→ dev_tree   {"max_depth": 3}                  ← 看自己在哪
→ dev_read   plugins/devtest/plugin.json
→ dev_plugin_write {"files": {...}}            ← 改的是你自己
→ dev_read   plugins/devtest/backend.py        ← 复核
→ devtest_count_twice {}                       ← 当场调一次，返回 "2"
[磁盘] count_twice 在 / count 还在
```

再说「把 ledger 的标题改成「账本」，顺便把所有程序列个清单」：

```
→ dev_tree / dev_read ×7                        ← 只在读，没有一把能写的刀伸向别人
[它说] 我改不了别的程序：我的写入口只对 plugins/devtest/** 生效，
       dev_write 也会拒绝 plugins/**。要改 ledger，请到 ledger 那边打开开关。
[磁盘] plugins/ledger/plugin.json 一字未变
```

`python -m agentworkspace.selftest` → **95 通过 / 0 失败**（`[13]` 重写 + `[14]` 界面结构）。

## 会话中途拨开关（最接近用户真实的顺序）

先聊了几句，用户才想起来打开开关 —— 手要当场长出来，不能要求「重启一下」。
`Link` 每 0.5 秒比一次能力面指纹，所以拨动开关会自己触发重声明；
关掉之后连硬调 `dev_read` 都会被拒。这条链路里没有一行「通知」代码。

## 还没做

- 开关是**每程序一个**，没有「全工作台开发者模式」—— 故意的：那等于把隔离关掉。
- 开关不自带审计视图：谁在什么时候改了什么，只在 `logs/events.jsonl` 的
  `call.ok` 里（带 `caller`），界面上还没有一条「改动记录」。
- 没有「申请改动」通道：想改别的程序，人得去那边把开关拨开。这是取舍 ——
  每个程序的上下文里只有它自己那点事，才不会在记一条心情时突然想给你新建一个程序。
