# 更新日志

本项目遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [0.1.2] - 2026-10-30

### 修复

- **看门狗前台探针自匹配 (静默失效)**: `scripts/watchdog.py` 的 `frontend_alive()`
  原先用 `Where-Object { $_.CommandLine -match 'huaien.app' }` 统计进程数, 但探测
  命令**自身的** PowerShell 进程命令行里就含 `huaien.app` —— 自己匹配自己,
  计数恒 >= 1。**前台进程全死时看门狗仍报 "frontend: alive"**, 于是永远不会
  去拉起前台。
  2026-10-30 回归看门狗实测: 前台真实进程 0 个, 旧探针计数 5 (4 个是探测链自身)。
  改为三重过滤: 只认 `python/pythonw` 进程 + 只认 `-m huaien.app` 模块调用 +
  排除含 `Get-CimInstance` 的探针自身。

### 新增

- **看门狗单测** (`tests/test_watchdog.py`): 11 条, 覆盖探针三条守卫、探针自身排除、
  以及"把旧坏探针拿出来必须被判死"的元测试 (保证回归测试本身有效)。
  测法: 把过滤条件当 PowerShell scriptblock 喂假进程, 用真 PowerShell 语义判定。

### 验证

- 杀掉前台后 `watchdog --status` 正确报 `frontend: DOWN`;
  `watchdog --once` 成功拉起 (新 PID 实测)。
- 把探针改回旧写法后, 新单测 5 条红 —— 确认测试有牙齿。
- `pytest tests/` 18 条全绿 + `ruff check` 全绿。

## [0.3.0] - 2026-10-30

### 新增

- **`huaien/web.py` 手机端 PWA** —— 同一个怀恩的网页版, 手机连同一 WiFi 打开,
  "添加到主屏幕"即原生 App。16 位随机口令保护。
- **`scripts/gui_drill.py` 前台真实操作测试器** —— 走事件层 (鼠标/键盘),
  断言只看屏幕上的文字、输入框内容、截图像素。11 个场景, 离线 + live 两种模式。
- **`scripts/sync_memory.py` 前后台记忆双向补齐** —— 条目级, 只补不删, 幂等, 带上限感知。
- **`scripts/package_version.py` 版本封存打包** —— 生成 `versions/Vx.y.z/` 快照 + 源码 zip。
- **产物协议** —— agent 用 `[[FILE:路径]]` / `[[IMG:路径]]` 标注产出, 前台渲染成可双击的一行。
- **状态行** —— 思考时一行灰字, 8 秒后显示"还在想 · N 秒"。不展开成面板。
- **`hermes send` 主动汇报入口** —— 复用 gateway 凭据, 能发文字/文件/图片。

### 修复

- **`memory.write_approval` 卡死写入** —— 无人值守模式没人能批准, agent 写不进去。
  已关 (只关 huaien profile, default 保持不变)。
- **记忆超限** —— `MEMORY.md 2780 > limit 2200`, 超限后所有后续写入被拒。
  两边 limit 提到 4000 / 2500。
- **消息被静默丢弃** —— `bridge.busy` 时用户的话凭空消失。现在明确提示"还在处理上一条"。
- **reset 没清干净** —— Esc 之后还留着上一轮的错误提示/产物/计时。
- **两个省略号叠加** —— reply 区显示 `…`, 状态行也显示 `…`。
- **测试拿到旧回复** —— `wait_reply` 只看非空, 且没等 bridge 空闲。已改。
- **S10 假 FAIL** —— 断言落在"它说完了"上, 应落在产物上。已改。

### 文档

- `docs/V0.3-能力方案.md` —— 设计 → 批判性审核 → 执行
- `docs/能力测试报告.md` —— 前台真实操作测试的完整记录
- `docs/外部通道-配置指南.md` —— 飞书/微信/局域网 PWA/公网四条路
- `docs/架构-前后台边界.md` —— 模型/工具/记忆/会话逐项对照表

## [0.2.0] - 2026-10-30

### 修复 —— 用户报"无法输入字、看不真切字"

