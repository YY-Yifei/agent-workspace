/* 对话闸门的**全矩阵**测试 —— 4 个相位 × 11 个事件，逐格断言。
 *
 * 跑法：node tests/turn.test.js
 * 为什么是纯 node：这个项目在 Session 0 里渲染不了窗口，而这类 bug 全是
 * 「某个顺序下按钮点不动 / 点两下发出两条」。它们是纯状态转移，
 * 用窗口去手点反而碰不全。和 md.test.js 同等待遇：只要断言 + 退出码。
 */
'use strict';

const T = require('../agentworkspace/web/turn.js');

let ok = 0, bad = 0;
function eq(name, got, want) {
  if (JSON.stringify(got) !== JSON.stringify(want)) {
    bad++; console.log(`✗ ${name}\n    要：${JSON.stringify(want)}\n    得：${JSON.stringify(got)}`);
  } else ok++;
}
function yes(name, cond, detail) {
  if (!cond) { bad++; console.log(`✗ ${name}${detail ? '\n    ' + detail : ''}`); } else ok++;
}

const EVENTS = [
  { type: 'submit' },
  { type: 'sent' },
  { type: 'reject' },
  { type: 'started' },
  { type: 'stop' },
  { type: 'cancelFailed' },
  { type: 'stopTimeout' },
  { type: 'terminal' },
  { type: 'linkDown' },
  { type: 'snapshot', busy: true },
  { type: 'snapshot', busy: false },
];
const NAMES = ['submit', 'sent', 'reject', 'started', 'stop', 'cancelFailed',
               'stopTimeout', 'terminal', 'linkDown', 'snapshot(忙)', 'snapshot(闲)'];

/* 期望矩阵：行 = 当前相位，列 = 事件。
   每一格都写出来，而不是"抽查几条"—— 这张表就是这份文件的规格。 */
const WANT = {
  idle:     ['sending', 'idle',    'idle',    'running', 'idle',     'idle',
             'idle',    'idle',    'idle',    'running', 'idle'],
  // linkDown 这一格：sending **不**动。断线说的是 aaas 连接，不是那趟还在路上的
  // /chat POST —— 它自己会以 reject 收场，走那条边回 idle。
  sending:  ['sending', 'running', 'idle',    'running', 'sending',  'sending',
             'sending', 'idle',    'sending', 'running', 'sending'],
  running:  ['running', 'running', 'running', 'running', 'stopping', 'running',
             'running', 'idle',    'idle',    'running', 'idle'],
  stopping: ['stopping', 'stopping', 'stopping', 'stopping', 'stopping', 'running',
             'idle',    'idle',    'idle',    'stopping', 'idle'],
};

for (const phase of Object.keys(WANT)) {
  EVENTS.forEach((ev, i) => {
    const got = T.next(phase, ev);
    eq(`${phase} + ${NAMES[i]}`, got, WANT[phase][i]);
  });
}

/* ---------------------------------------------------------------- 不变量 */
const PHASES = [T.IDLE, T.SENDING, T.RUNNING, T.STOPPING];
for (const p of PHASES) {
  // 1) 任何相位下按钮都得有个说得清的样子
  const b = T.btn(p);
  yes(`${p} 的按钮有文字`, typeof b.label === 'string' && b.label.length > 0);
  yes(`${p} 的按钮 action 合法`, ['send', 'stop', null].includes(b.action), String(b.action));
  // 2) 「不能按」与「没有动作」必须一致：disabled 却还带着 action 是可点的谎
  yes(`${p} 的 disabled 与 action 自洽`, !!b.disabled === (b.action === null),
      `disabled=${b.disabled} action=${b.action}`);
  // 3) 能发 ⇒ 不忙；忙 ⇔ 不能发
  yes(`${p} 的 isBusy 与 canSubmit 互补`, T.isBusy(p) === !T.canSubmit(p));
  // 4) 能停 ⇒ 一定在忙
  yes(`${p} 的 canStop ⇒ isBusy`, !T.canStop(p) || T.isBusy(p));
  // 5) 转移的落点必须是四个相位之一（拼错一个字符串就是界面永久卡住）
  for (const ev of EVENTS) yes(`${p} 的落点合法`, PHASES.includes(T.next(p, ev)), String(T.next(p, ev)));
}

