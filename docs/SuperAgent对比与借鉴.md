# Super-agent（DeepSeek）vs 怀恩 — 对比与借鉴清单

> 用户 2026-11-03: *"deepseek 开源了一个叫 super-agent 的项目，看看是不是和怀恩的目标相近。
> 是否有可参考和学习借鉴的地方。"*

## 一、先验真假（不能只看 README）

```
deepseek-ai/dsh-superagent-prototype  ->  REAL(code)
stars 14653 | forks 1567 | issues 0 | TypeScript | MIT
created 2026-10-31 | pushed 2026-10-31 | releases 0
code dirs: ['src']
root: src/ tests/ docs/ skills/ prompts/ examples/ scripts/ package.json
desc: Towards a More Autonomous Agent System
```

**是真项目**（有 `src/` + `tests/` + `docs/` + `skills/`），不是纸面仓库。

**但也要看到三条风险**：
- **`releases 0`** —— 没发过任何版本，"能装能跑"未被验证
- **`created` 和 `pushed` 是同一天** —— 开源仅 3 天，代码可能还在剧烈变动
- **`issues 0`** —— 还没被真实用户磨过

**结论：学它的架构思想，不抄它的具体实现。**

---

## 二、它的架构（读源码 + docs 得到）

```
plugins[Source plugins]  →  SourceContext  →  engine[Engine]
tools[Session tools]                      →  engine
engine → store[Store] / policy[Policy] / registry[SourceRegistry]
engine → assessor[MasterAssessor]   ← 评估: 这事该不该做
engine → executor[DshExecutor]      ← 执行: 独立 session + 独立目录
engine → outbox[Outbox]             ← 可靠投递
engine → panel[Panel]               ← 面板: 什么需要人处理
```

**一条工作项的完整生命周期**：

```
Trigger(一条触发消息)
  → Eval(评估)
  → Ticket(工作项)
  → Assignment(分派)
  → Execution(独立 session + 目录里干活)
  → report(报告)
  → 回原对话交付
```

**它的核心原则（README 原文）**：

> *"take as little human attention as possible, and give as much value as possible.
> An agent that can act for a whole team can also waste a whole team's attention,
> so it acts only when asked or when a rule the team wrote allows it,
> and speaks only when someone must act or can use a result."*

拆开就是三条：
1. **只在被要求时、或团队写的规则允许时才行动**
2. **只在有人必须行动、或能用上结果时才开口**
3. **尽量少占用人的注意力**

---

## 三、和怀恩逐项对照

| 维度 | Super-agent | 怀恩现状 | 差距 |
|---|---|---|---|
| 常驻接收端 | Engine 常驻 | ✅ gateway 常驻（计划任务 `Hermes_Gateway`） | **持平** |
| Worker 隔离 | 独立 session + 独立目录 | ✅ kanban workspace + `delegate_task` | **持平** |
| 跨渠道路由 | Source 插件（GitHub/飞书/IM） | ✅ `gateway.profile_routes`（微信→huaien） | **持平** |
| 看门狗 | 有 | ✅ `watchdog.py` + 纯脚本回归 cron | **持平** |
| 面板 | Panel（什么需要人处理） | ⚠️ 怀恩前台只有对话流 | **缺** |
| **规则驱动（Policy）** | ✅ 团队写规则才准主动 | ❌ **没有** | **大缺口** |
| **动手前评估（Assessor）** | ✅ MasterAssessor → Decision | ❌ **没有** | **大缺口** |
| **可靠投递（Outbox）** | ✅ 队列 + 重试 | ⚠️ 微信 iLink session 会废 | **小缺口**（已部分补） |
| **噪音控制** | ✅ 显式设计 | ❌ 没有 | **中** |
| 外部事件订阅 | ✅ GitHub / 飞书 / webhook | ⚠️ 只有 IM 入站 | **中** |

**一句话**：**怀恩的骨架（常驻端 / Worker 隔离 / 路由 / 看门狗）它都有，而且更成熟；
它比怀恩强的，是"主动但不打扰"这一层 —— 怀恩目前完全是被动响应。**

---

## 四、可借鉴的 5 条（按价值排序）

### 1️⃣ Policy —— 让用户用"写规则"代替"每次开口"（**最高价值**）

