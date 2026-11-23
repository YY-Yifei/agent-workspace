/* md.test.js —— agentworkspace/web/md.js 的单测。
 *
 * 跑法：node tests/md.test.js
 * 不引任何测试框架：这里要的东西只有「断言 + 退出码」，多一个依赖就多一个要维护的东西。
 */
'use strict';

const md = require('../agentworkspace/web/md.js');

let ok = 0, bad = 0;
function has(name, html, ...needles) {
  const miss = needles.filter((n) => !html.includes(n));
  if (miss.length) { bad++; console.log(`✗ ${name}\n    缺：${JSON.stringify(miss)}\n    得：${html}`); }
  else { ok++; }
}
function no(name, html, ...needles) {
  const hit = needles.filter((n) => html.includes(n));
  if (hit.length) { bad++; console.log(`✗ ${name}\n    不该有：${JSON.stringify(hit)}\n    得：${html}`); }
  else { ok++; }
}
function eq(name, html, want) {
  if (html !== want) { bad++; console.log(`✗ ${name}\n    要：${want}\n    得：${html}`); }
  else { ok++; }
}

/* ---------------------------------------------------------------- 基本排版 */
eq('空输入 → 空串', md(''), '');
eq('纯空白 → 空串', md('  \n\n '), '');
has('段落', md('你好'), '<p>你好</p>');
has('段内软换行断行', md('一\n二'), '<p>一<br>二</p>');
has('标题', md('## 小标题'), '<h2>小标题</h2>');
has('粗体', md('**粗**'), '<strong>粗</strong>');
has('斜体', md('*斜*'), '<em>斜</em>');
has('删除线', md('~~没了~~'), '<del>没了</del>');
has('行内 code', md('用 `a<b` 写'), '<code>a&lt;b</code>');
has('行内 code 不吃后面的语法', md('`**不是粗体**`'), '<code>**不是粗体**</code>');
no('行内 code 不吃后面的语法（反证）', md('`**不是粗体**`'), '<strong>');
has('分隔线', md('a\n\n---\n\nb'), '<hr>');
has('引用', md('> 一句'), '<blockquote><p>一句</p></blockquote>');

/* 下划线不许吃掉蛇形命名 —— 模型天天写 snake_case */
has('snake_case 不是斜体', md('用 snake_case_name 命名'), '<p>用 snake_case_name 命名</p>');
no('snake_case 不是斜体（反证）', md('用 snake_case_name 命名'), '<em>');

/* 反斜杠转义 */
has('\\* 不是斜体', md('乘号 \\* 号'), '乘号 * 号');
no('\\* 不是斜体（反证）', md('乘号 \\* 号'), '<em>');

/* ---------------------------------------------------------------- 围栏代码 */
const fence = md('```python\nprint("<b>")\n```');
has('围栏代码带语言', fence, '<pre><code class="lang-python">');
has('代码里的标签是文字', fence, '&lt;b&gt;');
no('代码里的标签不活', fence, '<b>');
has('围栏代码保留换行与缩进', md('```\n  a\n    b\n```'), '  a\n    b');

/* 流式：只有开头三反引号、内容还在路上 */
const open = md('看这里：\n```js\nlet a = 1;');
has('未闭合围栏也渲染成代码块', open, '<pre><code class="lang-js">let a = 1;</code></pre>');
no('未闭合围栏不把反引号漏成文字', open, '```');
has('围栏语言的怪字符被滤掉', md('```js"onx="\nx\n```'), '<code class="lang-js">');

/* ---------------------------------------------------------------- 列表 */
has('无序列表', md('- a\n- b'), '<ul><li>a</li><li>b</li></ul>');
has('有序列表', md('1. a\n2. b'), '<ol><li>a</li><li>b</li></ol>');
has('列表项里的行内语法', md('- **粗**'), '<li><strong>粗</strong></li>');
has('任务列表未完成', md('- [ ] 还没'), '<span class="task">○</span> 还没');
has('任务列表已完成', md('- [x] 做了'), '<span class="task done">✓</span> 做了');
has('嵌套列表', md('- a\n  - b'), '<ul><li>a\n<ul><li>b</li></ul></li></ul>');
has('列表到此为止（后面顶格正文不被吞）', md('- a\n正文'), '</ul>\n<p>正文</p>');
has('列表项的续行合进同一项', md('- 第一行\n  第二行'), '<li>第一行<br>第二行');

