"""index.html：粘贴 / 拖入 / 选文件 → 预览条 → 随消息发出去；气泡里画缩略图。"""
from pathlib import Path

p = Path(r"D:\projects\claw\open-agents\openagent-workspace\agentworkspace\web\index.html")
s = p.read_text(encoding="utf-8")
orig = s


def sub(old, new, count=1):
    global s
    assert old in s, f"没命中：{old[:80]!r}"
    s = s.replace(old, new, count)


# ---------------------------------------------------------------- CSS
sub(
    """  .composer{display:flex;gap:8px;padding:12px 16px;border-top:1px solid var(--line);
    align-items:flex-end}
  .solo .composer{max-width:720px;margin:0 auto;width:100%}""",
    """  /* 带图的输入区：预览条在上、输入行在下。加一行不改位置 —— 只是把
     「这一张要不要发」摆在发送键旁边，而不是藏在某处设置里。 */
  .composer{display:flex;flex-direction:column;gap:8px;padding:12px 16px;
    border-top:1px solid var(--line)}
  .crow{display:flex;gap:8px;align-items:flex-end}
  .solo .composer{max-width:720px;margin:0 auto;width:100%}
  /* 待发的图：小、可删、看得清。× 在角上而不是列表里 —— 删的是**那一张**。 */
  .pends{display:none;gap:8px;flex-wrap:wrap}
  .pends .pk{position:relative}
  .pends img{height:56px;border-radius:8px;border:1px solid var(--line);object-fit:cover;display:block}
  .pends .pk button{position:absolute;top:-6px;right:-6px;width:20px;height:20px;
    padding:0;border-radius:50%;font-size:12px;line-height:1;background:var(--panel);
    color:var(--ink);border:1px solid var(--line);cursor:pointer}
  .msg.me .shots{display:flex;gap:6px;flex-wrap:wrap;margin-top:6px}
  .msg.me .shots img{max-width:180px;max-height:180px;border-radius:10px;
    border:1px solid var(--line);display:block}""",
)

# ---------------------------------------------------------------- 输入区结构
sub(
    """      <form class="composer" id="composer">
        <textarea id="ta" rows="1" placeholder="${esc(ph(p))}"></textarea>
        <button class="p" id="sendb" type="button">发送</button>
      </form>""",
    """      <form class="composer" id="composer">
        <div class="pends" id="pends"></div>
        <div class="crow">
          <button type="button" id="pickb" title="加一张图（也可以直接粘贴或拖进来）">图</button>
          <textarea id="ta" rows="1" placeholder="${esc(ph(p))}"></textarea>
          <button class="p" id="sendb" type="button">发送</button>
        </div>
        <input type="file" id="pickf" accept="image/*" multiple hidden>
      </form>""",
)

# ---------------------------------------------------------------- 状态
sub(
    """  sid: {},        // pid → 当前会话号（磁盘上那份对话的名字）""",
    """  sid: {},        // pid → 当前会话号（磁盘上那份对话的名字）
  pend: {},       // pid → 待发送的图 [{blob,url,name}]（还没随任何一轮出去）""",
)

# ---------------------------------------------------------------- 历史镜像带上图
sub(
    """      S.msgs[pid] = (a.history || []).map((h) => ({
        role: h.role === 'user' ? 'me' : 'it', text: h.text || '', tools: [],
      }));""",
    """      S.msgs[pid] = (a.history || []).map((h) => ({
        role: h.role === 'user' ? 'me' : 'it', text: h.text || '', tools: [],
        files: h.files || [],
      }));""",
)
sub(
    """    S.msgs[pid] = (a.history || []).map((h) => ({
      role: h.role === 'user' ? 'me' : 'it', text: h.text || '', tools: [],
    }));""",
    """    S.msgs[pid] = (a.history || []).map((h) => ({
      role: h.role === 'user' ? 'me' : 'it', text: h.text || '', tools: [],
      files: h.files || [],
    }));""",
)
sub(
    """  const rows = r.messages.map((h) => ({
    role: h.role === 'user' ? 'me' : 'it', text: h.text || '', tools: [],
    note: h.sys ? true : undefined,      // 接力消息是程序自己发的，不冒充人话
  }));""",
    """  const rows = r.messages.map((h) => ({
    role: h.role === 'user' ? 'me' : 'it', text: h.text || '', tools: [],
    files: h.files || [],                // 引用，不是字节：界面按引用拼 /img/<name>
    note: h.sys ? true : undefined,      // 接力消息是程序自己发的，不冒充人话
  }));""",
)

# ---------------------------------------------------------------- 输入区的三条入口
sub(
    """function wireComposer() {
  const ta = $('#ta'), form = $('#composer'), btn = $('#sendb');
  if (!ta || !form) return;
  ta.oninput = () => { ta.style.height = 'auto'; ta.style.height = Math.min(ta.scrollHeight, 160) + 'px'; };""",
    """function wireComposer() {
  const ta = $('#ta'), form = $('#composer'), btn = $('#sendb');
  if (!ta || !form) return;
  ta.oninput = () => { ta.style.height = 'auto'; ta.style.height = Math.min(ta.scrollHeight, 160) + 'px'; };
  wirePictures(ta, form);
  paintPends(S.pid);""",
)

