"""桌面外壳 —— 一个原生窗口，不是浏览器标签页。

宿主本来就是一个本地 HTTP 服务（``agentworkspace/server.py``），所以「外壳」这件事只剩下：
**用什么把那个 URL 装起来。**

    pywebview   一个真正的桌面窗口：没有地址栏、没有标签栏、有图标、能最小化
    webbrowser  兜底：pywebview 起不来的时候，至少还能用

**为什么要「防白屏」**：``webview.start()`` 在 WebView2 初始化失败时**照样正常返回** ——
窗口开着、里面是白的、没有任何异常。如果就这样当真，用户看到的是一个白窗口，
而我们会以为一切正常。所以这里等 ``loaded`` 事件：**页面没加载过，就不算成功。**

三种结局，调用方必须区别对待：
    "ok"           窗口起来了，页面加载完了 —— 正常退出
    "blank"        窗口开了但页面没渲染（WebView2 缺失/无交互桌面）—— 关掉它，回退
    "unavailable"  pywebview 根本装不上 —— 回退

这条边界很干净：外壳不认识内核、不认识道，它只知道一个 URL。
将来换 Electron / Tauri / 系统托盘，也只动这一个文件。

**为什么这里要专门为「选中复制」写代码**（第二十三轮的账）：

pywebview 默认把「选中文字」这件事关掉了 —— ``create_window`` 的 ``text_select``
默认是 ``False``（6.2.1 的 ``window.py``），而它会变成 ``js/customize.js`` 里的
``disableTextCss``：``body{-webkit-user-select:none;…;cursor:default}``。
那不是「某个控件不能选」，是**整页**不能选。

就算选中了，WebView2 层还有两道闸，pywebview 把它们绑在自己的 ``debug`` 上
（``platforms/edgechromium.py``）：::

    AreBrowserAcceleratorKeysEnabled = _state['debug']   → Ctrl+C / Ctrl+A
    AreDefaultContextMenusEnabled   = _state['debug']    → 右键菜单里的「复制」

我们的窗口默认不是 debug，于是两者都是 ``False``：**选中、Ctrl+C、右键复制
三条路同时断**。三处一起修才是「能正常选中复制」，只修一处等于没修。

这里同时做兼容：老版本 pywebview 不认 ``text_select`` 参数（会 TypeError），
那就退回不传 —— 页面里还有一层 ``user-select:text`` 与 Ctrl+C 兜底，
不至于因为版本差一个参数就整页复制不了。

**为什么那几项设置必须投递到 UI 线程**（第三十五轮的账 —— 一次真正的回归）：

pywebview 的事件**不在 UI 线程上跑**。``window.events.loaded = Event(self)``
（6.2.1 ``window.py:164``）没传 ``should_lock``，而 ``should_lock=False`` 时
``Event.set`` 是 ``threading.Thread(target=execute).start()``（``event.py``）——
handler 永远落在一条**新线程**里，``set()`` 的调用者是谁都不影响。
所以「在 loaded 的 handler 里直接碰 ``CoreWebView2.Settings``」这件事，
从头到尾就是一次**跨线程 COM 调用**。

pywebview 自己从不这么干：这批 settings 它只在 ``on_webview_ready``
（CoreWebView2InitializationCompleted，UI 线程）里设（``edgechromium.py:286-296``），
别处一律 ``self.webview.Invoke(...)`` 投递过去（同文件 :152、:175），
并在注释里引了 WebView2Feedback#1976：
「cookies must be accessed in the main thread, otherwise an exception is thrown」。

所以这里也照办：``_on_ui_thread`` 先问 ``InvokeRequired``，已经在 UI 线程就就地做，
否则 ``BeginInvoke`` 投递并**有界**地等。有界是底线 —— 这几项设置只是装潢，
不该有任何一条路径能让外壳的启动卡住。
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

TITLE = "我的工作区"
LOAD_TIMEOUT = 12.0          # 页面 12 秒还没 loaded，就判定为白屏
MARGIN = 40                  # 窗口离工作区边缘留一点，别贴着任务栏
UI_WAIT = 2.0                # 投递到 UI 线程后最多等这么久（有界，绝不无限等）

_LOG = logging.getLogger("agentworkspace.shell")


def _trace(debug: bool, ok: bool, why: str) -> None:
    """把「门到底开没开」留一行痕。

    为什么补这一条：``open_copy_doors`` 的返回值在 ``_on_loaded`` 里一直被直接丢掉，
    于是两种失败（UI 线程超时 / 拿不到线程亲和性）在日志里一个字都没有。人看到的
    只是「右键复制不好使」，现场什么都没留下 —— 而这恰好是最难复现的那类问题。

    分寸：改不到**不是错误**（别的平台没有 CoreWebView2，页面侧还有兜底），所以
    ``debug=False`` 时连 info 都不刷（只在 DEBUG 级留一行，默认看不见）；
    ``debug=True`` 的人正好是想查这件事的人，那时把原因明确写出来。
    """
    _LOG.debug("复制开关 %s：%s", "已打开" if ok else "没打开", why)
    if debug and not ok:
        _LOG.warning("复制开关没打开（右键/快捷键可能用不了）：%s", why)


def work_area() -> tuple[int, int, int, int]:
    """桌面**工作区**（去掉任务栏）：(x, y, w, h)。拿不到就给保守值。"""
    try:
        import ctypes
        from ctypes import wintypes

        rect = wintypes.RECT()
        ok = ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0)
        if ok:
            return rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top
    except Exception:
        pass
    return 0, 0, 1440, 860


def fit(width: int, height: int, area: tuple[int, int, int, int] | None = None
        ) -> tuple[int, int, int, int]:
    """把想要的尺寸装进工作区，并居中。返回 (x, y, w, h)。

    这是纯函数，所以「窗口会不会掉到屏幕外」这件事可以被断言，而不必靠看图。
    """
    ax, ay, aw, ah = area or work_area()
    w = max(560, min(width, aw - MARGIN * 2))
    h = max(420, min(height, ah - MARGIN * 2))
    return ax + max(0, (aw - w) // 2), ay + max(0, (ah - h) // 2), w, h


def _on_ui_thread(control, func, timeout: float = UI_WAIT) -> tuple[bool, str]:
    """在 ``control`` 所属的那条线程上执行 ``func``；**有界**等待，绝不无限挂。

    返回 (是否真的执行完, 说明)。说明只给日志与测试看 —— 失败不是错误：
    这几项设置只是装潢，改不到还有页面侧兜底。

    三条判据，缺一条就退化成本轮要修的那个 bug：
      1. ``InvokeRequired`` 先问。已经在 UI 线程就地做 —— 投递给**自己**
         就是自己等自己，那才是最经典的那种死锁。
      2. 拿不到 ``InvokeRequired``（不是 WinForms 控件 / 平台不同）就**什么都不碰**：
         无法确认线程亲和性时碰 COM 是拿启动去赌。
      3. 投递之后只等 ``timeout``，不等它一辈子。``Control.Invoke``（同步版）
         没有超时重载，所以这里用 ``BeginInvoke`` + 自己等。
    """
    try:
        need = bool(control.InvokeRequired)
    except Exception as exc:            # 没有线程亲和性可问
        return False, f"{type(exc).__name__}: 拿不到线程亲和性，不碰控件"

    if not need:                        # 已经在这条线程上
        try:
            func()
        except Exception as exc:
            return False, f"{type(exc).__name__}: {exc}"
        return True, "inline"

    done = threading.Event()
    box: dict[str, BaseException] = {}

    def _run() -> None:                 # 这一段在 UI 线程上跑
        try:
            func()
        except BaseException as exc:
            box["err"] = exc
        finally:
            done.set()

    try:
        try:
            from System import Action       # pythonnet：控件要的是 Delegate，不是 callable
            delegate = Action(_run)
        except Exception:
            delegate = _run
        control.BeginInvoke(delegate)
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"

    if not done.wait(timeout):
        return False, f"UI 线程 {timeout:g}s 内没有执行，已放弃"
    err = box.get("err")
    if err is not None:
        return False, f"{type(err).__name__}: {err}"
    return True, "ui-thread"


def _open_copy_doors(window, debug: bool = False,
                     timeout: float = UI_WAIT) -> tuple[bool, str]:
    """把「选中 → 复制」那两道被 pywebview 关掉的闸打开（执行体，不留痕）。

    返回 (是否真的改到了, 说明) —— 这个返回值只给测试与日志用：
    改不到**不是错误**（别的平台没有 CoreWebView2），页面侧还有兜底。

    DevTools 仍然跟随 ``debug``：不因为要能复制就把 F12 一起送出去。

    整个 COM 访问都发生在 UI 线程的闭包里：``window.native`` 是 Python 属性
    （读它不碰 COM），``.webview`` 是 .NET 对象（同进程直接拿），
    ``CoreWebView2.Settings`` 才是第一处**必须**在 UI 线程上碰的东西。
    """
    try:
        control = window.native.webview          # 只是拿引用，还没碰 COM
    except Exception as exc:                    # 拿不到 native / 不是 WebView2
        return False, f"{type(exc).__name__}: {exc}"

    def _apply() -> None:                       # ← 这一整段都在 UI 线程上跑
        s = control.CoreWebView2.Settings
        s.AreDefaultContextMenusEnabled = True      # 右键 → 复制
        s.AreBrowserAcceleratorKeysEnabled = True  # Ctrl+C / Ctrl+A
        s.AreDevToolsEnabled = bool(debug)         # 仍然不给调试器

    return _on_ui_thread(control, _apply, timeout)


def open_copy_doors(window, debug: bool = False, timeout: float = UI_WAIT) -> tuple[bool, str]:
    """把闸打开，并且**无论谁调都留痕**。

    留痕放在这里而不是 ``_on_loaded`` 里，是因为「失败现场」属于这个函数：
    换一个调用方（测试、以后的别处）就不该悄悄丢掉原因。
    """
    ok, why = _open_copy_doors(window, debug, timeout)
    _trace(debug, ok, why)
    return ok, why


def run(url: str, *, title: str = TITLE, width: int = 1180, height: int = 800,
        debug: bool = False, storage: Path | None = None, icon: Path | None = None,
        timeout: float = LOAD_TIMEOUT) -> str:
    """把 ``url`` 装进一个桌面窗口，阻塞到窗口关闭。返回 ``ok`` / ``blank`` / ``unavailable``。

    失败**不抛**：外壳起不来不该让整个宿主起不来。
    """
    try:
        import webview
    except Exception:                      # 没装 / 装坏了
        return "unavailable"

    loaded = threading.Event()
    try:
        x, y, w, h = fit(width, height)        # 装进工作区，别让底部掉到屏幕外
        common = dict(min_size=(min(760, w), min(520, h)), background_color="#f7f6f4")
        try:
            # text_select=True → pywebview 不再注入 body{user-select:none}
            window = webview.create_window(title, url, width=w, height=h, x=x, y=y,
                                           text_select=True, **common)
        except TypeError:                       # 老版本 pywebview 没有这个参数
            window = webview.create_window(title, url, width=w, height=h, x=x, y=y,
                                           **common)

        def _on_loaded() -> None:          # 页面真的渲染出来了
            # 先把自己这件事办完：这个 handler 跑在 pywebview 新起的线程上，
            # 下面的开门是要投递到 UI 线程去的 —— 万一它卡住/超时，
            # 也**不能**连累「窗口到底成没成」这个判定（那是白屏判据，不是装潢）。
            loaded.set()
            # 这两项设置只在 WebView2 上存在，而且必须等 CoreWebView2 就绪
            # （正是 loaded 的这一刻），并且**必须回到 UI 线程**去设。改不到就算了。
            open_copy_doors(window, debug)

        # pywebview 的事件是**可 += 的对象**，不是装饰器。
        # 写成 @window.events.loaded 会直接抛 TypeError，外面看见的就是「不可用」——
        # 而真相是「窗口能开、只是没渲染」。这两种结局必须分清，不然回退的理由都是错的。
        window.events.loaded += _on_loaded

        def watchdog() -> None:            # 白屏兜底：关掉它，让 start() 返回
            if not loaded.wait(timeout):
                try:
                    window.destroy()
                except Exception:
                    pass

        threading.Thread(target=watchdog, name="shell-watchdog", daemon=True).start()
        kwargs = {"debug": debug}
        # 窗口图标：不给就落到 python.exe 那个（左上角是 Python 的标）。
        # pywebview 的文档说 icon 只在 GTK/QT 生效，但 winforms 实现里确实有这一段
        # （self.Icon = Icon(path)）—— 以实测为准，见 agentworkspace/winicon.py 的读回。
        if icon is not None and Path(icon).is_file():
            kwargs["icon"] = str(Path(icon))
        if storage is not None:            # 固定 profile 目录：不给临时目录出问题的机会
            Path(storage).mkdir(parents=True, exist_ok=True)
            kwargs.update(private_mode=False, storage_path=str(storage))
        webview.start(**kwargs)            # 阻塞：窗口关掉才返回
    except Exception:                      # 后端起不来（WebView2 缺失等）
        return "unavailable"
    return "ok" if loaded.is_set() else "blank"


def available() -> bool:
    """pywebview 能不能用（只探 import，不起窗口）。"""
    try:
        import webview  # noqa: F401
        return True
    except Exception:
        return False
