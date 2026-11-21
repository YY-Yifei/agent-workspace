# 设置入口 与 窗口图标

两个小东西，各自有一条「看起来能做」和「真的做到了」之间的沟。

---

## 一 · 设置：全系统只有一项

**入口在左下角**（`#rail` 底部，程序那一列的下方，中间一道分隔线）。
它不是程序：没有 pid、不进 `state()` 的 `programs`、不参与「一人一条会话」。
理由一句话：它管的是**整个工作区连哪个道**，不属于任何一个程序。

面板里只有一个字段：**aaas-service 地址**。下面对两行小字：

| 行 | 说什么 |
|---|---|
| `#setstat` | 此刻的连接状态（按**所有程序**汇总：已连上 N 个 / 正在连接 / 连不上 + 错误原文 / 还没有程序连过） |
| `#setmsg` | 这次保存的回执（成功 / 失败原因，错误消息直接来自后端） |

### 保存 = 落盘 + 立刻重连（不是只改字段）

```
面板  ──POST /api/settings {url}──►  settings.save_url()   校验 → 原子写 config.json
                                    settings.hub_cfg()    唯一翻译点（url + revision）
                                    hub.rebind(cfg)       停掉旧 Link，按新配置重建
       ◄── {status, url, reconnected:[pid…], agents} ──
```

**为什么必须重建**：每条 `Link` 的地址在 `_main()` 里读一次就建了 `aaas.Caller`。
只改 `cfg` 里的字段，已有连接不会改道 —— 「设置生效了」就成了假话。
代价是正在跑的那一轮会被中断，所以回执里会明说「正在用新地址重连 N 个程序」。

### 校验只做三件事

`settings.normalize()` 是一个纯函数（所以不起服务就能验）：

1. 去首尾空白；
2. 协议必须是 `ws / wss / http / https`；
3. 必须能看出主机名（`ws://:8888/` 拒绝、`ws://x:abc/` 拒绝、`[::1]:8888` 允许）。

**不替人补端口，不改路径** —— 猜错的地址比报错更难查。

### 这道门只属于人

`/api/settings` 没有对应的工具：agent 走不到这里。
`toolbox.RESERVED = ("config.json", "dev.json")` 仍然对 `write` 一律拒绝 ——
一个能写文件的程序如果能改这份配置，就等于能给自己或别人拨权限。

---

## 二 · 窗口图标：左上角那个标是谁的

pywebview 在 Windows 上默认把 `Form.Icon` 设成从 `sys.executable` 里抠出来的图标
（见 `webview/platforms/winforms.py`）—— 于是窗口左上角、任务栏、Alt-Tab 全是 Python 的蛇。

**文档与实现不一致**：pywebview 的参数说明写着「icon 只在 GTK/QT 生效」，
但 winforms 实现里确实有 `if _state['icon'] ... self.Icon = Icon(path)`。
这种时候只有一个办法：**读回窗口上的那个 HICON**。

### 图标本身（`agentworkspace/assets/make_icon.py`）

只用标准库（`zlib + struct`）画图并打包：

| 尺寸 | 格式 | 原因 |
|---|---|---|
| 16 / 24 / 32 / 48 / 64 | 32 位 BMP（DIB） | 老外壳与新窗口管理器都认 |
| 128 / 256 | PNG | Vista 起支持，体积小得多 |

图形语言和界面一致：左侧一条亮栏 + 右边两张卡 —— 就是这个窗口自己的样子。
16px 下仍认得出（`--preview` 会出一张整数倍放大的对照图，用眼睛验）。

### 装上去

`main.py` → `shell.run(..., icon=ROOT/"assets"/"icon.ico")` → `webview.start(icon=...)`。

### 验回来（`agentworkspace/winicon.py`）

```
WM_GETICON / GetClassLongPtr   → 窗口此刻挂着的大/小图标句柄
GetIconInfo + GetDIBits        → 把句柄拆成 RGBA 像素
逐像素相似度                    → 与 icon.ico 的 32px 帧比、与 python.exe 的图标比
```

实测（会话 2，真窗口）：

```
窗口 0x560600「我的工作区」；句柄 {'big': 46995013, 'small': 2298065}
窗口大图标 64×64
与我们的标相似度 89%          与 python.exe 的图标相似度 1%
结论：ours
```

### 两条只在 64 位上才炸的坑

1. **`GetObjectW` / `DeleteObject` 必须先声明 `argtypes`**。不声明时 ctypes 把
   `HGDIOBJ` 当 32 位 int，句柄一大就 `OverflowError: int too long to convert` ——
   而这个错**只在真的拿到句柄时才出现**，静态看代码永远看不出来。
2. `GetDIBits` 的 `biHeight` 用**负数**（自上而下），省一次翻转；
   `ICONINFO.hbmColor` 用完要 `DeleteObject`，否则每读一次泄漏两个位图。

---

## 三 · 验收

| 断言 | 怎么验 | 结果 |
|---|---|---|
| ⚙ 在左下角、不是程序 | 真浏览器量 `getBoundingClientRect` | 贴栏底、无 `data-pid` |
| 面板只一项、预填当前值、焦点在字段上 | 真点击 + 读 DOM | 通过 |
| 坏地址当场报错且不改任何东西 | 提交 `ftp://nope/` | `bad` 提示 + 地址未变 |
| 好地址落盘、`/api/settings` 一致、界面立即跟上 | 提交 + 读磁盘 + 读 API | 三者一致 |
| 关闭（Escape）、再开显示新地址 | 真按键 | 通过 |
| 图标真的是我们的 | `winicon` 逐像素比 | 89% vs python 1% |

```
python -m agentworkspace.selftest          # [4b] 设置 18 项
python -m agentworkspace.uiprobe           # 真窗口 28 项（自带工作区）
python -m agentworkspace.winicon           # 图标读回（要在有窗口的会话里）
python -m agentworkspace.qa desktop        # 壳 4 项
```

**uiprobe 自带工作区**（`.tmp/qa/ws-probe`）：它要真的保存一次设置，而那是写
`config.json` 的动作 —— 在别人的真实工作区里跑，就等于拿测试改用户的数据。