sub(
    """/* ------------------------------------------------------- 对话的忙碌闸门""",
    """/* ------------------------------------------------------------ 待发的图
   三条入口（粘贴 / 拖进来 / 选文件）落进**同一个列表**：同一个东西就该有
   同一条路 —— 三条各写一遍，迟早有一条忘了清、或者忘了判体量。 */
const MAX_PICS = 8;         // 与服务端一轮的上限同一个数（多出来的会被整批拒）

function pendpics(pid) { return S.pend[pid] || (S.pend[pid] = []); }

function addPics(pid, files) {
  if (pid !== S.pid) return;                 // 图只属于看得见的那个输入框
  const list = pendpics(pid);
  let room = MAX_PICS - list.length;
  for (const f of files) {
    if (!f || !String(f.type || '').startsWith('image/')) continue;
    if (room <= 0) {
      push(pid, { role: 'it', note: true, tools: [],
                  text: `一次最多 ${MAX_PICS} 张图 —— 多出来的这几张没有加进来` });
      break;
    }
    room -= 1;
    list.push({ blob: f, url: URL.createObjectURL(f), name: f.name || '图.png' });
  }
  paintPends(pid);
}

function paintPends(pid) {
  const box = $('#pends');
  if (!box || pid !== S.pid) return;
  const list = pendpics(pid);
  box.style.display = list.length ? 'flex' : 'none';
  box.innerHTML = list.map((p, i) => `<div class="pk"><img src="${esc(p.url)}"
      alt="${esc(p.name)}" title="${esc(p.name)}"><button type="button" data-pk="${i}"
      aria-label="移除这张" title="不发这张">×</button></div>`).join('');
  paintComposer(pid);                        // 只有图也能发：按钮的可按性跟着它动
}

function dropPic(pid, i) {
  const list = pendpics(pid);
  const gone = list.splice(i, 1)[0];
  if (gone && gone.url) URL.revokeObjectURL(gone.url);
  paintPends(pid);
}

function clearPics(pid) {
  for (const p of pendpics(pid)) if (p.url) URL.revokeObjectURL(p.url);
  S.pend[pid] = [];
  paintPends(pid);
}

/* 一张图 → 一次 POST 里的那一份。异步是因为 FileReader 异步；
   失败也**不 reject** —— 返回一份空 data，由服务端那道预检给出人话。 */
function encodePic(p) {
  return new Promise((resolve) => {
    const fr = new FileReader();
    fr.onload = () => {
      const s = String(fr.result || '');
      resolve({ name: p.name, mime: String(p.blob.type || ''), data: s.split(',')[1] || '' });
    };
    fr.onerror = () => resolve({ name: p.name, mime: '', data: '' });
    fr.readAsDataURL(p.blob);
  });
}

function wirePictures(ta, form) {
  const pick = $('#pickb'), inp = $('#pickf');
  if (pick && inp) {
    pick.onclick = () => inp.click();
    inp.onchange = () => { addPics(S.pid, [...inp.files]); inp.value = ''; };
  }
  /* 粘贴：**只有真的带着图才抢**。纯文本粘贴必须照旧交给浏览器 ——
     把它也拦下来，那种「粘贴没反应」的错最难查。 */
  ta.addEventListener('paste', (e) => {
    const files = [...((e.clipboardData && e.clipboardData.files) || [])];
    if (!files.length) return;
    e.preventDefault();
    addPics(S.pid, files);
  });
  form.ondragover = (e) => { e.preventDefault(); };
  form.ondrop = (e) => { e.preventDefault(); addPics(S.pid, [...((e.dataTransfer || {}).files || [])]); };
  const box = $('#pends');
  if (box) box.onclick = (e) => {
    const b = e.target.closest ? e.target.closest('button[data-pk]') : null;
    if (b) dropPic(S.pid, Number(b.dataset.pk));
  };
}

/* ------------------------------------------------------- 对话的忙碌闸门""",
)

