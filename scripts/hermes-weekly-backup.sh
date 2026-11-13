#!/usr/bin/env bash
# hermes-weekly-backup.sh
# 每周自动备份 Hermes 的 配置/记忆/技能/会话/宠物, 不带运行时。
# 输出：$SNAP_DIR/hermes-YYYYMMDD-HHMM.tar.gz
#
# 2026-10-30 修:
#   原版排除项太少, 实际要打包 4.1 GB / 10.3 万文件 (tools 1943 MB +
#   installs 1310 MB + environments 202 MB + lsp 36 MB 全是运行时/安装产物),
#   而脚本注释写着"预期 15-25 MB" —— 差 200 倍, 结果每次都跑不完/超时,
#   连续 7 周备份失败。
#   现在排除运行时, 保留真正需要恢复的东西。
#
# 保留: config.yaml .env auth.json state.db memories/ skills/ profiles/*/ skills
#       sessions/ attachments/ cron/ plugins/ pets/ vault/
# 排除: 运行时二进制 / 安装产物 / 语言服务器 / 日志 / 索引缓存 / __pycache__

set -uo pipefail

TS=$(date +%Y%m%d-%H%M)
OUT="${OUT_DIR}/hermes-${TS}.tar.gz"
SRC="${HOME}/AppData/Local"

mkdir -p "$OUT_DIR"
cd "$SRC" || { echo "❌ 进进去 $SRC"; exit 1; }

# state.db 是 SQLite, Hermes 一直在写它 —— 直接 tar 会拿到不一致的库。
# 先用 VACUUM INTO 做一份一致性快照, 把快照放进归档, 原库跳过。
PY="${HERMES_PYTHON:-${HOME}/AppData/Local/hermes/hermes-agent/venv/Scripts/python.exe}"
SNAP_DIR="${SNAP_DIR:-$HOME/.hermes-snapshots}"
OUT_DIR="${OUT_DIR:-$HOME/.hermes-backups}"
mkdir -p "$SNAP_DIR"
for db in "${HOME}/AppData/Local/hermes/state.db" "${HOME}/AppData/Local/hermes/profiles/huaien/state.db"; do
  [ -f "$db" ] || continue
  tag=$(echo "$db" | sed "s|${HOME}/AppData/Local/hermes/||; s|/|_|g")
  out="$SNAP_DIR/${tag}.snapshot.db"
  # VACUUM INTO 要读锁, Hermes 正在写库时会失败 -> 重试 3 次
  for i in 1 2 3; do
    if "$PY" -c "
import sqlite3, sys
src, out = sys.argv[1], sys.argv[2]
con = sqlite3.connect(src, timeout=20)
con.execute('VACUUM INTO ?', (out,))
con.close()
" "$db" "$out" 2>/dev/null; then
      break
    fi
    sleep 4
  done
  [ -f "$out" ] || echo "  (快照失败, 继续: $db)" >&2
done

tar -czf "$OUT" \
  --exclude='hermes/git' \
  --exclude='hermes/node' \
  --exclude='hermes/bin' \
  --exclude='hermes/bootstrap-cache' \
  --exclude='hermes/cache' \
  --exclude='hermes/audio_cache' \
  --exclude='hermes/image_cache' \
  --exclude='hermes/hermes-setup.exe' \
  --exclude='hermes/hermes-agent' \
  --exclude='hermes/tools' \
  --exclude='hermes/installs' \
  --exclude='hermes/environments' \
  --exclude='hermes/lsp' \
  --exclude='hermes/logs' \
  --exclude='hermes/.curator_backups' \
  --exclude='hermes/plugins-checkpoint-update' \
  --exclude='hermes/runtime' \
  --exclude='*.pyc' \
  --exclude='__pycache__' \
  --exclude='*/__pycache__/*' \
  --exclude='hermes/skills/.hub' \
  --exclude='hermes/profiles/*/skills/.hub' \
  --exclude='hermes/profiles/*/skills/.curator_backups' \
  --exclude='hermes/profiles/*/cache' \
  --exclude='hermes/profiles/*/logs' \
  --warning=no-file-changed \
  hermes 2>/tmp/hermes-backup-tar.err
TAR_RC=$?

# ⚠️ 判据不看 tar 的退出码 —— Hermes 一直在跑, tar 读到一半发现某个文件变了
#    就会退出 1, 但**归档本身是完整的** (之前就是被这个退出码判成失败,
#    连续 7 周没备份成功)。真正的判据是"归档能不能解开"。
if [ $TAR_RC -ne 0 ] && [ -f "$OUT" ]; then
  # 这句走 stderr —— stdout 是要投给用户的, 别拿技术细节打扰他
  echo "  (tar 退出码 $TAR_RC, 但归档已生成 —— 用可解压性判定)" >&2
  head -5 /tmp/hermes-backup-tar.err 2>/dev/null | sed 's/^/  /' >&2
fi

if [ -f "$OUT" ] && tar -tzf "$OUT" >/dev/null 2>&1; then
  SIZE=$(du -sh "$OUT" | cut -f1)
  echo "✅ Hermes 自动备份成功: $OUT ($SIZE)"
  "$PY" -c "
import glob, os
fs = sorted(glob.glob(os.path.expanduser('~/.hermes-snapshots/hermes-2*.tar.gz')), key=os.path.getmtime, reverse=True)[:3]
for f in fs:
    print('  -', f, f'({os.path.getsize(f)//1024//1024} MB)')
" 2>/dev/null || true
  # 把最近一份的清单也留下, 方便日后核对恢复了什么
  tar -tzf "$OUT" 2>/dev/null | head -50 > "${OUT%.tar.gz}.清单.txt" || true
  # 一致性快照单独留一份在盘上 (恢复时直接用, 不用再解包)
  cp -f "$SNAP_DIR"/*.snapshot.db "$SNAP_DIR/" 2>/dev/null || true
  exit 0
else
  echo "❌ Hermes 自动备份失败: 归档没生成或解不开 (tar 退出码 $TAR_RC)"
  echo "--- tar stderr (前 20 行) ---"
  head -20 /tmp/hermes-backup-tar.err 2>/dev/null
  exit 1
fi
