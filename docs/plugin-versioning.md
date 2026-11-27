# 插件程序的 git 版本化 —— 方案简报

> 目标读者：要落手的人（或下一轮的我）。所有「pythongit 支持 / 不支持」的结论都是**实跑**出来的，
> 不是读 README 读出来的；探针代码见文末「探针留档」。

## 0. 一句话

**每个插件目录就地长成一个独立 git 仓（`plugins/<pid>/.git`），唯一落盘路径 `toolbox._commit()` 在四道校验通过之后自动提交一版；
版本化是旁路，失败只留日志，绝不让写入跟着失败。**

**开发者模式下，插件自己的智能体拿到四个动词**（`dev_versions` `dev_show` `dev_diff` `dev_revert`）：
**能看自己的历史、能回到能工作的一版；但没有 remote / 分支 / reset / 任意 sha ——**
**自我演化要的是「记得住昨天」，不是「能改历史」**（§7.5）。

依赖：`pure-python-git`（PyPI `pure_python_git` 1.0.2，MIT，纯 Python，无运行时依赖，`requires-python>=3.9`，
装一个 `pygit` 脚本；`[git]` extra 才会额外装 `git` 同名 shim —— 我们**不要**那个 shim）。
**它在默认依赖里**（`dependencies`，不是 extra）：钢性成本只有"多装一个纯 Python 轮子"，
换来的却是"装完就有历史"—— 让用户为了回退去补一条 `pip install ...[vcs]`，等于这个能力默认不存在。

---

## 1. 不变量（先定这些，后面所有设计都是它们的推论）

| # | 不变量 | 为什么 |
|---|---|---|
| 1 | 版本化**不能**成为写入的单点故障 | 程序写不进去 = 用户的功能没了；历史上多一版少一版是可以接受的损失 |
| 2 | `data/` 永不入库 | 那是用户攒下的数据，不进版本、不进复制、不进回滚 |
| 3 | 历史**只增不改** | 回退 = 把旧内容再提交一次；不做 reset/force，不重写已有版本 |
| 4 | 插件自己的智能体**拿不到 git**，只拿到四个动词 | 能力面就是边界。但边界不是「不给」，是「只给不会造成不可逆损失的那几个」：看得见历史、回得去某一版，且回退 = 重新提交（不 reset）。见 §7.5 |
| 5 | 人必须能用普通 git 客户端打开 | 这是「借 pure-python-git」的一半动机：机器上没有 git 也能记版本，有 git 的人能直接 `git log` |

---

## 2. 仓库结构：就地 `.git`，不外置

```
workspace/plugins/<pid>/
├── .git/              ← 新增，就地
├── plugin.json        跟踪
├── backend.py         跟踪
├── ui/**              跟踪（排除 __pycache__ / *.pyc）
├── data/              不跟踪（用户数据）
└── __pycache__/       不跟踪
```

**为什么不用外置 gitdir（`workspace/.versions/<pid>` + `--work-tree`）**：`Repository(path, gitdir=...)` 是支持外置的，
但外置的代价是「人打不开」——`git -C plugins/x log` 失效，得记 `--git-dir/--work-tree` 两个参数。
就地 `.git` 换来的正是不变量 5。

**就地 `.git` 要付的价**：目录里多了一个标准隐藏目录，于是现有四个「遍历插件目录」的地方都要认它（见 §7）。
这四处是本方案唯一有真实回归风险的面，必须一起改，不能先合一半。

---

## 3. 入库范围：显式白名单，**不用**任何 ignore 机制

实测（探针 A/B 段）：`add -A` 与 `add .` **都不遵守 `.gitignore`**，把 `data/store.json` 和
`__pycache__/*.pyc` 一起收进了索引；改用 `.git/info/exclude` 同样无效。`add <显式路径>` 才干净。

所以：

```python
TRACKED_FIXED = ("plugin.json", "backend.py")      # 顶层两个固定文件
TRACKED_GLOB  = "ui/**"                            # 递归，跳过 __pycache__/*.pyc/.git
```

`add_paths(repo, [...])` 只喂这份清单。仍然写一份 `.gitignore`（`data/`、`__pycache__/`、`*.pyc`），
**但只当注释用**：它对人有用（拿真 git 打开时不至于脏），对 pythongit 无效——别指望它。

