"""怀恩 — 一键自检

依次验三层, 任何一层失败就退出码非零:
  1. 环境   python / tkinter / hermes 命令 / 项目文件
  2. 桥接   真的能跟 Hermes 对话并拿到流式回复
  3. 界面   能起窗口, 能显示, 能收到回复 (截图为证)

用法: python scripts/selftest.py [--skip-ui]
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys

PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJ)

RESULTS = []


def step(name):
    def deco(fn):
        def run(*a, **kw):
            print(f"\n--- {name} ---")
            try:
                ok = fn(*a, **kw)
            except Exception as e:
                print(f"[FAIL] {name}: {type(e).__name__}: {e}")
                ok = False
            RESULTS.append((name, ok))
            print(f"[{' OK ' if ok else 'FAIL'}] {name}")
            return ok
        return run
    return deco


@step("环境")
def s_env():
    import shutil
    ok = True
    ok &= os.path.isdir(os.path.join(PROJ, "huaien"))
    print("  项目目录:", "ok" if os.path.isdir(os.path.join(PROJ, "huaien")) else "缺")
    ok &= os.path.exists(os.path.join(PROJ, "huaien", "app.py"))
    ok &= os.path.exists(os.path.join(PROJ, "huaien", "bridge.py"))
    ok &= os.path.exists(os.path.join(PROJ, "huaien", "theme.py"))
    print("  核心文件:", "ok" if all(os.path.exists(os.path.join(PROJ, "huaien", f))
                                for f in ("app.py", "bridge.py", "theme.py")) else "缺")
    import importlib.util
    if importlib.util.find_spec("tkinter"):
        print("  tkinter: ok")
    else:
        print("  tkinter: 缺")
        ok = False
    h = shutil.which("hermes")
    print("  hermes:", h or "不在 PATH")
    ok &= bool(h)
    return ok


@step("对话桥")
def s_bridge():
    from huaien.bridge import ChatBridge
    b = ChatBridge()
    out = {}

    def on_text(p):
        out["stream"] = out.get("stream", "") + p

    def on_done(t, sid):
        out["text"] = t
        out["sid"] = sid

    def on_error(m):
        out["error"] = m

    b.ask("用一句话说: 你好", on_text=on_text, on_done=on_done, on_error=on_error)
    import time
    for _ in range(200):
        if "text" in out or "error" in out:
            break
        time.sleep(1)
    if "error" in out:
        print("  错误:", out["error"])
        return False
    print("  流式片段:", len(out.get("stream", "")))
    print("  回复:", (out.get("text") or "")[:120])
    print("  session:", (out.get("sid") or "")[:36])
    return bool(out.get("text"))


@step("界面")
def s_ui():
    # ⚠️ 截图落在 logs/ —— docs/ 会被封存脚本复制进版本包 (污染链)
    shot = os.path.join(PROJ, "logs", "shot_reply.png")
    from huaien.runtime import clean_env
    r = subprocess.run([sys.executable, "-m", "huaien.app", "--selftest"],
                       cwd=PROJ, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=300,
                       env=clean_env(),
                       creationflags=(getattr(subprocess, "CREATE_NO_WINDOW", 0)
                                      if os.name == "nt" else 0))
    print(" ", (r.stdout or "").strip()[:400])
    if r.returncode != 0:
        print("  stderr:", (r.stderr or "")[-400:])
        return False
    if not os.path.exists(shot):
        print("  没生成截图")
        return False
    size = os.path.getsize(shot)
    print("  截图:", size, "B")
    # 判据不能只看字节数: 短回复的 PNG 压缩后也可能只有 4 KB。
    # 精确判据 = 数非黑像素占比。纯黑窗口是 0%, 有内容必然 >0.1%。
    try:
        from PIL import Image
        img = Image.open(shot).convert("RGB")
        px = img.getdata()
        total = len(px)
        lit = sum(1 for r, g, b in px if r > 24 or g > 24 or b > 24)
        ratio = lit / total * 100
        print(f"  非黑像素: {lit}/{total} ({ratio:.3f}%)")
        if ratio < 0.1:
            print(f"  非黑像素只有 {ratio:.3f}% —— 窗口是空的 (没渲染出内容)")
            return False
    except ImportError:
        # 没有 PIL 就退回字节数下限 (纯黑 PNG 约 1.9 KB)
        if size < 3000:
            print(f"  截图只有 {size} B —— 很可能窗口是空的 (没渲染出内容)")
            return False
    return True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-ui", action="store_true")
    args = ap.parse_args()

    s_env()
    s_bridge()
    if not args.skip_ui:
        s_ui()

    print("\n" + "=" * 46)
    passed = sum(1 for _, ok in RESULTS if ok)
    for n, ok in RESULTS:
        print(f"  [{'PASS' if ok else 'FAIL'}] {n}")
    print(f"  {passed}/{len(RESULTS)} 通过")
    print("=" * 46)
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
