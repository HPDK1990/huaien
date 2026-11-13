"""怀恩 — 前台对话桥

职责: 把「一句话」送进 Hermes 的怀恩 profile, 把流式回复吐回来。

设计要点:
  - 用 `hermes -p huaien chat` 而不是自己实现协议 —— CLI 是 Hermes 最稳定的接口面
  - `--format stream-json` 逐字流式返回 -> 前台可以做打字机效果
  - `--resume <sid>` 维持多轮上下文 (sid 来自上一轮的 result 事件)
  - 全程后台, 不向前台暴露任何工具调用/日志/任务卡

被调用方 (前台 GUI) 需要提供:
    on_start()            -> 开始生成
    on_text(str)          -> 增量文本
    on_done(text, sid)    -> 完成, text 是完整回复, sid 供下一轮续接
    on_error(str)         -> 出错
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

HERMES_HOME = os.environ.get("HERMES_HOME") or os.path.join(
    os.path.expanduser("~"), "AppData", "Local", "hermes")

PROFILE = "huaien"
HERMES_BIN = os.path.join(HERMES_HOME, "bin")

# 产物标记: agent 用 [[FILE:路径]] / [[IMG:路径]] / [[PATH:路径]] 标注产出。
# 前台解析后把标记本身从正文里去掉, 只留一行极简卡片。
#
# 为什么用标记而不是"猜哪行是路径": 自由文本解析会被代码块、散文里的路径、
# 中文标点全部干扰。标记是 agent 一行指令就能稳定遵守的约定。
ARTIFACT_RE = re.compile(r"\[\[(FILE|IMG|PATH):([^\]\r\n]+)\]\]")


@dataclass
class Artifact:
    """agent 产出的一件东西 (文件/图片/目录)。"""
    kind: str        # file | image | path
    path: str
    exists: bool = False
    size: int = 0


def parse_artifacts(text: str) -> tuple[str, list[Artifact]]:
    """把正文里的产物标记剥掉, 返回 (干净正文, 产物列表)。

    标记重复出现只留一份; 路径去掉首尾空格和包裹引号。
    """
    found: list[Artifact] = []
    seen: set[tuple[str, str]] = set()

    def _take(m: re.Match) -> str:
        kind = m.group(1).lower()
        path = m.group(2).strip().strip('"').strip("'")
        key = (kind, path.lower())
        if path and key not in seen:
            seen.add(key)
            art = Artifact(kind=kind, path=path)
            try:
                art.exists = os.path.exists(path)
                art.size = os.path.getsize(path) if art.exists and os.path.isfile(path) else 0
            except OSError:
                pass
            found.append(art)
        return ""

    clean = ARTIFACT_RE.sub(_take, text or "")
    # 标记所在行如果留下空行, 收一收
    clean = re.sub(r"\n{3,}", "\n\n", clean).strip()
    return clean, found


class BridgeError(RuntimeError):
    """对话桥无法工作 (缺命令/缺 profile/超时)。"""


@dataclass
class BridgeConfig:
    profile: str = PROFILE
    timeout: int = 600          # 单轮硬超时 (秒)
    model: str | None = None
    extra_args: list = field(default_factory=list)


def _kill_tree(proc) -> None:
    """杀掉子进程**整棵树**。

    ⚠️ 实测坑: `hermes` 是 shim, 真正干活的是 uv python re-exec 出来的**孙进程**
       (命令行是 `python.exe -c "import os, runpy, site..."`)。
       `proc.kill()` 只杀 shim -> 孙进程活下来继续跑。
       实测后果: 手机上点"汇报进度", 超时后没停, 24 分钟还在读文件,
       还 spawn 了子代理一直不停 —— 正是用户说的"盯一大堆会话"。

    Windows 用 taskkill /T 杀树; 其它平台退回 kill + killpg。
    """
    if proc.poll() is not None:
        return
    if os.name == "nt":
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                           capture_output=True, timeout=60,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    else:
        with contextlib.suppress(OSError, AttributeError):
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(OSError):
            proc.kill()


def log_stderr(text: str) -> None:
    """把 Hermes 的 stderr 落到项目日志 —— 它常常是唯一的失败线索。"""
    try:
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        d = os.path.join(root, "logs")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "bridge_stderr.log"), "a", encoding="utf-8") as f:
            f.write(text.rstrip() + "\n")
    except OSError:
        pass


def _hermes_cmd() -> list:
    """定位 hermes 可执行文件。"""
    exe = shutil.which("hermes")
    if exe:
        return [exe]
    for cand in ("hermes.exe", "hermes"):
        p = os.path.join(HERMES_BIN, cand)
        if os.path.exists(p):
            return [p]
    raise BridgeError(f"找不到 hermes 命令 ( looked in {HERMES_BIN} )")


class ChatBridge:
    """一轮一轮的同步对话桥。每次 ask() 起一个独立进程, 靠 sid 续接上下文。"""

    def __init__(self, cfg: BridgeConfig | None = None):
        self.cfg = cfg or BridgeConfig()
        self.session_id: str | None = None
        self._lock = threading.Lock()
        self._busy = False
        self._proc: subprocess.Popen | None = None   # 当前子进程, 超时时能杀干净

    # ---------- 状态 ----------
    @property
    def busy(self) -> bool:
        return self._busy

    def reset(self) -> None:
        """忘掉上下文, 下一轮重新开始。"""
        self.session_id = None

    def ask_sync(self, message: str, timeout: int | None = None) -> tuple[str, list]:
        """同步问一句, 返回 (正文, 产物列表)。

        给 Web 前台用 —— HTTP 请求必须一次给一个完整答复, 不能流式。
        与 ask() 抢同一把锁, 所以不能同时用。
        """
        out: dict = {}
        done = threading.Event()

        def on_done(text, sid):
            out["text"] = text
            done.set()

        def on_error(msg):
            out["error"] = msg
            done.set()

        def on_artifacts(arts):
            out["arts"] = arts

        self.ask(message, on_done=on_done, on_error=on_error, on_artifacts=on_artifacts)
        limit = timeout if timeout is not None else self.cfg.timeout + 60
        if not done.wait(limit):
            # 超时了就把子进程**整棵树**杀掉, 否则它还在后台跑 ——
            # 用户看到的"一直转圈/一堆会话在跑"就是这么来的。
            if self._proc is not None:
                _kill_tree(self._proc)
            raise BridgeError(f"超时 ({limit}s)")
        if "error" in out:
            raise BridgeError(out["error"])
        return out.get("text", ""), out.get("arts", [])

    # ---------- 主流程 ----------
    def ask(self, message: str,
            on_start: Callable[[], None] | None = None,
            on_text: Callable[[str], None] | None = None,
            on_done: Callable[[str, str | None], None] | None = None,
            on_error: Callable[[str], None] | None = None,
            on_artifacts: Callable[[list], None] | None = None,
            on_elapsed: Callable[[float], None] | None = None) -> None:
        """发一句话。回调在后台线程触发, 调用方需自己切回 UI 线程。

        on_artifacts(list[Artifact])  —— 这一轮 agent 标注的产物
        on_elapsed(float)            —— 这一轮耗时 (秒)
        """
        with self._lock:
            if self._busy:
                if on_error:
                    on_error("还在处理上一条。")
                return
            self._busy = True
        threading.Thread(
            target=self._run,
            args=(message, on_start, on_text, on_done, on_error, on_artifacts, on_elapsed),
            daemon=True,
        ).start()

    def _run(self, message, on_start, on_text, on_done, on_error,
             on_artifacts=None, on_elapsed=None):
        t0 = time.time()
        try:
            if on_start:
                on_start()
            cmd = _hermes_cmd() + ["-p", self.cfg.profile, "chat", "-q", message,
                                    "--format", "stream-json", "--pass-session-id",
                                    "--oneshot"]
            if self.session_id:
                cmd += ["--resume", self.session_id]
            if self.cfg.model:
                cmd += ["-m", self.cfg.model]
            cmd += self.cfg.extra_args

            env = os.environ.copy()
            env["PATH"] = HERMES_BIN + os.pathsep + env.get("PATH", "")
            env.pop("PYTHONPATH", None)
            env.pop("PYTHONHOME", None)

            buf = []
            final_text = ""
            sid = None
            try:
                proc = subprocess.Popen(
                    cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True, encoding="utf-8", errors="replace",
                    env=env, bufsize=1,
                    creationflags=(getattr(subprocess, "CREATE_NO_WINDOW", 0)
                                   if os.name == "nt" else 0),
                )
            except OSError as e:
                raise BridgeError(f"启动 Hermes 失败: {e}") from e
            self._proc = proc

            def pump():
                nonlocal final_text, sid
                for line in proc.stdout:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        j = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    t = j.get("type")
                    if t == "text":
                        piece = j.get("text", "")
                        if piece:
                            buf.append(piece)
                            final_text += piece
                            if on_text:
                                on_text(piece)
                    elif t == "result":
                        sid = j.get("session_id") or sid
                        if j.get("text"):
                            final_text = j["text"]
                        rc = j.get("exit_code")
                        if rc not in (0, None):
                            raise BridgeError(f"Hermes 退出码 {rc}")

            err_thread = threading.Thread(target=pump, daemon=True)
            err_thread.start()
            try:
                proc.wait(timeout=self.cfg.timeout)
            except subprocess.TimeoutExpired:
                _kill_tree(proc)
                raise BridgeError(f"超时 ({self.cfg.timeout}s)") from None
            err_thread.join(timeout=10)

            stderr_txt = ""
            with contextlib.suppress(Exception):
                stderr_txt = (proc.stderr.read() or "")[:800]
            if stderr_txt:
                log_stderr(stderr_txt)

            if sid:
                self.session_id = sid
            else:
                # 没拿到 result 事件 = 这一轮没正常跑完 (被 kill / 异常退出)。
                # 必须把 session_id 清掉: 留着它, 下一轮会 resume 到一个半截会话上,
                # agent 会接着旧任务继续跑, 而不是回答新问题。
                # 实测踩过: 手机上点"汇报进度", 它去读运行日志 + 查端口 + spawn 子代理。
                self.session_id = None
            elapsed = time.time() - t0
            clean, artifacts = parse_artifacts(final_text.strip())
            if artifacts and on_artifacts:
                on_artifacts(artifacts)
            if on_elapsed:
                on_elapsed(elapsed)
            if on_done:
                on_done(clean, sid)
        except Exception as e:
            # 这一轮同样没跑完 (超时/启动失败/协议错误) —— 同样清掉 session_id,
            # 下一轮开新会话, 不要把用户的新问题接到旧任务后面。
            self.session_id = None
            if on_error:
                on_error(str(e))
        finally:
            self._proc = None
            with self._lock:
                self._busy = False


# ---------- 自检 ----------
def selftest() -> int:
    """最小连通性测试: 发一句话, 看能不能拿到回复。"""
    b = ChatBridge()
    out = {}

    def on_text(piece):
        out["stream"] = out.get("stream", "") + piece

    def on_done(text, sid):
        out["text"] = text
        out["sid"] = sid

    def on_error(msg):
        out["error"] = msg

    b.ask("用一句话说: 你好", on_text=on_text, on_done=on_done, on_error=on_error)
    for _ in range(180):
        if "text" in out or "error" in out:
            break
        time.sleep(1)

    if "error" in out:
        print("[FAIL]", out["error"])
        return 1
    print("[ OK ] stream 片段数:", len(out.get("stream", "")))
    print("[ OK ] 完整回复:", out.get("text", "")[:120])
    print("[ OK ] session_id:", (out.get("sid") or "")[:32])
    return 0


if __name__ == "__main__":
    sys.exit(selftest())