> 副产物：因为不依赖 ignore，**删除也要自己管**。文件被删掉时要 `workdir.rm_paths(repo, gone)`，
> 否则旧文件会一直留在索引里、每次提交都「无差异地」残留。

**实测（2026-11-23 落地时）：`add_paths` 相对 repo 的 worktree 解析，不依赖进程 cwd。**
证据：把 cwd 切到 `C:\Windows` 后对临时插件目录第一次提交 → 成功，`files_at()` 拿到
`['backend.py', 'plugin.json', 'ui/index.html']`；再把 cwd 切到插件目录本身提交第二次 → 也成功。
所以 `pluginvcs` 里**不需要 `os.chdir`**（那是全局副作用，会跟 HTTP 线程/智能体线程抢）。

同一次探针顺带确认了行尾：读回的是 `b"def ping() -> dict:\r\n    return {'ok': 1}\r\n"`
—— Windows 上 `Path.write_text` 落的确实是 **CRLF**。这是 §7.5「按字节写回」的实证。

---

## 4. 什么时候提交（触发器）

| 事件 | 是否提交 | 说明 |
|---|---|---|
| `plugin_write` / `dev_plugin_write` 成功（四道校验通过） | **是** | 唯一入口 `toolbox._commit()`，两个调用方共用 |
| 同上但校验失败（整份回滚） | 否 | 回滚后工作树回到旧版，本来就没变化；历史里只有「真的成了的版本」 |
| 首次见到没有 `.git` 的老插件 | 是（bootstrap） | 消息 `bootstrap: 接管既有版本`；**懒触发**（第一次写它才建，不在启动时扫一遍） |
| 用户/程序改 `data/` | 否 | 不进版本 |
| `plugin_delete` | 否 | 见 §7 第 3 条 |

**钩子位置**：`toolbox._commit()` 里 `_verify` 通过、`_rt().scan()` 之后，包在 `try/except Exception` 里，
异常只 `log`。这一处钩住 = 小助理和开发者模式两条路都覆盖（它们本来就共用这条落盘路径）。

**提交频率**：一次成功写入 = 一版。不做节流、不合并——「一版」的语义就应该是「一次被接受的改动」。

---

## 5. 身份与提交信息

```python
author = (f"{title}", f"{pid}@plugin.local")     # title 取 plugin.json 的 title，退化为 pid
```

`git log` 里就能看出**是哪个程序**改了自己、什么时候。

消息规范（subject 由动作推出，body 记录这次改了什么）：

```
feat: 加 pong 能力                       ← 首次出现 / 新增带注解的公开函数
fix: backend.py 语法通过（4 个能力）     ← 上版没通过体检、这次通过了（罕见但有意义）
format: 只动 ui/index.html              ← 只改界面
rewrite: 整份重写                        ← 文件集合与内容都大幅变化
bootstrap: 接管既有版本                  ← 建仓首版

Files: backend.py, ui/index.html
Caps:  todo.add, todo.list
By:    self | agent | user               ← caller 栈给出
Check: verify=ok, import=ok              ← 四道校验的结论
```

body 里的 `Files/Caps/By` 是给**人**看的：`git log -1` 一眼知道这版动了什么、谁动的手。

---

## 6. 实测能力边界 —— 这张表决定了实现形态

pythongit 1.0.2 自称 141 个内置子命令，但**标志位兼容是残缺的**，而且有几处是静默失败。实跑结果：

| 操作 | 结果 |
|---|---|
| `init` / `config user.*` / `add <显式路径>` / `commit -m` / `log --oneline` / `status -s` / `ls-tree -r <tree>` / `cat-file -p <oid>` | ✅ 可用 |
| `add -A`、`add .` 遵守 `.gitignore`（及 `.git/info/exclude`） | ❌ **不遵守**，把 `data/`、`.pyc` 全收进索引 |
| `status --porcelain` | ❌ argparse 直接拒（只认 `-s`） |
| `ls-tree --name-only` | ❌ 只认 `-r` |
| `rev-parse --short` | ❌ 只认 `--git-dir/--show-toplevel/--verify` |
| `diff --stat` | ❌ 只认 `--cached` |
| `log -1 --format=%H` | ❌ 只认 `--oneline/-n` |
| `show <rev>:<path>`、`cat-file <rev>:<path>` | ❌ rc=128 / `fatal: Not a valid object name` |
| `checkout <rev> -- <path>`、`restore --source` | ❌ argparse 拒（checkout 只认 `[-b] [target]`，restore 只认 `[--staged] paths`） |
| `HEAD~1` 这类 rev 语法 | ❌ `fatal: not a tree-ish`（`checkout HEAD~1` → `pathspec 'HEAD~1' did not match`） |
| `reset --hard HEAD~1` | ❌ rc=128 且**输出为空**（静默失败，最危险的那种） |
| 空提交 | ✅ rc=1 + `nothing to commit, working tree clean`（可判定，不会白建一版） |
| `read_object(repo, 8 位短 sha)` | ❌ `KeyError` → **自己永远存全号**，短号只用于显示 |
| 每次 CLI 命令的进程开销 | ≈145–175 ms（首次 `init` 246 ms） |
| 模块内调用：`Repository.init` 3–17 ms、`add_paths` 11 ms、`cli.cmd_commit` 13 ms | ✅ 一次完整提交 **≈42–45 ms**（含首版 61 ms） |

