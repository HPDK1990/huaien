#!/usr/bin/env python3
"""Weixin channel guardian for the huaien profile.

Lives in the huaien repo so it is version-controlled and auditable.
The copy under the Hermes home is the installed one; this file is the
source of truth for what that script does.

Checks three layers, each independently:
  1. gateway process alive (PID + command line must both look like Hermes)
  2. gateway heartbeat fresh (state/gateway.heartbeat mtime)
  3. weixin poll channel healthy (adapter logs must NOT be dominated by
     'poll error' / 'session timeout' in the recent window)

Recovery is deliberately ONE-SIDED. The Hermes_Gateway task runs
"gateway run --replace", which REPLACES a live gateway: every turn running
in it is fenced to status='unknown' and that session's FIFO stalls forever.
A kill-test proved this by taking down two unrelated sessions.

So this guardian may only ever act on a gateway that is ALREADY dead.
It never replaces a running gateway, and it never kills anything.

  - gateway process missing / heartbeat stale -> start the vendor task
    (nothing is running, so nothing can be fenced)
  - weixin poll broken but gateway alive     -> REPORT ONLY, no restart
    (the adapter's own backoff usually recovers; the two failures that need
    a human are -14 session timeout and send ret=-2, both fixed by the user
    sending ONE message to the bot)
  - ollama down (fallback target)            -> REPORT ONLY

Exit codes:
  0  healthy
  1  unhealthy but nothing to do (recovery attempted or not possible)
  2  configuration error (paths missing, etc.)

Designed to be run by cron every N minutes. It is idempotent and read-only
except for the explicit recovery actions above.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

_IS_WINDOWS = os.name == "nt"
# Windows only: spawn a console child WITHOUT giving it its own console window.
# 0 on POSIX (the flag does not exist there).
_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if _IS_WINDOWS else 0


def _hidden() -> dict:
    """kwargs for a subprocess spawn that must not flash a console window.

    WHY THIS EXISTS (do not remove):
    The scheduled task `Hermes_WeixinGuard` runs this script with pythonw.exe,
    which has NO console of its own. A console program spawned from a
    console-less parent gets a BRAND NEW console window -- Windows has no
    parent console to inherit. Every spawn below therefore had to flash a
    blue console box on the desktop every 5 minutes.

    The fix that was tried first (python.exe -> pythonw.exe) only hid the
    OUTER Python window; the INNER powershell children still opened their
    own windows, and switching to pythonw actually made it worse: with
    python.exe the children inherited the parent's console, with pythonw
    there was nothing to inherit and each child allocated a fresh visible
    console.

    NOTE: this flag only applies to the DIRECT child. A child that spawns
    its own console grandchildren needs the flag at ITS call site too --
    that is why `bridge.py` and `app.py` carry it as well.
    """
    return {"creationflags": _CREATE_NO_WINDOW}


HOME = os.environ.get("HERMES_HOME") or str(
    Path.home() / "AppData" / "Local" / "hermes"
)
PROFILE = os.path.join(HOME, "profiles", "huaien")

HEARTBEAT = os.path.join(HOME, "state", "gateway.heartbeat")
GATEWAY_STATE = os.path.join(HOME, "gateway_state.json")
# gateway.lock is the authoritative owner record: gateway_state.json can be
# stale or rewritten by a dying process, while the lock carries the live pid.
GATEWAY_LOCK = os.path.join(HOME, "gateway.lock")
GATEWAY_LOG = os.path.join(PROFILE, "logs", "gateway.log")
GUARD_LOG = os.path.join(HOME, "logs", "weixin-guard.log")

# Thresholds
HEARTBEAT_MAX_AGE_S = 180      # gateway heartbeat older than this = stale
LOG_WINDOW_BYTES = 300_000    # how much of the tail log to inspect
POLL_WINDOW_MINUTES = 15      # only poll errors from the last N minutes count
POLL_ERROR_LIMIT = 5          # more poll errors than this in the window = broken
OLLAMA_URL = "http://localhost:11434/api/tags"
OLLAMA_MAX_AGE_S = 300        # ollama unreachable for this long = down

# Fatal patterns that mean the weixin poll channel is broken.
POLL_BREAK_PATTERNS = (
    re.compile(r"poll error", re.I),
    re.compile(r"session timeout", re.I),
    re.compile(r"getaddrinfo failed", re.I),
    re.compile(r"WinError 121", re.I),
)

# Benign patterns that should not count as a break even if they match a
# fatal substring (e.g. a log line quoting a previous error).
IGNORE_LINES = re.compile(r"recover|restored|back online|after retry", re.I)


def log(msg: str) -> None:
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line)
    try:
        os.makedirs(os.path.dirname(GUARD_LOG), exist_ok=True)
        with open(GUARD_LOG, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def read_json(path: str):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _is_gateway_cmdline(cmdline: str) -> bool:
    """True when the command line is the real gateway entrypoint.

    The gateway's own command line ends with the literal token sequence
    "gateway run --replace". The ticket-helper child process and any
    PowerShell command that merely mentions "gateway run" must not match,
    or a dead gateway would look alive.
    """
    low = (cmdline or "").lower()
    if "hermes_cli" not in low and "hermes_bootstrap" not in low:
        return False
    return re.search(r"gateway\s+run(?:\s|$)", low) is not None


def scan_gateway_pids() -> list[int]:
    """Every live process whose command line is the gateway entrypoint.

    Scans the process table instead of trusting the lock file: the lock can
    name a pid that already died while a replacement is running, which would
    make the guardian start a second gateway.
    """
    ps = (
        "Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | "
        "Where-Object { $_.CommandLine -match 'hermes_cli|hermes_bootstrap' -and "
        "$_.CommandLine -match 'gateway\\s+run' } | "
        "Select-Object -ExpandProperty ProcessId"
    )
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True, timeout=45, **_hidden(),
        )
    except (OSError, subprocess.SubprocessError):
        return []
    out = (r.stdout or b"").decode("utf-8", errors="replace")
    pids = []
    for tok in out.split():
        if tok.isdigit():
            pids.append(int(tok))
    return pids


def gateway_pid() -> int:
    """Live gateway pid.

    Scan the process table first (a running gateway is the truth), then fall
    back to the recorded pid in gateway.lock / gateway_state.json.
    """
    live = scan_gateway_pids()
    if live:
        return live[0]
    for path in (GATEWAY_LOCK, GATEWAY_STATE):
        st = read_json(path)
        if not st:
            continue
        try:
            pid = int(st.get("pid") or 0)
        except (TypeError, ValueError):
            continue
        if pid > 0:
            return pid
    return 0


def pid_alive(pid: int) -> bool:
    """True only when the PID exists AND its command line looks like Hermes.

    Uses PowerShell/CIM rather than wmic: wmic emits GBK-encoded Chinese
    error text on this box, which raises UnicodeDecodeError under
    text=True and made this probe report a live gateway as dead.
    """
    if pid <= 0:
        return False
    ps = (
        f"$p = Get-CimInstance Win32_Process -Filter 'ProcessId={pid}' -ErrorAction SilentlyContinue; "
        "if ($p) { $p.CommandLine }"
    )
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True, timeout=30, **_hidden(),
        )
    except (OSError, subprocess.SubprocessError):
        return False
    # Decode leniently: the command line itself is ASCII, but a stray error
    # message may not be.
    out = (r.stdout or b"").decode("utf-8", errors="replace")
    if not out.strip():
        return False
    return _is_gateway_cmdline(out)


def heartbeat_age_s():
    try:
        return time.time() - os.path.getmtime(HEARTBEAT)
    except OSError:
        return None


def ollama_ok() -> bool:
    try:
        import urllib.request
        with urllib.request.urlopen(OLLAMA_URL, timeout=8) as r:
            return r.status == 200
    except Exception:
        return False


TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")


def poll_channel_broken() -> tuple[bool, int]:
    """Return (broken, poll_error_count) from RECENT gateway-log lines.

    Both a byte window and a time window matter. A byte window alone counts
    days-old errors (a DNS outage two days ago still sits in the log) and
    restarts a healthy gateway; the fix is to only count failures from the
    last POLL_WINDOW_MINUTES.
    """
    try:
        size = os.path.getsize(GATEWAY_LOG)
        with open(GATEWAY_LOG, "rb") as fh:
            if size > LOG_WINDOW_BYTES:
                fh.seek(size - LOG_WINDOW_BYTES)
            fh.readline()  # drop partial first line
            tail = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return False, 0

    cutoff = time.time() - POLL_WINDOW_MINUTES * 60
    hits = 0
    for line in tail.splitlines():
        if IGNORE_LINES.search(line):
            continue
        if not any(p.search(line) for p in POLL_BREAK_PATTERNS):
            continue
        m = TS_RE.match(line)
        if m:
            try:
                ts = time.mktime(time.strptime(m.group(1), "%Y-%m-%d %H:%M:%S"))
            except ValueError:
                ts = None
            if ts is not None and ts < cutoff:
                continue  # too old to count
        hits += 1
    return hits > POLL_ERROR_LIMIT, hits


HERMES_CLI = None


def _hermes_cli() -> str:
    """Locate the hermes CLI executable for the current install."""
    global HERMES_CLI
    if HERMES_CLI:
        return HERMES_CLI
    try:
        r = subprocess.run(["hermes", "--version"], capture_output=True,
                           timeout=30, **_hidden())
        if r.returncode == 0:
            HERMES_CLI = "hermes"
            return HERMES_CLI
    except (OSError, subprocess.SubprocessError):
        pass
    return ""


def recover_gateway() -> bool:
    """Bring the gateway back using the vendor's GRACEFUL restart path.

    Order matters. The vendor's gateway restart goes through the planned-stop
    marker: the gateway refuses new turns, drains the turns already running,
    then exits and the supervisor relaunches it. A turn that is mid-flight
    finishes normally instead of being fenced to status='unknown' (a fenced
    turn stalls that session's FIFO forever).

    Starting the Hermes_Gateway scheduled task runs "gateway run --replace",
    which REPLACES a live gateway without draining. That is the path that
    fenced two unrelated sessions, so it is only the last-resort fallback.
    """
    cli = _hermes_cli()
    if cli:
        try:
            r = subprocess.run(
                [cli, "gateway", "restart"],
                capture_output=True, timeout=180,
                env={**os.environ, "HERMES_HOME": HOME},
                **_hidden(),
            )
            out = ((r.stdout or b"").decode("utf-8", errors="replace") +
                   (r.stderr or b"").decode("utf-8", errors="replace")).strip()
            if r.returncode == 0:
                log("graceful gateway restart succeeded (drain, then supervisor relaunch)")
                return True
            log(f"graceful restart rc={r.returncode}: {out[-300:]}")
        except (OSError, subprocess.SubprocessError) as exc:
            log(f"graceful restart failed to run: {exc}")

    for cmd in (
        ["schtasks", "/Run", "/TN", "Hermes_Gateway"],
        ["powershell", "-NoProfile", "-Command", "Start-ScheduledTask -TaskName 'Hermes_Gateway'"],
    ):
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=60, **_hidden())
            if r.returncode == 0:
                log(f"FALLBACK: started Hermes_Gateway task via {cmd[0]} "
                    "(hard replace, no drain)")
                return True
        except (OSError, subprocess.SubprocessError):
            continue
    log("could not recover the gateway")
    return False


def main() -> int:
    if not os.path.isdir(HOME):
        log(f"config error: HERMES_HOME not found: {HOME}")
        return 2

    pid = gateway_pid()
    alive = pid_alive(pid)
    age = heartbeat_age_s()
    broken, poll_hits = poll_channel_broken()
    ollama = ollama_ok()

    log(
        f"probe: gateway_pid={pid} alive={alive} "
        f"heartbeat_age={age if age is None else round(age)}s "
        f"poll_errors={poll_hits} broken={broken} ollama={ollama}"
    )

    problems = []
    if not alive:
        problems.append("gateway process down")
    elif age is not None and age > HEARTBEAT_MAX_AGE_S:
        problems.append(f"gateway heartbeat stale ({round(age)}s)")
    if broken:
        problems.append(f"weixin poll channel broken ({poll_hits} errors in window)")
    if not ollama:
        problems.append("ollama fallback target unreachable")

    if not problems:
        log("healthy: nothing to do")
        return 0

    log("unhealthy: " + "; ".join(problems))

    # Only a DEAD gateway may be replaced. A live gateway is never touched:
    # "gateway run --replace" fences every in-flight turn, and a fenced turn
    # permanently stalls that session.
    gateway_dead = (not alive) or (age is not None and age > HEARTBEAT_MAX_AGE_S)
    if gateway_dead:
        recover_gateway()
        time.sleep(20)
        pid2 = gateway_pid()
        age2 = heartbeat_age_s()
        log(
            f"after recovery: gateway_pid={pid2} alive={pid_alive(pid2)} "
            f"heartbeat_age={age2 if age2 is None else round(age2)}s"
        )
    else:
        log("gateway is alive -> NOT restarting it (a live gateway must never be replaced)")
        if broken:
            log(
                "weixin poll channel unhealthy. Usually the adapter's own backoff "
                "recovers. If it persists, human-side fixes: (a) send ONE message to "
                "the bot to refresh the iLink session, (b) check reachability of "
                "ilinkai.weixin.qq.com:443."
            )
        if not ollama:
            log("ollama fallback target unreachable - start the ollama server manually")

    return 1


if __name__ == "__main__":
    sys.exit(main())
