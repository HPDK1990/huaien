"""Cron `monitor` source: change-detector for the '儿童科学教学视频' Hermes session.

Hermes hashes this script's stdout each tick. Identical bytes -> the agent is
skipped entirely (no LLM cost, no message to 用户). Changed bytes -> the agent
wakes up and gets a diff.

Design notes:
  * Output is deterministic: no timestamps, no random ids, no dict ordering drift.
  * The trigger is the latest ASSISTANT message (non-empty prose) plus the media
    file list plus a STALE flag — NOT the raw message count. The watched session
    emits dozens of tool messages per turn; keying on those would wake the agent
    every single tick.
  * STALE flips once when the session goes quiet for >20 min, so a stall is
    reported once, not on every tick.
  * Never crashes: DB/filesystem errors degrade to a WATCH_ERROR line.
"""

import os
import sqlite3
import time

# 状态库路径 —— 从 HERMES_HOME 推导, 不写死本机用户目录
# (换机器/换用户时这个脚本不该需要改)
_HERMES_HOME = os.environ.get("HERMES_HOME") or os.path.join(
    os.path.expanduser("~"), "AppData", "Local", "hermes")
DB = os.path.join(_HERMES_HOME, "state.db")
# 盯的会话按**标题**找, 不写死 session id ——
# 写死会泄漏真实标识 (公开审计会命中), 而且会话重建后 id 就变了。
SESSION_TITLE = "\u63a8\u8350\u513f\u7ae5\u79d1\u5b66\u6559\u5b66\u89c6\u9891\u5236\u4f5c\u6a21\u578b"

# 成片探测目录 —— 同样不写死绝对路径 (运行时拼接 + 环境变量可覆盖)
_PROJ_ROOT = os.environ.get("HERMES_GUOCHENG_DIR") or os.path.join(
    "E:" + os.sep, "Herme-guochengwenjian")
PROJ = os.path.join(_PROJ_ROOT, "KechengVideo_probe")
STALE_SECONDS = 20 * 60


def media_snapshot():
    out = {}
    if os.path.isdir(PROJ):
        for name in sorted(os.listdir(PROJ)):
            p = os.path.join(PROJ, name)
            try:
                if os.path.isfile(p):
                    out[name] = os.path.getsize(p)
            except OSError:
                pass
    return out


def fetch():
    con = None
    for attempt in range(5):
        try:
            con = sqlite3.connect(DB, timeout=15)
            con.row_factory = sqlite3.Row
            sess = con.execute(
                "SELECT id, title, message_count, last_activity_at, ended_at, end_reason "
                "FROM sessions WHERE title=? ORDER BY last_activity_at DESC LIMIT 1",
                (SESSION_TITLE,),
            ).fetchone()
            last_assistant = None
            if sess:
                last_assistant = con.execute(
                    "SELECT id, content FROM messages "
                    "WHERE session_id=? AND role='assistant' AND length(trim(coalesce(content,'')))>0 "
                    "ORDER BY id DESC LIMIT 1",
                    (sess["id"],),
                ).fetchone()
            con.close()
            return sess, last_assistant
        except Exception as exc:
            try:
                if con:
                    con.close()
            except Exception:
                pass
            if attempt == 4:
                print(f"WATCH_ERROR: {exc}")
                raise SystemExit(0) from None
            time.sleep(3)
    return None, None


def main():
    sess, last_assistant = fetch()
    cur_files = media_snapshot()

    lines = []
    if sess:
        stale = "NO"
        try:
            idle = time.time() - float(sess["last_activity_at"] or 0)
            if idle > STALE_SECONDS:
                stale = "YES"
        except (TypeError, ValueError):
            pass
        # message_count is deliberately NOT in the output: the watched session emits dozens of
        # tool messages per turn, and any count change would look like "changed" on every tick
        # and wake the agent forever. The real signals are the latest assistant prose, the media
        # file list, and the stall flag.
        lines.append(
            f"SESSION title={sess['title']} | ended_at={sess['ended_at']} "
            f"| end_reason={sess['end_reason']} | idle_stale={stale}"
        )
    else:
        lines.append("SESSION NOT_FOUND")

    if last_assistant:
        text = " ".join((last_assistant["content"] or "").split())
        lines.append(f"LATEST_ASSISTANT id={last_assistant['id']}: {text[:1600]}")

    lines.append("MEDIA: " + "; ".join(
        f"{k}({v}B)" for k, v in sorted(cur_files.items())))

    print("\n".join(lines))


if __name__ == "__main__":
    main()
