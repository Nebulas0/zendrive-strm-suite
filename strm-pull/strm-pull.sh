#!/usr/bin/env bash
set -uo pipefail
LOCK="${STRM_PULL_LOCK:-/tmp/strm-pull.lock}"
LOG="${STRM_PULL_LOG:-/home/plexuser/strm-pull/pull.log}"
DST="${STRM_PULL_DST:-/mnt/local/strm/library}"
MAX_LOG_BYTES=1073741824  # 1 GB

# Sections that are synced as a single rclone sync (no split)
SIMPLE_SECTIONS="${STRM_PULL_SIMPLE_SECTIONS:-xxx sports courses}"

# Sections that are split into subdirectory syncs for parallelism
SPLIT_TELEVISION="${STRM_PULL_SPLIT_TELEVISION:-00s 10s 20s 4k 4k-dv 70s 80s 90s alt-cuts anime anime-dub int int-dubbed}"
SPLIT_MOVIES="${STRM_PULL_SPLIT_MOVIES:-00s 10s 20s 3d 4k 4k-dv 70s 80s 90s alt-cuts anime anime-dub int}"

# Per-subdir rclone settings
TRANSFERS="${STRM_PULL_TRANSFERS:-16}"
CHECKERS="${STRM_PULL_CHECKERS:-64}"
MAX_DELETE="${STRM_PULL_MAX_DELETE:-50000}"

# Max concurrent rclone processes (across all split subdirs)
MAX_CONCURRENT="${STRM_PULL_MAX_CONCURRENT:-8}"

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

# Build the list of all sync jobs: each job is "section/subdir" or just "section"
JOBS=()

# Simple sections (no split)
for section in $SIMPLE_SECTIONS; do
  JOBS+=("$section")
done

# Split sections
for subdir in $SPLIT_TELEVISION; do
  JOBS+=("television/$subdir")
done
for subdir in $SPLIT_MOVIES; do
  JOBS+=("movies/$subdir")
done

TOTAL_JOBS=${#JOBS[@]}
echo "$(date -Is) START pull ($TOTAL_JOBS jobs, max $MAX_CONCURRENT concurrent)" >>"$LOG"

# Function to run a single sync job
run_sync() {
  local job="$1"
  local label="$job"

  # Create a FIFO for this job's rclone log
  local fifo
  fifo="/tmp/rclone-log-$(echo "$job" | tr '/' '-')-$$"
  mkfifo "$fifo"

  # Background: read from FIFO, prefix each line with [label], append to shared log
  sed -u "s/^/[$label] /" < "$fifo" >> "$LOG" &
  local sed_pid=$!

  # Determine remote and local paths
  local remote_src local_dst
  remote_src="zendrive:strm-tree/$job"
  local_dst="$DST/$job"

  # Run rclone sync
  /usr/bin/rclone sync "$remote_src" "$local_dst" \
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
    --retries 1 --low-level-retries 3 \
    --stats 2m --stats-one-line \
    --log-file "$fifo" --log-level INFO
  local rc=$?

  # Wait for the sed process to finish flushing
  wait "$sed_pid" 2>/dev/null
  rm -f "$fifo"

  if [ $rc -ne 0 ]; then
    echo "$(date -Is) [$label] FAILED (rc=$rc)" >>"$LOG"
  else
    echo "$(date -Is) [$label] OK" >>"$LOG"
  fi

  return $rc
}

# Run jobs with concurrency limit
# Use a simple job control approach that works with set -u
FAILED=0
COMPLETED=0
RUNNING=0

for job in "${JOBS[@]}"; do
  # Wait if at max concurrency
  while [ "$RUNNING" -ge "$MAX_CONCURRENT" ]; do
    wait -n 2>/dev/null
    rc=$?
    RUNNING=$((RUNNING - 1))
    COMPLETED=$((COMPLETED + 1))
    if [ $rc -ne 0 ]; then
      FAILED=1
    fi
  done

  echo "$(date -Is) Starting: $job" >>"$LOG"
  run_sync "$job" &
  RUNNING=$((RUNNING + 1))
done

# Wait for all remaining jobs
while [ "$RUNNING" -gt 0 ]; do
  wait -n 2>/dev/null
  rc=$?
  RUNNING=$((RUNNING - 1))
  COMPLETED=$((COMPLETED + 1))
  if [ $rc -ne 0 ]; then
    FAILED=1
  fi
done

echo "$(date -Is) END pull rc=$FAILED dur=$(( $(date +%s) - START ))s jobs=$TOTAL_JOBS" >>"$LOG"