**结论（很重要）**：不要写成「拼 git 命令字符串」的薄壳。
**走模块 API**（`pythongit.Repository` + `workdir.add_paths/write_tree/flatten_tree` + `objects.read_object/parse_commit` + `refs`），
只在「建提交对象」这一步复用 `cli.cmd_commit(["-m", msg])`（argparse 已解析、13 ms、比子进程省一个数量级）。
这样既不碰残缺的标志位，也不受 `HEAD~1` 缺失影响——历史自己沿 parent 链走。

---

## 7. 四个动词 + 四处补丁

### 动词

```python
ensure(pdir)                     -> Repository        # 有仓打开，无仓 init + 写身份
commit_all(pdir, message)        -> (bool, str)        # 永不抛；无差异/失败都返回 False + 原因
versions(pdir, limit=20)         -> [{sha, short, msg, epoch}]   # first-parent 链
read_at(pdir, sha, path)         -> bytes | None       # commit→tree→flatten→blob
revert(pdir, sha, paths=None)    -> (bool, str)        # read_at 出来的内容写回工作树，**再走 _commit()**
```

- `versions` / `read_at` **不解析任何 CLI 输出**，全在 objects 层（`parse_commit` / `flatten_tree` / `read_object`）。
- `revert` 的语义必须是「**重新提交一版**」，而不是 `reset`：这样四道校验仍然挡在前面（回退到一版坏代码会被拒），
  并且历史不重写。回退按钮点下去，效果等于「把那一版的内容再交一次」。
- `commit_all` 的前提是 `os.chdir(pdir)`（`add_paths` 按 cwd 解析相对路径）——**必须**在 `finally` 里 chdir 回来，
  否则整个宿主进程的相对路径都跟着偏。

### 补丁（不变量「就地 `.git`」的价）

| 位置 | 现状 | 必须加什么 | 不加会怎样 |
|---|---|---|---|
| `toolbox._snapshot()` | 拷走 `pdir` 下除 `data/` 外**所有**文件 | 跳过 `.git` | 每次写都拷一份仓库进 `.backup/`，几十毫秒变几秒 |
| `toolbox._restore()` | 删掉 `pdir` 下除 `data/` 外**所有**文件再复原 | 跳过 `.git` | **一次失败的回滚会把整个历史删掉**（最致命的一处） |
| `toolbox.plugin_delete()` | `rmtree(pdir)` | 先把 `.git` 挪到 `.backup/<pid>.git-<ts>`（或 `.trash-<pid>-git`） | 删程序 = 连历史一起没了，而「删了想找回上一版」正是最需要历史的时候 |
| `pyproject.toml` 打包 / `web/` 静态路由 | 静态路由只服务 `plugins/<pid>/ui/**`（`server.py:149` 已验，`.git` 不可达） | 打包/备份脚本排除 `.git` | 仓库被塞进 artefact |

其余遍历点已核对：「`scan()` 只读 `plugin.json`/`backend.py`，不递归」；
`dev_tree` 会多显示一个 `.git`（噪声，可接受，也可以在 `tree()` 里过滤隐藏目录）。
`plugin_verify` / `_import_probe` 不受影响（子进程 import `backend.py`，`.git` 无关）。

---

## 7.5 开发者模式下的能力面（四个动词，不是 git）

> 这一节修正了本节开头那条「插件自己的智能体拿不到 git」。用户的原话：
> **「开发者模式下应该暴露给 agent 插件能力，具备 git 能力也是插件能够自我演化的重要一环。」**

