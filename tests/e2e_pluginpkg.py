"""真浏览器里点一次「导出」、再点一次「导入」—— 判的是**落下来的那个文件**，不是代码里有没有那行字。

为什么单独一份：`selftest` 能验包的内容、能验路由的字节，但它验不了两件事 ——

  1. 那颗按钮按下之后，浏览器**真的**收到了一次下载（而不是把 zip 当一页打开）；
  2. 选文件 → 看一眼 → 点导入，这条**人点的那条路**真的通（而不是只有 curl 通）。

两台真东西：真 Chrome（Playwright channel=chrome）+ 真服务器 + 真 runtime（不 mock 任何路由）。
所以这一份里没有 STATE 替身：`/api/state` 是内核现算的。

    python tests/e2e_pluginpkg.py
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import threading
import zipfile
from pathlib import Path
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

ok = fail = 0


def check(name: str, cond, detail: str = "") -> bool:
    global ok, fail
    if cond:
        ok += 1
        print(f"  [ok]   {name}")
    else:
        fail += 1
        print(f"  [FAIL] {name}" + (f" —— {detail}" if detail else ""))
    return bool(cond)


BACKEND = '''"""真浏览器用的示例程序。"""

n = 0


def bump(by: int = 1) -> str:
    """加一点。

    Args:
        by: 加多少
    """
    global n
    n += by
    return f"现在是 {n}"
'''

UI_HTML = "<!doctype html><html><body><h1>样例</h1></body></html>"


def make_plugin(pdir: Path, title: str, *, data: bool) -> Path:
    """造一个最小说法的程序：plugin.json + backend.py + ui/index.html（+ data/）。"""
    (pdir / "ui").mkdir(parents=True, exist_ok=True)
    (pdir / "plugin.json").write_text(
        json.dumps({"title": title, "ui": "ui/index.html"}, ensure_ascii=False), encoding="utf-8")
    (pdir / "backend.py").write_text(BACKEND, encoding="utf-8")
    (pdir / "ui" / "index.html").write_text(UI_HTML, encoding="utf-8")
    if data:
        (pdir / "data").mkdir(exist_ok=True)
        (pdir / "data" / "store.json").write_text('{"n": 7}', encoding="utf-8")
    return pdir


class _Hub:
    """最小替身：门需要的只是「有个池子」这个形状。

    不连 aaas（这一份验的是包，不是道）。但**必须有 `ensure`** —— 页面一打开就会
    对一个程序 POST 一次 `/link`，`hub=None` 会让那条路由抛 AttributeError，
    而那段 traceback 会躺在探针输出里，看起来像产品坏了。
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        self.url = ""

    def ensure(self, pid: str) -> dict:
        return {"pid": pid, "status": "offline"}

    def status(self) -> dict:
        return {}

    def sessions(self, pid: str, limit: int = 50) -> dict:
        return {"status": "missing"}

    def send(self, pid: str, text: str, images=None) -> str:
        return "sent"

    def cancel(self, pid: str) -> str:
        return "noop"

    def rebind(self, cfg: dict) -> list[str]:
        return []


def start_server(tmp: Path):
    """真 runtime + 真门。工作台在临时目录里，**不碰**仓库里那份。"""
    from agentworkspace import kernel
    from agentworkspace.server import serve

    ws = tmp / "workspace"
    ws.mkdir(parents=True, exist_ok=True)
    make_plugin(ws / "plugins" / "pomodoro", "番茄钟", data=True)
    rt = kernel.Runtime(ws)
    rt.scan()
    httpd = serve(rt, _Hub(ws), ROOT / "agentworkspace" / "web", port=0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}", ws


