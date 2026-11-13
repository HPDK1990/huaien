# 怀恩 · Hermes-HuaiEn

**一句话: 怀恩是跑在你自己电脑上的微信机器人。**
你在微信里跟它说话, 它把重活派给后台的 Hermes Agent, 做完把结果发回你微信。

```
   你的手机                        你的电脑                      你的微信
  ┌────────┐    微信消息     ┌────────────────────┐          ┌────────┐
  │ 微信    │ ─────────────▶ │  Hermes gateway      │ ───────▶ │ 微信    │
  │ 私聊    │ ◀───────────── │  + 怀恩 (前台 + 后台) │ ◀─────── │ 你自己  │
  └────────┘    回复 / 文件 └────────────────────┘          └────────┘
```

## 三种用法

| 用法 | 需要什么 | 适合 | 能否跨网 |
|---|---|---|---|
| **微信** (推荐) | 手机微信 + 扫码一次 | 在外面用手机指挥 | ✅ 能 |
| **桌面窗口** | 无 | 坐在电脑前 | — |
| **手机网页 (PWA)** | 手机和电脑连**同一个 WiFi** | 在家想用手机 | ❌ 不能 |

> 手机网页那个地址**等同于你电脑的权限**, 只在自家局域网用, 不要转发给别人。
> 要跨网就用微信通道。

---

## 它是什么

