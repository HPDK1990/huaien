"""huaien/butler.py —— 网页/手机端用的那一份。

重点: 缓存必须真的生效 (用户打开手机端要秒回, 重算要 3-10 秒)。
"""
import importlib.util
import os
import sys
import time

PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJ)


def _load():
    spec = importlib.util.spec_from_file_location(
        "huaien_butler", os.path.join(PROJ, "huaien", "butler.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_module_exposes_impl_functions():
    """逻辑真身只有一份 —— web.py/app.py/CLI 都用同一个。"""
    mod = _load()
    for name in ("load_policy", "assess", "build_panel", "render_text", "panel"):
        assert callable(getattr(mod, name)), name


def test_panel_caches_between_calls(monkeypatch):
    """TTL 内第二次不重算 —— 重算要拉 kanban 子进程, 3-10 秒。"""
    mod = _load()
    calls = []

    def fake_assess(rules):
        calls.append(1)
        return []

    monkeypatch.setattr(mod, "assess", fake_assess)
    monkeypatch.setattr(mod, "_cache", {"at": 0.0, "data": None})
    mod.panel()
    mod.panel()
    assert len(calls) == 1, "缓存没生效"


def test_panel_force_bypasses_cache(monkeypatch):
    """force=True 必须跳过缓存 (cron 主动汇报要拿最新的)。"""
    mod = _load()
    calls = []

    def fake_assess(rules):
        calls.append(1)
        return []

    monkeypatch.setattr(mod, "assess", fake_assess)
    monkeypatch.setattr(mod, "_cache", {"at": 0.0, "data": None})
    mod.panel()
    mod.panel(force=True)
    assert len(calls) == 2, "force 没跳过缓存"


def test_cache_expires(monkeypatch):
    """超过 TTL 就重算 —— 状态不能永远停在旧值。"""
    mod = _load()
    calls = []

    def fake_assess(rules):
        calls.append(1)
        return []

    monkeypatch.setattr(mod, "assess", fake_assess)
    monkeypatch.setattr(mod, "_cache", {"at": 0.0, "data": None})
    mod.panel()
    monkeypatch.setitem(mod._cache, "at", time.time() - mod._TTL - 1)
    mod.panel()
    assert len(calls) == 2, "过期了还返回旧值"


def test_warmup_never_raises(monkeypatch):
    """预热失败不该拖垮整个服务。"""
    mod = _load()

    def boom(*a, **k):
        raise RuntimeError("炸了")

    monkeypatch.setattr(mod, "panel", boom)
    mod.warmup()      # 不抛就算过
