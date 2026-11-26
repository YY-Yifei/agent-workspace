# 已实施（0.4.7）

五个动词落地：`dev_page_open` / `dev_page_eval` / `dev_page_shot` /
`dev_page_check` / `dev_page_close`。**实测数字**（不是推演）：

| 门 | 结果 |
|---|---|
| 冒烟（真插件 dingtalk，copy 模式） | 六面全绿；窗口 1180×800 → 实测 1167×765（dpr 2.0），页面区 725×765 |
| `_tmp/p43_gates.py`（eval 语义 / 页面 / 变异 / 隔离） | 20 项通过 / 0 失败 |
| `_tmp/p43_e2e.py`（真 Runtime + 真 dispatch + 真 devtools） | 21 项通过 / 0 失败 |
| `selftest`（含新增 [D10] 10 条） | 466 通过 / 0 失败 |

## 实施中被推翻的三处设计

1. **`dev_page_eval` 默认在插件页面的 iframe 里求值**（`frame="page"`）。
   端到端跑出来的事实：`document.title` 回的是 `"Agent Workspace"`（壳的标题），
   不是插件页面的标题。让 agent 每次自己写 `contentDocument` 前缀，等于把
   「我在看哪一层」压在记忆里 —— 十个调用写错一个，就得到一个看起来很有道理、
   其实在说壳的结论。所以做成参数，默认站在它的页面上。

2. **`reload` 中断的请求不能算失败**。首次冒烟 `network` 面红了，报
   `failed /api/theme/resolved`。查下来是**我们自己 reload** 把加载期正在飞的
   fetch 打断了（`net::ERR_ABORTED`）。判据拆成两栏：真失败 / 被导航中断。

3. **Playwright 的 sync API 不在后台线程派发事件**，它在下一次 API 调用时 pump。
   只跑 `network` 这一面时一次 API 都没调，队列里的 `console.error` 与 404
   一条都取不出来 —— 而报告会显示「全绿」。`check()` 开头必须主动 pump 一次。

## 实施中修掉的两个真 bug（都在我自己的代码里）

- `_contrast` 嵌套函数与模块级函数同名 → 闭包遮蔽 → `NameError`。
- `print(json.dumps(...)[:20000])` —— 截断的 JSON 不是 JSON。

---

# 

> **v3 修订（第 43 轮，用户授权）**：v2 把「不给 agent 裸 `evaluate_js`」当成一条底线来守，
> 并为此付出了两处代价（发明 `uiAreas` 声明面、CDP 只做单向观察）。**这条底线是假的**，见 §零。
> v3 把 eval 放成一等动词 —— 这不是新增能力面，是**删掉一份写死在文件里的白名单**。

---

## 零、v2 错在哪（三条，都不是「安全」问题，是「道理不成立」）

**1. 「eval 会读到 `S.settings.token`」—— 同一份 token 一句 `dev_read` 就有。**

`toolbox._safe()`（`toolbox.py:54`）只挡「越出工作区」，而 `config.json` **就在工作区根**，
`aaas.token` 在它里面。也就是说：

```python
dev_read("config.json")     # ← 现在就能拿到 token，一个字没改
```

所以「给 eval 就等于泄露凭据」这句话，在 `dev_read` 存在的那天起就不成立。
真正的边界是**开发者模式本身**（`devtools._me()`：`caller` 不在 `plugins` 里就直接说
「这条路只给打开了开发者模式的程序用」），不是某个动词的粒度。**用错的理由守对的事，
守不住的从来不是那件事。**

**2. 那个能力本来就在仓库里，只是写死在文件里。**

`uiprobe._probe()` 里有十余处 `win.evaluate_js(...)` —— 读 DOM、点按钮、派发键盘事件、
读 `S.settings.url`。它早就在对真窗口做任意 JS 了。缺的只是**把 JS 从文件里挪到参数里**。

而写死的那份必然腐烂：`#setbtn` / `#setmsg` / `S.settings.url` 这些选择器，插件页面改一次名，
`uiprobe` 就静默失效一次（它有 `skip` 分支，所以是**静默**的）。白名单不会保护任何人，
它只是把维护成本挪到一个没人看的地方。

**3. v2 的 `uiAreas`（声明面）是为了弥补「没有 eval」而发明的机制。**

没有 eval 时，agent 没法自己走到第二个状态，所以只能求插件作者在 `plugin.json` 里写
「怎么到那个面」。有 eval 之后，agent 自己会点、会滚、会读 DOM —— **这个字段该删**。
它当初解决的唯一问题已经不存在，留着就是让每个插件作者写一份自己也不会维护的清单。