### 为什么它是「演化的一环」，而不是一个可选的展示面板

先把「没有它的时候 agent 在干什么」写清楚 —— 这才是判据：

- 它改自己走 `dev_plugin_write`，四道校验只保证**合法**（能装载、函数有注解），
  **不保证不退化**（把 `pong` 删掉了，校验照样过）。
- 改坏了，它唯一的手段是**凭上下文记忆重写一遍**。而上下文会被折叠：
  本内核 100 万字符预算、折叠即丢 —— 几轮之后它就不记得自己上一版长什么样。
- `.backup/<pid>/` 只有**一版**（上一次提交前）。退一次可以，退两次就没了。

于是「自我演化」缺的恰好是两件事：**记得住**（历史）与**退得回**（回退）。
给这两件事 = 给 agent 一份不会忘的短期记忆，而这正是它靠自己做不到的。

### 能力面（3 读 1 写，全部只作用于自己的仓）

| 工具 | 参数 | 语义 | 没有它会怎样 |
|---|---|---|---|
| `dev_versions(limit=20)` | 无 pid | 自己仓的 first-parent 链：`v3 17:02 revert: 回到 v1 · 2 文件 · self` | 每一轮都是失忆重写 |
| `dev_show(version, path)` | 无 pid | 某一版的某个文件全文（`version` 只接受链上出现过的号/短号） | 报错是 v2 引入的，却看不见 v2 写了什么 |
| `dev_diff(version, path=None)` | 无 pid | 行级 diff：那一版 → 当前工作树 | 只能整份回退，定位不到「哪一行改坏了」 |
| `dev_revert(version, paths=None, why="")` | 无 pid | 把那一版的内容**再提交一次**（走 `_commit`，四道校验照样拦） | 演化闭环没有回路 |

配套加一个参数：`dev_plugin_write(files, why="")` —— 让 agent 自己写一句**为什么**
（落到 commit body 的 `Why:`，去换行、截 200 字）。历史因此从「一堆 sha」变成
**可读的演化日志**，而这正是它下一轮要读的东西。

### 四条安全属性（每条对着一个具体的越权面）

1. **没有 pid 参数**（与 `dev_plugin_write` 同一条纪律）：`_me()` 从 caller 栈取 →
   「碰别人的仓」在**签名上**就不存在。少一个参数，少一条能走错的路。
2. **没有任意 sha**：`version` 必须在 `versions()` 的链上 → 白名单校验，
   不做 sha 语法解析（也就不存在 `HEAD~1`、`rev:path` 那些 §6 里全都不支持的写法）。
3. **回退 = 再提交一版**：不 reset、不 force、不删历史。于是**回退本身可逆**
   （历史只增不改，可以再回退回来），且四道校验仍然挡在前面 ——
   回退到一版坏代码会被拒，并告诉你为什么。回退不是绕过校验的后门。
4. **`data/` 连只读都不给**：`dev_show("data/store.json")` **入口就拒**，
   白名单与写路径同一份（`plugin.json` / `backend.py` / `ui/**`）。
   理由不是「历史里本来没有」，而是「这个能力一次都不该被打开」。

### 明确不给的（写死，别让下一轮的我心软）

- ❌ 没有 `git` 这个动词：没有 remote / fetch / push / branch / merge / tag / stash /
  reset / clean / rebase。跨机器同步是另一个功能，不是「自我演化」。
- ❌ 不删版本、不改写历史、不切分支。任何一处都能把 agent 推到
  「历史里没有一版能工作」的状态，而那时旁边没人救 —— 装载发生在启动时。
- ❌ 不跨插件：`dev_versions` 里看不见别人，`dev_revert` 也碰不到别人。

### 实现上的一点好消息

`kernel.mount_devtools()` 是**扫模块里所有公开函数**自动挂上去的（`kernel.py:584`），
所以这 4 个函数加到 `devtools.py` 里就自动进能力面，**内核一行不用改**；
它们只是 `pluginvcs` 那几个动词的薄封装（`versions` / `read_at` / `revert` 在 P0 里已经要写），
所以「把 git 交出去」的成本其实很低 —— 低是因为交出去的不是 git，是四个语义明确的动作。

### 一条实测出来的硬规矩：回退必须按**字节**写回

