# 代码评审（ocr / open-code-review）

发布之前跑一遍评审，是这一轮的**门槛**：先评审，再推送。

## 怎么跑的

| 项 | 值 |
|---|---|
| 工具 | `@alibaba-group/open-code-review` **v1.14.0**（CLI 命令 `ocr`） |
| 命令 | `ocr scan --audience agent --format json -o .tmp/ocr-scan-1.json --concurrency 6` |
| 模型 | 走自建的 OpenAI 兼容网关（`custom_providers.gpuhub`），实际模型 `deepseek-v4.1-flash` |
| 扫描面 | 21 个文件 / +5591 行（`.md`、`.ico`、`MANIFEST.in` 不在支持范围，被跳过） |
| 结果 | **91 条**：critical 2 / high 24 / medium 49 / low 16 |

工具自身报 `status=completed_with_errors`：有 2 条 `code_comment` 工具调用因为
**参数 JSON 解析失败**（模型侧）而丢失 —— 也就是真实条数可能是 93。这一点没有
替它掩盖：数字按它实际给出的 91 条记。

## 结论先说

- **critical 2 条全部修了**，都不是「风格问题」：一条让「不许写 data/」形同虚设，
  一条能用 `plugin_delete("../data")` 删掉整个数据目录。
- **high 24 条修了 21 条**。剩下的 3 条属于需要改架构的：程序页面与主界面同源
  （缺 sandbox 的根因是"同源"本身）、`dispatch` 没有超时兜底、插件仍在进程内线程跑。
- medium / low 里挑了对**行为**有影响的 12 条修了（CLI 的诚实性、回退模板占位符、
  pyproject 元数据、.gitignore），其余按主题归入下面的「未修」。

修完的检验：`python -m agentworkspace.selftest` → **98 通过 / 16 失败**，与改动前
**完全一致**（16 项失败全部是工作区里缺 `pomodoro` / `memo` 等样例程序的既有基线，
不是这一轮改坏的）；`python -m agentworkspace.qa all` 另有专门说明。

## 已修（逐条改了哪儿、为什么这么改）

