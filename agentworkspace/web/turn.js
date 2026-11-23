/* turn.js —— 对话的忙碌状态机。**纯函数**：不看 DOM、不发请求、不记时间。
 *
 * 为什么单独拎出来当一个文件：
 *   这里出过的 bug 全是「某个顺序下按钮点不动」——按下发送到 turn.started 之间
 *   有一次 fetch 往返，那段时间原来的代码认为「不忙」，于是连按两次回车就发了两条，
 *   第二条必被服务端拒、而且字已经先被清掉了。这种错在窗口里靠手点是碰不全的
 *   （这个项目在 Session 0 里还渲染不了窗口），但它是**纯状态转移**——
 *   抽成纯函数就能用纯 node 把整个矩阵跑一遍（tests/turn.test.js）。
 *
 * 四个相位，没有第五个：
 *   idle     空闲 —— 能发
 *   sending  已按下发送、服务端还没回话 —— 不许再发
 *   running  服务端已受理这一轮 —— 能停
 *   stopping 已请求停止、终态还没到 —— 不许再停（否则人会连点）
 *
 * 一条铁律：**只有终态（turn.completed / turn.failed）才能回到 idle。**
 * 迟到的 sent / reject 不许把一个已经在跑的轮次解锁 —— 那是两轮并发的入口。
 */
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.TURN = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';

  const IDLE = 'idle';
  const SENDING = 'sending';
  const RUNNING = 'running';
  const STOPPING = 'stopping';

  /* 事件 → 新相位。刻意写成 switch 而不是查表：
     查表里「没写」和「写成原相位」看起来一样，而这里的每个「不动」都是判断。 */
  function next(phase, ev) {
    const p = phase || IDLE;
    const t = (ev && ev.type) ? ev.type : String(ev || '');
    switch (t) {
      // 人按下发送 / 回车。忙的时候什么都不发生：不提交、不清空、不报错。
      case 'submit':
        return p === IDLE ? SENDING : p;

      // 服务端回「sent」。只认 sending 这一档：终态之后迟到的 ack 不许复活一轮。
      case 'sent':
        return p === SENDING ? RUNNING : p;

      // 服务端拒了（上一轮还没结束 / 没能连上 / 网络炸了）。同样只认 sending。
      case 'reject':
        return p === SENDING ? IDLE : p;

      // turn.started。可能比 POST 的响应先到（SSE 是另一条路），所以这一条
      // 也要能把 sending 推进到 running；已经在停止中的则保持不变 ——
      // 一轮刚起步就收到「已停止」，那仍然是停止。
      case 'started':
        return p === STOPPING ? p : RUNNING;

      // 人按下停止。只有真在跑才谈得上停。
      case 'stop':
        return p === RUNNING ? STOPPING : p;

      // 停止请求没被受理（连接断了 / 服务端不支持）：它还在跑，如实退回去。
      case 'cancelFailed':
        return p === STOPPING ? RUNNING : p;

      // 请求停止后一直等不到终态：界面强制解锁，且由调用方明说「没能确认停止」。
      // 不做这一条，服务端不回终态时界面就永久转圈，只能重启。
      case 'stopTimeout':
        return p === STOPPING ? IDLE : p;

      // 终态：completed / failed 都算这一轮结束了。
      case 'terminal':
        return IDLE;

      /* 连接断了：那一轮跟着没了，闸门必须开回去。
         但**不许解锁 sending**：那条事件说的是「aaas 连接断了」，而我们那趟
         /api/p/<pid>/chat 可能还在路上、稍后就会有结果 —— 提前解锁等于放行
         第二轮，而第一趟的拒绝会落在它不再拥有的状态上（正是这台机器要防的连发）。
         它自己会以 reject 收场：走那条边回 idle。 */
      case 'linkDown':
        return p === SENDING ? p : IDLE;

      /* /api/state 的快照（订阅建立/重连时用来补齐错过的终态）。
         两处不许它越界：
           · 「不忙」不许解锁 sending —— 我们那次 POST 可能还没到服务端，
             解锁就是放行第二轮；
           · 「忙」不许把 stopping 翻回 running —— 人已经按下停止了。 */
      case 'snapshot':
        if (ev && ev.busy) return p === STOPPING ? p : RUNNING;
        return p === SENDING ? p : IDLE;

      default:
        return p;
    }
  }

  // 现在能不能发：只有空闲能发。
  function canSubmit(phase) { return (phase || IDLE) === IDLE; }
  // 现在能不能停：只有真在跑能停。
  function canStop(phase) { return phase === RUNNING; }
  // 闸门关着吗（界面据此画「…」和自动展开工具组）。
  function isBusy(phase) { return (phase || IDLE) !== IDLE; }

  /* 按钮长什么样。label / cls / disabled / action 四样就够，
     全部由相位决定 —— 界面里不再有第二处「忙不忙」的判断。 */
  function btn(phase) {
    switch (phase || IDLE) {
      case SENDING:
        return { label: '发送中…', cls: 'p busy', disabled: true, action: null };
      case RUNNING:
        return { label: '停止', cls: 'stop', disabled: false, action: 'stop' };
      case STOPPING:
        return { label: '正在停止…', cls: 'stop busy', disabled: true, action: null };
      default:
        return { label: '发送', cls: 'p', disabled: false, action: 'send' };
    }
  }

  return {
    IDLE: IDLE, SENDING: SENDING, RUNNING: RUNNING, STOPPING: STOPPING,
    next: next, canSubmit: canSubmit, canStop: canStop, isBusy: isBusy, btn: btn,
  };
});