`revert` 要用 `open(..., "wb")` 把 `read_at()` 拿到的 bytes 直接写回，
**不许 decode→encode**。三条现场证据（见文末探针留档）：

1. 同一份源码文本，默认落盘（Windows = CRLF）与 `newline=""`（LF）
   得到的是**两个不同 blob**。谁在这条路上换了约定，「回到那一版」就变成
   「回到那一版的内容、但字节不同」—— 历史里立刻多一行整文件假 diff。
2. 文件不是 UTF-8（比如 GBK）时 `decode("utf-8")` **直接炸**（`UnicodeDecodeError`）；
   `wb` 在 GBK / BOM / 普通 UTF-8 三种文件上都逐字节还原。
3. 所以 `read_at() -> bytes` 不是风格问题，是**契约**：拿到的必须是能直接写回磁盘的东西。

**推论（很重要，别反着做）**：不要为了「统一 LF」去改 `_commit` 的落盘。
Windows 上 `write_text` 现在写的就是 CRLF，既有插件的 blob 全是 CRLF；
一旦把落盘统一成 LF，**每个插件下次写入都会变成「整文件重写」的一版** ——
用一个假 diff 换一个假整齐。blob 的判据是「磁盘上是什么」，不是「我觉得应该是什么」。

---
## 7.6 结果体量：**分页优先，落盘兜底**（2026-11-23 定）

用户问：「文件内容可能很大，是不是把结果写到临时文件、返回文件路径更合适些？」

方向对 —— 大结果会淹掉 agent 的上下文。但**落盘不该是默认**，而且现状有个比「太大」更严重的问题，得先修它。

### 现状：不是「太大」，是**静默截断**

| 位置 | 事实 |
|---|---|
| `toolbox.py:101` | `{"text": text[:20000], "lines": 全文行数}` —— 截到 20000 字符，**返回值里没有一个字段说它被截了** |
| `link.py:252-254` | `parts=(Part.t(res.text or ""),)` —— 链路层**零**上限，20000 字符原封进远端上下文 |

后果：agent 看见 `lines: 3000`、实际拿到 800 行，**它无法自证不完整** —— 于是相信自己读完了整份文件，然后基于半份文件「改自己」。这比报错糟得多：报错会让它换办法，静默会让它写错。

**所以第一条不是落盘，是说实话**：返回 `truncated` / `shown_lines` / `total_lines`，并让 `dev_read(path, offset=1, limit=200)` 能接着读。

### 三层，按代价从低到高

1. **说实话 + 分页**（零新增故障面）：`dev_read` 加 `offset`/`limit`；越界不只截，还明确 `more: true` + 下一页参数。
2. **改结果形状**（省掉一次往返）：四个动词默认返**摘要 + 少量样本**，不是全文。
   - `dev_versions(limit=20)` —— 每版一行，本来就不大；
   - `dev_show(v)` **不带 path** → 该版**文件清单 + 每文件行数 + 变更摘要**（先给目录、再给片段，比给全文和给路径都好）；
   - `dev_diff(v)` → **统计头**（n 文件 / +x−y / m hunk）+ 有限 hunk。
3. **兜底落盘**（最后一道，不是第一道）：仍超硬顶 → 落 `workspace/.dev-cache/<pid>/<ts>-<slug>.txt` + 返路径 + 一句「用 `dev_read(path, offset=…)` 读」。

### 为什么「一律落盘返路径」不划算

1. **省不了往返**：落盘只省这一轮的 token，代价是下一轮多调一次；而 agent 拿到路径后往往还是要全读（它不知道要看哪段）。给「准」比给「全」便宜。
2. **救不了注意力**：20000 字符即使不爆预算，也会挤掉它原本记得的上下文 —— 被折叠后它忘了自己在干什么。落盘解决带宽，不解决这个。
3. **存的是可重算的东西**：git 里的内容随时能重取（实测一次完整提交 ≈42–45 ms，读一版更便宜）。为省一次重算而引入**持久状态**（清理 / 并发重名 / 磁盘增长 / 清理失败），是拿一个新故障面换一点带宽。
4. **破坏可重放**：结果从「确定的一次计算」变成「指向某个时刻的文件」；同一份输入两次跑，读到的可能不是同一份东西。

### 落盘目录的三条硬约束（都来自已核实的代码）