def state_of(base: str) -> dict:
    with urlopen(base + "/api/state", timeout=10) as r:
        return json.loads(r.read())


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="aw-e2e-pkg-"))
    httpd, base, ws = start_server(tmp)
    print(f"真服务：{base}\n真工作台：{ws}\n")

    # 造一枚**待导入**的包（pid=demo，带数据），放在磁盘上，等下由「真选文件」选它
    from agentworkspace import pluginpkg

    demo = make_plugin(tmp / "src" / "plugins" / "demo", "样例程序", data=True)
    demo_pkg = tmp / "demo.awp"
    demo_pkg.write_bytes(pluginpkg.build(demo, include_data=True))
    # 再造一枚**撞名**的包（pid=pomodoro）：门要证明「撞名不覆盖」在界面上也成立
    clash = make_plugin(tmp / "src" / "plugins" / "pomodoro", "冒名番茄钟", data=False)
    clash_pkg = tmp / "clash.awp"
    clash_pkg.write_bytes(pluginpkg.build(clash))

    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:                         # pragma: no cover
        print(f"[跳过] 没装 playwright：{exc}")
        httpd.shutdown()
        shutil.rmtree(tmp, ignore_errors=True)
        return 0

    rc = 0
    with sync_playwright() as p:
        try:
            br = p.chromium.launch(headless=True, channel="chrome")
        except Exception as exc:                     # pragma: no cover
            print(f"[跳过] 这台机器起不了真 Chrome：{type(exc).__name__}: {exc}")
            httpd.shutdown()
            shutil.rmtree(tmp, ignore_errors=True)
            return 0
        print(f"真浏览器：{br.version}")
        ctx = br.new_context(viewport={"width": 1180, "height": 800},
                             accept_downloads=True, color_scheme="dark")
        page = ctx.new_page()
        errs: list[str] = []
        page.on("pageerror", lambda e: errs.append(str(e)))
        page.goto(base, wait_until="domcontentloaded")
        page.wait_for_selector("#setbtn", timeout=15000)

        # ---- 打开设置面板：插件清单里只有真插件（内置的小助理不出现）
        page.click("#setbtn")
        page.wait_for_selector("#mask.on")
        opts = page.eval_on_selector_all("#exppid option", "els => els.map(e => e.value)")
        check("设置面板里的清单只有真插件（小助理 builtin，导不出）",
              opts == ["pomodoro"], str(opts))
        check("「连同数据」默认**不勾**", page.is_checked("#expdata") is False)
        page.wait_for_timeout(400)               # 等 dry=1 的预览回来
        prev = page.inner_text("#expprev")
        check("导出前先报「会装进什么」（带几个文件 / 多大）",
              "个文件" in prev and "不含数据" in prev, prev)

        # ---- 真点一次导出：判的是**落下来的那个文件**
        with page.expect_download(timeout=20000) as info:
            page.click("#expbtn")
        d1 = info.value
        path1 = Path(d1.path())
        check("点「导出」真的落下一个文件（不是把 zip 当一页打开）",
              path1.is_file() and d1.suggested_filename.endswith(".awp"),
              f"{d1.suggested_filename}")
        names1 = zipfile.ZipFile(path1).namelist()
        check("落下来的包里有程序本体", {"plugin/plugin.json", "plugin/backend.py"} <= set(names1),
              str(names1))
        check("**没勾数据就没有 data/**（默认不在包里，哪怕工作台里明明有一份）",
              "plugin/data/store.json" not in names1, str(names1))

        # ---- 勾上再导一次：这一条是上一条的对照
        page.check("#expdata")
        page.wait_for_timeout(400)
        prev2 = page.inner_text("#expprev")
        check("勾上之后预览改口（「其中数据 N 个」）", "其中数据" in prev2, prev2)
        with page.expect_download(timeout=20000) as info2:
            page.click("#expbtn")
        d2 = info2.value
        names2 = zipfile.ZipFile(Path(d2.path())).namelist()
        check("勾了数据，包里就有 data/store.json",
              "plugin/data/store.json" in names2, str(names2))

        # ---- 真选一个文件导入：看一眼 → 导入 → 左边栏里多了它
        # 查的是**看得见**（is_visible），不是 `element.hidden`：属性翻不翻是 JS 的事，
        # 「人眼前有没有这一格」是 CSS 的事，而这两件事上一版正好相反（见补丁注释）。
        check("还没选文件时，「装成哪个程序」不该出现在眼前",
              page.is_visible("#impaswrap") is False)
        page.set_input_files("#impfile", str(demo_pkg))
        page.wait_for_function(
            "document.querySelector('#impprev').textContent.trim().length > 0", timeout=15000)
        page.wait_for_function("!document.querySelector('#impaswrap').hidden", timeout=15000)
        check("选完文件之后它才出现（在这一步之前人是没什么可填的）",
              page.is_visible("#impaswrap") is True)
        first = page.inner_text("#impprev")
        check("选完文件先报包里是什么（标题 / 文件数 / 带没带数据）",
              "样例程序" in first and "个文件" in first and "含数据" in first, first)
        check("「导入为」预填了包里的 pid", page.input_value("#impas") == "demo",
              page.input_value("#impas"))
        page.click("#impbtn")
        page.wait_for_function(
            "document.querySelector('#impprev').textContent.includes('已导入')", timeout=25000)
        msg = page.inner_text("#impprev")
        check("点「导入」之后有一句人话（不是「操作成功」）",
              "已导入" in msg and "个文件" in msg, msg)
        check("导入之后左边栏里多了一个程序（读真状态，不是页面自己猜）",
              [p["id"] for p in state_of(base)["programs"]].count("demo") == 1,
              str([p["id"] for p in state_of(base)["programs"]]))
        check("盘上真的有这个程序，数据也跟着（包里带了）",
              (ws / "plugins" / "demo" / "data" / "store.json").is_file()
              and (ws / "plugins" / "demo" / "backend.py").is_file())

        # ---- 撞名：界面先说破，拒绝时也不改原程序
        before = (ws / "plugins" / "pomodoro" / "backend.py").read_bytes()
        page.set_input_files("#impfile", str(clash_pkg))
        page.wait_for_function(
            "document.querySelector('#impprev').textContent.includes('同名')", timeout=15000)
        clash_msg = page.inner_text("#impprev")
        check("撞名在「看一眼」这一步就说破，并预填一个空着的名字",
              "同名" in clash_msg and page.input_value("#impas") == "pomodoro_2",
              f"{clash_msg} / {page.input_value('#impas')}")
        page.fill("#impas", "pomodoro")           # 人**硬要**用这个名字
        page.click("#impbtn")
        page.wait_for_function(
            "document.querySelector('#impprev').textContent.includes('已经有一个叫')", timeout=20000)
        deny = page.inner_text("#impprev")
        check("人硬要用撞名的 pid → 拒绝，且说清怎么办", "不覆盖" in deny, deny)
        check("被拒之后原程序一个字节都没动",
              (ws / "plugins" / "pomodoro" / "backend.py").read_bytes() == before)

        # ---- 面板还是那个面板：关得掉，外观没被这几下弄乱
        page.keyboard.press("Escape")
        page.wait_for_timeout(200)
        check("设置面板还能用 Esc 关掉（插件那两块没把它弄坏）",
              page.eval_on_selector("#mask", "e => e.classList.contains('on')") is False)

        page.screenshot(path=str(tmp / "settings.png"))
        print(f"\n截图：{tmp / 'settings.png'}")
        check("整场没有 JS 报错（页面上一个异常都没冒出来）", not errs, str(errs[:3]))
        ctx.close()
        br.close()
        rc = 0 if fail == 0 else 1

    httpd.shutdown()
    shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n{'全绿' if fail == 0 else '有失败'}：{ok} 项过 / {fail} 项不过")
    return rc


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
