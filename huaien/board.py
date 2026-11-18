"""怀恩 · 秘书台数据源

把 Hermes 原生看板拆成用户一屏能看完的三段:

    待你处理 (blocked / review / triage)
    进行中   (running / ready / todo / scheduled)
    已完成   (done, 最近 N 条)

外加两块"多 agent 编排"要看的东西:

    graph  —— 依赖边 (task_links), 也就是"谁在等谁"
    agents —— 每个 profile 手上有几件活 (task_runs 的实时快照)

## 为什么直接读 sqlite 而不是调 `hermes kanban list`

`kanban list --json` 不返回依赖关系, 也不返回"哪个 agent 正在跑"。
这两样恰恰是秘书台最该显示的。所以直接读库:

  - 只读模式 (file:...?mode=ro) —— 绝不写
  - 缺表/缺列就降级成空, 不炸 —— Hermes 升级改了 schema 也不会让怀恩前台挂掉

数据源仍然是 Hermes 自己的看板 —— 怀恩不另造一套任务系统。
"""
from __future__ import annotations

import os
import sqlite3
import time

HERMES_HOME = os.environ.get("HERMES_HOME") or os.path.join(
    os.path.expanduser("~"), "AppData", "Local", "hermes")
KANBAN_DB = os.path.join(HERMES_HOME, "kanban.db")

# 需要用户亲自拍板的
NEEDS_YOU = ("blocked", "review", "triage")
# 已经在跑的
IN_FLIGHT = ("running", "ready", "todo", "scheduled")


def _connect() -> sqlite3.Connection | None:
    if not os.path.exists(KANBAN_DB):
        return None
    try:
        con = sqlite3.connect(f"file:{KANBAN_DB}?mode=ro", uri=True, timeout=5)
        con.row_factory = sqlite3.Row
        return con
    except sqlite3.Error:
        return None