Super-agent 的 `Policy` 是**团队写的规则文件**，规定 agent 在什么条件下可以主动行动。
它把"主动"从 LLM 的自由裁量变成**可审计的授权**。

**怀恩怎么落**：在 `profiles/huaien/` 加一份 `POLICY.md`，让定时任务和主动推送前先读它：

```markdown
# 怀恩主动行动规则
- 看板出现 blocked 状态的卡 → 立刻推微信，并附上卡标题和卡号
- 每周备份失败 → 推微信
- 会话库体检 > 500 MB → 推微信
- 其他情况 → 保持安静，不要打扰
```

**价值**：用户不用守着屏幕问"任务到哪了"，怀恩自己判断该不该开口。

### 2️⃣ Assessor —— 动手前先判断"值不值得 / 要不要人拍板"

Super-agent 有独立的 `MasterAssessor`，产出 `Decision` 对象：
**要么"我来做"，要么"这里需要你决定"**。

**怀恩怎么落**：轻量版 —— 怀恩每次要干"有副作用"的事（改配置、删文件、推消息）前，
先产出一句判断：**这件事是"我现在就能做完"还是"要你点头"**。

### 3️⃣ Outbox —— 主动推送要能重试（**微信 session 寿命问题**）

怀恩微信实测：**iLink 的 sendmessage session 连发约 3 条就废**，
报 `ret=-2 prepare failed — the user must send the bot a message first`，**退避无效**。

**怀恩怎么落**：主动推送放进 outbox 队列，失败时**排队等用户下次发消息后再重投**，
而不是直接丢弃。

### 4️⃣ Panel —— "什么需要你处理"要单独一屏

Super-agent 的 Panel 明确把 **"需要人处理的东西"** 从对话流里抽出来。
怀恩现在的所有内容都混在聊天记录里，用户要翻。

**怀恩怎么落**：怀恩前台加一个固定的"待你处理"区（blocked 卡 / 失败的推送 / 决策点）。

### 5️⃣ Source 插件化 —— 事件源可插拔

它的 `Source` 是插件（GitHub / 飞书 / IM），加新源只要写一个插件。
怀恩的事件源目前只有 IM。

**怀恩怎么落**：怀恩真正需要的源是 **cron 事件 / 看板状态变化 / 文件变化** ——
这三个 Hermes 已经全有了，只是没被当成"源"组织。

---

## 五、**不该学的**（批判性）

| Super-agent 的做法 | 为什么怀恩不该抄 |
|---|---|
| TypeScript + pnpm + cordis 插件框架 | 怀恩已在 Python + Hermes 上跑通了，**换技术栈 = 推倒重来** |
| 自己写 Engine / Executor / Store | Hermes 的 gateway + kanban + delegate_task **已经是这些** |
| 面向"单仓库 coding agent" | 怀恩是**通用桌面管家**（文件/电脑操作/微信/日程），目标不同 |
| 目录切分 + patch 交付 | 怀恩交付的是**文件和产物**，不是 git patch |
| 刚开源 3 天、0 release | **代码还在剧烈变动**，抄实现等于抄一个移动靶 |

**一句话**：**它是一个"给单个 GitHub 仓库做事的自主系统"，
怀恩是"管用户整台电脑的私人管家"。骨架通用，场景不同。**

---

## 六、本轮已经落地的（一处真实改进）

**发现**：怀恩的定时任务结果**推不到微信** ——
`scripts/report.py` 的 `find_feishu_chat()` 硬编码只认飞书，
而 5 个 cron 里有 3 个是 `deliver=local`（结果只落在本地会话里，用户看不到）。

**已修**（`scripts/report.py`）：
- `find_feishu_chat()` → `find_chat(platform)`，通用化到微信/飞书
- 新增 `--platform`（默认 `weixin`，用户的日常通道）
- 微信推送失败时识别 session 失效并给出明确提示（而不是只打印 rc=1）
- 飞书路径回归正常

**实测**：
```
report.py --kanban --dry             → 目标: weixin:o9cq80_...@im.wechat
report.py --kanban --dry --platform feishu → 目标: feishu:<CHANNEL_ID>...
report.py --say "自检"              → hermes send -> rc=0 sent   ← 真的发出去了
```

**主动汇报 → 微信，这条路现在通了。**