/* 铁律：**只有终态、超时、断线、快照说闲、被拒（且仍在 sending）**能回到空闲。
   一旦有人在别的边上偷偷解锁，两轮并发就从这个口子进来。
   键按**列名**而不是 ev.type：两个 snapshot 事件的 type 一模一样，按 type 取键
   会让 snapshot(闲) 这一列永远查不到许可 —— 这一版就是这么先写错的。 */
const BACK_TO_IDLE = {
  'reject': ['sending'],          // 只有「我自己那次发送被拒」才谈得上回滚
  'stopTimeout': ['stopping'],    // 等不到终态的兜底解锁
  'terminal': PHASES,             // 终态当然可以
  'linkDown': ['running', 'stopping'],  // 连接没了那一轮跟着没了；sending 除外（POST 还在路上）
  'snapshot(闲)': ['running', 'stopping'],  // 服务端说不忙 = 那一轮真的结束了。
  // 注意 sending **不**在内：实现里 snapshot(闲) 遇到 sending 是原地不动
  // （我们那次 POST 可能还没到服务端，解锁就是放行第二轮），所以它根本落不到 idle。
};
for (const ev of EVENTS) {
  const name = NAMES[EVENTS.indexOf(ev)];
  for (const p of PHASES) {
    if (T.next(p, ev) !== T.IDLE || p === T.IDLE) continue;
    const allowed = BACK_TO_IDLE[name] || [];
    yes(`${p} + ${name} 回空闲被许可`, allowed.includes(p),
        '这条边不在许可表里 —— 它会让一个还在跑的轮次被解锁');
  }
}

/* 具体场景：这几条是用户真会撞上的，单独钉一遍，读起来比矩阵直观。 */
eq('连按两次回车不会发出两条', T.next(T.next(T.IDLE, { type: 'submit' }), { type: 'submit' }), T.SENDING);
eq('SSE 比 POST 响应先到：turn.started 先把相位推到 running',
   T.next(T.SENDING, { type: 'started' }), T.RUNNING);
eq('迟到的 sent 不许把 running 改回 sending',
   T.next(T.RUNNING, { type: 'sent' }), T.RUNNING);
eq('迟到的 reject 不许把 running 解锁',
   T.next(T.RUNNING, { type: 'reject' }), T.RUNNING);
eq('停止只请求一次：连点两下仍是 stopping',
   T.next(T.next(T.RUNNING, { type: 'stop' }), { type: 'stop' }), T.STOPPING);
eq('一轮刚起步就收到停止：仍是 stopping（不许翻回 running）',
   T.next(T.STOPPING, { type: 'started' }), T.STOPPING);
eq('停止没被受理 → 退回 running（它还在跑，别假装停了）',
   T.next(T.STOPPING, { type: 'cancelFailed' }), T.RUNNING);
eq('等不到终态 → 强制解锁',
   T.next(T.STOPPING, { type: 'stopTimeout' }), T.IDLE);
eq('终止后回到空闲，按钮变回发送',
   T.btn(T.next(T.RUNNING, { type: 'terminal' })).label, '发送');
eq('未知事件不改变相位', T.next(T.RUNNING, { type: 'message.delta' }), T.RUNNING);
eq('相位缺失当空闲', T.next(undefined, { type: 'submit' }), T.SENDING);
eq('事件缺失不炸', T.next(T.RUNNING, undefined), T.RUNNING);

/* 按钮五态的样子 —— 文字是给用户看的判据，别退化成「忙/闲」两个字。 */
eq('空闲：发送', T.btn(T.IDLE).label, '发送');
eq('发送中：写明在发送', T.btn(T.SENDING).label, '发送中…');
eq('运行中：停止', T.btn(T.RUNNING).label, '停止');
eq('正在停止：写明在停止', T.btn(T.STOPPING).label, '正在停止…');
yes('停止键是红的（.stop 类）', T.btn(T.RUNNING).cls.includes('stop'));
yes('发送中 / 正在停止都不可点', T.btn(T.SENDING).disabled && T.btn(T.STOPPING).disabled);

console.log(`\n${ok} 通过 / ${bad} 失败`);
process.exit(bad ? 1 : 0);
