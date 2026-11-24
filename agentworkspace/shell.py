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
"""

from __future__ import annotations

import threading
from pathlib import Path

TITLE = "我的工作区"
LOAD_TIMEOUT = 12.0          # 页面 12 秒还没 loaded，就判定为白屏
MARGIN = 40                  # 窗口离工作区边缘留一点，别贴着任务栏


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


def open_copy_doors(window, debug: bool = False) -> tuple[bool, str]:
    """把「选中 → 复制」那两道被 pywebview 关掉的闸打开。

    返回 (是否真的改到了, 说明) —— 这个返回值只给测试与日志用：
    改不到**不是错误**（别的平台没有 CoreWebView2），页面侧还有兜底。

    DevTools 仍然跟随 ``debug``：不因为要能复制就把 F12 一起送出去。
    """
    try:
        core = window.native.webview.CoreWebView2
        s = core.Settings
        s.AreDefaultContextMenusEnabled = True      # 右键 → 复制
        s.AreBrowserAcceleratorKeysEnabled = True  # Ctrl+C / Ctrl+A
        s.AreDevToolsEnabled = bool(debug)         # 仍然不给调试器
        return True, "webview2 settings applied"
    except Exception as exc:                        # 拿不到 native / 不是 WebView2
        return False, f"{type(exc).__name__}: {exc}"


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
            # 这两项设置只在 WebView2 上存在，而且必须等 CoreWebView2 就绪
            # （正是 loaded 的这一刻）。改不到就算了 —— 页面里有兜底。
            open_copy_doors(window, debug)
            loaded.set()

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