**v3 一句话**：能力全开，只保留**技术约束**（哪些做法根本做不到）与**事实差异**（验的和生产哪里不一样），
不再保留**权限约束**（哪些做法不让你做）。

---

## 一、五个动词

```
dev_page_open(viewport="pane"|"desktop")              -> sid
dev_page_eval(js, sid=None)                       -> {ok, value, error, wrapped, navigated, ms}
dev_page_shot(sid=None, full=False, name="")      -> {path, w, h, bytes} + 图进上下文
dev_page_check(sid=None, areas=None)              -> 文字报告（§七）
dev_page_close(sid=None)                          -> 关；省略 sid = 全关
```

**只有五个，因为 `wait` / `click` / `scroll` / `read` / `hover` 全是 eval。**
这是一个必须守住的判断：每加一个动词，就要在提示词里解释一次、在 schema 里写一遍、
在 selftest 里验一次。而它们全都是「跑一段 JS」的特例。**万能动词 + 一条语义说明，
比十个别名准确** —— 而且 agent 不会被「该用哪个」困住。

`sid` 省略时指向「当前那个会话」：一次只验一个页面是常态，让 agent 每次都回填
一串 id 是无意义的负担。开第二个会话才需要显式带。

**会话是常驻的**：`open` 之后窗口和 CDP 连接一直活着，直到 `close` 或空闲 300 秒。
一次性批跑做不了「看一眼 → 改一处 → 再看一眼」，而那正是页面调试的全部内容。

---

## 二、一条路：真页面（没有副本）

引擎只有 **pywebview(WebView2)** 一个（v2 的结论没变：验 Chrome ≠ 验用户的机器）。

**没有副本工作区。** 验的就是真工作区：真插件、真会话、真设置。

宿主从哪来，只有一条规则：

| 情况 | 做法 |
|---|---|
| 有正在跑的宿主（`rt.port` 有值，`main.py:258`） | 直接连它：窗口加载 `http://127.0.0.1:<port>/#p/<pid>`，**不起**第二个服务 |
| 没有（宿主不是带界面启动的） | 自己 spawn 一个 `main --serve --port 0 --workspace <真工作区>` |

**为什么把 `copy` 删掉**：副本能截图、能点按钮，但它不回答「我的页面在真实数据下长什么样」
—— 空会话里的聊天页永远只有一条「今天想聊点什么」，那种页面**怎么验都是绿的**。
而它原本要解决的问题（点坏、脏数据、故意报错）用 eval 就能做，不需要第二个工作区。

**代价必须写进报告**：真页面有副作用。插件自己在加载时就会写 `plugins/<pid>/data/`
（实测：`store.json` 变了）。所以 `check` 与 `close` 都带一个 `data_changed`，
如实列出这次动过的运行时数据 —— **不判红**，因为那是「真页面」的定义，不是缺陷。
不许变的是**插件源码与 `config.json`**，那道门在（`_tmp/p44_real.py` 25 项）。

**窗口归属**：新起一个窗口，不碰用户正在看的那个。理由不是安全，是**别跟用户抢**：
他在左边打字，你的 eval 把 iframe 导航走了，这不是「能力」，是干扰。

---

## 三、eval 的六条语义（v3 的核心新增 —— 每条都是一个会踩的坑）

### 1. 值不好序列化，也不能失败

`returnByValue` 遇到 DOM 节点、循环引用、函数，拿不到值。
**规则：永远返回点东西** —— 类型名 + 描述 +（若是元素）`outerHTML` 前 300 字符。

```json
{"ok": true, "value": null, "type": "HTMLDivElement",
 "desc": "<div class=\"pane\" id=\"appframe\">…", "serialized": false}
```

返回 `{ok:false}` 只会让 agent 以为自己的 JS 写错了，然后去改**没写错的那一行**。

### 2. `awaitPromise` 必开

`uiprobe._wait()` 的注释写得很直白：**「`evaluate_js` 不 await」**（`uiprobe.py:279`），
所以它只能**轮询**一个同步表达式。CDP 的 `Runtime.evaluate` 有 `awaitPromise`，
开了之后 `await fetch(...)`、`await new Promise(r=>setTimeout(r,300))` 直接可用。

这一条把 uiprobe 里那种 `_wait(win, expr, 8.0)` 轮询整套删掉 —— **轮询是没得 await 时的妥协，
不是方法**。剩下的唯一用法是「等某个条件成立」，而那也是：