- **必须在工作区内**：`_safe()`（`toolbox.py:55-56`）越界即拒 —— 工作区外的路径 `dev_read` 打不开，「返回路径」就成了比不返回更糟的假承诺。
- **必须不在插件目录内**：`_snapshot()`（`toolbox.py:395-401`）是 `pdir.rglob("*")`，**只**排除 `rel.parts[0] == "data"`，**不排除隐藏目录** → 缓存落进插件目录会被整个拷进 `.backup/<pid>/`（那目录只有一版，每次提交前 `rmtree` 重拷）。
- **必须点开头**：`search()`（`toolbox.py:117`）跳过 `.` 开头的路径段 → `.dev-cache/` 不会被自己的检索扫到。

→ 三条同时满足的位置只有一个：`workspace/.dev-cache/<pid>/`。
附带一条：缓存**丢了不影响正确性**（读不到就报错，agent 改走分页），所以清理可以很粗暴：启动清一次 / 保留最近 20 份 / 24h 过期。

### 阈值（写死，别每轮重议）

| 项 | 值 | 理由 |
|---|---|---|
| `dev_show` 默认 | 200 行 | 一屏看完，约 12–20 KB |
| `dev_show` 硬顶 | 500 行 | 再多就该分页要 |
| `dev_diff` 默认 | 300 行 hunk | 先看统计头，再看最相关的那几个 |
| 落盘门槛 | 12000 字符 | 比 `toolbox.read` 的 20000 更小：20000 是给人的，12000 是给 agent 的 |

### 验收（进 selftest）

- 造一个 3000 行文件：`dev_read` 不传参 → `truncated: true`、`shown_lines == 200`、`more: true`
- 传 `offset=201` → 首行 == 全文第 201 行
- `dev_show(v)` 不带 path → 只有清单，**不含**任何文件正文
- 落盘路径在 `.dev-cache/` 内、且**不在**任一插件目录内（`_snapshot` 之后 `.backup/<pid>/` 里不含它）

## 8. 降级与自检（不变量 1 的实现）

- `import pythongit` 失败 / 版本不符 / 自检不过 → **版本化整体静默关闭**：`commit_all` 恒返回 `(False, "版本化未启用")`，
  写路径一行都不改，只打一条日志。插件功能照常。
- **启动自检**（结果缓存进进程内变量，一次就够）：在 `tempfile.mkdtemp()` 里跑一遍
  `init → add(1 个文件) → commit → log --oneline → ls-tree -r HEAD`，全绿才启用。
  为什么值得多花 ~200 ms：上面那张表证明了「同名命令的标志位随时会变」，靠 pin 版本号挡不住。
- 依赖声明：`dependencies` 里带 `pure-python-git>=1.0.2,<2`（**默认装** —— 版本化是核心能力，
  不该让人补 extra 才有），`vcs = []` 留作空 extra 让老命令 `pip install ...[vcs]` 仍成立。
  pin 上界是因为 §6 那些标志位差异都是 behaviour，不是 bug。
- **降级逻辑不因为"默认装了"而删**：上面那条自检防的是"装了但坏了"（版本漂移 / 半装 /
  被别的包拽走），不是"没装"。少了这层，一次坏的 pythongit 就会变成写不进去。
- 每次提交失败都 `log` 一句 `[vcs] pid=<pid> commit failed: <的原因>`，不弹窗、不打扰用户。

---

## 9. 性能

- 一次成功写入 + 一版提交：**≈45 ms**（模块内），相对 `_verify` 里那次子进程 import（数百毫秒）是噪声。
- bootstrap 8 个既有插件 ≈0.5 s → 所以**懒触发**：第一次写某个插件才给它建仓，启动路径一毫秒都不加。
- 仓库体积：只装文本（`plugin.json`/`backend.py`/`ui/**`），单插件几十 KB 量级；`data/` 不入库是关键。
  加一条保险：`versions()` 只走 `limit` 步、不做全历史遍历。

---

## 10. 边界：明确不做的事

- ❌ 给插件自己的智能体**任意** git 能力（不加 `dev_git`）。给的是四个动词（§7.5），
  其中 `dev_revert` 只是「把旧内容再走一遍 `dev_plugin_write`」，**不拼 git、不解析 CLI 输出**。
- ❌ 分支 / 合并 / cherry-pick / 推远端。单仓单线（`main`），线性历史。
- ❌ 把 `data/` 纳入版本（哪怕用户说「我的数据也想回滚」——那是另一个功能，走数据备份，不是程序版本）。
- ❌ 一个仓管所有插件（用户要的是「每一个具体的插件程序」各自成仓）。
- ❌ 装 `pure-python-git[git]` 的 `git` shim（会 shadow 系统 git，坑远大于收益）。

