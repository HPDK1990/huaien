# 怀恩 · Hermes-HuaiEn

一个**极简的前台管家界面**。你只跟它说话, 它把复杂的事都放到后台去。

```
┌─────────────────────────────────┐
│                                 │
│  你说的话                         │
│                                 │
│  管家说的话                       │
│                                 │
│  ▸ 刚写好的文件.py                │
│  ─────────────────────────  ▌   │
└─────────────────────────────────┘
```

纯黑。无标题栏。无菜单。无按钮。只有一个会呼吸的光标。

## 它是什么

**前台** —— 一个 Python/Tkinter 写的单对话框窗口, 极简科技风, 中文界面。
**后台** —— 你本机的 [Hermes Agent](https://github.com/NousResearch/hermes-agent),
带独立人格 (`huaien` profile): 话不多, 不展示过程, 只给结论。

前台只负责"你说一句, 它答一句"。任务拆解、工具调用、日志、看板、任务卡 —— 全部在后台,
除非你明确问, 它不会提。

**怀恩不是另一个 AI。** 模型、工具、记忆、会话库、看板、定时任务全部是 Hermes 原生的,
前台只做三件事: 给你一个能打字的框、把 Hermes 的话显示出来、把回车变成一次 `hermes chat`。
逐项对照表见 [docs/架构-前后台边界.md](docs/架构-前后台边界.md)。

## 快速开始

| 场景 | 怎么做 |
|---|---|
| **电脑上用** | 双击 `scripts\启动怀恩.bat` |
| **手机上用** | 双击 `scripts\启动怀恩手机端.bat`, 窗口里会打印一个带口令的网址。手机连同一个 WiFi 打开它, 再"添加到主屏幕" |
| **手动** | `python -m huaien.app` (桌面) / `python -m huaien.web` (手机端) |

> 手机端那个网址**等同于你的电脑权限**, 不要转发给别人。

## 它怎么工作

```
怀恩窗口 (Tkinter)  或  浏览器 (PWA)
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
- **人格**: `~/.hermes/profiles/huaien/SOUL.md` 定义"话不多, 但活要干"
- **记忆**: 前台与后台的记忆由 `scripts/sync_memory.py` 做条目级双向补齐
- **产物**: agent 用 `[[FILE:...]]` / `[[IMG:...]]` 标注产出, 前台渲染成可双击打开的一行

## 快捷键

| 键 | 作用 |
|---|---|
| `Enter` | 发送 |
| `Shift+Enter` | 换行 |
| `Esc` | 清空, 重新开始 (丢上下文) |
| `Ctrl+Q` | 退出 |
| 鼠标滚轮 | 滚历史 |
| 双击产物行 | 用系统默认程序打开那个文件 |
| 拖动窗口顶部空白处 | 移窗口 |

## 目录

```
huaien/
  app.py       前台界面 (Tkinter)
  web.py       手机/局域网 Web 前台 (PWA, 带口令)
  bridge.py    对话桥 (Hermes CLI -> 流式回调 + 产物解析)
  theme.py     配色 / 字体 / 尺寸
docs/          版本文档、架构、能力方案、截图
scripts/       启动脚本、看门狗、自检、记忆同步、前台真实操作测试
tests/         单元测试
versions/      封存快照 (每个版本一份)
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

V0.3.0 —— 见 [CHANGELOG.md](CHANGELOG.md) 和 [docs/V0.3-能力方案.md](docs/V0.3-能力方案.md)。

## 许可

MIT, 见 [LICENSE](LICENSE)。