```js
await new Promise((ok, bad) => { const t0 = Date.now();
  (function look(){ if (cond()) return ok(true);
                     if (Date.now()-t0 > 8000) return bad(new Error("超时: " + expr));
                     setTimeout(look, 100); })(); })
```

### 3. 语句不是表达式

`Runtime.evaluate` 只吃**表达式**。`let x = 1; x * 2` 会报 `SyntaxError: Unexpected token 'let'`。

**规则**：先按表达式跑；SyntaxError 时包成 `(function(){ … })()` 重跑一次，
返回里带 `wrapped: true`。告诉 agent 发生过这件事，它才知道自己写的是语句块。

### 4. eval 里导航会毁掉执行上下文

`Page.navigate` / `location.href = …` / `location.reload()` 之后原上下文消失，
CDP 回 `Execution context was destroyed`。

**这不是错误，这是事实** —— 页面确实跳走了。返回：

```json
{"ok": true, "navigated": true, "note": "执行期间页面跳到了 /plugin/x/"}
```

并且**自动重挂**：下一个 `eval` 落到新文档上，会话不失效。判成 error 会让 agent 以为
「eval 坏了」，而其实是它自己让页面跳走的 —— 然后它会去修一个不存在的问题。

### 5. 大返回值必须落盘

一次 `document.documentElement.outerHTML` 就是几百 KB。上限 4000 字符，
超出落 `.dev-cache/<pid>/eval-<ts>.txt` 并返回路径（复用 `devtools._spill` 的落点约定：
工作区内、点开头、不进快照、`dev_read` 打得开）。

### 6. 工具执行体在**唯一**那个工作线程上（隐式依赖，写在这里）

`link.py:491` 把 `dispatch` 交给 `self._exec`，而 `_SerialExecutor`（`link.py:121`）
只有**一个** `threading.Thread`。所以：

- Playwright 的**同步** API 不会撞上 asyncio 循环（不会抛 `Sync API inside the asyncio loop`）
- 也不会有跨线程问题（`Page` / `Browser` 对象不是线程安全的，而这里根本只有一条线程）

**这两条现在是免费的，但它们是隐式依赖**：哪天 `_exec` 改成线程池，CDP 会话就会随机炸。
所以 CDP 会话要**自己钉一条专属线程**（`open` 时起、`close` 时收），
不靠「反正现在只有一个线程」。后者是运气，前者是设计。

---

## 四、技术约束（v2 挖出来的，一条没松）

| 约束 | 事实 | v3 怎么处理 |
|---|---|---|
| **`hidden=True` 截不到图** | `Page.screenshot` 30 秒超时：从不显示的窗口不合成帧 | 窗口**可见但屏幕外**（`x=-3000, y=-3000`）。这是「不打扰用户 + 能截图」的唯一交集 |
| **console 事件不重放** | 探针 1：加载完才 attach → `liveHits = 0` | **attach 之后 `reload` 一次**，验的是第二次加载（代价：页面加载两次，第一次的 console 拿不到 —— 明写在报告里） |
| **不往被验的页面里注入脚本** | v2 用 `add_init_script` 抢在导航前；但那等于改了我们正在验的东西 | **删掉**。`Runtime.enable` + `Network.enable` + `reload` 已经能拿到全部事件。注入脚本是「为了观察而改变观察对象」 |
| **视口 ≠ 窗口尺寸** | 探针 3：请求 420×320，截图 814×570（含 DPI 2.0 与边框） | 报告给 `innerWidth/innerHeight` **和** `devicePixelRatio`，不拿请求值冒充实测值 |
| **`text_select`** | `shell.run` 传 `text_select=True`（`shell.py:230`）；否则 pywebview 往 `<head>` 尾巴注入 `user-select:none` | pagecheck 照抄这个参数 |
| **宽度** | 生产里插件页在 `index.html:615` 的 iframe 里，宽 `calc(100% - var(--chatw,380px))`，1180 窗口 → **800** | `viewport="pane"` 直接按生产公式算窗口宽，**不是猜 1180** |

**一处角色变化**：`add_init_script` 从方案里删掉，是因为它是一个**不必要的侵入**。
顺手说明它当初为什么看起来必要：探针 2 是先 `add_init_script` 再 `goto`，所以拿到了加载期事件；
但「先 attach 再 reload」拿到的是同一批事件，且不动页面。**能不动观察对象的做法，
优先于能动的那个** —— 这条比「attach 要早」更根本。