---

## 11. 分期

| 期 | 内容 | 判据 | 状态 |
|---|---|---|---|
| **P0** | 单仓 + 显式白名单 + `_commit` 钩子 + bootstrap + 四处补丁 + 自检降级 | 改一次程序 → `versions()` 多一版；`data/` 不在库里；写入失败时版本化不添乱 | ✅ 已落地 |
| **P1** | 只读面板：版本列表（时间/摘要/改动文件）+「回退到这一版」（语义 = `revert` 再提交）；HTTP `GET /api/p/<pid>/versions` | 人在左栏能看见自己程序的历史并回退，全程不碰命令行 | ✅ 已落地 |
| **P1.5**（**不是**可选项） | 开发者模式的能力面：`dev_versions` / `dev_show` / `dev_diff` / `dev_revert`（+ `why`），全部只作用于自己的仓，`_me()` 取 pid、**无 pid 参数**（§7.5） | 智能体能看见自己上几版、能回到能工作的一版，全程不碰 reset / remote | ✅ 已落地 |

落地顺序有一个**不能调换的依赖**：`toolbox.read` 的 `offset`/`limit` 必须排在四个动词之前做 ——
否则「分页」没有载体，§7.6 里「太长就落盘返路径」这条路根本走不通（拿到路径也读不动）。
实际落地时它确实是先做的。

---

## 12. 风险与对策

| 风险 | 对策 |
|---|---|
| `.git` 被 `_restore` 删掉（历史一次性消失） | §7 三处补丁 + 一条断言：`_restore` 前后 `.git/HEAD` 内容不变 |
| pythongit 未来版本改标志位/行为 | 只用模块 API（不用标志位）+ 上界 pin + 启动自检 |
| 行尾漂移：`Path.write_text` 在 Windows 上把 `\n` 写成 `\r\n`，blob 里就是 CRLF（**已实测**：`b"...dict:\r\n ..."`） | ~~落盘统一 LF~~ → **不统一，改为按字节写回**（§7.5）。`read_at() -> bytes`，回退用 `wb` 原样落盘。统一 LF 会把每个插件**下一次写入**变成「整文件重写」的假 diff —— 既有插件的 blob 全是 CRLF，拿一个假 diff 换一个假整齐，不划算 |
| 每次写都多 45 ms | 可接受；真嫌慢就改成「写线程提交」，不进请求路径 |
| 并发（HTTP 线程 + 智能体线程同时写同一插件） | 每 pid 一把 `threading.Lock`，提交与 `_commit` 的落盘在同一把锁里 |
| 老插件没有 `.git`、首次写入时 bootstrap 失败 | 失败就当没版本化，写入照旧；日志留痕，下次再试 |

---

## 13. 验收判据（写死在 selftest 里）

1. 新插件首次写入 → `.git` 存在，`versions()` 长度 1，`data/store.json` **不在** `flatten_tree(HEAD)` 里。
2. 再写一版 → 长度 2；`read_at(HEAD~1 的全号, "backend.py")` 等于第一版内容。
3. 写一版**校验失败** → 回滚后 `versions()` 长度不变（坏版本不入历史）。
4. `_restore` 之后 `.git/HEAD` 与调用前一致（补丁生效）。
5. `commit_all` 在 pythongit 被拔掉时返回 `(False, ...)` 且不抛，插件写入成功。
6. 删插件 → `.backup/<pid>.git-*` 存在，历史可取回。
7. 8 个既有插件全量 bootstrap 后，宿主启动耗时**无变化**（懒触发成立）。

---

## 落地记录（2026-11-23）

方案到代码之间有三处偏离，都是**跑出来的**，不是想出来的：

| 方案里写的 | 实际 | 为什么 |
|---|---|---|
| `import pure_python_git`（文档早期写法） | **`import pythongit`** | 包名 `pure-python-git`、发行名与导入名不同；`pygit` 只是 `Scripts/pygit.exe` 那个入口，不是可导入模块。`available()` 里同时认这几个名字，缺一个都算不可用 |
| 每次成功写入都「多一版」 | **空提交不建版** | 原样回写（内容没变）→ `tree == head_tree` → `nothing to commit`。这是对的：历史里只该有**真的变了**的版本。相应地，selftest 里「多一版」的断言必须用**内容真的变了**的写入来验 —— 第一版断言就是写错了这里，跑出来 `1 → 1` 才发现的 |
| 「四处补丁」 | 另加一条**懒触发 bootstrap** | 8 个既有插件不在启动路径上建仓；第一次成功写入时才 `init`，否则每次开机多付 ~0.5 s |

