# Hermes 每周备份 —— 修复记录

> 发现时间: 2026-10-30（做怀恩 V0.3 时顺带发现的）
> 原状态: **连续失败 8 周**，最后两次成功是 2026-10-23 和更早。

---

## 一、症状

```
hermes cron list
  de6ce40dc963 [active]
    Name:      Hermes-每周五13:30备份
    Last run:  2026-10-23T13:45:10  error: Script exited with code 1  (7 failures in a row)
    ⚠ The result was not delivered: no delivery target resolved for deliver=all
```

`failure_streak` 一路涨到 8。

---

## 二、根因（三层叠加）

### 1. 打包的东西比预想的大 200 倍

脚本注释写着"体积预期：15-25 MB"，实测要打的是 **4.1 GB / 10.3 万个文件**：

| 目录 | 体积 | 性质 |
|---|---|---|
| `tools` | 1943 MB | 运行时二进制，**不需要备份** |
| `installs` | 1310 MB | 安装产物，可重新下载 |
| `environments` | 202 MB | 运行时 |
| `lsp` | 36 MB | 语言服务器 |
| `profiles/*/skills` | 265 MB | 核心资产，要留 |
| `attachments` / `sessions` / `skills` | 260 MB | 核心资产，要留 |

排除项里只写了 `git/node/bin/cache/...`，**没排除 tools 和 installs**。

### 2. 判据错了：把 tar 退出码 1 当成失败

Hermes 一直在运行，tar 读到一半发现文件变了：

```
tar: hermes: file changed as we read it
```

tar 因此退出 1。**但归档本身是完整的** —— 原脚本 `set -e` + `if [ -f "$OUT" ]` 的写法，
tar 一报错就整个脚本退出，压根走不到"检查文件在不在"那步。

### 3. 交付目标没配

`deliver=all`，但飞书没有 home channel → 每次都报
`no delivery target resolved`，备份结果**从来没送到用户眼前**。

---

## 三、修法

### 1. 排除运行时（4.1 GB → 496 MB）

```
--exclude='hermes/tools'      --exclude='hermes/installs'
--exclude='hermes/environments' --exclude='hermes/lsp'
--exclude='hermes/logs'       --exclude='*.pyc' --exclude='__pycache__'
--exclude='hermes/skills/.hub' --exclude='hermes/profiles/*/skills/.hub'
```

### 2. 判据改成"归档能不能解开"

```bash
if [ -f "$OUT" ] && tar -tzf "$OUT" >/dev/null 2>&1; then   # 能解开 = 成功
```

不再看 tar 的退出码（Hermes 在跑的时候它必然是 1）。

### 3. state.db 做一致性快照

`state.db` 是 SQLite，Hermes 一直在写 → 直接打包会拿到不一致的库。
现在备份前先 `VACUUM INTO` 做快照（带 3 次重试，避开写锁），快照也进归档。

### 4. cron job 改成纯脚本 + 投到飞书

```
no_agent = True                              ← 不再调模型
script   = hermes-weekly-backup.sh
deliver  = feishu:<FEISHU_CHAT_ID>
```

> 顺带说一句：**这个改动也躲开了 401**。
> 当时其它 agent 型 cron job 全在报 `HTTP 401: Missing Authentication header`
> 和 `Request timed out` —— 备份根本不需要模型，改成纯脚本后这类失败直接消失。

---

## 四、实测

| 项 | 结果 |
|---|---|
| 手动跑脚本 | ✅ `Hermes 自动备份成功: hermes-20261030-1404.tar.gz (496M)`，**96 秒** |
| 归档可解压性 | ✅ `tar -tzf` 通过 |
| 一致性快照 | ✅ `state.db.snapshot.db` + `profiles_huaien_state.db.snapshot.db` |
| cron job 端到端 | ✅ `Ran now: succeeded.`（连挂 8 周后首次成功） |
| 下次自动跑 | 2026-11-06 13:30 |

---

## 五、遗留

- **`state.db` 已经 311 MB**（5 万条消息）。不是故障，但会话库会越来越慢。
  想瘦身可以定期归档老会话（`hermes sessions` 有归档/清理功能）。
- **`config.yaml` 的 fallback chain 里还留着 `openrouter`**（用户明确不用它），
  那就是 401 的来源之一。**没动** —— 改全局降级链超出本次范围，单独处理。
- 老的备份产物（1500-1800 MB 那几份）是旧排除项打的，占地方；
  建议保留最近 3-4 份，旧的挪走或删（**需要用户确认后才动**）。