---

## 五、console / network 仍然排第一

白屏时截图是一张**干净的空白**，DOM 里什么都不缺，`readyState` 甚至是 `complete`。
只有错误通道能说出「它其实炸了」。所以 `check` 的固定顺序是
`console → network → load → overflow → interactive → narrow → theme → shot`，
**前两项出红，后面全部标红并不再往下跑**（一个已经崩了的页面，量它的溢出宽度没有意义）。

`network` 用 CDP 的 `Network.responseReceived` / `loadingFailed`，不是页面里的 fetch 钩子 ——
`<img src>` 404、`<script>` 加载失败，fetch 钩子一条都看不见（第 41 轮那个「破图标 + image.png」
就是这类：没有 JS 报错，只有一张 404 的图）。

---

## 六、清单：只有通用面，没有声明面

| 面 | 判据 |
|---|---|
| `console` | 零 error / 零 pageerror（warn 只计数不判红） |
| `network` | 无 4xx/5xx、无 loadingFailed |
| `load` | `document.readyState === "complete"`，且第一个可见元素有非零尺寸 |
| `overflow` | `scrollWidth <= clientWidth`（横向不出界）。纵向允许滚动 |
| `interactive` | 每个可点控件点一遍，看有没有 JS 错误 / 页面变白。**默认不在清单里**（不会自己点）；显式 `areas="interactive"` 才跑 —— 这些按钮发的是真消息 |
| `narrow` | 420px 不塌（控件不被切、不被挤成一列） |
| `theme` | `data-theme` 深浅两档都拍一张，色差非零（防「切了没生效」） |
| `shot` | 一张 PNG，非空，`w > 0` |

**「完整」依然没法用一个数字定义**，但 v3 的补法变了：不再求插件作者声明，而是
**报告里列出「我实际碰过哪些面、哪些没碰、为什么」**。agent 可以自己用 eval 跑到第 9 个状态 ——
那不是清单的责任，是 eval 的责任。清单负责**不漏掉那 8 个基本项**。

---

## 七、报告形状

```json
{
  "text": "8 个面：6 绿 / 2 红；视口 800×740（dpr 2.0）；真页面\n\n!! console 3 条错误，第一条：Uncaught TypeError: S.pid is undefined\n!! overflow 横向溢出 42px（.chat 右边界 1222 > 视口 1180）",
  "engine": {"browser": "Edg/158.0.0.0", "via": "webview2-cdp", "dpr": 2.0},
  "viewport": {"asked": "pane", "inner": [800, 740]},
  "source": {"workspace": "<真工作区>", "host": "复用正在跑的宿主", "url": "http://127.0.0.1:51234/#p/mood"},
  "reloaded": true,
  "data_changed": {"added": [], "changed": ["store.json"], "dir": "plugins/mood/data"},
  "areas": [
    {"id": "console", "ok": false, "facts": {"errors": 3, "first": "Uncaught TypeError: S.pid is undefined"}},
    {"id": "shot",    "ok": true,  "facts": {"path": ".dev-cache/mood/shots/pane.png", "w": 1600, "h": 1480}}
  ],
  "skipped": [{"id": "interactive", "why": "默认不点（这些按钮发真消息）；要点头显式 areas=interactive"}]
}
```

`skipped` 的第一条永远是「没做，以及为什么」—— 和 selftest 的 `skip` 同一条原则。
**不写 skipped 的「完整」，就是第 41 轮那种假绿。**

---

## 八、端口：从「防御」降级为「卫生」

v2 §七 花了三条缓解措施去守那个端口。v3 不再守它 —— 但**仍然要让它早点消失**，
理由从「防别人」变成「别在地上留东西」：

1. **端口随机**（系统分配，不写死、**不落盘**）
2. **随会话消失**：`close` → `destroy` 窗口 → 子进程退出 → 端口消失；空闲 300 秒自关
3. **只在开发者模式**：非 dev 程序调不到这五个动词

一句话写进工具描述：**验证期间本机存在一个可执行任意 JS 的调试端口，`close` 后消失。**
说清楚，不用它当借口 —— 这正是 v2 的错：拿「安全」当理由，去做一件根本不成立的事。

---

## 九、门（13 道，先红后绿）

**eval 语义（6 道，本轮新增的核心）**

