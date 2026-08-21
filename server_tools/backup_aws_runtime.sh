#!/usr/bin/env bash
set -Eeuo pipefail

SRC="$HOME/noramu-backtest"
WORK="$HOME/noramu-github-backup"
ARCHIVE_REL="archive/aws-runtime-backup-20260821"
DST="$WORK/$ARCHIVE_REL"
PRIVATE="$HOME/noramu-private-skipped-20260821"
LOG="$HOME/noramu_backup.log"
SUCCESS="$HOME/NORAMU_GITHUB_BACKUP_SUCCESS"

exec > >(tee -a "$LOG") 2>&1

echo "===== NORAMU AWS BACKUP START $(date -Is) ====="
rm -f "$SUCCESS"

if [ ! -d "$SRC" ]; then
  echo "ERROR: $SRC not found"
  exit 1
fi

if crontab -l 2>/dev/null | grep -q 'US_FROZEN_V1_AUTO'; then
  echo "ERROR: trading cron still exists. Abort."
  exit 2
fi

if pgrep -af 'toss_us_|run_us_frozen|sanggu_live|fast_rebound|rsi_live_shadow' | grep -v 'backup_aws_runtime' >/dev/null; then
  echo "ERROR: trading process still running. Abort."
  pgrep -af 'toss_us_|run_us_frozen|sanggu_live|fast_rebound|rsi_live_shadow' || true
  exit 3
fi

echo "[1/6] Fresh clone"
rm -rf "$WORK"
git clone https://github.com/irotomokor-jpg/noramu-backtest.git "$WORK"
cd "$WORK"
git checkout main
mkdir -p "$DST" "$PRIVATE"
rm -rf "$DST"/*

echo "[2/6] Select and copy AWS-only source/config/small result files"
SRC="$SRC" DST="$DST" PRIVATE="$PRIVATE" python3 - <<'PY'
from pathlib import Path
import os, shutil, re

src = Path(os.environ['SRC']).resolve()
dst = Path(os.environ['DST']).resolve()
private = Path(os.environ['PRIVATE']).resolve()

skip_dirs = {'.git', '.venv', 'toss_replay_cache', '__pycache__', '.pytest_cache', '.mypy_cache'}
allowed = {'.py','.sh','.json','.md','.txt','.yaml','.yml','.toml','.css','.ini','.cfg','.csv'}
never_names = {'.env','.env.local','.env.production'}
secret_ext = {'.pem','.key','.p12','.pfx'}
secret_re = re.compile(r'''(?ix)
(?:api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret|private[_-]?key|authorization)
\s*[:=]\s*["'][^"']{8,}["']
''')

copied=[]; sensitive=[]; large=[]; ignored=[]
for p in src.rglob('*'):
    if not p.is_file():
        continue
    rel = p.relative_to(src)
    if any(part in skip_dirs for part in rel.parts):
        continue
    if p.name in never_names or p.suffix.lower() in secret_ext:
        sensitive.append((rel, 'secret-filetype'))
        target = private / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, target)
        continue
    ext = p.suffix.lower()
    if ext not in allowed:
        ignored.append((rel, 'extension'))
        continue
    size = p.stat().st_size
    limit = 2*1024*1024 if ext == '.csv' else 10*1024*1024
    if size > limit:
        large.append((rel, size))
        continue
    try:
        text = p.read_text(encoding='utf-8', errors='ignore')
    except Exception:
        text = ''
    if secret_re.search(text):
        sensitive.append((rel, 'literal-secret-pattern'))
        target = private / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, target)
        continue
    target = dst / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(p, target)
    copied.append((rel, size))

(dst/'BACKUP_MANIFEST.tsv').write_text(
    'path\tsize_bytes\n' + ''.join(f'{r}\t{s}\n' for r,s in copied), encoding='utf-8')
(dst/'SKIPPED_LARGE.tsv').write_text(
    'path\tsize_bytes\n' + ''.join(f'{r}\t{s}\n' for r,s in large), encoding='utf-8')
(dst/'SENSITIVE_NOT_UPLOADED.tsv').write_text(
    'path\treason\n' + ''.join(f'{r}\t{reason}\n' for r,reason in sensitive), encoding='utf-8')
(dst/'BACKUP_README.md').write_text('''# AWS auto-trading runtime backup\n\nCreated before repurposing the AWS instance for Transit PWA.\n\nIncluded: Python/shell/config/docs and small result summaries from the AWS runtime.\n\nIntentionally excluded from GitHub: `.env`, private keys, files containing likely literal secrets, virtualenvs, caches, logs, DB/binary files, and large generated/replay datasets. The ~11 GB `toss_replay_cache` is reproducible and intentionally excluded.\n\nPossible sensitive files skipped from GitHub are preserved temporarily on the AWS host under `~/noramu-private-skipped-20260821` until migration is complete.\n''', encoding='utf-8')
print(f'copied_files={len(copied)}')
print(f'sensitive_skipped={len(sensitive)}')
print(f'large_skipped={len(large)}')
print(f'private_preserve={private}')
PY

echo "[3/6] Add saved crontab if present"
if [ -f "$HOME/noramu_crontab_backup_20260821.txt" ]; then
  cp "$HOME/noramu_crontab_backup_20260821.txt" "$DST/CRONTAB_BACKUP.txt"
fi

echo "[4/6] Backup size / oversized check"
du -sh "$DST"
find "$DST" -type f -size +50M -print -delete

echo "[5/6] Commit"
git config user.name "aws-backup"
git config user.email "aws-backup@users.noreply.github.com"
git add "$ARCHIVE_REL"
if git diff --cached --quiet; then
  echo "Nothing new to commit."
else
  git commit -m "Backup AWS auto-trading runtime before Transit migration"
fi

echo "[6/6] Push"
if ! git push origin main; then
  echo "PUSH_FAILED: backup remains safe on AWS at $DST and original remains at $SRC"
  echo "Likely GitHub write authentication is missing on this AWS host."
  exit 10
fi

printf '%s\n' "$(date -Is)" > "$SUCCESS"
echo "===== GITHUB BACKUP PUSH COMPLETED ====="
git log -1 --oneline
du -sh "$DST"
echo "success_marker=$SUCCESS"
echo "===== NORAMU AWS BACKUP END $(date -Is) ====="
