"""给 server.py 接上图片：chat 收图 + 一个只发图的小端点。"""
from pathlib import Path

p = Path(r"D:\projects\claw\open-agents\openagent-workspace\agentworkspace\server.py")
s = p.read_text(encoding="utf-8")
orig = s


def sub(old, new, count=1):
    global s
    assert old in s, f"没命中：{old[:70]!r}"
    s = s.replace(old, new, count)


sub("from . import settings\n", "from . import sessions, settings\n")

# 1) 路由：会话里的一张图。名字形状（16 位 sha + 后缀）由正则钉死 ——
#    它同时是路径分量，形状收紧了，越界就不必再靠 resolve 兜。
sub(
    '''SESSION_ACT_RE = re.compile(
    r"^/api/p/(?P<pid>[a-z][a-z0-9_]*)/session/(?P<act>new|switch|rename|delete)$")
''',
    '''SESSION_ACT_RE = re.compile(
    r"^/api/p/(?P<pid>[a-z][a-z0-9_]*)/session/(?P<act>new|switch|rename|delete)$")
# 一张图。名字就是盘上那个名字（``<16 位 sha>.<后缀>``），形状在这里钉死。
SESSION_IMG_RE = re.compile(
    r"^/api/p/(?P<pid>[a-z][a-z0-9_]*)/session/(?P<sid>s_[0-9a-f]{8})"
    r"/img/(?P<name>[0-9a-f]{16}\\.(?:png|jpg|webp|gif))$")
''',
)

# 2) GET：把图发出去
sub(
    '''        m = PLUGIN_RE.match(path)
        if m:
            pid, rest = m.group("pid"), m.group("rest")''',
    '''        # 会话里的一张图。**只发图，不列目录**：名字必须是那种形状，
        # 而 store.image_path 会再判一次（两处判据同一份，不是两份）。
        m = SESSION_IMG_RE.match(path)
        if m:
            pid, sid, name = m.group("pid"), m.group("sid"), m.group("name")
            if rt.program(pid) is None:
                return self._json({"error": f"没有程序 {pid}"}, 404)
            # 不用 hub.link(create=True)：那会顺手把一个智能体叫起来，
            # 而「看一眼历史里的图」不该有那个副作用。Store 只读盘。
            target = sessions.Store(rt.root).image_path(pid, sid, name)
            if target is None or not target.is_file():
                return self._json({"error": "没有这张图"}, 404)
            return self._file(target)

        m = PLUGIN_RE.match(path)
        if m:
            pid, rest = m.group("pid"), m.group("rest")''',
)

# 3) POST chat：收图；空消息的判据改成「没有字也没有图」
sub(
    '''            text = str((body or {}).get("text", ""))
            if not text.strip():
                return self._json({"error": "空消息"}, 400)
            return self._json({"status": hub.send(pid, text), "pid": pid})''',
    '''            text = str((body or {}).get("text", ""))
            images = (body or {}).get("images") or []
            if not isinstance(images, list):
                return self._json({"error": "images 必须是数组"}, 400)
            # 只有图、没有字是合法的一句话（「这张怎么样」的省略式）。真正的空
            # 消息是**两样都没有** —— 那才拒。
            if not text.strip() and not images:
                return self._json({"error": "空消息"}, 400)
            return self._json({"status": hub.send(pid, text, images=images), "pid": pid})''',
)

assert s != orig
p.write_text(s, encoding="utf-8")
print("ok", len(orig), "->", len(s))