| # | 门 |
|---|---|
| 1 | `eval("1+1")` → `2`；`eval("document.body")` → **有描述、不报错**（序列化门） |
| 2 | `eval("await new Promise(r=>setTimeout(()=>r(42),50))")` → `42`（await 门） |
| 3 | `eval("let a=1; a*2")` → `2` 且 `wrapped: true`（语句门） |
| 4 | `eval("location.reload()")` → `navigated: true`，**且下一次 eval 仍能用**（上下文门） |
| 5 | `eval("throw new Error('砰')")` → `ok:false` + `"砰"` + 行号，**不是 traceback** |
| 6 | `eval("'x'.repeat(200000)")` → 落盘 + 路径，结果 ≤ 4000 字符（水位门） |

**页面（4 道）**

| # | 门 |
|---|---|
| 7 | 把 `.chat header` 的 `align-items` 改回 `baseline` → 报告**必须红**（第 41 轮那 3.1px） |
| 8 | 页面里 `throw` → **不是全绿**；`<img src="/不存在.png">` → **也不是全绿**（console 挡不住 404，network 才挡） |
| 9 | `dev_page_shot` 的图真进 `FunctionResult.parts`（`link.py:495` 现在只发 `Part.t`，`Part.image(raw, mime, name)` 早就有） |
| 10 | 真页面跑完：插件源码与 `config.json` **sha256 逐字节相同**；`plugins/<pid>/data` 的变化**如实进 `data_changed`**（不判红） |

**变异（3 道，证明这些约束真的在起作用）**

| # | 变异 | 必须 |
|---|---|---|
| 11 | 把 attach 挪到 `reload` 之后（即不 reload） | 门 8 必须红（证明加载期事件确实靠 reload 拿） |
| 12 | `hidden=True` 代替屏幕外坐标 | 门 9 必须红（证明截图约束真实存在） |
| 13 | 报告里的 `engine.browser` 换成 `HeadlessChrome/…` | 门 8 必须红（`engine` 从页面 UA 取，取不到才退化成版本号 —— 这一步早读就会静默退化） |

---

## 十、与 e2e 的分工：不替代，共享面不共享断言

`tests/e2e_browser_ui.py`（75 项，系统 Chrome）能拦网络、改 DOM、断言精确到 `21.9 vs 25.0`
—— 那是**发版前的门**，要的是确定性。
`dev_page_*` 是**运行时自查**：agent 跑、只能看不能说谎、验的是真引擎。

两者差异（Chrome vs WebView2）不是缺陷，是分工：**发版门要确定性，自查要保真**。
加一条门：比较「清单里的面」与「e2e 覆盖的面」，缺了就说 —— 不要求相等，但要求都碰过。

---

## 十一、工作量

| 文件 | 改动 |
|---|---|
| `agentworkspace/pagecheck.py` | **新建** ~480 行（子进程+窗口、CDP 会话线程、eval 六条语义、8 个面、报告）；第44轮删掉 `prepare()`/`--mode`/`DEAD_URL`（-60 行），加 `_engine_name`/`_data_delta` |
| `agentworkspace/devtools.py` | +5 个动词 ~110 行（会话登记表、idle 回收、`_spill` 复用） |
| `agentworkspace/kernel.py` | `Result.images`（`kernel.py:271`）+5 行；`rt.port` +2 行 |
| `agentworkspace/link.py` | `Part.image` 拼装（`:495` 一行变三行）+6 行 |
| `agentworkspace/main.py` | `rt.port = httpd.server_address[1]` +1 行 |
| `plugins/plugin-dev.md` | 一段判据：什么时候该跑 `check`、什么时候该直接 eval |
| `pyproject.toml` | `[uiqa]` extra 只装 `playwright`（**只作 CDP 客户端，不下载浏览器**） |
| `selftest.py` | 门 1–10 + 三条变异 ~220 行 |

`playwright` 只用来 `connect_over_cdp`，**不需要** `playwright install` 下浏览器 ——
引擎是系统 WebView2，本来就在。

---

## 十二、没做的

- **`interactive` 默认不跑**：它不在默认清单里，显式点名才会点。要点真页面又不发真消息，
  得给后端一个 dry-run 档 —— 那是另一件事，不该塞进页面验证里
- **视觉回归（像素 diff）**：两次跑必然有抗锯齿、字体 hinting 的抖动，要先解决基线抖动，这轮不值得
- **验宿主界面**（`index.html` 本身）：`dev_*` 的作用域就是 `_pdir()`；那是另一副手
- **webview2 的 `--force-device-scale-factor`**：能固定 dpr 让截图可复现，但它也会改变
  用户机器的真实渲染 —— 为了可复现而脱离真相，方向反了
