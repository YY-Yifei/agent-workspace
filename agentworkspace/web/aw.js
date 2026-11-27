/* aw.js —— 宿主服务（插件网页面）。由宿主 ``GET /aw.js`` 现拼：文件里的
   __AW_PID__ / __AW_THEME__ / __AW_PREF__ 三个占位会被替换成这一次请求的真实值。

   为什么是「一个文件 + 三个占位」，而不是每次现场拼一整段 JS：这份代码要能被
   node 直接读进来跑一遍（自检里的「面门」就是这么数三面集合的）。生成器里拼
   字符串，那个门就永远只能靠读源码猜。

   用法（插件页面里）：

       <script src="/aw.js"></script>
       <script>
         aw.theme()                      // "dark" | "light" —— 解析后的，直接能画
         aw.on_theme(t => paint(t))      // 宿主改了外观就叫你
         aw.get('messages')              // 你自己的 store（与后端 ctx 同一份）
         aw.call('dingtalk', 'list')     // 调别的程序：走同一个 dispatch
       </script>

   ``aw.pid`` 留空时按页面自己的地址猜（``/plugin/<pid>/...``），所以插件页面
   不必把自己的 pid 再抄一遍。 */
(function (root) {
  'use strict';

  // -- pid：宿主填的优先；没填就从地址里认 ----------------------------------
  var FIXED_PID = '__AW_PID__';
  function guessPid() {
    if (FIXED_PID && FIXED_PID.indexOf('__') !== 0) return FIXED_PID;
    var p = (root.location && root.location.pathname) || '';
    var m = /^\/plugin\/([a-z][a-z0-9_]*)\//.exec(p);
    return m ? m[1] : '';
  }
  var PID = guessPid();

  // -- 外观：**宿主解析，插件只消费** --------------------------------------
  // 初值由宿主在发这份文件时填进来；之后跟着事件流改。插件永远拿不到 "auto"。
  var THEME = '__AW_THEME__';
  var PREF = '__AW_PREF__';

  // -- 外观：写进**自己这份文档** ------------------------------------------
  // 插件页在 iframe 里，那是另一份文档：宿主 <html data-theme> 传不过来。
  // 所以这条属性得有人在自己的 documentElement 上写 —— 写在这里，而不是让每个
  // 插件各自 paint()：抄的人一多，总有一份忘了跟着改（那正是 aw.css 要解决的问题）。
  // node 里跑（selftest 数三面集合）没有 document，它必须安静退场。
  function paint() {
    if (typeof document === 'undefined' || !document.documentElement) return;
    document.documentElement.dataset.theme = (THEME === 'light' ? 'light' : 'dark');
  }
  paint();

  function post(path, body) {
    return fetch(path, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(body || {})
    }).then(function (r) { return r.json(); });
  }

  function target(pid) { return pid || PID; }

  // -- 状态：与后端 ctx.get/set 是同一份 store.json -------------------------
  function get(key, dflt) {
    return fetch('/api/p/' + target() + '/store?key=' + encodeURIComponent(String(key)))
      .then(function (r) { return r.json(); })
      .then(function (j) { return (j && Object.prototype.hasOwnProperty.call(j, 'value')) ? j.value : dflt; });
  }

  function set(key, value) {
    return post('/api/p/' + target() + '/store', { key: String(key), value: value })
      .then(function (j) { return !!(j && j.ok); });
  }

  // -- 喊一声 ---------------------------------------------------------------
  function emit(kind, data) {
    return post('/api/p/' + target() + '/emit', { kind: String(kind), data: data || {} })
      .then(function (j) { return !!(j && j.ok); });
  }

  // -- 调一个能力：**不是新入口**，落到同一个 dispatch ----------------------
  // aw.call('list') 调自己；aw.call('dingtalk', 'list', {...}) 调别人。
  // 返回与后端 ctx.call 同一个形状：{status, text, data}。
  function call(pid, fn, args) {
    if (fn === undefined || fn === null) { fn = pid; pid = PID; }
    return post('/api/p/' + pid + '/call/' + fn, args || {});
  }

  // -- 外观：读 -------------------------------------------------------------
  function theme() { return THEME; }
  function theme_pref() { return PREF; }

  // -- 订阅 -----------------------------------------------------------------
  function on_event(cb, kinds) {
    if (typeof EventSource === 'undefined') return function () { };
    var es = new EventSource('/api/events');
    es.onmessage = function (m) {
      var ev;
      try { ev = JSON.parse(m.data); } catch (e) { return; }
      // 外观先记下来：那是**宿主的值**，与「谁订阅了」无关。
      if (ev.type === 'ui.theme' && ev.data) {
        THEME = ev.data.resolved || THEME;
        PREF = ev.data.pref || PREF;
        paint();
      }
      if (kinds && kinds.indexOf(ev.type) < 0) return;
      try { cb(ev); } catch (e) { /* 回调炸了别把订阅带走 */ }
    };
    return function () { try { es.close(); } catch (e) { } };
  }

  function on_theme(cb) { return on_event(cb, ['ui.theme']); }

  // -- 看一眼全局 -----------------------------------------------------------
  function state() { return fetch('/api/state').then(function (r) { return r.json(); }); }

  // -- 导出：名字与后端 ctx / 原生 Native **逐字相同** ----------------------
  root.aw = {
    version: 1,
    pid: PID,
    dir: '/plugin/' + PID,
    get: get,
    set: set,
    emit: emit,
    call: call,
    theme: theme,
    theme_pref: theme_pref,
    on_theme: on_theme,
    on_event: on_event,
    state: state
  };
})(typeof globalThis !== 'undefined' ? globalThis : this);
