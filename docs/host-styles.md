# 宿主样式 → 插件：一份文件，三种用法

> 一句话：插件的样式不该是「抄宿主的」，而是「引宿主的」。
> 那个 URL 是 `/aw.css` —— **宿主自己引的也是它**，不是它的副本。

## 0. 起点：这件事不是"风格统一"，是"继承根本不存在"

插件页住在 `<iframe src="/plugin/<pid>/">` 里。同源，但是**另一份文档**。
三条都断在文档边界上：

| 想传的东西 | 为什么传不过去 |
|---|---|
| `--bg`、`--accent` 这些自定义属性 | 它们沿**元素树**继承。iframe 里的 `<html>` 不在宿主的元素树里 |
| `::-webkit-scrollbar{…}` | 文档级伪元素，只作用于它自己那份文档 |
| `color-scheme` | 每个文档各算各的（原生输入框、滚动条底色、下拉箭头都看它） |

第十轮把宿主的滚动条从头做了一遍（17px → 8px 热区 / 4px 视觉、箭头归零、
两档对比度按 WCAG 算过）。那时候宿主自己好看了，**插件页里还是那条 17px 的系统默认条** ——
两张截图放在一起像两个产品。原因不是"忘了改插件"，是上面那三条：
**你在宿主页写下的样式，一个字节都不会进 iframe。**

于是「让插件像宿主」只有两条路：

- 每份插件各抄一遍 —— 第一处改动起开始漂移，而且抄的人（包括 agent）不会记得回来改；
- 或者，**同一个 URL**。

这份文件就是那个 URL。

## 1. 三种用法，一份文件

| 用法 | 谁动手 | 拿到的 |
|---|---|---|
| **① 插件自己引**（推荐）`<link rel="stylesheet" href="/aw.css">` | 插件 | 令牌 + 底座 + 滚动条；再引 `/aw.js` 就自动跟着深浅切换 |
| **② 只借样式、不要桥**：只引 `aw.css`，不引 `aw.js` | 插件 | 同上，深浅跟随系统（`@media` 兜底，门是 `:not([data-theme])`） |
| **③ 宿主注入** | 宿主，插件零协商 | 同一份字节，但整份在 `@layer` 里 → 插件自己写的规则赢；只补它没写的 |

③ 是**兜底**，不是主路：它保证「一份都没引」的老插件也有像样的滚动条和
`color-scheme`，但它补不出「插件用它自己的类名画自己的界面」这件事 —— 那只能靠 ①。

三种用法拿到的字节**完全相同**（`GET /aw.css` 直接读包里那份，selftest 有一条
断言在比对字节）。这是刻意的：如果注入用的是"另一份精简版"，两份迟早会不一致，
而"不一致"的表现形式是某一个页面在某一档主题下颜色不对 —— 最难查的那类 bug。

## 2. `@layer` 是这份文件的骨架，不是风格

整份 `aw.css` 被两个层包住：

```css
@layer aw.tokens, aw.base;
```

作用只有一个：**让「注入」不等于「替别人做决定」**。
CSS 里未分层的规则永远赢过分层的规则，与书写顺序无关。所以：

- 插件自己写了 `body{background:#fff}` → 它赢，注入的底色不会盖掉它；
- 插件没写滚动条 → `aw.base` 里的伪元素生效，滚动条自动变成宿主那条；
- 插件想退出某一层，写一条**未分层**的同名规则即可。那是退出口，不是漏洞。

**如实说一个代价**：`box-sizing:border-box` 也在 `aw.base` 里，所以被注入的插件页
如果没有自己声明过盒子模型，会从 `content-box` 变成 `border-box`。这是刻意的取舍 ——
「插件的默认盒子模型」和「插件的滚动条」是同一类东西，不该由它自己重新决定；
不认同的插件用上面那个退出口覆盖即可。

支持区间：`@layer` 需要 Chromium 99+ / Gecko 97+。桌面壳走 WebView2，满足。

## 3. 契约：只认 `--aw-*`

```
--aw-bg      --aw-panel   --aw-line    --aw-ink     --aw-dim
--aw-bubble  --aw-accent  --aw-ok      --aw-bad
--aw-sb      --aw-sb-thumb            --aw-sb-thumb-hi
```

三条规则：

1. **插件只用 `--aw-*`。** 宿主内部还叫 `--bg` / `--panel` / …，那是它自己的别名
   （`index.html` 的 `:root` 里一行行映射过去）。那不是契约，随时会变。