验收（2026-11-23 全绿）：

- `python -m agentworkspace.selftest` → **209 通过 / 0 失败**（在补齐 fixture 的临时工作区上跑；
  真工作区因 `pomodoro`/`memo` 已被删而少 16 条，那些是**数据缺失**，`_fixture_note()` 已如实标注）
- `node tests/turn.test.js` → **135 通过 / 0 失败**
- `node tests/md.test.js` → **81 通过 / 0 失败**

新增断言（落在 selftest 里，共 ~30 条）：九项能力面恰好于此、四个动词无 `pid` 参数、
`data/` 连只读都不给、写坏的那一次不入历史、旧版按字节回读、空提交不建版、
`revert_files` 不动 HEAD、`archive` 能把 `.git` 挪走、版本面板存在且不弹 `confirm`。

---

## 探针留档

三轮探针（`probe_git.py` / `probe_git2.py` / `probe_git3.py`）与候选实现
（`pluginvcs.py` 的 `ensure / commit_all / versions / read_at` 四件全部跑通）都是**一次性**的，
跑完即删、不进仓库；结论已并进 §6 与 §7，这里只留现场输出：

```
commit#1: (True, '[main 853a3ac] init: demo')     61.2ms（含首次 init）
commit#2: (True, '[main cb3c1f9] feat: v2')       44.8ms
commit#3(无变化): (False, 'nothing to commit, working tree clean')
versions: [{'short': 'cb3c1f97', 'msg': 'feat: v2'}, {'short': '853a3ac0', 'msg': 'init: demo'}]
HEAD~1 backend.py = b"def ping() -> dict:\r\n    return {'v': 1}\r\n"   ← 注意 CRLF
HEAD 里的文件： ['backend.py', 'plugin.json', 'ui/index.html']
data/ 进库了吗： False     pyc 进库了吗： False
```

复现方式：`pip download pure-python-git --no-deps` 解 wheel，对一个假插件目录按 §7 的四个动词走一遍。
**不要**用 CLI 拼字符串 —— 一拼就撞上 §6 那串不支持。

（`\r\n` 就是 §12 那条行尾漂移风险的现场证据。）

### 第二轮探针（开发者模式能力面，2026-11-23）

```text
Q1 v1（默认落盘，Windows 文本模式）blob: b"def ping() -> dict:\r\n    return {'v': 1}\r\n"
Q1 v2（newline='' 落盘）        blob: b"def ping() -> dict:\n    return {'v': 1}\n"
Q1 同一份源码、两种约定 → blob 相同吗： False        ← §7.5 那条硬规矩的现场证据

Q2 原字节: b"def ping() -> dict:\n    return {'note': '\xd6\xd0\xce\xc4'}\n"   ← GBK
Q2 decode('utf-8') 直接炸： UnicodeDecodeError 'utf-8' codec can't decode byte 0xd6 in position 41

Q3 wb 写回逐字节还原： [('backend.py', True), ('gbk.py', True), ('bom.py', True)]
Q4 BOM 文本模式写回一致吗： True | wb 一致吗： True
```

**一段自我纠错（值得留着）**：第一版探针（`probe_revert.py`）打印过
「Q1 revert 往返逐字节一致： True」—— 那不是证据，是**循环论证**：
它的 v1 和「回退后那一版」两侧用的是同一套落盘约定，等于用同一把尺子量自己。
而且它把「Windows 默认写 CRLF」当成了 v1 的事实，于是 Q2 的反例也被写成了同一份字节
（两次都是 CRLF，看起来「一致」）。真正会咬人的是**两个 writer 用不同约定**，
所以第二版探针只问一件事：同一份文本、两种约定，blob 是否相同 —— 答案是 **False**。

结论落到实现上就一句话：`read_at()` 返回 `bytes`，`revert` 用 `open(..., "wb")` 原样写回；
**不要**为了「统一 LF」去改 `_commit` 的落盘（那会把每个插件下次写入变成整文件重写）。
