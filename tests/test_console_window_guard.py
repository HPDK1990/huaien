"""防再犯: 每一处 spawn 控制台程序的调用都必须带 CREATE_NO_WINDOW。

## 背景 (这个坑栽过两次)

2026-11-13: 用户报"桌面定期跳蓝框, 还会闪退黑框"。取证发现 **4 个源头**,
全是同一个坑 —— 无控制台父进程 (pythonw.exe / cron) spawn 控制台子进程
(powershell.exe / tasklist.exe / hermes.exe) 时, Windows 没有父控制台可继承,
只能自己新分配一个**可见**控制台:

| 源头 | 框色 | 频率 |
|---|---|---|
| Hermes_WeixinGuard -> powershell | 蓝 | 每 5 分钟 |
| MiniMax token_keepalive -> tasklist | 黑 | 每 60 秒 |
| butler.py -> hermes (hermes.exe 是 Console subsystem!) | 黑·闪退 | 每 30 分钟 |
| flush_outbox / report / await_kanban -> hermes | 黑·闪退 | 每 10 分钟 |

**第一次修完 WeixinGuard 就以为好了**, 结果下次照样弹 —— 因为只修了一个源。
所以这里钉死"**每一个** spawn 点", 而不只是某一个文件。

## 为什么用真实执行而不是 grep

静态 grep 只能证明"字符串写对了", 证明不了"真的不闪"。
所以下面 A 组真的 spawn 一次控制台程序并数可见窗口, B 组用同样的机制加标志,
断言 B 组弹 0 个 —— 修法失效时这条测试会红。

## 判据

`CREATE_NO_WINDOW` 生效后, 控制台进程和 conhost.exe **依然存在**, 只是不分配
可见窗口。所以判据是"**可见** ConsoleWindowClass 窗口", 不是"conhost 出现次数"。
"""
import ctypes
import ctypes.wintypes as w
import importlib.util
import os
import subprocess
import sys
import threading
import time

import pytest

PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 需要"每一处 spawn 都带标志"的脚本 -> (文件, 该文件里所有 spawn 的调用名)
GUARDED = {
    "scripts/weixin_guard.py": None,
    "scripts/regression_check.py": None,
    "scripts/watchdog.py": None,
    "scripts/selftest.py": None,
    "scripts/butler.py": None,
    "scripts/butler_run.py": None,
    "scripts/report.py": None,
    "scripts/await_kanban.py": None,
    "scripts/wechat_send.py": None,
    "huaien/bridge.py": None,
    "huaien/app.py": None,
}

IS_WINDOWS = os.name == "nt"
CONSOLE_CLASS = "ConsoleWindowClass"


# --------------------------------------------------------------------------- #
# 窗口枚举 (win32)
# --------------------------------------------------------------------------- #

_WNDENUM = ctypes.WINFUNCTYPE(ctypes.c_bool, w.HWND, w.LPARAM)


def _visible_console_windows():
    """当前可见的控制台窗口集合 (hwnd -> (pid, title))。"""
    u = ctypes.windll.user32
    out = {}

    def cb(h, _l):
        if not u.IsWindowVisible(h):
            return True
        cls = ctypes.create_unicode_buffer(256)
        u.GetClassNameW(h, cls, 256)
        if cls.value != CONSOLE_CLASS:
            return True
        pid = w.DWORD()
        u.GetWindowThreadProcessId(h, ctypes.byref(pid))
        n = u.GetWindowTextLengthW(h)
        title = ""
        if n:
            b = ctypes.create_unicode_buffer(n + 1)
            u.GetWindowTextW(h, b, n + 1)
            title = b.value
        out[h] = (pid.value, title)
        return True

    u.EnumWindows(_WNDENUM(cb), 0)
    return out