def _rows(con: sqlite3.Connection, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
    try:
        return con.execute(sql, args).fetchall()
    except sqlite3.Error:
        return []


def _runs(con: sqlite3.Connection) -> dict[str, dict]:
    """正在跑的 run: task_id -> {profile, status, heartbeat}"""
    out: dict[str, dict] = {}
    for r in _rows(con, "SELECT task_id, profile, status, last_heartbeat_at, "
                          "started_at FROM task_runs"):
        out[r["task_id"]] = {
            "profile": r["profile"],
            "status": r["status"],
            "heartbeat": r["last_heartbeat_at"],
            "started": r["started_at"],
        }
    return out


def _links(con: sqlite3.Connection) -> list[tuple[str, str]]:
    return [(r["parent_id"], r["child_id"])
            for r in _rows(con, "SELECT parent_id, child_id FROM task_links")]


def _layers(nodes: list[str], edges: list[tuple[str, str]]) -> dict[str, int]:
    """拓扑层级: 根=0, 依赖它的下一层=1... 用来把 DAG 按列排开。

    有环也不怕 —— 剩下的节点统一放最后一层。
    """
    indeg = {n: 0 for n in nodes}
    adj: dict[str, list[str]] = {n: [] for n in nodes}
    for p, c in edges:
        if p in indeg and c in indeg:
            adj[p].append(c)
            indeg[c] += 1

    layer: dict[str, int] = {}
    frontier = sorted(n for n in nodes if indeg[n] == 0)
    depth = 0
    while frontier and depth < 64:
        nxt = []
        for n in frontier:
            layer[n] = depth
            for c in adj[n]:
                indeg[c] -= 1
                if indeg[c] == 0:
                    nxt.append(c)
        frontier = sorted(set(nxt))
        depth += 1
    for n in nodes:                      # 环里的
        layer.setdefault(n, depth)
    return layer


def _ts(v) -> int:
    """时间戳归一成 int。

    库里存的是**字符串**秒 ('1793254604')。直接按字符串比较是字典序 ——
    位数不同时 ('999' > '1000') 会排错, 必须转数值。
    """
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def _card(t, runs: dict) -> dict:
    """一行 task -> 秘书台用的卡片。"""
    return {
        "id": t["id"],
        "title": (t["title"] or "").strip(),
        "status": (t["status"] or "").strip(),
        "assignee": (t["assignee"] or "").strip() or "未指派",
        "priority": t["priority"],
        "created_at": t["created_at"],
        "started_at": t["started_at"],
        "completed_at": t["completed_at"],
        "error": (t["last_failure_error"] or "").strip() or None,
        "run": runs.get(t["id"]),
    }


def board(limit_done: int = 12) -> dict:
    """返回秘书台的三段 + 依赖图 + agent 负载。"""
    con = _connect()
    if con is None:
        return {"needs_you": [], "in_flight": [], "done": [], "done_total": 0,
                "agents": [], "graph": {"nodes": [], "edges": [], "layers": {}},
                "total": 0, "available": False}

    try:
        tasks = _rows(con, "SELECT id, title, status, assignee, priority, created_by, "
                           "created_at, started_at, completed_at, last_failure_error "
                           "FROM tasks")
        runs = _runs(con)
        edges = _links(con)

        needs, inflight, done = [], [], []
        for t in tasks:
            card = _card(t, runs)
            st = card["status"]
            if st == "archived":          # 归档的既不算待办也不算总数
                continue
            if st in NEEDS_YOU:
                needs.append(card)
            elif st in IN_FLIGHT:
                inflight.append(card)
            elif st == "done":
                done.append(card)

        # 待你处理: 新的在前; 进行中: running 优先, 其次新的在前; 已完成: 最近完成在前
        needs.sort(key=lambda c: -_ts(c["created_at"]))
        inflight.sort(key=lambda c: (c["status"] != "running", -_ts(c["created_at"])))
        done.sort(key=lambda c: -_ts(c["completed_at"]))

        # 依赖图: 画出**所有**跟依赖边沾边的卡 (含已完成的, 前端标灰)。
        # 为什么不全过滤成"活着的": 编排图的价值恰恰在于让人看出
        # "上一批活是怎么串的" —— 全灰的时候也要看得见骨架。
        known = {c["id"] for c in needs + inflight + done}
        graph_edges = [(p, c) for p, c in edges if p in known and c in known]
        touched = {x for e in graph_edges for x in e}
        all_cards = {c["id"]: c for c in (needs + inflight + done)}
        graph_nodes = [all_cards[n] for n in touched if n in all_cards]
        # 层内按创建时间排 —— 同一层是并行的一批活, 顺序应该是"先后开工"而不是 id
        graph_nodes.sort(key=lambda c: _ts(c["created_at"]))
        layers = _layers([n["id"] for n in graph_nodes], graph_edges)

        # agent 负载: 进行中 + 待处理的, 按 assignee 数
        load: dict[str, int] = {}
        for c in inflight + needs:
            load[c["assignee"]] = load.get(c["assignee"], 0) + 1
        agents = [{"name": k, "count": v} for k, v in
                  sorted(load.items(), key=lambda kv: (-kv[1], kv[0]))]

        return {
            "needs_you": needs,
            "in_flight": inflight,
            "done": done[:limit_done],
            "done_total": len(done),
            "agents": agents,
            "graph": {
                "nodes": [{"id": c["id"], "title": c["title"], "status": c["status"],
                           "assignee": c["assignee"], "layer": layers.get(c["id"], 0)}
                          for c in graph_nodes],
                "edges": [{"from": p, "to": c} for p, c in graph_edges],
                "layers": layers,
            },
            "total": len(needs) + len(inflight) + len(done),
            "available": True,
            "updated": int(time.time()),
        }
    finally:
        con.close()


def summary_text() -> str:
    """一段话版本 —— 微信里发的那种 (不用看图)。"""
    b = board()
    if not b["available"]:
        return "看板暂时读不到。"
    lines = []
    if b["needs_you"]:
        lines.append(f"待你处理 {len(b['needs_you'])} 件")
        for c in b["needs_you"][:4]:
            lines.append(f"  [{c['status']}] {c['title'][:40]} ({c['assignee']})")
    if b["in_flight"]:
        lines.append(f"进行中 {len(b['in_flight'])} 件")
        for c in b["in_flight"][:4]:
            lines.append(f"  [{c['status']}] {c['title'][:40]} ({c['assignee']})")
    if not lines:
        lines.append("看板空着, 没有要你处理的东西。")
    else:
        lines.append(f"已完成 {b['done_total']} 件")
    return "\n".join(lines)


# 快捷指令 -> 一段话。全部直读看板, 不问 LLM (要的就是秒回)。
QUICK_KEYS = ("progress", "todo", "busy", "live")


def quick_text(key: str) -> str:
    """给手机上的快捷指令用 —— 纯看板查询, 毫秒级, 不走模型。

    ⚠️ 为什么不用 LLM: "汇报进度"这种问题问模型等于让它自己查一遍看板,
       实测要 80 秒起步 (还会 spawn 子代理), 而这里直接读 SQLite 是 1.5 毫秒。
    """
    b = board()
    if not b["available"]:
        return "看板暂时读不到。"

    if key == "progress":
        return summary_text()

    if key == "todo":
        if not b["needs_you"]:
            return "没有要你处理的。"
        out = [f"待你处理 {len(b['needs_you'])} 件:"]
        for c in b["needs_you"]:
            out.append(f"  [{c['status']}] {c['title'][:40]}")
            if c.get("error"):
                out.append(f"      卡住原因: {str(c['error'])[:90]}")
        return "\n".join(out)

    if key == "busy":
        if not b["agents"]:
            return "没有 agent 在忙。"
        out = ["谁在忙:"]
        for a in b["agents"]:
            out.append(f"  {a['name']} {a['count']} 件")
        if b["in_flight"]:
            out.append("  手上的活:")
            for c in b["in_flight"][:6]:
                out.append(f"    [{c['status']}] {c['title'][:36]} ({c['assignee']})")
        return "\n".join(out)

    if key == "live":
        if not b["in_flight"] and not b["needs_you"]:
            return "现在没活。"
        out = ["现在有什么活:"]
        for c in b["in_flight"][:8]:
            out.append(f"  [{c['status']}] {c['title'][:40]} ({c['assignee']})")
        for c in b["needs_you"][:4]:
            out.append(f"  [要你处理] {c['title'][:40]}")
        return "\n".join(out)

    return "不认识的指令。"