/* ---------------------------------------------------------------- 表格 */
const tbl = md('| 名 | 值 |\n|---|--:|\n| a | 1 |');
has('表头', tbl, '<th>名</th>');
has('对齐', tbl, '<th style="text-align:right">值</th>');
has('单元格', tbl, '<td>a</td>');
has('表格不吞后面的段落', md('|a|b|\n|-|-|\n|c|d|\n\n后文'), '</tbody></table>\n<p>后文</p>');

/* ---------------------------------------------------------------- 链接 */
has('链接', md('[文档](https://x.dev/a)'), '<a href="https://x.dev/a" target="_blank" rel="noopener noreferrer">文档</a>');
has('链接文字里的粗体', md('[**粗**](https://x.dev)'), '<strong>粗</strong>');
has('mailto 放行', md('[信](mailto:a@b.c)'), 'href="mailto:a@b.c"');
has('裸地址成链接', md('见 https://x.dev/a 吧'), 'href="https://x.dev/a"');
has('裸地址尾巴的句号留在外面', md('见 https://x.dev/a。'), '</a>。');
has('地址里的括号配平', md('https://x.dev/A_(b)'), 'href="https://x.dev/A_(b)"');

/* ---------------------------------------------------------------- XSS：这一节是重点 */
has('原始 HTML 变文字', md('<img src=x onerror=alert(1)>'), '&lt;img src=x onerror=alert(1)&gt;');
no('原始 HTML 变文字（反证）', md('<img src=x onerror=alert(1)>'), '<img');
has('script 变文字', md('<script>alert(1)</script>'), '&lt;script&gt;');
eq('javascript: 链接退回文字', md('[点我](javascript:alert(1))'),
   '<p>[点我](javascript:alert(1))</p>');
eq('大小写混写的 javascript:', md('[点我](JaVaScRiPt:alert(1))'),
   '<p>[点我](JaVaScRiPt:alert(1))</p>');
eq('带控制字符的 java\\0script:', md('[点我](java\u0000script:alert(1))'),
   '<p>[点我](javascript:alert(1))</p>');
eq('data: 链接退回文字', md('[点我](data:text/html;base64,PHN2Zz4=)'),
   '<p>[点我](data:text/html;base64,PHN2Zz4=)</p>');
eq('协议相对的 //x 退回文字', md('[点我](//evil.example)'), '<p>[点我](//evil.example)</p>');
eq('file: 退回文字', md('[点我](file:///C:/Windows)'), '<p>[点我](file:///C:/Windows)</p>');

/* 引号注入：地址里塞 " 想把属性截断 */
const q = md('[x](https://a.dev/"onmouseover="alert(1))');
no('地址里的引号不能截断属性', q, '"onmouseover="');
has('地址里的引号变成实体', q, '&quot;onmouseover=&quot;');
no('不产生内联事件属性', md('<a onclick="alert(1)">x</a>'), 'onclick="alert');
no('图片不加载（![] 不变成 img）', md('![图](https://x.dev/a.png)'), '<img');

/* 属性注入：语言名、alt、表头里的引号 */
no('语言名里的引号被滤', md('```a"onmouseover="x\nc\n```'), 'onmouseover');

/* 一段「什么都塞一点」的攻击串，一次性检查产物里没有任何活标签 */
const nasty = [
  '<svg onload=alert(1)>', '<iframe src=javascript:alert(1)>', '"><script>x</script>',
  '[a](javascript:alert`1`)', '[b](vbscript:msgbox(1))', '`</code></pre><script>x</script>`',
  '```<script>x</script>\ny\n```', '| <script> |\n|---|\n| x |',
].join('\n\n');
const out = md(nasty);
no('综合攻击串里没有 script 标签', out, '<script');
no('综合攻击串里没有 svg/iframe', out, '<svg', '<iframe');
no('综合攻击串里没有 javascript: 链接', out, 'href="javascript:');
no('综合攻击串里没有 vbscript', out, 'href="vbscript:');