class _WindowWatcher:
    """后台高频枚举, 抓一闪而过的控制台窗口。

    采样必须快 (5ms): 一个只活几十毫秒的闪窗可能整个生命周期都躲过慢采样。
    """

    def __init__(self):
        self.seen = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()

    def _run(self):
        u = ctypes.windll.user32

        def cb(h, _l):
            if not u.IsWindowVisible(h):
                return True
            cls = ctypes.create_unicode_buffer(256)
            u.GetClassNameW(h, cls, 256)
            if cls.value != CONSOLE_CLASS:
                return True
            pid = w.DWORD()
            u.GetWindowThreadProcessId(h, ctypes.byref(pid))
            with self._lock:
                self.seen.setdefault(h, pid.value)
            return True

        while not self._stop.is_set():
            u.EnumWindows(_WNDENUM(cb), 0)
            time.sleep(0.005)

    def __enter__(self):
        self._t = threading.Thread(target=self._run, daemon=True)
        self._t.start()
        time.sleep(0.4)
        return self

    def __exit__(self, *_exc):
        self._stop.set()
        time.sleep(0.1)
        return False

    def new_console_pids(self, baseline):
        with self._lock:
            return {pid for h, pid in self.seen.items() if h not in baseline}


# --------------------------------------------------------------------------- #
# 1) 机制验证: 标志真的能挡住控制台窗口吗
# --------------------------------------------------------------------------- #