# ---------------------------------------------------------------- 发送时带上图
sub(
    """async function submitDraft() {
  const pid = S.pid, ta = $('#ta');
  if (!ta) return;
  if (!TM.canSubmit(phaseOf(pid))) { ta.focus(); return; }
  const text = ta.value.trim();
  if (!text) return;
  // 先摆上去：消息顺序必须对。turn.started / delta 可能比 POST 的响应**先到**，
  // 那时才 push 的话，人说的话会排在自己的回答后面。
  const mine = push(pid, { role: 'me', text, tools: [] });
  ta.value = ''; ta.style.height = 'auto';
  setPhase(pid, 'submit');
  S.cur[pid] = null;
  const r = await fetch(`/api/p/${pid}/chat`, {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ text }),
  }).then((x) => x.json()).catch((e) => ({ status: String(e) }));""",
    """async function submitDraft() {
  const pid = S.pid, ta = $('#ta');
  if (!ta) return;
  if (!TM.canSubmit(phaseOf(pid))) { ta.focus(); return; }
  const text = ta.value.trim();
  const pends = pendpics(pid);
  // 只有图、没有字是**一句话**（「这张怎么样」的省略式）。两样都没有才是空。
  if (!text && !pends.length) return;
  // 先摆上去：消息顺序必须对。turn.started / delta 可能比 POST 的响应**先到**，
  // 那时才 push 的话，人说的话会排在自己的回答后面。
  const shots = pends.map((p) => ({ name: p.name, url: p.url }));
  const mine = push(pid, { role: 'me', text, tools: [], files: shots });
  ta.value = ''; ta.style.height = 'auto';
  setPhase(pid, 'submit');
  S.cur[pid] = null;
  const images = await Promise.all(pends.map(encodePic));
  const r = await fetch(`/api/p/${pid}/chat`, {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ text, images }),
  }).then((x) => x.json()).catch((e) => ({ status: String(e) }));""",
)

sub(
    """  const st = String((r && r.status) || '');
  if (st === 'sent') {                     // 受理了：那句成为真的「我说过的话」
    setPhase(pid, 'sent');
    return;
  }""",
    """  const st = String((r && r.status) || '');
  if (st === 'sent') {                     // 受理了：那句成为真的「我说过的话」
    clearPics(pid);                        // 图已经随这一轮出去了，预览条清空
    setPhase(pid, 'sent');
    return;
  }""",
)

# 被拒时的两句人话：图还挂在输入框上，别说成「这句放回输入框」了事
sub(
    """  if (restoreDraft(pid, text)) {
    dropMsg(pid, mine);
    push(pid, { role: 'it', note: true, tools: [],
                text: `没能送出去：${st || '未知原因'} —— 这句已经放回输入框` });
  } else {
    mine.failed = true;
    push(pid, { role: 'it', note: true, tools: [],
                text: `没能送出去：${st || '未知原因'} —— 已经切到别的程序，这句留在这里` });
    if (pid === S.pid) paintThread();
  }""",
    """  // 带图的这一轮被拒时，**图还留在预览条上**（上面那些 dropPic/clearPics
  // 只在受理后才动它）：人改一个字再发，图不必重新挑一遍。
  const kept = shots.length ? `（${shots.length} 张图还在输入框上）` : '';
  if (restoreDraft(pid, text)) {
    dropMsg(pid, mine);
    push(pid, { role: 'it', note: true, tools: [],
                text: `没能送出去：${st || '未知原因'} —— 这句话${kept || '已经放回输入框'}` });
  } else {
    mine.failed = true;
    push(pid, { role: 'it', note: true, tools: [],
                text: `没能送出去：${st || '未知原因'} —— 已经切到别的程序，这句话留在这里` });
    if (pid === S.pid) paintThread();
  }""",
)

# ---------------------------------------------------------------- 气泡里画缩略图
sub(
    """function msgHtml(m) {
  if (m.role === 'me') {
    // failed = 没送出去、又没能放回输入框（人切了程序）。它不能被画成
    // 「我说过的一句话」，否则下一次看历史会以为它发出去了。
    return `<div class="msg me${m.failed ? ' failed' : ''}"><div class="body">${esc(m.text)}</div></div>`;
  }""",
    """function msgHtml(m) {
  if (m.role === 'me') {
    // failed = 没送出去、又没能放回输入框（人切了程序）。它不能被画成
    // 「我说过的一句话」，否则下一次看历史会以为它发出去了。
    const body = m.text ? `<div class="body">${esc(m.text)}</div>` : '';
    return `<div class="msg me${m.failed ? ' failed' : ''}">${body}${shotsHtml(S.pid, m)}</div>`;
  }""",
)

sub(
    """function toolsHtml(m) {""",
    """/* 一句话带的图。两个来源、一种画法：
   刚发出去的那条手上是本地 object URL；从盘上读回来的那条只有引用，
   于是拼 /img/<name> —— 两处指向同一张图，界面上没有第二种样子。 */
function shotsHtml(pid, m) {
  const list = m.files || [];
  if (!list.length) return '';
  const sid = S.sid[pid];
  const out = list.map((f) => {
    const src = f.url || (sid ? `/api/p/${pid}/session/${sid}/img/${f.name}` : '');
    if (!src) return '';
    const label = f.title || f.name || '图';
    return `<img src="${esc(src)}" alt="${esc(label)}" title="${esc(label)}" loading="lazy">`;
  }).join('');
  return out ? `<div class="shots">${out}</div>` : '';
}

function toolsHtml(m) {""",
)

assert s != orig
p.write_text(s, encoding="utf-8")
print("ok", len(orig), "->", len(s))