/* ---------------------------------------------------------------- 评审回归（ocr 第一轮） */
/* 每一条对应一个被改掉的真 bug。删掉任何一条，那个 bug 就有机会回来。 */

/* [high] 占位符只还原一趟 → `\*` 在行内 code 里时索引数字漏到界面上 */
eq('行内 code 里的反斜杠是字面（编号不许漏出来）', md('`\\*`'), '<p><code>\\*</code></p>');
has('链接文字里的反斜杠不许漏编号', md('[a\\*b](https://x.dev)'),
    '>a*b</a>');
no('链接文字里的反斜杠不许漏编号（反证）', md('[a\\*b](https://x.dev)'), 'a0b');

/* [low] 标题收尾的 # 吃掉 C# / F# —— 这个项目聊的就是代码，标题里出现 C# 很正常 */
eq('# C# 不该被吃掉', md('# C#'), '<h1>C#</h1>');
eq('## F# 不该被吃掉', md('## F#'), '<h2>F#</h2>');
eq('收尾的 # 该吃的还是要吃', md('## 标题 ##'), '<h2>标题</h2>');

/* [low] 图片：不加载，但那个 `!` 也不许留在界面上 */
no('图片不留 ! 残渣', md('![图](https://x.dev/a.png)'), '!<a');
has('图片退化成可点的链接', md('![图](https://x.dev/a.png)'), '>图</a>');

/* [low] 有序列表的起始编号是内容，不是排版 */
has('3. 的起始编号要写进 ol', md('3. c'), '<ol start="3">');
has('从 1 开始不写 start', md('1. a'), '<ol><li>a</li></ol>');
no('从 1 开始不写 start（反证）', md('1. a'), 'start=');
has('标记变了就是另一个列表', md('1. a\n- b'), '</ol>\n<ul><li>b</li></ul>');

/* [medium] 围栏里的 tab 是内容：Makefile 的 recipe 必须以 tab 开头 */
has('围栏代码保留 tab', md('```\n\tx\n```'), '\tx');
no('围栏代码里的 tab 不被换成空格', md('```\n\tx\n```'), '    x');
has('围栏外 tab 仍当缩进用（tab 列表）', md('\t- a'), '<ul><li>a</li></ul>');

/* [medium] 引用递归不许打爆调用栈：一串 > 也算「怪输入不许抛异常」 */
{
  const deep = '>'.repeat(20000) + ' x';
  let got = null, err = null;
  try { got = md(deep); } catch (e) { err = e; }
  if (err) { bad++; console.log(`✗ 两万个 > 抛异常了：${err}`); }
  else if (typeof got !== 'string') { bad++; console.log('✗ 两万个 > 没返回字符串'); }
  else { ok++; }
}

/* [low] 死代码已删：段落分支不再有「buf 可能为空」的假兜底 —— 但行为不许变 */
eq('段落仍旧只出一段', md('第一行\n第二行\n第三行'), '<p>第一行<br>第二行<br>第三行</p>');

/* ---------------------------------------------------------------- 健壮性 */
/* 每一行都是「看起来像开头、又不像」的东西：不许抛异常、不许吞行 */
const weird = '---\n>\n#\n- \n1.\n|x|\n```\n~~~\n***\n[ ]\n[](x)\n**\n_';
eq('怪输入不抛异常且不返回 null', typeof md(weird), 'string');
has('怪输入不丢内容（超链接的成对标记还在）', md('[](x)'), '<p>[](x)</p>');
eq('undefined/null 不炸', md(null), '');

/* 性能：50KB 一屏也不该秒级卡住 */
const big = ('- 一条列表项，带 **粗体** 和 `code` 与 https://x.dev/a\n').repeat(900);
const t0 = Date.now();
md(big);
const ms = Date.now() - t0;
if (ms > 800) { bad++; console.log(`✗ 50KB 渲染太慢：${ms}ms`); } else { ok++; }

console.log(`\n${bad === 0 ? '全绿' : '有失败'}：${ok} 通过 / ${bad} 失败`);
process.exit(bad === 0 ? 0 : 1);