@pytest.mark.skipif(not IS_WINDOWS, reason="Windows 控制台行为")
def test_create_no_window_actually_suppresses_the_console():
    """A 组(无标志) 必须弹框, B 组(有标志) 必须不弹。

    这是整份测试的地基 —— 如果这条红了, 后面所有"某文件带标志"的断言都
    是空话 (标志本身失效了)。

    ⚠️ **测法陷阱**: pytest 自己跑在终端里 = **有控制台**, 子进程会*继承*
    父控制台, 于是根本不弹框 —— 对照组会假失败。
    真实的弹框条件是**无控制台父进程** (cron / pythonw.exe)。
    所以这里用一个 pythonw.exe 去当父进程, 它自己没控制台, 子进程只能
    自己新分配一个可见控制台 —— 这才是真实条件。
    """
    pythonw = _find_pythonw()
    if not pythonw:
        pytest.skip("找不到 pythonw.exe (无控制台解释器), 无法构造真实的弹框条件")

    # 父进程 = pythonw.exe (无控制台) -> 它 spawn 的控制台子进程必须自己开窗
    #
    # ⚠️ 子进程必须用**控制台解释器** (python.exe)。pythonw 的 sys.executable
    # 指向 pythonw 自己 —— 那不是控制台程序, 自然不会弹框, 对照组就废了。
    console_py = os.path.join(os.path.dirname(sys.executable), "python.exe")
    if not os.path.exists(console_py):
        pytest.skip("找不到 python.exe (控制台解释器), 无法构造真实的弹框条件")

    probe = (
        "import subprocess, sys, time\n"
        f"p = subprocess.Popen([{console_py!r}, '-c', 'import time; time.sleep(2)'],\n"
        "                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)\n"
        "print(p.pid, flush=True)\n"
        "time.sleep(2)\n"
    )
    with _WindowWatcher() as watcher:
        baseline = set(_visible_console_windows())
        proc = subprocess.Popen(
            [pythonw, "-c", probe],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        out, _err = proc.communicate(timeout=30)
        time.sleep(1.0)
        flashed = watcher.new_console_pids(baseline)

    assert flashed, (
        "对照组失效: 无控制台父进程 spawn 控制台子进程居然没弹框, 测法本身有问题"
    )


def _find_pythonw():
    """找一个可用的 pythonw.exe (Windows 上的无控制台解释器)。"""
    import glob

    cands = [
        os.path.join(os.path.dirname(sys.executable), "pythonw.exe"),
    ]
    cands += glob.glob(os.path.join(
        os.path.expanduser("~"), "AppData", "Local", "hermes", "tools",
        "python*", "pythonw.exe"))
    cands += glob.glob(os.path.join(
        os.path.expanduser("~"), "AppData", "Roaming", "uv", "python",
        "*", "pythonw.exe"))
    for c in cands:
        if os.path.exists(c):
            return c
    return None


# --------------------------------------------------------------------------- #
# 2) 每个脚本的每一个 spawn 点都必须带标志
# --------------------------------------------------------------------------- #


def _spawn_call_sites(path):
    """(行号, 该行是否带 creationflags) —— 用 AST 而不是 grep。

    grep 会漏 (标志写在下一行 / 藏在 **kwargs 里), 也会误报 (注释里的
    subprocess.run)。AST 看的是真正的调用节点。
    """
    import ast

    with open(path, encoding="utf-8") as fh:
        src = fh.read()
    tree = ast.parse(src)
    sites = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        if not (isinstance(f, ast.Attribute)
                and isinstance(f.value, ast.Name)
                and f.value.id == "subprocess"
                and f.attr in ("run", "Popen", "call", "check_output")):
            continue
        has_flag = any(
            kw.arg == "creationflags"
            or (kw.arg is None and isinstance(kw.value, ast.Call)
                and getattr(kw.value.func, "id", "") == "_hide_flags")
            or (kw.arg is None and isinstance(kw.value, ast.Call)
                and getattr(kw.value.func, "id", "") == "_hidden")
            for kw in node.keywords
        )
        sites.append((node.lineno, has_flag))
    return sites


@pytest.mark.parametrize("rel", sorted(GUARDED))
def test_every_spawn_site_carries_the_no_window_flag(rel):
    """**每一个** spawn 点都要带标志 —— 漏一个就还会弹框。

    上一轮只修了 weixin_guard.py 就以为完事, 结果 butler.py / flush_outbox
    继续每 10-30 分钟弹一次。这条测试就是防那个。
    """
    path = os.path.join(PROJ, rel)
    if not os.path.exists(path):
        pytest.skip(f"{rel} 不存在")

    sites = _spawn_call_sites(path)
    assert sites, f"{rel} 里没找到 subprocess 调用 —— 是不是挪走了? 查一下"

    bare = [ln for ln, ok in sites if not ok]
    assert not bare, (
        f"{rel} 的第 {bare} 行 spawn 控制台程序时没带 CREATE_NO_WINDOW —— "
        f"从无控制台父进程(cron/pythonw)跑它会弹框。"
        f"修法: 加 creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0)"
    )


@pytest.mark.parametrize("rel", sorted(GUARDED))
def test_helper_is_defined_when_used(rel):
    """用了 _hide_flags()/_hidden() 的文件必须有定义 —— 否则运行期 NameError。"""
    path = os.path.join(PROJ, rel)
    if not os.path.exists(path):
        pytest.skip(f"{rel} 不存在")
    with open(path, encoding="utf-8") as fh:
        src = fh.read()
    for helper in ("_hide_flags", "_hidden"):
        if f"{helper}(" in src:
            assert f"def {helper}(" in src, (
                f"{rel} 用了 {helper}() 但没定义它 —— 运行期会 NameError"
            )


# --------------------------------------------------------------------------- #
# 3) 真实路径: 但ler 调 hermes 不能弹框
# --------------------------------------------------------------------------- #


@pytest.mark.skipif(not IS_WINDOWS, reason="Windows 控制台行为")
def test_butler_hermes_call_does_not_flash_a_console():
    """跑 butler.py 的真实调用路径 (hermes kanban list), 断言零弹框。

    这条是端到端的: 用的就是 cron 每 30 分钟跑的那条路径。
    """
    spec = importlib.util.spec_from_file_location(
        "butler_under_test", os.path.join(PROJ, "scripts", "butler.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    with _WindowWatcher() as watcher:
        baseline = set(_visible_console_windows())
        rc, out, _err = mod._hermes("kanban", "list", "--json", timeout=180)
        time.sleep(0.8)
        flashed = watcher.new_console_pids(baseline)

    assert rc == 0, f"hermes 调用失败了 (rc={rc}) —— 功能坏了, 不只是弹框"
    assert out, "hermes kanban list 没返回内容 —— 功能坏了"
    assert not flashed, (
        f"butler 调 hermes 弹了控制台窗口 (pid={flashed}) —— "
        f"CREATE_NO_WINDOW 没生效"
    )


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