2. **插件可以加自己的语义变量**，但值必须来自上表：`--card: var(--aw-panel)`。
   这是 helloworld 现在的写法（它以前抄了六个颜色值，宿主改一次色它就停在旧色上）。
3. **插件不写 `::-webkit-scrollbar`。** 写了就赢过 `aw.base`（未分层 > 分层），
   然后它就得自己维护 WCAG 对比度那两档 —— 那是宿主该管的事。

## 4. 深浅是怎么跳过去的

```
config.json（真源：auto / dark / light）
   └─ /api/state.settings.theme → 宿主 applyTheme()
        ├─ ① 宿主自己的 <html data-theme>            ← aw.css 的 :root[data-theme=light] 认它
        ├─ ② 事件 ui.theme（EventSource）
        │     ├─ aw.js：paint() → 插件自己那份文档的 <html data-theme>
        │     └─ 原生窗口：/api/theme 一问一答
        └─ ③ 宿主注入时顺手把 data-theme 贴给 iframe（没有 aw.js 的插件页没人写这个属性）
```

为什么不让插件各自 `paint()`：抄的人一多，总有一份忘了跟着改 —— 与抄颜色是同一个病。
`aw.js` 现在自己会做这件事（`function paint()`），插件只在自己需要**在 JS 里**对主题
做额外动作时才订阅 `aw.on_theme`。

`@media (prefers-color-scheme:light)` 那一段是兜底，只服务「引了样式、没引桥」的页面。
它的门是 `:root:not([data-theme])` —— 属性一出现它立刻失效，**两条判据永远不可能同时
说话**。第十轮踩过的坑正是这个：宿主页里留着 `@media`，设置里又选深色，于是
"设成深色的人被系统切浅"。

那段浅色与主浅色块必须**逐字相同**；selftest 有一条断言在比对它们的声明集合
（同一份浅色写两遍，那正是要漂移的地方）。

## 5. 自建插件怎么落地（给 agent 的操作顺序）

1. `<link rel="stylesheet" href="/aw.css">` 放 `<head>` 里、自己的 `<style>` **之前**；
2. `<script src="/aw.js"></script>`；
3. 颜色一律 `var(--aw-*)`，不写裸 hex；
4. 不写滚动条样式、不写 `color-scheme`；
5. 动手前先 `dev_host_services()` —— 清单从活代码反射，三面（backend / web / native）同名；
6. 写完 `dev_page_open` + `dev_page_check(shot=true)` 看**真页面**：六面里
   「0 console 错误 / 无横向溢出」这两面会立刻抓到"样式没引上"这一类问题。

## 6. 已知没做的（边界，不装作做了）

- **没有强制**：插件可以不引 `aw.css`。注入只补滚动条/`color-scheme`/盒子模型这几样
  它没写过的东西，补不出「用宿主的视觉语言画自己的控件」。
- **没有控件库**：`aw.css` 只有令牌 + 底座（正文、滚动条、盒子模型），
  没有任何 `.btn` / `.card` 之类的组件类。现在宿主自己的按钮是 index.html 里
  的页面私有 CSS —— 要变成公共组件，得先把它们从宿主页里抽出来，
  那是另一件事（也是这套机制的下一步）。
- **没有 lint**：「页面里有没有裸 hex 颜色」目前只能靠人/agent 自觉。
  下一步该做的是 `page_check` 加一条「颜色令牌覆盖率」，让偏差可见 ——
  与「报错文案里不许出现模板拼出来的文件名」是同一条原则：**不查就别声称**。

## 7. 验收凭据

- `agentworkspace/web/aw.css` 是唯一真源；`GET /aw.css` 读的就是它（`server.py`）。
- `index.html` 只留 `:root` 别名 + 窄栏那条 `#rail::-webkit-scrollbar{width:8px}`；
  selftest 断言「宿主页里 `::-webkit-scrollbar` 只出现 1 次」—— 多一份就红。
- selftest 8 条：aw.css 在 / 宿主页引了它且在 `<style>` 之前 / 令牌只有一份 /
  注入的是同一个 URL 且带 `data-aw-skin` / 整份在 `@layer` 里 / 注入时贴 `data-theme` /
  两处浅色逐字相同 / `/aw.css` 200 + `text/css` 且字节与包里那份相同。
- `plugins/helloworld/ui/index.html` 是范本：它以前抄了六个颜色，现在只有
  `--card: var(--aw-panel)` 一条本地变量，外加 `<link href="/aw.css">`。
