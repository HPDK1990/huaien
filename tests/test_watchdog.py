"""怀恩 — 看门狗单元测试

跑: python -m pytest tests/ -q   或   python tests/test_watchdog.py

重点覆盖 2026-10-30 回归看门狗发现的真 bug:
看门狗的前台探针**自己匹配自己**, 导致前台死了也永远报 alive。

测法: 把探针的过滤条件当 PowerShell scriptblock, 喂**假进程**进去,
用真正的 PowerShell 语义判定 —— 不在 Python 里复刻一遍正则, 免得两边跑偏。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJ)
sys.path.insert(0, os.path.join(PROJ, "scripts"))

import watchdog  # noqa: E402

# 当年的坏探针: 只按 'huaien.app' 匹配, 探测命令自己也会被算进去。
OLD_FILTER_PS = "$_.CommandLine -match 'huaien.app'"

# 假进程样本。probe_self 是关键 —— 它模拟"探测命令自己的命令行"。
FAKE_PROCS = [
    {"Name": "python.exe",
     "CommandLine": r"%LOCALAPPDATA%\AppData\Local\hermes\hermes-agent\venv"
                    r"\Scripts\python.exe -m huaien.app"},
    {"Name": "bash.exe",
     "CommandLine": "bash.exe -lic \"cd /e/Hermes-HuaiEn && "
                    "python -m huaien.app > logs/frontend.log\""},
    {"Name": "powershell.exe",
     "CommandLine": "powershell -NoProfile -Command \"" + watchdog.FRONTEND_FILTER_PS + "\""},
    {"Name": "python.exe",
     "CommandLine": r"C:\...\python.exe C:\...\scratch\probe_huaien_proc.py"},
    {"Name": "python.exe", "CommandLine": r"C:\...\python.exe -m other.module"},
]


def _count(filter_ps: str, procs=None) -> int:
    """把 filter_ps 当 scriptblock, 对 procs 求命中数 (真 PowerShell 语义)。"""
    procs = FAKE_PROCS if procs is None else procs
    ps = (
        "$data = @'\n" + json.dumps(procs) + "\n'@ | ConvertFrom-Json; "
        "$sb = [scriptblock]::Create('" + filter_ps.replace("'", "''") + "'); "
        "@($data | Where-Object $sb).Count"
    )
    r = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=120)
    if r.returncode != 0:
        raise AssertionError(f"PowerShell 执行失败: {r.stderr.strip()[:400]}")
    return int((r.stdout or "0").strip())


def test_probe_matches_only_real_frontend():
    """五条假进程里, 只应命中真正的前台那一条。"""
    assert _count(watchdog.FRONTEND_FILTER_PS) == 1, \
        "探针只该命中真正的 '-m huaien.app' python 进程"


def test_probe_excludes_itself():
    """回归核心: 探针不能把自己算成前台。

    探针自身的命令行里就含过滤条件文本, 含 'huaien.app' —— 旧写法会自匹配。
    """
    probe_self = [{"Name": "powershell.exe",
                   "CommandLine": "powershell -NoProfile -Command \""
                                  + watchdog.FRONTEND_FILTER_PS + "\""}]
    assert _count(watchdog.FRONTEND_FILTER_PS, probe_self) == 0, \
        "探针又把自己算进去了 — 会重演'前台死了也报 alive'的 bug"


def test_probe_excludes_bash_wrapper():
    """bash 包装器命令行里也有 '-m huaien.app', 但它不是前台进程。"""
    wrapper = [{"Name": "bash.exe",
                "CommandLine": "bash.exe -lic \"cd /e/Hermes-HuaiEn && "
                               "python -m huaien.app > logs/frontend.log\""}]
    assert _count(watchdog.FRONTEND_FILTER_PS, wrapper) == 0, \
        "bash 包装器不算前台进程"


def test_probe_excludes_other_python():
    """别的 python 进程 (探针自己 / 别的模块) 不算前台。"""
    others = [{"Name": "python.exe",
               "CommandLine": r"C:\...\python.exe C:\...\scratch\probe.py"},
              {"Name": "python.exe", "CommandLine": r"C:\...\python.exe -m other.module"}]
    assert _count(watchdog.FRONTEND_FILTER_PS, others) == 0, \
        "不相关的 python 进程被误判成前台"


def test_old_buggy_probe_would_self_match():
    """把当年的坏探针拿出来对照 —— 它确实会自匹配 (这就是 bug 的根)。

    这条是"看门狗自己的看门狗": 证明上面的守卫不是空转, 而是真的修掉了东西。
    """
    probe_self = [{"Name": "powershell.exe",
                   "CommandLine": "powershell -NoProfile -Command \""
                                  + OLD_FILTER_PS + "\""}]
    assert _count(OLD_FILTER_PS, probe_self) == 1, \
        "旧探针应自匹配 —— 若不匹配, 说明这个对照用例已失去意义"
    assert _count(watchdog.FRONTEND_FILTER_PS, probe_self) == 0, \
        "当前探针仍会自匹配 — bug 没修干净"


def test_probe_contains_all_three_guards():
    """三条判据都必须在探针字符串里 —— 防止以后有人精简掉。"""
    ps = watchdog.FRONTEND_FILTER_PS
    assert "python(w)?" in ps, "缺 python 进程名过滤"
    assert "-m" in ps and "huaien" in ps, "缺 '-m huaien.app' 模块调用过滤"
    assert "Get-CimInstance" in ps and "-notmatch" in ps, "缺探针自身排除"


def test_probe_ps_uses_the_filter():
    """实际下发的探针命令必须真的用上这个过滤条件。"""
    assert watchdog.FRONTEND_FILTER_PS in watchdog.FRONTEND_PROBE_PS


def test_frontend_alive_returns_bool():
    """live 探针必须返回真正的 bool, 不能抛异常或返回字符串。"""
    r = watchdog.frontend_alive()
    assert isinstance(r, bool), f"frontend_alive() 返回了 {type(r)}"


def test_gateway_alive_returns_bool():
    r = watchdog.gateway_alive()
    assert isinstance(r, bool), f"gateway_alive() 返回了 {type(r)}"


def test_check_once_state_shape():
    """check_once 返回的 state 必须三个键都在 (gateway/frontend/memory)。"""
    st = watchdog.check_once(fix=False)
    assert set(st.keys()) == {"gateway", "frontend", "memory", "web"}, f"state 键不对: {st.keys()}"
    assert isinstance(st["gateway"], bool)
    assert isinstance(st["frontend"], bool)
    for k, v in st.items():
        assert isinstance(v, bool), f"{k} 不是 bool: {type(v)}"


def test_failed_result_class():
    """_Failed 替身必须有 stdout/stderr/returncode, 免得探测失败时崩掉。"""
    f = watchdog._Failed("boom")
    assert f.stdout == "" and f.stderr == "boom" and f.returncode == -1


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"  [PASS] {fn.__name__}")
        except Exception as e:
            failed += 1
            print(f"  [FAIL] {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n  {len(fns)-failed}/{len(fns)} 通过")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run_all())