| 层 | 是什么 | 干什么 |
|---|---|---|
| **前台** | Python/Tkinter 写的单对话框窗口 + 局域网 Web (PWA) | 给你一个能打字的框, 把 Hermes 的话显示出来 |
| **后台** | 你本机的 [Hermes Agent](https://github.com/NousResearch/hermes-agent), 带独立人格 (`huaien` profile) | 模型、工具、记忆、会话库、看板、定时任务、派工 |

**怀恩不是另一个 AI。** 它没有自己的模型层 —— 前台只做三件事:
给你一个能打字的框、把 Hermes 的话显示出来、把回车变成一次 `hermes chat`。
逐项对照见 [docs/架构-前后台边界.md](docs/架构-前后台边界.md)。

---

## 前置条件

| 组件 | 要求 | 怎么确认 |
|---|---|---|
| **Hermes Agent** | **必需** —— 怀恩只是前台, 模型/工具/记忆全在它身上 | `hermes --version` |
| Python | **≥ 3.10, 且必须带 tkinter** | `python -c "import tkinter; print('ok')"` |
| uv | 可选但推荐 (管虚拟环境) | `uv --version` |
| 微信 | 想用微信通道才需要 (手机微信, 扫码一次) | — |

> **怀恩自己零第三方依赖** —— 运行时只用标准库 (tkinter 属于标准库)。
> `pyproject.toml` 里 `dependencies = []` 是真的。

---

## 安装

### 方式一 · 人工操作

**Step 0 — 装 Hermes Agent (后台)**

Windows (PowerShell):

```powershell
iex (irm https://hermes-agent.nousresearch.com/install.ps1)
```

macOS / Linux / WSL2:

```bash
curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash
```

装完配一个模型 (一条 OAuth 覆盖模型 + 搜索/图像/语音/浏览器四个工具):

```bash
hermes setup --portal
```

**Step 1 — 拿代码**

```bash
git clone https://github.com/HPDK1990/huaien.git
cd huaien
```

**Step 2 — 建环境**

用 uv (推荐):

```bash
uv venv .venv --python 3.11
uv pip install --python .venv/Scripts/python.exe -e ".[dev]"
```

没有 uv 的话 (标准库 venv):

```bash
python -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

**Step 3 — 建 `huaien` 人格 profile**

```bash
hermes profile create huaien --clone
```

把仓库里的人格文件放进去 (Windows, cmd):

```bat
copy profiles\huaien-SOUL.md "%LOCALAPPDATA%\hermes\profiles\huaien\SOUL.md"
```

macOS / Linux 的 profile 目录是 `~/.hermes/profiles/huaien/SOUL.md`。

**Step 4 — 验一下装对了**

```bash
.venv\Scripts\python.exe -m pytest               # 单元测试
.venv\Scripts\python.exe scripts/selftest.py     # 端到端: 起窗口 -> 发一句 -> 收回复 -> 截图
```

> **没有 Hermes 也能跑测试** —— 契约类测试会自动 skip, 不误报。

**Step 5 — 接微信** → 往下看 [★ 接微信](#-接微信主线用法) 那一节。

---

### 方式二 · AI 操作

> 适合 Claude Code / Codex / Cursor / Hermes 这类带终端的 agent。
> **注意: 微信扫码那一步 AI 代替不了** —— 手机在你手里, 必须你本人扫。

把下面这段整段丢给 agent:

```text
请在本机安装「怀恩」(仓库 HPDK1990/huaien)。

背景: 怀恩是跑在本机的微信机器人前台, 后台依赖本机的 Hermes Agent。
请按顺序做, 每步做完汇报结果, 失败就停下来说明原因, 不要猜:

1. 确认前置条件。运行 `hermes --version` 和 `python -c "import tkinter; print('ok')"`。
   如果 hermes 不存在, 先告诉我安装命令 (Windows PowerShell:
   iex (irm https://hermes-agent.nousresearch.com/install.ps1)), **不要替我执行安装向导**。
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
   把失败项**原样**贴给我, 不要自己改测试来让它通过。

6. 跑 `.venv/Scripts/python.exe scripts/selftest.py`, 告诉我三层自检是否 3/3。

7. 告诉我接微信需要我本人做哪几步 (扫码那步你做不了),
   以及桌面和手机端分别怎么启动。

约束:
- 不要改代码, 只做安装和验证
- 不要写死本机绝对路径到任何文件
- 任何一步失败就停下, 把真实报错给我
```

AI 协作的更多细节 (项目地图、硬约束、发布流程) 见
[docs/AI协作说明.md](docs/AI协作说明.md)。

---

## ★ 接微信 (主线用法)

> 微信是怀恩的**主要用法**, 也是唯一能跨网的通道。
> 这一节分三步: 建机器人 → 指向怀恩 → 验证。**第 1 步不能跳。**

### 第 1 步 · 建立微信机器人 (必须先做, 扫码要你本人操作)

Hermes 官方支持**个人微信**, 走腾讯的 **iLink Bot API**:
扫码登录、**不需要公网 IP**、**不需要 webhook** (长轮询)。

```bash
hermes gateway setup
```

菜单里选 **Weixin** → 用**手机微信扫一扫** → 在手机上确认。

> Windows 上如果 `hermes` 不在 PATH, 双击 `scripts\连接微信飞书.bat`
> (它会先把 Hermes 目录加进 PATH 再跑向导)。
>
> ⚠️ 扫码之后, Hermes 拿到的是一个**独立的机器人身份** (`xxx@im.bot`),
> **不是你的微信本身**。你在自己微信里跟这个机器人说话, 它才回你。

### 第 2 步 · 让微信里的对话走 `huaien` 这个 profile (怀恩的人格)

怀恩和 Hermes 默认人格是两个 profile。要让微信里的对话是"怀恩"而不是默认助手,
配一条路由 (在 Hermes 的配置里):

```yaml
gateway:
  multiplex_profiles: true
  profile_routes:
    - profile: huaien
      platform: weixin
      chat_id: <扫码后拿到的 chat_id>
```

改配置**用 `hermes config set`, 不要手改 `config.yaml`** —— 手改的缩进会损坏运行中的配置。

```bash
hermes config get gateway.profile_routes     # 先看当前长什么样 (照它给你的格式改)
hermes config set gateway.profile_routes '<你按上面格式写的值>'
```

### 第 3 步 · 验证

```bash
hermes gateway status                        # 应该有 weixin 通道
hermes -p huaien sessions list               # 微信会话会出现在 huaien 这个 profile 里
```

然后**在微信里私聊机器人发一句话** (比如 "你好"), 应该收到回复。

### 微信的硬限制 (腾讯 iLink 的官方行为, 改不了)

| 场景 | 能不能用 | 说明 |
|---|---|---|
| **私聊 (DM)** | ✅ **可靠** | 这是微信这条路**唯一正经的用法** |
| 普通微信群 | ❌ 基本不通 | 机器人拉不进群, 群里的消息 (包括 @ 机器人) 基本收不到 |
| 群机器人 webhook | ❌ 不支持 | — |
| 企业微信 | ❌ 个人走不通 | 要企业微信组织账号 + 管理后台建 Bot |

> 群聊不通是**腾讯的限制**, 不是 Hermes 能绕过的。
> 所以微信这条路只能做「私聊指挥怀恩」, 不能拉群。

### 微信通道能做什么

| 能力 | 状态 |
|---|---|
| 文字收发 | ✅ |
| 发图片 / 视频 / 文件给你 | ✅ (注意: **一个附件一条消息**, 一次发多个会拆成多条) |
| 语音入站 (你发语音给它) | ⚠️ 需要另配本地 STT (faster-whisper), **不是开箱即用** |
| 语音出站 | ❌ 未支持 (微信原生语音要 SILK 编码) |
| 连续主动发消息 | ⚠️ 约 3 条后可能需要你再发一条刷新会话 |

主动推送为主时, 发不出去的消息会进**待发队列 (outbox)**, 等你下次发消息时自动补发。

更细的配置和排障见 [docs/外部通道-配置指南.md](docs/外部通道-配置指南.md)
和 [docs/微信飞书-现在就能弄.md](docs/微信飞书-现在就能弄.md)。

---

## 桌面用法

双击 `scripts\启动怀恩.bat`。
它会自己检查 Python / tkinter / hermes 在不在, 缺什么会明确告诉你。

> ⚠️ **启动脚本是中文 Windows 专用** (文件是 GBK 编码, cmd 按 cp936 读)。
> 英文 Windows 上直接跑 Python 命令: `.venv\Scripts\python.exe -m huaien.app`。

| 键 | 作用 |
|---|---|
| `Enter` | 发送 |
| `Shift+Enter` | 换行 |
| `Esc` | 清空, 重新开始 (丢上下文) |
| `Ctrl+Q` | 退出 |
| 鼠标滚轮 | 滚历史 |
| 双击产物行 | 用系统默认程序打开那个文件 |
| 拖动窗口顶部空白处 | 移窗口 |

## 手机网页用法 (局域网)

双击 `scripts\启动怀恩手机端.bat`, 窗口里会打印一个**带口令**的网址。
手机连**同一个 WiFi** 打开它, 再"添加到主屏幕"。

> 这个网址等同于你电脑的权限。**只在自己家的局域网用, 不要转发给别人。**
> 需要跨网就用微信通道。

## 它怎么工作

```
怀恩窗口 (Tkinter)  或  浏览器 (PWA)  或  微信消息
    │  一句话
    ▼
ChatBridge  ──►  hermes -p huaien chat -q "..." --format stream-json
    │              (Hermes CLI = 最稳定的接口面)
    │  逐字流式
    ▼
打字机效果渲染  ──►  [[FILE:路径]] 标记被剥掉, 变成可双击打开的一行
```

- **流式**: 用 Hermes 的 `--format stream-json`, 收到一段渲染一段, 所以有打字机效果
- **多轮**: 上一轮的 `session_id` 通过 `--resume` 传回去, 上下文连续
- **人格**: profile 里的 `SOUL.md` 定义"话不多, 但活要干"
- **记忆**: 前台与后台的记忆由 `scripts/sync_memory.py` 做条目级双向补齐
- **产物**: agent 用 `[[FILE:...]]` / `[[IMG:...]]` 标注产出, 前台渲染成可双击打开的一行

---

## 常见问题

| 现象 | 原因 | 怎么办 |
|---|---|---|
| 微信机器人不回话 | ① 还没扫码 ② 消息发到群里了 ③ 路由没指向 `huaien` profile | 先 `hermes gateway status` 看 weixin 通道在不在; **私聊**发一句试试; 再看 `hermes -p huaien sessions list` 有没有新会话 |
| 微信群里机器人不说话 | **腾讯 iLink 的限制** | 群聊不通是官方行为, 只能私聊 |
| 微信发出去几条就不动了 | 会话 token 需要你再发一条刷新 | 你在微信里给机器人发一句, 之前的消息会补发 |
| 启动 bat 一闪而过 | 窗口里报了缺什么, 但一闪就没了 | 在 `cmd` 里跑 `scripts\启动怀恩.bat` 看报错; 它会明确说缺 Python / tkinter / hermes |
| `cannot import name '_imaging' from 'PIL'` | 环境里 `PYTHONPATH` 被别的东西污染了 | 用项目自己的 `.venv\Scripts\python.exe` 跑, 别用系统 Python |
| `hermes: command not found` | Hermes 没装, 或不在 PATH | 装 Hermes; Windows 上确认 `%LOCALAPPDATA%\hermes\bin` 在 PATH |
| 手机打不开那个网址 | 手机和电脑不在同一个 WiFi | 必须是**同一局域网**; 跨网用微信通道 |
| 契约测试报"上游接口漂移" | **这是它在正常工作** —— Hermes 升级改了接口 | 跑 `python -m pytest tests/test_hermes_contract.py` 看具体哪条漂了, 然后更新 `config/hermes_contract.json` |
| 装完还是跑不起来 | 缺 `huaien` profile | `hermes profile create huaien --clone`, 再把 `profiles/huaien-SOUL.md` 拷过去 |

---

## 目录

```
huaien/          前台 (界面 / 对话桥 / 主题 / 手机网页)
config/          管家策略 + Hermes 接口契约
profiles/        人格文件模板 (拷进 Hermes profile)
docs/            版本文档、架构、能力方案、通道配置、安装与协作说明
scripts/         启动脚本、看门狗、自检、记忆同步、发布与审计工具
tests/           单元测试 + 契约测试
```

## 测试与自检

```bash
python -m pytest                    # 单元测试
python -m ruff check .              # 静态检查
python scripts/selftest.py          # 端到端: 起窗口 -> 发一句 -> 收回复 -> 截图
python scripts/gui_drill.py         # 前台真实操作测试 (鼠标点击/打字/回车)
python scripts/gui_drill.py --live  # 同上, 但真给 Hermes 发消息 (验证干活能力)
python scripts/sync_memory.py --check   # 前后台记忆是否对齐
python scripts/watchdog.py --status      # gateway / 前台 / web / 记忆 四件事
```

## 状态

**V0.5.0** —— 见 [CHANGELOG.md](CHANGELOG.md) 和 [docs/路线-丙路线.md](docs/路线-丙路线.md)。

这一版的三件事:

1. **契约层** —— 怀恩依赖 Hermes 的每个接口 (CLI 子命令 / JSON 根键 / 文件表结构)
   都写在 `config/hermes_contract.json`, 每条标明"谁依赖它"。
   **上游 Hermes 升级导致接口漂移时, 怀恩会测试变红, 而不是静默坏掉。**
2. **守护归位** —— 微信通道守护脚本的真身进了本仓库 (受版本管理),
   外部只留一个薄包装转发。
3. **上架隔离** —— 开发仓和公开副本物理分离, 脱敏在导出时自动完成,
   日常开发不用手工清理。见 [docs/上架隔离机制.md](docs/上架隔离机制.md)。

## 许可

MIT, 见 [LICENSE](LICENSE)。