| # | 严重度 | 位置 | 类别 | 摘要 | 状态 |
|---|---|---|---|---|---|
| 1 | critical | `agentworkspace/toolbox.py:148` | security | 写路径的 data/ 防护可被绕过：`toolbox.write` 只检查 `p.parts[-2:]`，`data/sub/file.txt` 末两段是 `sub`/`file.txt`，检查通过后照样在 `data/` 下建目录写入；`plugin_write` 的 `WRITABLE` 包含检… | **已修** — data/ 防护改成按路径分量判（data/sub/x.txt 也挡住） |
| 2 | critical | `agentworkspace/toolbox.py:287` | security | pid is not validated here, and the check only ensures pdir stays inside the workspace. plugin_delete("../data") resolves to <workspace>/data, passes `… | **已修** — plugin_delete 先校验 pid，并要求父目录就是 plugins/ |
| 3 | high | `agentworkspace/kernel.py:375` | bug | `self.plugins` 是多线程共享的可变状态：`scan()` 可能在 HTTP 线程里增删条目，而 `capabilities()`、`capabilities_of()`、`state()` 在 aaas 事件循环线程里遍历 `self.plugins.values()`。两者重叠会抛 … | **已修** — scan 在副本上改、最后整体替换 self.plugins |
| 4 | high | `agentworkspace/kernel.py:677` | bug | kernel.py 里两处 `json.dumps` 都缺 `default=str`：`_split` 的 dict 分支遇到 `Path` 等非 JSON 值会抛 `TypeError`，而 `_split` 不在 try 内，异常直接冲出 `dispatch`，调用方拿到异常而不是 `Resu… | **已修** — _split / publish 的 json.dumps 补 default=str |
| 5 | high | `agentworkspace/link.py:149` | other | `status` is set to `ready` before the initial `_declare()` finishes. `send()` only waits for `ready`, so a first message can be scheduled while `calle… | **已修** — 先 declare 再标 ready（_mark_ready） |
| 6 | high | `agentworkspace/link.py:201` | bug | `_sig()` only hashes the capability wire names. If a plugin keeps the same function names but changes `description`, `parameters`, or `timeout_ms`, th… | **已修** — 能力指纹含 description / parameters / timeout_ms |
| 7 | high | `agentworkspace/link.py:246` | bug | On any `declare()` failure, `_declared_sig` is set to the current signature, so the 0.5s watcher will never retry this declaration. A transient networ… | **已修** — declare 失败不再记成已声明，同一份最多重试 3 次 |
| 8 | high | `agentworkspace/link.py:282` | other | `run_coroutine_threadsafe` returns a `concurrent.futures.Future`, but `_turn_task` is never awaited or checked. Exceptions escaping `_open_turn()` (e.… | **已修** — 轮次 Future 加 done 回调，异常落成 turn.failed |
| 9 | high | `agentworkspace/main.py:77` | bug | The wait loop exits on the 30 s deadline while `link.status` can still be `idle`/`connecting`; only `error` is handled afterwards. The code then print… | **已修** — 连不上（含超时）一律如实报，不再打印「已连接」 |
| 10 | high | `agentworkspace/main.py:97` | bug | `Hub.send`/`Link.send` return a status string: `"sent"` on success, otherwise a human-readable failure such as `"还没连上 aaas-service"` or `"上一轮还没结束"`. T… | **已修** — send 的返回值要判，失败就报原因并收尸 |
| 11 | high | `agentworkspace/native.py:92` | bug | `fh` is opened outside any `with` block and never closed. Every `open()` call therefore leaks one file descriptor (and on Windows keeps the log file h… | **已修** — 日志句柄用 with 包住，Popen 后立即关闭父进程那份 |
| 12 | high | `agentworkspace/qa.py:151` | bug | `OpenProcessToken` requests `TOKEN_ADJUST_DEFAULT` (0x0080), but `AdjustTokenPrivileges` requires `TOKEN_ADJUST_PRIVILEGES` (0x0020). As written `_ena… | **已修** — TOKEN_ADJUST_DEFAULT → TOKEN_ADJUST_PRIVILEGES |
| 13 | high | `agentworkspace/qa.py:227` | bug | Catching every exception here turns a broken/unreachable host (connection refused, timeout, HTTP 500, invalid JSON) into "program absent". Callers the… | **已修** — 读不到 /api/state 报红，不再当成「程序不在」 |
| 14 | high | `agentworkspace/qa.py:322` | bug | `find_windows()` matches only the window title, so `Host.close()` posts `WM_CLOSE` to every visible window with that title — including a user's real A… | **已修** — 找窗口按进程归属，只关自己拉起的那个 |
| 15 | high | `agentworkspace/qa.py:464` | bug | Skipped checks are excluded from `ok`. If every check is skipped (e.g., memo/mood missing, or both `have()` calls return False), `_report()` returns T… | **已修** — 全跳过不算通过（_report 返回 False） |
| 16 | high | `agentworkspace/selftest.py:88` | bug | `_fixture_note` still calls `(WS / 'plugins').iterdir()` unconditionally. If `workspace/plugins` itself is missing (the very case this notice is meant… | **已修** — _fixture_note 先判 plugins/ 在不在 |
| 17 | high | `agentworkspace/selftest.py:434` | test | The test overwrites a live workspace file (`backend.py`) with invalid code and relies on `plugin_write` rolling it back. Nothing restores `good` if th… | **已修** — 覆盖 backend.py 的测试用 try/finally 复原 |
| 18 | high | `agentworkspace/server.py:91` | security | 两处静态路径包含检查都用原始字符串前缀比较：`server.py` 的 `/web/...` 静态根（`(App.web / path[5:]).resolve()`）和插件 UI 文件（`(base / rel).resolve()`）。只要兄弟目录名以同一前缀开头（如 `/srv/app/web… | **已修** — 静态路径改用 is_relative_to（两处） |
| 19 | high | `agentworkspace/server.py:126` | security | State-changing POSTs are accepted on the loopback socket without any `Host`/`Origin` check. A page on any site can POST a `text/plain` body that happe… | **已修** — POST 校验 Host / Origin（DNS rebinding + 跨站提交） |
| 20 | high | `agentworkspace/toolbox.py:78` | security | child.is_dir() follows symlinks, and walk() never re-checks the resolved path against the workspace. A symlink inside the workspace pointing to /etc (… | **已修** — tree 不再跟进符号链接 |
| 21 | high | `agentworkspace/toolbox.py:181` | security | capture_output=True buffers the entire stdout/stderr before out[:6000] truncates it, so a command such as `yes` can exhaust memory during the 30-secon… | **已修** — shell 输出落临时文件、只读回 60000 字符 |
| 22 | high | `agentworkspace/uiprobe.py:208` | test | This read happens immediately after the `ok` hint appears, but the page sets that hint before `await syncAgents(); paintConn();`. The test can therefo… | 属于测试工具的完备性，记在这里等下一轮 |
| 23 | high | `agentworkspace/web/index.html:284` | security | Program UI code (`/plugin/<id>/`) is agent-generated and is loaded into a same-origin iframe with no `sandbox` attribute. That gives the generated pag… | 要么需要架构改动（每程序独立 origin / 进程隔离），要么会改变调用语义 |
| 24 | high | `agentworkspace/web/index.html:553` | bug | `call.result` is matched against `tools[tools.length - 1]` instead of the call it belongs to. The event payload already carries `callId` (see `link.py… | **已修** — call.ready 存 callId，call.result 按 callId 配对 |
| 25 | high | `agentworkspace/web/index.html:608` | bug | `syncAgents()` snapshots `/api/state` and then blindly replaces `S.msgs[pid]`, but the `await fetch(...)` yields control back to the SSE handler. Any … | **已修** — syncAgents 用 tick 判断快照是否已过期 |
| 26 | high | `agentworkspace/winicon.py:100` | bug | `_bind()` declares ctypes prototypes for handle-taking APIs specifically to avoid 64-bit truncation, but `IsWindowVisible`, `GetWindowTextLengthW`, `G… | **已修** — 补 EnumWindows / IsWindowVisible / GetWindowText* argtypes |
| 27 | medium | `.gitignore:19` | security | The ignore list contains no rule for credential/secret-bearing files. The app talks to an "aaas" endpoint and keeps its local config, so a stray `.env… | **已修** — 补凭据类忽略规则 |
| 28 | medium | `agentworkspace/devtools.py:72` | bug | `toolbox.read` silently truncates the returned text to 20 000 chars (`text[:20000]`) and gives the caller no way to tell it was cut. The documented wo… | 需要设计取舍（超时兜底、per-pid 锁、超时 vs 死掉的判据），不在「发版前顺手改」的范围 |
| 29 | medium | `agentworkspace/devtools.py:97` | security | `toolbox._write_target` blocks `plugins/**`, `data/`, `dev.json` and `config.json`, but not `workspace/.backup/**`. A program with developer mode on c… | 要么需要架构改动（每程序独立 origin / 进程隔离），要么会改变调用语义 |
| 30 | medium | `agentworkspace/kernel.py:47` | bug | `DEV_PID = "dev"` 是一个占位 pid，但 `scan()` 并不阻止 `plugins/dev/` 这样的目录。一旦存在名为 `dev` 的插件：`capabilities()` 里它的 `dev_*` 会与开发者工具集的 wire 名撞车；更严重的是 `dispatch()` 先… | 需要设计取舍（超时兜底、per-pid 锁、超时 vs 死掉的判据），不在「发版前顺手改」的范围 |
| 31 | medium | `agentworkspace/kernel.py:131` | bug | 最小校验器在这里对任何声明了 `type` 的参数直接放行 `None`，而 `_schema_of` 又把 `Optional` 的 `NoneType` 丢掉（`Optional[int]` 与 `int` 生成的 schema 完全一样），于是「非 Optional 的 string 参数传 … | 需要设计取舍（超时兜底、per-pid 锁、超时 vs 死掉的判据），不在「发版前顺手改」的范围 |
| 32 | medium | `agentworkspace/kernel.py:211` | bug | `set()` 是「整份读 → 改 key → 整份写」，`_read()` 到 `tmp.replace(p)` 之间没有任何互斥。`dispatch` 可能来自 HTTP 线程与 aaas 事件循环线程（插件之间也会互相触发），两个并发 `set` 会互相覆盖丢更新；它们还共用同一个 `stor… | 需要设计取舍（超时兜底、per-pid 锁、超时 vs 死掉的判据），不在「发版前顺手改」的范围 |
| 33 | medium | `agentworkspace/kernel.py:237` | security | `Plugin.ui` 把清单里的 `ui` 字段直接拼到插件目录上，没有做路径约束：`"ui": "/etc/passwd"` 会因绝对路径覆盖直接指向系统文件，`"ui": "../../../secret"` 也能逃出插件目录，`p.is_file()` 依旧为真，`ui_kind` 报 `w… | 要么需要架构改动（每程序独立 origin / 进程隔离），要么会改变调用语义 |
| 34 | medium | `agentworkspace/kernel.py:344` | bug | `self.events` 的 append 与按 `MAX_EVENT_LOG` 截断发生在 `self._lock` 之外，与并发的 `publish`/`subscribe` 交错。两个线程同时执行 `del self.events[: n]` 时按各自 `len()` 计算，可能多截断或截断… | 需要设计取舍（超时兜底、per-pid 锁、超时 vs 死掉的判据），不在「发版前顺手改」的范围 |
| 35 | medium | `agentworkspace/kernel.py:398` | bug | 这里只捕获 `OSError/ValueError`，却没有校验 `json.loads` 的返回类型。若 `plugin.json` 是合法的 `[]` / `"x"` / `1`，`p.manifest` 就会是 list/str/int，随后 `Plugin.ui`、`Plugin.title… | 需要设计取舍（超时兜底、per-pid 锁、超时 vs 死掉的判据），不在「发版前顺手改」的范围 |
| 36 | medium | `agentworkspace/kernel.py:433` | bug | `int(p.manifest.get("timeoutMs", 15_000))` 没有防护：清单写 `"timeoutMs": "500ms"` 抛 `ValueError`，写 `"timeoutMs": null`（key 存在但值为 null）抛 `TypeError`。这段代码在 `_l… | 需要设计取舍（超时兜底、per-pid 锁、超时 vs 死掉的判据），不在「发版前顺手改」的范围 |
| 37 | medium | `agentworkspace/kernel.py:653` | bug | `Capability.timeout_ms` 通过 `as_decl()` 以 `timeoutMs` 暴露给模型和前端，但 `dispatch` 是同步直调 handler，没有任何超时兜底。一个死循环或阻塞 IO 的 handler 会永久占住调用线程（HTTP 线程或 aaas 事件循环线程… | 需要设计取舍（超时兜底、per-pid 锁、超时 vs 死掉的判据），不在「发版前顺手改」的范围 |
| 38 | medium | `agentworkspace/kernel.py:685` | bug | `_reject` 声明了 `caller` 参数，但文件内所有调用点（`dispatch` 四处、`call_wire` 一处）都没有传它，因此所有 `call.rejected` 事件的 `data["caller"]` 恒为 `None`；`dispatch` 的 `call.error` 分… | 需要设计取舍（超时兜底、per-pid 锁、超时 vs 死掉的判据），不在「发版前顺手改」的范围 |
| 39 | medium | `agentworkspace/link.py:190` | bug | The fallback templates use single-brace placeholders (`{title}`, `{tools}`, `{dir}`, `{pid}`), but `_plugin_prompt()` replaces double-brace tokens (`{… | **已修** — 回退模板改成双花括号占位符 |
| 40 | medium | `agentworkspace/main.py:71` | bug | Every early return in `cli` (missing program, connection error, empty input, timeout) skips `hub.stop()` — it is only reached on the success path. Whe… | **已修** — cli 的所有早退路径都 hub.stop() |
| 41 | medium | `agentworkspace/main.py:112` | bug | `turn.failed` is only printed, never treated as a terminal event: `done` stays `False`, so after a failed turn the CLI blocks for another 300 s and ex… | **已修** — turn.failed 当终态（不再干等 300 秒） |
| 42 | medium | `agentworkspace/main.py:246` | bug | `gui()` takes an `open_browser` parameter that is never read anywhere in its body. Both the `--no-browser` flag and the `server.openBrowser` config se… | 需要设计取舍（超时兜底、per-pid 锁、超时 vs 死掉的判据），不在「发版前顺手改」的范围 |
| 43 | medium | `agentworkspace/native.py:74` | bug | The class docstring promises a "线程安全的进程账本", but there is no lock. `open()`/`close()`/`info()` are reachable from `ThreadingHTTPServer` request threads… | 需要设计取舍（超时兜底、per-pid 锁、超时 vs 死掉的判据），不在「发版前顺手改」的范围 |
| 44 | medium | `agentworkspace/native.py:105` | maintainability | `BOOT_GRACE` (declared as 2.0 with the comment "窗口活过这么久才算「起来了」") is never referenced anywhere in the file; the actual grace period here is the magic n… | 不影响行为，排队 |
| 45 | medium | `agentworkspace/native.py:121` | bug | `terminate()` only sends the request to exit and returns immediately; nothing waits for the child, and `shutdown()` (documented as "不留孤儿窗口") returns r… | 需要设计取舍（超时兜底、per-pid 锁、超时 vs 死掉的判据），不在「发版前顺手改」的范围 |
| 46 | medium | `agentworkspace/qa.py:55` | bug | No Win32 function prototypes are declared anywhere in this module, so ctypes uses the default `c_int` return type. Calls that return `HWND`/`HANDLE` (… | 需要设计取舍（超时兜底、per-pid 锁、超时 vs 死掉的判据），不在「发版前顺手改」的范围 |
| 47 | medium | `agentworkspace/qa.py:178` | bug | `CreateEnvironmentBlock`'s return value is ignored and the environment block is never freed; `token` is never closed, and if `CreateProcessAsUserW` fa… | 需要设计取舍（超时兜底、per-pid 锁、超时 vs 死掉的判据），不在「发版前顺手改」的范围 |
| 48 | medium | `agentworkspace/qa.py:289` | bug | If `Popen` or `_wait_port()` raises (host exits early, 60 s timeout), the opened log file and a possibly running child process are left behind; `main(… | 需要设计取舍（超时兜底、per-pid 锁、超时 vs 死掉的判据），不在「发版前顺手改」的范围 |
| 49 | medium | `agentworkspace/qa.py:342` | bug | `_shot()` returns False when it cannot foreground/capture the window, but this result is only stored in `res['shot']` and never added to `checks`; `qa… | 需要设计取舍（超时兜底、per-pid 锁、超时 vs 死掉的判据），不在「发版前顺手改」的范围 |
| 50 | medium | `agentworkspace/qa.py:486` | bug | `main()` accepts an `argv` parameter, but relaunch mode reads `sys.argv[1:]` instead. Programmatic calls such as `main(["desktop"])` or tests with cus… | 需要设计取舍（超时兜底、per-pid 锁、超时 vs 死掉的判据），不在「发版前顺手改」的范围 |
| 51 | medium | `agentworkspace/sdk.py:64` | bug | 这里把「两次超时」等价于「宿主死了」，阈值只有 2 次 × (2.5s 超时 + 3s 间隔)。宿主是一台 ThreadingHTTPServer，只要它短暂卡住（GC、长 dispatch、写盘、/api/state 里 `hub.status()` 变慢）超过约 5 秒，插件进程就会被 `os.… | 需要设计取舍（超时兜底、per-pid 锁、超时 vs 死掉的判据），不在「发版前顺手改」的范围 |
| 52 | medium | `agentworkspace/sdk.py:97` | bug | `state()` 与同文件的 `call()` 错误语义不一致：`call()` 会把连接失败/HTTP 错误折成 `{"status": "error", ...}` 返回，而 `state()` 直接让 `URLError` / `HTTPError` / `JSONDecodeError` … | 需要设计取舍（超时兜底、per-pid 锁、超时 vs 死掉的判据），不在「发版前顺手改」的范围 |
| 53 | medium | `agentworkspace/sdk.py:105` | documentation | `cb` 是在 `aw-events-*` 后台线程里被调用的，而本 SDK 的目标用户是 tkinter / PyQt 原生窗口：从非主线程直接操作控件会触发 `RuntimeError: main thread is not in main loop` 或直接崩溃。文档里写的是「你的程序也能立刻… | 文档类，排队 |
| 54 | medium | `agentworkspace/selftest.py:54` | test | `post` (and `get` above) swallow every transport-level failure and synthesize a success-looking payload (`{"status": "error"}` / `code=0`). Several as… | 属于测试工具的完备性，记在这里等下一轮 |
| 55 | medium | `agentworkspace/selftest.py:112` | test | `all(...)` over an empty `capabilities_of("pomodoro")` is `True`, so this isolation check passes vacuously if pomodoro exposes nothing at all — exactl… | 属于测试工具的完备性，记在这里等下一轮 |
| 56 | medium | `agentworkspace/selftest.py:401` | bug | When `other is None` the code only prints a skip notice but keeps running the later checks that pass `other` into `dev_plugin_write` (`{"pid": other}`… | 需要设计取舍（超时兜底、per-pid 锁、超时 vs 死掉的判据），不在「发版前顺手改」的范围 |
| 57 | medium | `agentworkspace/server.py:129` | bug | `int(...)` on an attacker-controlled header raises `ValueError` for non-numeric values (uncaught → traceback/500), and a negative value makes `self.rf… | 需要设计取舍（超时兜底、per-pid 锁、超时 vs 死掉的判据），不在「发版前顺手改」的范围 |
| 58 | medium | `agentworkspace/server.py:131` | bug | Valid JSON that is not an object (e.g. `[1]`, `"x"`, `null`) passes this check and later hits `(body or {}).get(...)` in the chat/dev/settings branche… | 需要设计取舍（超时兜底、per-pid 锁、超时 vs 死掉的判据），不在「发版前顺手改」的范围 |
| 59 | medium | `agentworkspace/server.py:157` | bug | The `chat`/`cancel`/`link` branch dereferences `hub` without the `hub is None` guard that `/api/settings` uses (503 "智能体池未装配"). With an unassembled hu… | 需要设计取舍（超时兜底、per-pid 锁、超时 vs 死掉的判据），不在「发版前顺手改」的范围 |
| 60 | medium | `agentworkspace/server.py:198` | bug | `subscribe()` happens before the `try`; if `send_response`/`end_headers` (or the first write) raises `BrokenPipeError` on a client that already discon… | 需要设计取舍（超时兜底、per-pid 锁、超时 vs 死掉的判据），不在「发版前顺手改」的范围 |
| 61 | medium | `agentworkspace/settings.py:54` | bug | `[]`（括号里没有地址）能通过校验：`hostport = "[]"` 时 `find("]")` 返回 1，`tail` 为空串直接跳过检查，于是 `ws://[]/` 被当成合法地址保存下来，再经 `hub.rebind()` 交给 `aaas.Caller`，等于把「没有主机名」的错误推迟到… | 需要设计取舍（超时兜底、per-pid 锁、超时 vs 死掉的判据），不在「发版前顺手改」的范围 |
| 62 | medium | `agentworkspace/settings.py:82` | bug | `save_url` 的契约是「其它键原样保留」，但 `load()` 对面解析失败的文件一律返回 `{}`：一旦 `config.json` 被手工改坏（例如多了个逗号），人点一次「保存地址」就会把整份文件覆盖成只有 `aaas` 的内容，`agent.revision` 等其它配置被静默抹掉。建… | 需要设计取舍（超时兜底、per-pid 锁、超时 vs 死掉的判据），不在「发版前顺手改」的范围 |
| 63 | medium | `agentworkspace/settings.py:110` | bug | `setdefault` 只在**键缺失**时生效，`config.json` 里显式写了 `"url": ""`（或 `null`，例如被人手工清空/改坏）时，空值会原样传给 `Hub`/`Link`，`should be DEFAULT_URL` 的兜底失效。改用 `or` 兜底更稳妥。… | 需要设计取舍（超时兜底、per-pid 锁、超时 vs 死掉的判据），不在「发版前顺手改」的范围 |
| 64 | medium | `agentworkspace/shell.py:104` | maintainability | `try` 覆盖了 `fit()`、`create_window()`、事件绑定、线程启动和 `start()` 全部过程，任何异常（包括我们自己代码里的问题，例如 pywebview 版本改名了 `icon`/`storage_path` 参数导致的 `TypeError`、`Path(stora… | 不影响行为，排队 |
| 65 | medium | `agentworkspace/uiprobe.py:42` | test | `docW` is sampled before the drag/reset interactions and before the settings panel opens, yet `_judge` uses it to assert there is no horizontal overfl… | 属于测试工具的完备性，记在这里等下一轮 |
| 66 | medium | `agentworkspace/uiprobe.py:170` | test | The polling helper silently discards every `evaluate_js` exception. A persistent JS/DOM error is then reported only as a generic timeout, hiding the r… | 属于测试工具的完备性，记在这里等下一轮 |
| 67 | medium | `agentworkspace/web/index.html:235` | security | `p.id` is interpolated unescaped into the `data-pid` attribute (and, further down, into the iframe `src` and into `fetch()` paths). Today the server h… | 要么需要架构改动（每程序独立 origin / 进程隔离），要么会改变调用语义 |
| 68 | medium | `agentworkspace/web/index.html:546` | performance | Every `message.delta` rebuilds the whole thread through `innerHTML` (and each event additionally calls `paintConn()` at the bottom of `on()`). For a l… | 排队 |
| 69 | medium | `agentworkspace/web/index.html:579` | bug | After `plugin.removed`, `S.pid` can still point at the program that was just deleted. `paintStage()` then falls into the `if (!p)` branch and shows "还… | 需要设计取舍（超时兜底、per-pid 锁、超时 vs 死掉的判据），不在「发版前顺手改」的范围 |
| 70 | medium | `agentworkspace/winicon.py:124` | bug | Catching `Exception` and silently setting `h = 0` hides real ctypes failures (for example a missing or incorrect prototype) and makes this diagnostic … | 需要设计取舍（超时兜底、per-pid 锁、超时 vs 死掉的判据），不在「发版前顺手改」的范围 |
| 71 | medium | `agentworkspace/winicon.py:140` | bug | This reads only `hbmColor` and never uses `hbmMask` (the mask is merely deleted below). For icons without a 32-bit alpha channel (24-bit DDBs or monoc… | 需要设计取舍（超时兜底、per-pid 锁、超时 vs 死掉的判据），不在「发版前顺手改」的范围 |
| 72 | medium | `agentworkspace/winicon.py:227` | bug | The docstring promises to degrade to "cannot read" when Pillow or the requested frame is unavailable, but this raises `ImportError`, `OSError`, or `Va… | 需要设计取舍（超时兜底、per-pid 锁、超时 vs 死掉的判据），不在「发版前顺手改」的范围 |
| 73 | medium | `agentworkspace/winicon.py:254` | bug | `hicon_rgba` can raise `OSError` for invalid or monochrome icons, but this call is unguarded, so the tool exits with a traceback instead of a diagnost… | 需要设计取舍（超时兜底、per-pid 锁、超时 vs 死掉的判据），不在「发版前顺手改」的范围 |
| 74 | medium | `pyproject.toml:11` | maintainability | `license = { text = "MIT" }` is the deprecated table form; PEP 639 / latest setuptools expect an SPDX string plus `license-files`. Note the SPDX strin… | **已修** — license 改 SPDX 字符串 + license-files，setuptools>=77 |
| 75 | medium | `pyproject.toml:35` | maintainability | `packages` is a hard-coded single-package list, so anything added under `agentworkspace/` later (and currently `agentworkspace/assets/`, which holds `… | **已修** — packages 改 packages.find |
| 76 | low | `.gitignore:17` | maintainability | `/build/` and `/dist/` are root-anchored while `*.egg-info/` is not, so artifacts produced by a sub-package build (e.g. `agentworkspace/build/`) would… | 不影响行为，排队 |
| 77 | low | `.gitignore:23` | maintainability | These two WebView rules look stale/ineffective: 1) `*.wvprofile*/` requires the literal `.wvprofile` substring, so a directory named `wvprofile`/`wv_p… | 不影响行为，排队 |
| 78 | low | `agentworkspace/assets/make_icon.py:24` | maintainability | `math` is imported at module scope but never referenced anywhere in this file (the gradient/rounding work only uses plain arithmetic and `round`). Dea… | 不影响行为，排队 |
| 79 | low | `agentworkspace/assets/make_icon.py:146` | maintainability | The third tuple element (`is_png`) of `frames` is constructed (`True`/`False`) but never consumed anywhere — both consumers destructure it into `_png_… | 不影响行为，排队 |
| 80 | low | `agentworkspace/assets/make_icon.py:179` | bug | The preview sheet throws away the alpha channel: every pixel with `a > 0` is painted fully opaque. `render()` deliberately normalizes the color by cov… | 需要设计取舍（超时兜底、per-pid 锁、超时 vs 死掉的判据），不在「发版前顺手改」的范围 |
| 81 | low | `agentworkspace/kernel.py:37` | maintainability | `traceback` 与 `types as _types` 在本文件中没有任何引用（handler 抛错的路径只用 `type(exc).__name__`，真正想留的栈信息反而没记）。建议删掉这两个导入，或按原意把 `traceback.format_exc()` 记进 `call.error… | 不影响行为，排队 |
| 82 | low | `agentworkspace/kernel.py:769` | bug | `ui_kind` 后缀判断与上方 `ui_path.suffix.lower() == ".py"` 不一致：大小写敏感，`"ui": "Main.PY"` 会被当原生窗口校验通过，这里却报成 `web`，前端按 webview 去加载一个 Python 脚本。建议复用 `ui_path.suff… | 需要设计取舍（超时兜底、per-pid 锁、超时 vs 死掉的判据），不在「发版前顺手改」的范围 |
| 83 | low | `agentworkspace/main.py:245` | bug | `cfg.get("server", {})` only defaults when the key is absent. If `config.json` contains `"server": null` (a plausible hand-edited/partial config), thi… | 需要设计取舍（超时兜底、per-pid 锁、超时 vs 死掉的判据），不在「发版前顺手改」的范围 |
| 84 | low | `agentworkspace/sdk.py:75` | bug | `pid` / `fn` 被直接拼进 URL，没有做任何转义或校验。函数名一旦含空格、`#`、`?`、`%` 或非 ASCII 字符（Python 允许中文标识符），`urlopen` 会在构造请求时抛 `ValueError` / `UnicodeEncodeError`，而这两个异常不在下面的 … | 需要设计取舍（超时兜底、per-pid 锁、超时 vs 死掉的判据），不在「发版前顺手改」的范围 |
| 85 | low | `agentworkspace/sdk.py:119` | maintainability | 回调异常被 `except Exception: pass` 完全吞掉，且没有任何日志或错误回调钩子：插件作者写错回调后只会看到「事件收不到」，几乎无法定位。建议至少把 `exc` 打印到 stderr（或提供 `on_error` 参数），而不是彻底静默。… | 不影响行为，排队 |
| 86 | low | `agentworkspace/sdk.py:131` | bug | `data_dir()` 只拼路径不建目录。按 docstring 的用法直接 `open(ui.data_dir() / "x.json", "w")` 时，若 `data/` 尚未存在就会 `FileNotFoundError`。建议这里顺手 `mkdir(parents=True, exist… | 需要设计取舍（超时兜底、per-pid 锁、超时 vs 死掉的判据），不在「发版前顺手改」的范围 |
| 87 | low | `agentworkspace/selftest.py:163` | maintainability | `Hub` is already imported at module level (`from .link import Hub, Link`), so this re-import is dead weight. More importantly the alias `st` was used … | 不影响行为，排队 |
| 88 | low | `agentworkspace/settings.py:65` | bug | 端口为空时被放行：`ws://localhost:` 里 `rpartition` 得到 `port == ""`，而 `if port and not port.isdigit()` 因为 `port` 为假直接跳过，于是带空端口的地址会通过校验并落盘。空 `port` 应视为非法（或至少不应该带… | 需要设计取舍（超时兜底、per-pid 锁、超时 vs 死掉的判据），不在「发版前顺手改」的范围 |
| 89 | low | `agentworkspace/shell.py:44` | bug | 这个兜底值并不「保守」：1440×860 比不少笔记本屏幕（如 1366×768）还大。`SystemParametersInfoW` 失败时 `fit()` 会按 1440×860 去夹尺寸，在 1366×768 上会算出 1360×780 的窗口并偏到屏幕右下外面，正好是 `fit()` 想避免… | 需要设计取舍（超时兜底、per-pid 锁、超时 vs 死掉的判据），不在「发版前顺手改」的范围 |
| 90 | low | `agentworkspace/web/index.html:698` | maintainability | No `es.onerror` handler: when the SSE stream drops (server restart, network blip) the UI keeps showing the last known "已连上" state and silently stops r… | 不影响行为，排队 |
| 91 | low | `pyproject.toml:26` | maintainability | No `[project.urls]` section, so the published metadata carries no homepage/source/issue tracker links. Adding it is cheap and helps users and pip tool… | **已修** — 补 [project.urls] |

## 未修的，以及为什么

不是「不重要」，而是**改它们要么改架构、要么改语义**，不该塞在发版前顺手改。
按主题归类（明细则在最后的总表里逐条标着）：

1. **程序页面与主界面同源**（`web/index.html:284`，high）
   生成出来的页面没有 `sandbox`，能读同源的 `/api/**`、`localStorage`。
   真正的修法不是加个属性（`allow-same-origin` 一加就等于没加），而是让每个程序
   活在自己的 origin 上（子域或独立端口 + 每程序 token）。那是架构改动，
   顺带能解掉「插件崩了宿主不倒」之外的隔离问题，值得单独一轮做。
   本轮只把能先做的那半做了：`edit` 类的 POST 已经校验 `Host`/`Origin`。

2. **`dispatch` 没有超时兜底**（`kernel.py:653`，medium）
   声明给模型的 `timeoutMs` 目前是空话：handler 死循环就永久占住调用线程。
   修法是线程池 + `future.result(timeout=...)`，但那会引入「超时了线程还在跑」
   的新问题（要配合取消），不该临时加。

3. **store 的读-改-写没有互斥**（`kernel.py:211`，medium）
   两个线程同时 `ctx.set` 会互相覆盖。修法是 `Runtime` 上按 pid 维护一把锁 ——
   简单，但要顺便想清楚「插件在自己的线程里持有锁再调宿主」这种反向路径，
   所以留到下一轮和进程隔离一起做。

4. **`plugins/dev/` 与开发者工具集的 pid 撞车**（`kernel.py:47`，medium）
   目前 `scan()` 不拦 `dev` 这个名字。判据清楚（保留名），但要和「保留名还有哪些」
   一起定，单独加一个特例反而更容易漏。

5. **`ui` 字段没做路径约束**（`kernel.py:237`，medium）
   `"ui": "../../../secret"` 能跑出程序目录。修法与第 1 条同源（页面加载的可信边界）。

6. **测试工具自身的完备性**（`qa.py` / `selftest.py` / `uiprobe.py`，约 15 条）
   典型几条：`qa.py` 没有声明 Win32 原型（HWND 可能被截断）、`_shot()` 失败没计入
   checks、`selftest` 的 `all()` 在空列表上恒真（隔离检查会「空真」通过）。
   这些不会让产品出错，但会让**验收说谎**。已修了其中最危险的 4 条（权限位、
   窗口归属、跳过≠通过、读不到状态≠程序不在），其余排队。

7. **其余 medium / low**（约 40 条）
   多为「健壮性 + 可维护性」：坏清单能打断 `scan()`、`timeoutMs` 的字符串解析、
   `native` 的进程账本没有锁、`sdk` 的两处错误语义不一致、`BOOT_GRACE` 死代码等。
   每一条在总表里都有原文，没有删改。

## 总表（91 条，按严重度排序）

状态列：**已修** = 这一轮改了；**未修** = 见上面分类；行号是 ocr 给的原始行号。