| # | 症状 | 根因 | 修法 |
|---|---|---|---|
| 1 | **中文打不出来** | 输入层是 Canvas + `<Key>` 手工模拟键盘 (`e.char`)。IME 组合时 `e.char` 为空，Tk 拿不到组合结果 | 输入框换成**原生 `tk.Text`**（走 Windows IME），手工键盘层彻底删除 |
| 2 | 光标位置不准 | 自己按字符宽度估算 | 用控件自带光标（`insertbackground` = 强调色），位置由 Tk 自己算 |
| 3 | 焦点不在输入框 | `focus_set()` 只在 `run()` 里调 | `__init__` 里就设 + 150ms 后再设一次 |
| 4 | **字看不清** | 字号 15/19/10pt（96 DPI 下 10pt 行高仅 18px） | 提到 17/21/12pt |
| 5 | **字看不清** | `FG_FAINT` #4A4A4A（2.37:1）、`ACCENT_DIM` #2F5D4C（2.79:1） | 提到 #7A7A7A（4.89:1）、#4E8C74（5.33:1） |
| 6 | 输入框不随内容长高 | `<<TextChanged>>` 在本机 Tk 8.6.12 **根本不触发** | 改状态比对轮询（`_tick` 每 45ms 查一次） |
| 7 | 输入框永远 1 行高 | `_set_entry_text` 先写缓存 → 状态比对认为"没变"→ 提前返回 | 加 `force=True` 分支 |
| 8 | 横线跑到输入框下面 | `side="bottom"` 时先 pack 的在更下面 | 用容器 frame 统一装横线+输入框 |
| 9 | 回复被拆成一行几个字 | Tk Canvas 自动换行对中文不可靠（只认空格断点） | 自己按 `tkfont.measure()` 精确换行，中文按字、英文按词 |
| 10 | 宽度算成负数 | `winfo_width() or WINDOW_W` —— 未映射时返回 **1**（truthy！） | 显式判 `<= 1` 才算无效 |

### 新增 —— 前后台记忆打通

- `scripts/sync_memory.py`：前台 `huaien` profile ↔ 后台 `default` profile 的
  `MEMORY.md` / `USER.md` **条目级双向补齐**（只补不删，幂等）。
- 看门狗每次检查顺带同步一次；`--status` 会报 `memory: in sync`。
- 为什么不能直接共用 profile：Hermes 没有"换人格但共享记忆"的开关，
  `--ignore-rules` 会把 memory 一起跳过。

### 文档

- `docs/架构-前后台边界.md`：模型/工具/记忆/会话/看板/cron/凭据逐项对照表，
  回答"是另做的还是用 Hermes 的""Hermes 里能不能看到"。

### 验证

- `pytest` 29/29 · `ruff` 0 问题 · 三层自检 3/3（连跑 2 次）
- 交互测试 13 项全过（焦点/ASCII 按键/IME 提交/退格/Shift+Enter/Enter 发送/
  占位提示/高度自适应/字号/无边框/光标色）
- 记忆同步 8/8（造差异→双向补齐→收敛→幂等→不丢东西→还原）

## [0.1.1] - 2026-10-29

### 修复

- **窗口居中**: Tkinter 在窗口未映射时 `winfo_screenwidth()` 返回 1, 窗口会停在 (0,0)。
  改用 Win32 `GetSystemMetrics(0/1)` 取真实屏幕尺寸。
- **居中引发的空窗回归**: 二次校准里重复 `lift()` + `topmost` 会打断绘制, 画面被清空。
  拆成 `_reposition()`, 只改坐标不动 z 序。
- **自检判据错误**: 原判据"截图 > 5000 B"对短回复会误报失败 (4.4 KB 也被判失败)。
  改为数**非黑像素占比** (<0.1% 才算空窗口), 阈值不再靠猜。

## [0.1.0] - 2026-10-29

首个版本。

### 新增

- **前台界面** (`huaien/app.py`): 纯黑科技风单对话框, 无标题栏无菜单无按钮,
  逐字打字机效果, 全中文, 用户消息(灰) / 管家回复(白大) 分层显示。
- **对话桥** (`huaien/bridge.py`): 通过 `hermes -p huaien chat --format stream-json`
  拿到逐字流式回复, 用 `--resume` 维持多轮上下文。含独立自检入口。
- **管家人格** (`persona/SOUL.md`): 独立 Hermes profile `huaien`,
  话不多、不用 Markdown 标记、不附加自检行、不主动展示后台过程。
- **主题** (`huaien/theme.py`): 配色 / 字体 / 尺寸集中一处。
- **看门狗** (`scripts/watchdog.py`): 检查前台进程与 Hermes gateway 存活, 必要时拉起。
- **一键启动** (`scripts/启动怀恩.bat`): 双击即用, 带自检输出, 失败可见。

### 已知限制

- 前台每次发消息起一个独立 Hermes 进程 (约 2-4 秒启动开销), 靠 `--resume` 续接上下文。
- 前台与后台工程 profile 记忆隔离 (设计如此: 管家归管家, 工程归工程)。
