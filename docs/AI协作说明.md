# AI 协作说明 —— 给带终端的 AI 助手

> 你正在读一个**已经能跑**的项目。这份文件告诉你: 它是什么、能改什么、不能碰什么、怎么验。
>
> 普通人装它看 [README](../README.md); 你是来改代码的, 看这份。

## 它是什么

**怀恩** = 极简前台管家界面 (Tkinter / PWA) + 本机 [Hermes Agent](https://github.com/NousResearch/hermes-agent) 作后台。

| 层 | 负责 | 不负责 |
|---|---|---|
| 前台 `huaien/` | 给你一个能打字的框、把 Hermes 的话显示出来、把回车变成一次 `hermes chat` | **不碰**模型 / 工具 / 记忆 / 会话库 |
| 后台 Hermes | 模型、工具、记忆、会话库、看板、cron、调度 | — |

**怀恩不是另一个 AI。** 它没有任何自己的模型层。
逐项对照见 [架构-前后台边界.md](架构-前后台边界.md)。

## 快速上手

```bash
# 环境 (Python >= 3.10, 建议用 uv)
uv venv .venv --python 3.11
uv pip install --python .venv/Scripts/python.exe -e ".[dev]"

# 验证
python -m pytest                  # 单元测试
python -m ruff check .            # 静态检查
python scripts/selftest.py        # 端到端: 起窗口 -> 发一句 -> 收回复 -> 截图
```

**没有 Hermes 也能跑测试** —— 契约类测试会自动 skip, 不误报。

## 硬约束 (改之前先看)

| # | 约束 | 为什么 |
|---|---|---|
| 1 | **不要在前台里塞逻辑** | 前台只做"显示 + 输入"。判断、派工、干活全在 Hermes 侧 |
| 2 | **不要在代码里写死本机路径** | 用 `Path.home()` / `HERMES_HOME` / `%LOCALAPPDATA%` / `%~dp0..`。写死了换机器就废 |
| 3 | **依赖必须显式声明** | 不许靠 `PYTHONPATH` 蹭别的环境的包。曾经因此 `ModuleNotFoundError` |
| 4 | **不要改 `huaien/runtime.py` 的路径清理** | 它挡的是 Hermes 注入的 `PYTHONPATH` 污染。不清就 `ImportError: cannot import name '_imaging' from 'PIL'` |
| 5 | **bat 文件内容必须纯 ASCII** | cmd 按代码页逐行解析, 中文变乱码 -> 脚本死在第一条命令之前。**文件名可以中文** |
| 6 | **不要删目录, 用改名** | `shutil.rmtree` / `Remove-Item -Recurse` 是永久删除, 不进回收站。移到 `_deprecated_<日期>/` |
| 7 | **契约改了要同步改 `config/hermes_contract.json`** | 那是怀恩对 Hermes 的接口声明。上游漂移时它会变红, 而不是静默坏掉 |

## 目录地图

```
huaien/          前台
  app.py           桌面窗口 (Tkinter)
  web.py           手机/局域网 Web 前台 (PWA, 带口令)
  bridge.py        对话桥 -> hermes CLI 流式
  runtime.py       运行时路径清理 (关键, 别动)
  butler.py        主动提醒
scripts/         脚本
  启动怀恩.bat       桌面启动 (带前置自检)
  启动怀恩手机端.bat   局域网 PWA 启动
  selftest.py       三层自检
  gui_drill.py      真实窗口操作测试
  watchdog.py       死亡检测 + 自动拉起
  export_public.py  公开副本导出 + 脱敏
  audit_publish.py  敏感信息审计
  publish_release.py 一键发布
tests/           单元测试 + 契约测试
config/hermes_contract.json   怀恩对 Hermes 的接口契约
profiles/huaien-SOUL.md        人格文件 (拷进 Hermes profile)
docs/            文档
```

## 常用命令

```bash
python scripts/selftest.py             # 三层自检 (环境/对话桥/界面)
python scripts/gui_drill.py            # 前台真实操作 (离线)
python scripts/gui_drill.py --live     # 同上, 真给 Hermes 发消息
python scripts/watchdog.py --status    # gateway / 前台 / web / 记忆 四件事
python scripts/sync_memory.py --check  # 前后台记忆是否对齐
python scripts/db_health.py            # 会话库体检 (只读)
python scripts/preflight.py            # 动 state.db 之前的安全门禁
```

## 发布 (上传组)

开发仓和公开副本**物理隔离**:

```
<开发仓>\                日常开发
      │  scripts/export_public.py --src <开发仓> --out <公开副本>
      ▼
<公开副本>\              中性脱敏副本 -> git 单提交 -> GitHub
```

- 脱敏在**导出时**做, 日常开发**不用**手工清理
- 审计不过就**不推** (`scripts/audit_publish.py`, 退出码非 0 = 致命命中)
- 公开副本每次**重建单提交历史**, 不复用旧历史
- 详见 [上架隔离机制.md](上架隔离机制.md)

## 与 Hermes 的契约

怀恩依赖 Hermes 的这些接口, 全部写在 `config/hermes_contract.json`:

| 类别 | 内容 |
|---|---|
| CLI | `hermes` 及其子命令 / 全局 flag (`-z` `-p` `-m` `--provider`) |
| JSON | `hermes kanban show --json` 的根键 |
| 文件 | `gateway.lock` / `gateway.heartbeat` / `state.db` 的表和列 |

契约测试: `tests/test_hermes_contract.py`。
**上游 Hermes 升级导致接口漂移时, 这里会变红** —— 这是故意的。

## 写给 agent 的话

- 改完东西**必须跑** `python -m pytest` 和 `python -m ruff check .` —— 没跑过的改动等于没改
- Windows 上 `bash` 脚本跑不了, 用 `cmd` 或直接调 Python
- 路径一律用 `pathlib` / `Path.home()`, 不用字符串拼接
- 中文写进 `.py` 文件时, 用 UTF-8 且**不要**在注释里混编不兼容的全角标点 (会破坏 `ast.parse`)
- 有疑问先读 `docs/` 再说, 里面有大量已推翻的结论和事故复盘
