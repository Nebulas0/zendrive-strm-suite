#!/usr/bin/env bash
set -uo pipefail
LOCK="${STRM_PULL_LOCK:-/tmp/strm-pull.lock}"
LOG="${STRM_PULL_LOG:-/home/plexuser/strm-pull/pull.log}"
DST="${STRM_PULL_DST:-/mnt/local/strm/library}"
MAX_LOG_BYTES=1073741824  # 1 GB

# Sections to sync in parallel (space-separated)
SECTIONS="${STRM_PULL_SECTIONS:-television movies xxx sports courses}"

# Per-section rclone settings
TRANSFERS="${STRM_PULL_TRANSFERS:-32}"
CHECKERS="${STRM_PULL_CHECKERS:-32}"
MAX_DELETE="${STRM_PULL_MAX_DELETE:-50000}"

exec 9>"$LOCK"
if ! flock -n 9; then
  echo "$(date -Is) SKIP: previous pull still running" >>"$LOG"
  exit 0
fi

START=$(date +%s)

# Trim the log to ~1GB at the start of each run.
if [ -f "$LOG" ]; then
  LOG_SIZE=$(stat -c%s "$LOG" 2>/dev/null || echo 0)
  if [ "$LOG_SIZE" -gt "$MAX_LOG_BYTES" ]; then
    tmp=$(mktemp)
    tail -c "$MAX_LOG_BYTES" "$LOG" > "$tmp" 2>/dev/null
    mv "$tmp" "$LOG"
  fi
fi

echo "$(date -Is) START pull (parallel: $SECTIONS)" >>"$LOG"

# Launch each section in parallel
declare -A SECTION_PIDS
for section in $SECTIONS; do
  echo "$(date -Is) Starting section: $section" >>"$LOG"
  /usr/bin/rclone sync "zendrive:strm-tree/$section" "$DST/$section" \
    --ignore-size --fast-list \
    --transfers "$TRANSFERS" --checkers "$CHECKERS" \
    --use-server-modtime \
    --max-delete "$MAX_DELETE" \
    --ignore-errors \
    --exclude "*.partial" \
    --exclude "*.partial.*" \
    --exclude ".recyclebin/**" \
    --exclude ".downloads/**" \
    --exclude ".inbound/**" \
    --retries 3 --low-level-retries 10 \
    --stats 2m --stats-one-line \
    --log-file "$LOG" --log-level INFO &
  SECTION_PIDS[$section]=$!
done

# Wait for each section and report results
FAILED=0
for section in $SECTIONS; do
  wait ${SECTION_PIDS[$section]}
  RC=$?
  if [ $RC -ne 0 ]; then
    echo "$(date -Is) Section $section FAILED (rc=$RC)" >>"$LOG"
    FAILED=1
  else
    echo "$(date -Is) Section $section OK" >>"$LOG"
  fi
done

echo "$(date -Is) END pull rc=$FAILED dur=$(( $(date +%s) - START ))s" >>"$LOG"
