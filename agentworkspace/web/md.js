/* md.js —— 把**智能体说的话**渲染成 HTML。
 *
 * 为什么自己写而不用 marked/markdown-it：
 *   ① 这是离线桌面壳。引一个远端 <script> = 断网就白屏 + 一条供应链；
 *   ② 引一个本地库 = 又多一个要跟着打包、跟着修的东西（见 pyproject 的 package-data：
 *      当年只写了 web/*.html，多一个 .js 就会静默漏进 wheel）；
 *   ③ 我们要的只是「对话里那点排版」，不是完整 CommonMark。
 *
 * 三条硬规矩（改这个文件之前先读）：
 *   1. **先转义、再变换。** 输入里的一切 < > " ' & 在进任何正则之前就变成实体，
 *      所以输出里除了本文件自己写出来的标签，不可能有别的东西 —— 原始 HTML 一律当文字。
 *   2. **链接只放行 http/https/mailto。** javascript:/data:/file:/协议相对(//x) 变文字，
 *      不给「点一下就在本机壳里执行」留口子。
 *   3. **不产任何内联事件属性**、不加载任何外部资源（图片也不加载，退化成链接）。
 *
 * 无 DOM 依赖：可以在 node 里直接 require 进来做单测（tests/md.test.js）。
 */
;(function (root) {
  'use strict';

  const ESC = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
  const BLANK = (s) => s === null || s === undefined ? '' : String(s);

  const esc = (s) => BLANK(s).replace(/[&<>"']/g, (c) => ESC[c]);

  /* 只认三种协议。判之前先把控制字符抹掉：`java\0script:` / `java\nscript:` 这类绕过
     全靠「正则前不进一串看不见的字节」才成立。 */
  const safeUrl = (u) => {
    const v = BLANK(u).replace(/[\u0000-\u001f\u007f]/g, '').trim();
    return /^(https?:\/\/|mailto:)/i.test(v) ? v : null;
  };

  /* ------------------------------------------------------------------ 行内 */
  /* 占位符用 \u0000 包夹：源文本里的 \u0000 进来第一步就被剥掉，
     所以「伪造一个占位符来偷渡标签」这条路不存在。 */
  const PH = '\u0000';
  const TOKEN = /\u0000(\d+)\u0000/g;

  /* 只有粗/斜/删除线：给链接文字用。不许产生占位符 —— 占位符要在最外层还原到不动点，
     这里再造新的层级只会让它更难收敛。 */
  const emph = (t) => String(t)
    .replace(/\*\*([^\n]+?)\*\*/g, '<strong>$1</strong>')
    .replace(/(^|[^\w])__([^_\n]+?)__(?!\w)/g, '$1<strong>$2</strong>')
    .replace(/~~([^~\n]+?)~~/g, '<del>$1</del>')
    .replace(/\*([^*\n]+?)\*/g, '<em>$1</em>')
    .replace(/(^|[^\w])_([^_\n]+?)_(?!\w)/g, '$1<em>$2</em>');

  /* 裸地址的尾巴：`见 http://x/a。` 里的句号不属于地址。
     括号要配平（wiki 那种以 (…) 结尾的地址别被吃掉一半）。 */
  const cutUrl = (m) => {
    let u = m;
    let tail = '';
    ['&quot;', '&#39;', '&lt;', '&gt;'].forEach((s) => {
      const i = u.indexOf(s);
      if (i >= 0) { tail = u.slice(i) + tail; u = u.slice(0, i); }
    });
    for (;;) {
      const last = u.slice(-1);
      if (/[.,;:!?、。，；：！？*_~]/.test(last)) { tail = last + tail; u = u.slice(0, -1); continue; }
      if (last === ')') {
        const open = (u.match(/\(/g) || []).length;
        const close = (u.match(/\)/g) || []).length;
        if (close > open) { tail = last + tail; u = u.slice(0, -1); continue; }
      }
      break;
    }
    return [u, tail];
  };

  const inline = (raw) => {
    // ① 先转义：从这一行往后，t 里没有「活的」标签，只有实体。
    let t = esc(raw).replace(/\u0000/g, '');
    const stash = [];
    const keep = (html) => PH + (stash.push(html) - 1) + PH;

    // ② 只把「转义的反引号」提前收起来：它是字面反引号，不该被 ③ 当成 code 的边界
    t = t.replace(/\\`/g, () => keep(esc('`')));

    // ③ 行内 code：反引号里的一切原样 —— 包括反斜杠（CommonMark 里 code span 不吃转义，
    //    `` `\\n` `` 就该照着 `\\n` 印出来）。此时内容已是实体，所以安全。
    t = t.replace(/(`+)([^\n]+?)\1(?!`)/g, (m, f, body) =>
      keep('<code>' + body.replace(/^ ([^\n]*[^ ]) $/, '$1') + '</code>'));

    // ④ 其余反斜杠转义（\* 不该变成斜体）：必须排在 code 之后，否则 code 里的反斜杠被吃掉
    t = t.replace(/\\([\\*_{}\[\]()#+\-.!~|>])/g, (m, c) => keep(esc(c)));

    // ⑤ 链接 [文字](地址)：不认的协议退回文字，不做一个「点得动但有毒」的东西。
    //    `!` 一并吃掉：图片不加载（规矩 3），但也不能把 `!` 留在界面上变成「!图」。
    t = t.replace(/!?\[([^\]\n]{0,300})\]\(([^()\s]{1,800})\)/g, (m, text, url) => {
      const u = safeUrl(url);
      if (!u) return keep(m);
      return keep('<a href="' + u + '" target="_blank" rel="noopener noreferrer">' +
                  emph(text) + '</a>');
    });

    // ⑥ 裸地址：排在链接之后 —— 已经进了 stash 的地址不会被二次包一层
    t = t.replace(/\bhttps?:\/\/[^\s\u0000]+/g, (m) => {
      const [u, tail] = cutUrl(m);
      if (!safeUrl(u)) return m;
      return keep('<a href="' + u + '" target="_blank" rel="noopener noreferrer">' + u + '</a>')
             + tail;
    });

    // ⑦ 粗/斜/删除线（** 在 * 之前配对）
    t = emph(t);

    // ⑧ 还原占位符，**要还原到不动点**：② 的占位符会被 ③/④ 包进 stash 的 HTML 里
    //   （`\*` 出现在行内 code 或链接文字里就是如此），单趟替换不会回头看替换结果，
    //   索引数字就会漏到界面上 —— 曾经的 bug：md('`\\*`') 输出成 <code>0</code>。
    //   stash[i] 只可能引用比 i 更早的槽位，所以无环，必然收敛。
    for (let guard = 0; guard < 16 && t.indexOf(PH) >= 0; guard++) {
      const before = t;
      t = t.replace(TOKEN, (m, i) => stash[Number(i)] === undefined ? '' : stash[Number(i)]);
      if (t === before) break;
    }

    // ⑨ 段内软换行：对话里一行一句是常态，读起来该断行
    return t.replace(/\n/g, '<br>');
  };

  /* ------------------------------------------------------------------ 块 */
  const HR = /^ {0,3}([-*_])[ \t]*(?:\1[ \t]*){2,}$/;
  const FENCE = /^( {0,3})(`{3,}|~{3,})[ \t]*([^`]*)$/;
  // 收尾的 # 序列前**必须有空白**：否则「C#」「F#」会被当成闭合标记吃掉
  const HEADING = /^ {0,3}(#{1,6})[ \t]+(.*?)(?:[ \t]+#+)?[ \t]*$/;
  const ITEM = /^( *)([-*+]|\d{1,9}[.)])( +)([\s\S]*)$/;
  const QUOTE = /^ {0,3}>[ \t]?/;
  const SEPCELL = /^ *:?-{1,}:? *$/;
  const CLOSE_FENCE = /^ {0,3}(`{3,}|~{3,})[ \t]*$/;

  /* 递归上限：`>` 串每行剥一个 > 就递归一层，上万字符的引用片段足以打爆调用栈 ——
     而流式每收一个 delta 都要重走一遍，越接近崩溃点越频繁。超限就退回纯文本。 */
  const MAX_DEPTH = 32;
  const indentOf = (s) => s.length - s.replace(/^ +/, '').length;

  const splitRow = (s) => {
    const t = BLANK(s).trim().replace(/^\|/, '').replace(/\|$/, '');
    const cells = [];
    let cur = '';
    for (let k = 0; k < t.length; k++) {
      const c = t.charAt(k);
      if (c === '\\' && t.charAt(k + 1) === '|') { cur += '|'; k++; continue; }
      if (c === '|') { cells.push(cur.trim()); cur = ''; continue; }
      cur += c;
    }
    cells.push(cur.trim());
    return cells;
  };

  const isTableStart = (lines, i) => {
    if (i + 1 >= lines.length) return false;
    if (lines[i].indexOf('|') < 0) return false;
    const sep = splitRow(lines[i + 1]);
    return sep.length > 0 && sep.every((c) => SEPCELL.test(c));
  };

  const startsBlock = (lines, i) => {
    const l = lines[i];
    return FENCE.test(l) || HEADING.test(l) || HR.test(l) || QUOTE.test(l) ||
           ITEM.test(l) || isTableStart(lines, i);
  };

  const parseTable = (lines, start, depth) => {
    const head = splitRow(lines[start]);
    const sep = splitRow(lines[start + 1]);
    const align = sep.map((c) => {
      const l = c.charAt(0) === ':';
      const r = c.charAt(c.length - 1) === ':';
      return l && r ? 'center' : (r ? 'right' : (l ? 'left' : ''));
    });
    const cell = (tag, text, k) => {
      const a = align[k] ? ' style="text-align:' + align[k] + '"' : '';
      return '<' + tag + a + '>' + inline(text) + '</' + tag + '>';
    };
    let i = start + 2;
    let body = '';
    while (i < lines.length && lines[i].trim() && lines[i].indexOf('|') >= 0) {
      const row = splitRow(lines[i]);
      body += '<tr>' + head.map((_, k) =>
        cell('td', row[k] === undefined ? '' : row[k], k)).join('') + '</tr>';
      i++;
    }
    const html = '<table><thead><tr>' + head.map((h, k) => cell('th', h, k)).join('') +
      '</tr></thead>' + (body ? '<tbody>' + body + '</tbody>' : '') + '</table>';
    return { html, next: i };
  };

  const parseList = (lines, start, depth) => {
    const first = ITEM.exec(lines[start]);
    const baseIndent = first[1].length;
    const ordered = /^\d/.test(first[2]);
    // 起始编号是**内容**，不是排版：模型「接着上文往下数」（3. 4. 5.）很常见，
    // 丢掉它浏览器就从 1 开始重编，读起来就是错的。
    const startNo = ordered ? parseInt(first[2], 10) : 1;
    const items = [];
    let cur = null;
    let i = start;

    while (i < lines.length) {
      const ln = lines[i];
      const mm = ITEM.exec(ln);
      // 标记变了就是另一个列表：`1. a` 后面跟 `- b` 不该并进同一个 <ol>
      if (mm && mm[1].length >= baseIndent && mm[1].length <= baseIndent + 1 &&
          /^\d/.test(mm[2]) === ordered) {
        const col = mm[1].length + mm[2].length + mm[3].length;
        let body = mm[4];
        let task = null;
        const tk = /^\[([ xX])\][ \t]+/.exec(body);
        if (tk) { task = tk[1].toLowerCase() === 'x'; body = body.slice(tk[0].length); }
        cur = { task, col, lines: [body] };
        items.push(cur);
        i++;
        continue;
      }
      if (!ln.trim()) {
        // 空行：后面还是本列表的东西就续着（保持段落分隔），否则列表到此为止
        let j = i + 1;
        while (j < lines.length && !lines[j].trim()) j++;
        if (j >= lines.length) { i = j; break; }
        const nx = lines[j];
        const nm = ITEM.exec(nx);
        const more = (nm && nm[1].length > baseIndent + 1) || indentOf(nx) > baseIndent;
        const sibling = nm && nm[1].length >= baseIndent && nm[1].length <= baseIndent + 1;
        if (more || sibling) { if (cur) cur.lines.push(''); i = j; continue; }
        i = j;
        break;
      }
      if (indentOf(ln) > baseIndent && cur) {
        // 更深的一行 = 当前项的内容（嵌套列表也从这里进去，递归处理）
        cur.lines.push(ln.replace(new RegExp('^ {0,' + cur.col + '}'), ''));
        i++;
        continue;
      }
      break;                        // 顶格又不是新项 → 列表结束，别把后面整段正文吞进来
    }

    const tag = ordered ? 'ol' : 'ul';
    const attr = ordered && startNo !== 1 ? ' start="' + startNo + '"' : '';
    const html = items.map((it) => {
      const inner = blocks(it.lines.slice(), depth + 1).trim()
        // 第一段摊平：`- a` 后跟子列表时是 <li>a<ul>…</ul></li>，
        // 而不是 <li><p>a</p><ul>…</ul></li> —— 后者让每一项多出一段行距。
        .replace(/^<p>([\s\S]*?)<\/p>/, '$1');
      const box = it.task === null ? '' :
        (it.task ? '<span class="task done">\u2713</span> ' : '<span class="task">\u25cb</span> ');
      return '<li>' + box + inner + '</li>';
    }).join('');
    return { html: '<' + tag + attr + '>' + html + '</' + tag + '>', next: i };
  };

  const blocks = (lines, depth) => {
    const d = depth || 0;
    const out = [];
    let i = 0;
    while (i < lines.length) {
      const line = lines[i];
      if (!line.trim()) { i++; continue; }

      // 围栏代码。**没有收尾也算代码块**：流式输出里 ``` 先到、内容后到，
      // 那时候如果当普通文字渲染，已经显示出来的代码会闪一下再变回去。
      const f = FENCE.exec(line);
      if (f) {
        const fence = f[2];
        const code = [];
        let closed = false;
        let j = i + 1;
        for (; j < lines.length; j++) {
          const cl = CLOSE_FENCE.exec(lines[j]);
          if (cl && cl[1].charAt(0) === fence.charAt(0) && cl[1].length >= fence.length) {
            closed = true;
            break;
          }
          code.push(lines[j]);
        }
        // 语言名只用来写一个 class。**截断**而不是删字符：`js"onx="` 里那串东西
        // 删剩 `jsonx` 会编出一个不存在的语言，截断成 `js` 才是它本来的意思。
        const lang = (f[3] || '').trim().split(/\s+/)[0].replace(/[^a-z0-9+#._-][\s\S]*$/i, '');
        out.push('<pre><code' + (lang ? ' class="lang-' + lang + '"' : '') + '>' +
                 esc(code.join('\n')) + '</code></pre>');
        i = closed ? j + 1 : lines.length;
        continue;
      }

      const h = HEADING.exec(line);
      if (h) {
        const n = h[1].length;
        out.push('<h' + n + '>' + inline(h[2]) + '</h' + n + '>');
        i++;
        continue;
      }

      if (HR.test(line)) { out.push('<hr>'); i++; continue; }

      if (QUOTE.test(line)) {
        const inner = [];
        while (i < lines.length && QUOTE.test(lines[i])) {
          inner.push(lines[i].replace(QUOTE, ''));
          i++;
        }
        const body = d < MAX_DEPTH
          ? blocks(inner, d + 1)
          : '<p>' + inline(inner.join('\n')) + '</p>';   // 太深了：当文字，别把栈打穿
        out.push('<blockquote>' + body + '</blockquote>');
        continue;
      }

      if (isTableStart(lines, i)) {
        const tb = parseTable(lines, i, d);
        out.push(tb.html);
        i = tb.next;
        continue;
      }

      if (ITEM.test(line)) {
        const lb = parseList(lines, i, d);
        out.push(lb.html);
        i = lb.next;
        continue;
      }

      const buf = [];
      while (i < lines.length && lines[i].trim() && (buf.length === 0 || !startsBlock(lines, i))) {
        buf.push(lines[i]);
        i++;
      }
      out.push('<p>' + inline(buf.join('\n')) + '</p>');
    }
    return out.join('\n');
  };

  /* 制表符只在**围栏之外**展开成 4 个空格（缩进判定只认空格）。
     围栏之内一字不动：Makefile 的 recipe 必须以 tab 开头，换成空格整段就废了；
     浏览器在 <pre> 里本来就能正确显示 tab。 */
  const expandOutsideFences = (lines) => {
    const out = [];
    let fence = null;
    for (const ln of lines) {
      if (fence === null) {
        const open = FENCE.exec(ln);
        if (open) { fence = { ch: open[2].charAt(0), n: open[2].length }; out.push(ln); continue; }
        out.push(ln.replace(/\t/g, '    '));
        continue;
      }
      const close = CLOSE_FENCE.exec(ln);
      if (close && close[1].charAt(0) === fence.ch && close[1].length >= fence.n) fence = null;
      out.push(ln);
    }
    return out;
  };

  const render = (src) => {
    const text = BLANK(src).replace(/\r\n?/g, '\n');
    if (!text.trim()) return '';
    return blocks(expandOutsideFences(text.split('\n')), 0);
  };

  render.esc = esc;
  render.inline = inline;
  render.MAX_DEPTH = MAX_DEPTH;

  if (typeof module !== 'undefined' && module.exports) module.exports = render;
  if (root) root.mdToHtml = render;
})(typeof globalThis !== 'undefined' ? globalThis : this);
