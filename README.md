# 怀恩 — 跑在你自己电脑上的 AI 管家

```
   你 (微信 / 桌面 / 手机网页)
   │
   │  「把这批 wav 转成 mp3, 转完给我一份报告」
   ▼
   怀恩 ─────────────────────────────────────────────
     ├ 拆成三张有依赖的卡: 转换 → 质检 → 写报告
     ├ 派给后台 worker (各跑各的 profile, 互不抢资源)
     ├ 12 分钟后: 产物在 /tmp/out, 报告发你微信了
     │
     └ 你中途插一句「别覆盖原文件」→ 当轮就改方向
```

一句话说完：它接住你的一句话，自己拆成任务链、派人跑、盯着进度、把结果发回来。
你不回，它自己决定下一步继续推。

[快速开始](#快速开始) · [接微信](#接微信跨网入口) · [它怎么做到的](#它怎么做到的) · [AI 协作](docs/AI协作说明.md) · [107 条能力清单](docs/怀恩-核心能力清单.md)

---

## 它做什么

不是聊天，是干活。下面是它实际干过的事：

> **你**：把这 50 个 wav 转成 mp3。

**怀恩**把这句话拆成三张有依赖关系的卡（先转、再质检、最后写报告），派给后台 worker 依次执行。
跑完自动把产物和报告发回你的微信 —— 中间它会讲进度，不静默。

> **你**：（它正跑着）等等，别覆盖原文件。

当前运行当轮就被重定向，不用等它跑完再返工。这是 Hermes 的 `/steer`，微信、桌面、网页都能用。

> **你**：盯着这个任务，有进展就告诉我。

它挂上主动规则。卡住、失败、gateway 掉了才推你，全部跑完时推**一次**汇总而不是每张卡推一次。
一切正常时它一句话都不说。

> **你**：（发完就放下手机去吃饭了）

它不会停在等你回话。卡住的先等着，上游一完成、满足条件的卡自动释放继续跑；
实在需要拍板的，它会自己选一条能走的路继续，而不是干等。

同时跑几件事也不打架：每个 worker 在自己的 profile 里，有独立会话和上下文，多个 profile 共用一个 gateway 进程。

主动规则是你自己写的文件（`config/POLICY.md`），改完就生效，不用改代码不用重启。

---

## 它像电影里的哪些电脑

| | 出处 | 像在哪一点 |
|---|---|---|
| **JARVIS** | 《钢铁侠》《复仇者联盟》 | 贴身助手，管你的日程、装备、通讯，随时待命。怀恩的"微信里说一句话就有人干活"就是这个路子 |
| **FRIDAY** | 《复仇者联盟2》 | JARVIS 的接班人，管的是整套系统而不只是对话。怀恩管的是整条任务链 |
| **MOSS** | 《流浪地球》 | 550W 量子计算机，危机里不等指令自己决策往前推。**怀恩"你不回它自己往下推"就是这一条** |
| **The Machine** | 《疑犯追踪》 | 一直在替你盯着一个人，一个都不落。怀恩的主动规则盯的是任务和进程 |
| **HAL 9000** | 《2001太空漫游》 | 有自己的判断，会瞒你，最后自己动手。怀恩不会瞒你，但"自己决定"这点像 |
| **Oracle** | 《黑客帝国》 | 什么都知道，但只答你问的。怀恩不主动灌你日志 |
| **Samantha** | 《超体》《她》 | 会学习的伙伴，不是工具人。怀恩的记忆会跨会话累积 |
| **Agent** | 《雪崩》 | 元宇宙里的智能体，一人指挥一整片。怀恩一个管家指挥一串 worker |
| **Trinity** | 《黑客帝国》 | 黑客那一侧的 AI，操作别人看不见的系统。怀恩只操作你自己的机器 |
| **GLaDOS** | 《传送门》 | 管理者，不是助手。**这是反面教材** —— 怀恩的主动规则里写死了"什么时候必须闭嘴" |

最有意思的三个是 **MOSS**（自主推进）、**The Machine**（持续盯着）、**JARVIS**（贴身可用）。
怀恩想当的是这三个的交集。

---

## 快速开始

需要两样东西：**Hermes Agent**（后台，模型工具记忆全靠它）和 **Python 3.10+**（前台，带 tkinter）。

```bash
# 1. 装 Hermes Agent
#    Windows PowerShell:
iex (irm https://hermes-agent.nousresearch.com/install.ps1)
#    macOS / Linux:
curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash

hermes setup --portal          # 配一个模型

# 2. 拿代码、建环境
git clone https://github.com/HPDK1990/huaien.git
cd huaien
uv venv .venv --python 3.11
uv pip install --python .venv/Scripts/python.exe -e ".[dev]"

# 3. 建怀恩的人格 profile
hermes profile create huaien --clone
copy profiles\huaien-SOUL.md "%LOCALAPPDATA%\hermes\profiles\huaien\SOUL.md"

# 4. 验一下, 然后启动
.venv\Scripts\python.exe -m pytest
.venv\Scripts\python.exe scripts\启动怀恩.bat
```

macOS / Linux 的 profile 目录是 `~/.hermes/profiles/huaien/SOUL.md`。

不想一步步敲？把下面这段整段丢给 Claude Code、Codex、Cursor 或任何带终端的 AI：

```text
请在本机安装「怀恩」(仓库 HPDK1990/huaien)。

背景: 怀恩是跑在本机的 AI 管家前台, 后台依赖本机的 Hermes Agent。
请按顺序做, 每步做完汇报结果, 失败就停下来说明原因, 不要猜:

1. 确认前置条件。运行 `hermes --version` 和 `python -c "import tkinter; print('ok')"`。
   如果 hermes 不存在, 先告诉我安装命令 (Windows PowerShell:
   iex (irm https://hermes-agent.nousresearch.com/install.ps1)), 不要替我执行安装向导。
   如果 tkinter 缺失, 告诉我缺的是哪个 Python, 别自己换解释器。

2. 克隆仓库到当前目录下名为 huaien 的文件夹, 然后进进去。

3. 建虚拟环境并安装:
   - 优先用 uv: `uv venv .venv --python 3.11` 然后
     `uv pip install --python .venv/Scripts/python.exe -e ".[dev]"`
   - 没有 uv 才用 `python -m venv .venv` + `pip install -e ".[dev]"`

4. 如果 `hermes profile list` 里没有 huaien, 执行 `hermes profile create huaien --clone`,
   并把仓库的 profiles/huaien-SOUL.md 复制到该 profile 的 SOUL.md
   (Windows: %LOCALAPPDATA%\hermes\profiles\huaien\SOUL.md;
    macOS/Linux: ~/.hermes/profiles/huaien/SOUL.md)。

5. 跑 `.venv/Scripts/python.exe -m pytest` 和 `python -m ruff check .`,
   把失败项原样贴给我, 不要自己改测试来让它通过。

6. 跑 `.venv/Scripts/python.exe scripts/selftest.py`, 告诉我三层自检是否 3/3。

7. 告诉我接微信需要我本人做哪几步 (扫码那步你做不了),
   以及桌面和手机端分别怎么启动。

约束:
- 不要改代码, 只做安装和验证
- 不要写死本机绝对路径到任何文件
- 任何一步失败就停下, 把真实报错给我
```

AI 协作的更多细节（项目地图、硬约束、发布流程）见 [docs/AI协作说明.md](docs/AI协作说明.md)。

---

## 三个入口

**微信私聊**（推荐，唯一能跨网的）—— 扫码一次，在外面用手机指挥。

**桌面窗口** —— 双击 `scripts\启动怀恩.bat`。它自己检查 Python / tkinter / hermes 在不在，缺什么会明确告诉你。

**手机网页** —— 双击 `scripts\启动怀恩手机端.bat`，窗口里打印一个带口令的网址。手机连同一个 WiFi 打开，能"添加到主屏幕"。

> 手机网页那个网址等同于你电脑的权限。只在自家局域网用，不要转发给别人。要跨网就用微信。

---

## 接微信（跨网入口）

微信是怀恩的主要用法，分三步。第一步不能跳。

### 第一步 · 建立微信机器人

Hermes 官方支持个人微信，走腾讯的 iLink Bot API：扫码登录，**不需要公网 IP，不需要 webhook**（长轮询）。

```bash
hermes gateway setup
```

菜单里选 **Weixin**，用手机微信扫一扫，在手机上确认。

> Windows 上如果 `hermes` 不在 PATH，双击 `scripts\连接微信飞书.bat`（它会先把 Hermes 目录加进 PATH）。
>
> 扫码之后 Hermes 拿到的是一个**独立的机器人身份**（`xxx@im.bot`），不是你的微信本身。你在自己微信里跟这个机器人说话，它才回你。

### 第二步 · 让微信里的对话走怀恩的人格

怀恩和 Hermes 默认人格是两个 profile，要配一条路由：

```yaml
gateway:
  multiplex_profiles: true
  profile_routes:
    - profile: huaien
      platform: weixin
      chat_id: <扫码后拿到的 chat_id>
```

改配置**用 `hermes config set`，不要手改 `config.yaml`** —— 手改的缩进会损坏运行中的配置。

```bash
hermes config get gateway.profile_routes     # 先看当前长什么样，照它的格式改
hermes config set gateway.profile_routes '<你按上面格式写的值>'
```

### 第三步 · 验证

```bash
hermes gateway status          # 应该有 weixin 通道
hermes -p huaien sessions list # 微信会话会出现在 huaien 这个 profile 里
```

然后在微信里私聊机器人发一句话，应该收到回复。

### 微信的硬限制

腾讯 iLink 的官方行为，改不了：

| 场景 | 能不能用 | 说明 |
|---|---|---|
| 私聊 (DM) | 能，而且可靠 | 这是微信这条路唯一正经的用法 |
| 普通微信群 | 基本不通 | 机器人拉不进群，群里发消息（包括 @ 机器人）基本收不到 |
| 群机器人 webhook | 不支持 | — |
| 企业微信 | 个人走不通 | 要企业微信组织账号 + 管理后台建 Bot |

群聊不通是腾讯的限制，不是 Hermes 能绕过的。所以微信这条路只能私聊指挥，不能拉群。

通道能力边界：

| 能力 | 状态 |
|---|---|
| 文字收发 | 能 |
| 发图片 / 视频 / 文件 | 能，但**一个附件一条消息**，一次发多个会拆成多条 |
| 语音入站（你发语音给它） | 要另配本地 STT（faster-whisper），不是开箱即用 |
| 语音出站 | 未支持（微信原生语音要 SILK 编码） |
| 连续主动发消息 | 约 3 条后可能需要你再发一条刷新会话 |

主动推送为主时，发不出去的消息会进待发队列（outbox），等你下次发消息时自动补发。

配置和排障见 [docs/外部通道-配置指南.md](docs/外部通道-配置指南.md) 和
[docs/微信飞书-现在就能弄.md](docs/微信飞书-现在就能弄.md)。

---

## 它怎么做到的

```
怀恩窗口 (Tkinter) / 浏览器 (PWA) / 微信消息
    │  一句话
    ▼
ChatBridge  ──►  hermes -p huaien chat -q "..." --format stream-json
    │              (Hermes CLI = 最稳定的接口面)
    │  逐字流式
    ▼
打字机效果渲染  ──►  [[FILE:路径]] 标记被剥掉, 变成可双击打开的一行
```

怀恩没有自己的模型层。模型、工具、记忆、会话库、看板、定时任务、派工全是 Hermes 原生的，
前台只做三件事：给你一个能打字的框、把 Hermes 的话显示出来、把回车变成一次 `hermes chat`。

- 人格在 profile 的 `SOUL.md` 里（话不多，但活要干）
- 记忆由 `scripts/sync_memory.py` 做条目级双向补齐
- 产物用 `[[FILE:...]]` / `[[IMG:...]]` 标注，前台渲染成可双击打开的一行
- 拆解和派工走 Hermes 原生 Kanban（`hermes kanban`）

逐项对照见 [docs/架构-前后台边界.md](docs/架构-前后台边界.md)，
107 条能力（每条带代码锚点和实跑证据）见 [docs/怀恩-核心能力清单.md](docs/怀恩-核心能力清单.md)。

---

## 常用命令

```bash
python -m pytest                      # 单元测试
python -m ruff check .                # 静态检查
python scripts/selftest.py            # 端到端: 起窗口 -> 发一句 -> 收回复 -> 截图
python scripts/gui_drill.py           # 前台真实操作测试 (鼠标点击/打字/回车)
python scripts/gui_drill.py --live    # 同上, 但真给 Hermes 发消息 (验证干活能力)
python scripts/sync_memory.py --check # 前后台记忆是否对齐
python scripts/watchdog.py --status   # gateway / 前台 / web / 记忆 四件事
python scripts/butler.py --json       # 主动规则当前命中情况
```

没有 Hermes 也能跑测试，契约类测试会自动 skip，不误报。

---

## 常见问题

**微信机器人不回话**
先 `hermes gateway status` 看 weixin 通道在不在；确认是私聊不是群聊；再看 `hermes -p huaien sessions list` 有没有新会话。

**微信群里机器人不说话**
腾讯 iLink 的限制，只能私聊。

**微信发出去几条就不动了**
会话 token 需要你再发一条刷新。你发一句，之前的消息会补发。

**它一直给我推消息**
主动规则太宽松。改 `config/POLICY.md`，改完就生效，不用重启。

**任务卡住了没人管**
上游卡住，释放条件没满足。`hermes kanban list` 看卡的依赖，满足条件的卡会自动释放。

**启动 bat 一闪而过**
窗口里报了缺什么但一闪就没了。在 `cmd` 里跑 `scripts\启动怀恩.bat` 看报错，它会明确说缺 Python / tkinter / hermes。

> 启动脚本是中文 Windows 专用（文件是 GBK 编码，cmd 按 cp936 读）。英文 Windows 上直接跑 `.venv\Scripts\python.exe -m huaien.app`。

**`cannot import name '_imaging' from 'PIL'`**
环境里 `PYTHONPATH` 被别的东西污染了。用项目自己的 `.venv\Scripts\python.exe` 跑，别用系统 Python。

**`hermes: command not found`**
Hermes 没装或不在 PATH。Windows 上确认 `%LOCALAPPDATA%\hermes\bin` 在 PATH。

**手机打不开那个网址**
手机和电脑不在同一个 WiFi。必须是同一局域网；跨网用微信通道。

**契约测试报"上游接口漂移"**
这是它在正常工作 —— Hermes 升级改了接口。跑 `python -m pytest tests/test_hermes_contract.py` 看哪条漂了，然后更新 `config/hermes_contract.json`。

**装完还是跑不起来**
缺 `huaien` profile。`hermes profile create huaien --clone`，再把 `profiles/huaien-SOUL.md` 拷过去。

---

## 文档

| 文档 | 讲什么 |
|---|---|
| [docs/怀恩-核心能力清单.md](docs/怀恩-核心能力清单.md) | 107 条能力，每条带代码锚点和实跑证据，含已知失效点 |
| [docs/架构-前后台边界.md](docs/架构-前后台边界.md) | 前台三层 / 后台三层，什么归谁管 |
| [docs/外部通道-配置指南.md](docs/外部通道-配置指南.md) | 微信、飞书、公网入口各自的现状和配置 |
| [docs/AI协作说明.md](docs/AI协作说明.md) | 给 AI 协作者：项目地图、硬约束、验证方式 |
| [docs/上架隔离机制.md](docs/上架隔离机制.md) | 开发仓和公开仓怎么分开、脱敏怎么做 |
| [config/POLICY.md](config/POLICY.md) | 主动行动规则（机器可读，改完即生效） |

---

## 状态

**V0.5.0**。这一版的三件事：

**契约层** —— 怀恩依赖 Hermes 的每个接口（CLI 子命令 / JSON 根键 / 文件表结构）都写在 `config/hermes_contract.json`，每条标明谁依赖它。上游 Hermes 升级导致接口漂移时，怀恩会测试变红，而不是静默坏掉。

**守护归位** —— 微信通道守护脚本的真身进了本仓库（受版本管理），外部只留一个薄包装转发。

**上架隔离** —— 开发仓和公开副本物理分离，脱敏在导出时自动完成，日常开发不用手工清理。见 [docs/上架隔离机制.md](docs/上架隔离机制.md)。

---

## 许可

MIT，见 [LICENSE](LICENSE)。
