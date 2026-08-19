#!/bin/sh
set -eu

: "${START_DELAY_SECONDS:=15}"
: "${DIR_TIMEOUT_SECONDS:=1800}"
: "${MAX_PARALLEL_REFRESHES:=3}"
: "${SKIP_IF_CACHED_DIRS:=1000}"
: "${STATE_DIR:=/state/precache}"
: "${WATCHDOG_STAGNATION_SECONDS:=600}"
: "${WATCHDOG_CHECK_INTERVAL_SECONDS:=30}"
: "${KEEP_ALIVE:=true}"
: "${REFRESH_INTERVAL_SECONDS:=0}"  # 0 = run once; >0 = loop forever
: "${MOUNT_SPECS:=movies:5572:/mnt/remote/zendrive/movies
television:5573:/mnt/remote/zendrive/television
sports:5574:/mnt/remote/zendrive/sports
xxx:5575:/mnt/remote/zendrive/xxx
courses:5576:/mnt/remote/zendrive/courses
movies-int:5577:/mnt/remote/zendrive/movies-int
television-int:5578:/mnt/remote/zendrive/television-int}"

log() {
  printf '%s %s\n' "$(date -Is)" "$*"
}

json_escape() {
  printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g'
}

rc_post() {
  port="$1"
  endpoint="$2"
  payload="$3"
  wget -q -T 30 \
    --header='Content-Type: application/json' \
    --post-data="$payload" \
    -O - "http://127.0.0.1:${port}/${endpoint}"
}

wait_for_rc() {
  name="$1"
  port="$2"
  tries=0
  while :; do
    if rc_post "$port" rc/noop '{}' >/dev/null 2>&1; then
      log "mount=${name} rc=ready port=${port}"
      return 0
    fi
    tries=$((tries + 1))
    if [ "$tries" -ge 60 ]; then
      log "mount=${name} rc=timeout port=${port}"
      return 1
    fi
    sleep 2
  done
}

get_vfs_counts() {
  port="$1"
  stats="$(wget -q -T 10 --header='Content-Type: application/json' \
    --post-data='{}' -O - "http://127.0.0.1:${port}/vfs/stats" 2>/dev/null)" || return 1
  dirs="$(printf '%s' "$stats" | sed -n 's/.*"dirs": *\([0-9]*\).*/\1/p' | head -1)"
  files="$(printf '%s' "$stats" | sed -n 's/.*"files": *\([0-9]*\).*/\1/p' | tail -1)"
  printf '%s %s' "${dirs:-0}" "${files:-0}"
}

refresh_dir() {
  port="$1"
  dir="$2"
  recursive="${3:-true}"
  mount_name="${4:-unknown}"
  escaped_dir="$(json_escape "$dir")"
  payload="{\"dir\":\"${escaped_dir}\",\"recursive\":\"${recursive}\"}"

  if [ "$recursive" = "false" ]; then
    # Non-recursive: simple timeout
    timeout "$DIR_TIMEOUT_SECONDS" sh -c '
      port="$1"
      payload="$2"
      wget -q -T "$DIR_TIMEOUT_SECONDS" \
        --header="Content-Type: application/json" \
        --post-data="$payload" \
        -O - "http://127.0.0.1:${port}/vfs/refresh"
    ' sh "$port" "$payload" >/dev/null 2>&1
    return $?
  fi

  # Recursive: use watchdog approach
  # Start the vfs/refresh in background
  wget -q -T 86400 \
    --header="Content-Type: application/json" \
    --post-data="$payload" \
    -O - "http://127.0.0.1:${port}/vfs/refresh" >/dev/null 2>&1 &
  refresh_pid=$!

  # Watchdog: monitor VFS stats for progress
  # If wget process is still alive, rclone is still processing the request
  # Only kill if process is alive AND no VFS stats change for WATCHDOG_STAGNATION_SECONDS
  # This handles both: new entries being added (stats increase) and re-validation (stats same but process alive)
  current="$(get_vfs_counts "$port")" || current="0 0"
  last_dirs="${current%% *}"
  last_files="${current##* }"
  last_progress_time="$(date +%s)"
  start_time="$last_progress_time"

  while kill -0 "$refresh_pid" 2>/dev/null; do
    sleep "$WATCHDOG_CHECK_INTERVAL_SECONDS"
    current="$(get_vfs_counts "$port")" || current="0 0"
    cur_dirs="${current%% *}"
    cur_files="${current##* }"
    now="$(date +%s)"
    elapsed=$((now - start_time))

    if [ "$cur_dirs" -gt "$last_dirs" ] || [ "$cur_files" -gt "$last_files" ]; then
      delta_dirs=$((cur_dirs - last_dirs))
      delta_files=$((cur_files - last_files))
      log "mount=${mount_name} status=watchdog dir=\"${dir}\" dirs=${cur_dirs} files=${cur_files} +dirs=${delta_dirs} +files=${delta_files} elapsed=${elapsed}s"
      last_dirs="$cur_dirs"
      last_files="$cur_files"
      last_progress_time="$now"
    else
      stagnation=$((now - last_progress_time))
      # Log a heartbeat every check interval so the user can see the refresh is still running
      log "mount=${mount_name} status=watchdog-heartbeat dir=\"${dir}\" dirs=${cur_dirs} files=${cur_files} elapsed=${elapsed}s stagnation=${stagnation}s"
      if [ "$stagnation" -ge "$WATCHDOG_STAGNATION_SECONDS" ]; then
        # Process is still alive but no stats change — could be re-validating existing entries
        # Log it but don't kill; the process will exit on its own when rclone finishes
        log "mount=${mount_name} status=watchdog-stale dir=\"${dir}\" stagnation=${stagnation}s dirs=${cur_dirs} files=${cur_files} elapsed=${elapsed}s (process still alive, waiting)"
        # Reset progress time so we don't spam this every interval
        last_progress_time="$now"
      fi
    fi
  done

  # wget process has exited — check exit status
  wait "$refresh_pid" 2>/dev/null
  rc=$?
  elapsed_total=$(($(date +%s) - start_time))
  log "mount=${mount_name} status=watchdog-done dir=\"${dir}\" rc=${rc} dirs=${cur_dirs:-?} files=${cur_files:-?} elapsed=${elapsed_total}s"
  return $rc
}

should_skip_dir() {
  mount_name="$1"
  top_dir="$2"
  case "${mount_name}:${top_dir}" in
    movies:int|television:int) return 0 ;;
    *) return 1 ;;
  esac
}

wait_for_slot() {
  while [ "$running" -ge "$MAX_PARALLEL_REFRESHES" ]; do
    if wait -n; then
      completed=$((completed + 1))
    else
      completed=$((completed + 1))
      failed=$((failed + 1))
    fi
    running=$((running - 1))
    elapsed=$(($(date +%s) - mount_start))
    remaining=$((total_to_refresh - completed))
    if [ "$completed" -gt 0 ]; then
      eta=$((elapsed * remaining / completed))
    else
      eta=0
    fi
    log "mount=${name} status=progress completed=${completed}/${total_to_refresh} running=${running} failed=${failed} elapsed=${elapsed}s eta=${eta}s"
  done
}

is_mount_cached() {
  port="$1"
  stats="$(wget -q -T 10 --header='Content-Type: application/json' \
    --post-data='{}' -O - "http://127.0.0.1:${port}/vfs/stats" 2>/dev/null)" || return 1
  dirs="$(printf '%s' "$stats" | sed -n 's/.*"dirs": *\([0-9]*\).*/\1/p' | head -1)"
  [ -n "$dirs" ] || return 1
  [ "$dirs" -ge "${SKIP_IF_CACHED_DIRS}" ]
}

warm_mount() {
  name="$1"
  port="$2"
  path="$3"

  log "mount=${name} status=starting path=${path} port=${port} parallel=${MAX_PARALLEL_REFRESHES} recursive=true"
  if ! wait_for_rc "$name" "$port"; then
    log "mount=${name} status=skipped reason=rc-not-ready"
    return 1
  fi

  if [ ! -d "$path" ]; then
    log "mount=${name} status=skipped reason=path-missing path=${path}"
    return 1
  fi

  if is_mount_cached "$port"; then
    dirs="$(wget -q -T 10 --header='Content-Type: application/json' \
      --post-data='{}' -O - "http://127.0.0.1:${port}/vfs/stats" 2>/dev/null \
      | sed -n 's/.*"dirs": *\([0-9]*\).*/\1/p' | head -1)"
    files="$(wget -q -T 10 --header='Content-Type: application/json' \
      --post-data='{}' -O - "http://127.0.0.1:${port}/vfs/stats" 2>/dev/null \
      | sed -n 's/.*"files": *\([0-9]*\).*/\1/p' | tail -1)"
    log "mount=${name} status=already-cached dirs=${dirs} files=${files} checking-for-updates"

    # Do a quick root refresh to detect new top-level directories
    rc_post "$port" vfs/refresh '{"recursive":"false"}' >/dev/null 2>&1 || true

    # Compare current top-level dirs with saved state
    state_file="${STATE_DIR}/${name}.dirs"
    mkdir -p "$STATE_DIR"
    current_file="/tmp/rclone-precache-current-${name}.$$"
    find "$path" -mindepth 1 -maxdepth 1 -type d -print | sort >"$current_file"

    new_dirs_file="/tmp/rclone-precache-newdirs-${name}.$$"
    : >"$new_dirs_file"

    # Detect rclone restart: compare the mount point's mtime with the saved value.
    # When rclone remounts, the mount point's mtime updates to the current time.
    # If the mount is newer than our last refresh, rclone was restarted and its
    # in-memory VFS cache was lost — force a full refresh.
    mounttime_file="${STATE_DIR}/${name}.mounttime"
    rclone_restarted=0
    current_mount_mtime="$(stat -c %Y "$path" 2>/dev/null || echo 0)"
    if [ -f "$mounttime_file" ]; then
      saved_mount_mtime="$(cat "$mounttime_file" 2>/dev/null || echo 0)"
      if [ -n "$saved_mount_mtime" ] && [ "$saved_mount_mtime" -gt 0 ] 2>/dev/null; then
        if [ "$current_mount_mtime" -gt "$saved_mount_mtime" ]; then
          log "mount=${name} status=rclone-restart-detected mount_mtime=${current_mount_mtime} saved_mtime=${saved_mount_mtime} forcing-full-refresh"
          rclone_restarted=1
        fi
      fi
    fi

    if [ "$rclone_restarted" = "1" ]; then
      # Rclone was restarted — force full refresh of all directories
      cp "$current_file" "$new_dirs_file"
      has_state=0
    elif [ -f "$state_file" ]; then
      # State file exists = previous full recursive refresh was done
      # Only need to check for new top-level directories
      comm -13 "$state_file" "$current_file" >"$new_dirs_file"
      has_state=1
    else
      # No state file = never done full recursive refresh before
      # Need to recursively refresh ALL directories to populate VFS cache
      cp "$current_file" "$new_dirs_file"
      has_state=0
    fi

    new_count="$(wc -l <"$new_dirs_file" | tr -d ' ')"
    if [ "$new_count" = "0" ]; then
      log "mount=${name} status=up-to-date no-new-directories"
      # Save current state for next run (before deleting temp files)
      cp "$current_file" "$state_file" 2>/dev/null || true
      # Save mount mtime for rclone-restart detection
      echo "${current_mount_mtime}" > "$mounttime_file" 2>/dev/null || true
      rm -f "$current_file" "$new_dirs_file"
      return 0
    fi

    if [ "$has_state" = "1" ]; then
      log "mount=${name} status=found-new-directories count=${new_count} recursive=true mode=update"
    else
      log "mount=${name} status=found-new-directories count=${new_count} recursive=true mode=full-population"
    fi

    # Refresh only the new directories recursively
    refresh_file="/tmp/rclone-precache-refresh-${name}.$$"
    : >"$refresh_file"
    while IFS= read -r abs_dir; do
      [ -n "$abs_dir" ] || continue
      dir="${abs_dir#$path/}"
      if should_skip_dir "$name" "$dir"; then
        continue
      fi
      printf '%s\n' "$dir" >>"$refresh_file"
    done <"$new_dirs_file"

    total_to_refresh="$(wc -l <"$refresh_file" | tr -d ' ')"
    if [ "$total_to_refresh" = "0" ]; then
      log "mount=${name} status=no-new-directories-to-refresh"
      cp "$current_file" "$state_file" 2>/dev/null || true
      echo "${current_mount_mtime}" > "$mounttime_file" 2>/dev/null || true
      rm -f "$current_file" "$new_dirs_file" "$refresh_file"
      return 0
    fi

    # Save current state for next run
    cp "$current_file" "$state_file"
    rm -f "$current_file" "$new_dirs_file"
    # Save mount mtime for rclone-restart detection
    echo "${current_mount_mtime}" > "$mounttime_file" 2>/dev/null || true

    # Fall through to parallel refresh of new directories only
    mount_start="$(date +%s)"
    queued=0
    completed=0
    failed=0
    running=0

    while IFS= read -r dir; do
      queued=$((queued + 1))
      wait_for_slot
      log "mount=${name} status=refreshing-new directory="${dir}" queued=${queued}/${total_to_refresh} running=$((running + 1))"
      (
        dir_start="$(date +%s)"
        if refresh_dir "$port" "$dir" "true" "$name"; then
          result="ok"
        else
          result="failed"
        fi
        elapsed_dir=$(($(date +%s) - dir_start))
        log "mount=${name} status=${result} directory="${dir}" last=${elapsed_dir}s"
        [ "$result" = "ok" ]
      ) &
      running=$((running + 1))
    done <"$refresh_file"

    while [ "$running" -gt 0 ]; do
      if wait -n; then
        completed=$((completed + 1))
      else
        completed=$((completed + 1))
        failed=$((failed + 1))
      fi
      running=$((running - 1))
      elapsed=$(($(date +%s) - mount_start))
      remaining=$((total_to_refresh - completed))
      if [ "$completed" -gt 0 ]; then
        eta=$((elapsed * remaining / completed))
      else
        eta=0
      fi
      log "mount=${name} status=progress completed=${completed}/${total_to_refresh} running=${running} failed=${failed} elapsed=${elapsed}s eta=${eta}s"
    done

    rm -f "$refresh_file"
    total_elapsed=$(($(date +%s) - mount_start))
    log "mount=${name} status=complete-new directories=${total_to_refresh} failed=${failed} elapsed=${total_elapsed}s"
    [ "$failed" -eq 0 ]
    return 0
  fi

  log "mount=${name} status=root-refresh recursive=false"
  rc_post "$port" vfs/refresh '{"recursive":"false"}' >/dev/null 2>&1 || true

  dirs_file="/tmp/rclone-precache-dirs-${name}.$$"
  refresh_file="/tmp/rclone-precache-refresh-${name}.$$"
  find "$path" -mindepth 1 -maxdepth 1 -type d -print | sort >"$dirs_file"
  : >"$refresh_file"

  total_seen=0
  while IFS= read -r abs_dir; do
    total_seen=$((total_seen + 1))
    dir="${abs_dir#$path/}"
    if should_skip_dir "$name" "$dir"; then
      log "mount=${name} status=skipped-directory directory=\"${dir}\""
      continue
    fi
    printf '%s\n' "$dir" >>"$refresh_file"
  done <"$dirs_file"

  total_to_refresh="$(wc -l <"$refresh_file" | tr -d ' ')"
  if [ "$total_to_refresh" = "0" ]; then
    log "mount=${name} status=no-directories-to-refresh seen=${total_seen}"
    rm -f "$dirs_file" "$refresh_file"
    return 0
  fi

  mount_start="$(date +%s)"
  queued=0
  completed=0
  failed=0
  running=0

  while IFS= read -r dir; do
    queued=$((queued + 1))
    wait_for_slot
    log "mount=${name} status=refreshing directory=\"${dir}\" queued=${queued}/${total_to_refresh} running=$((running + 1))"
    (
      dir_start="$(date +%s)"
      if refresh_dir "$port" "$dir" "true" "$name"; then
        result="ok"
      else
        result="failed"
      fi
      elapsed_dir=$(($(date +%s) - dir_start))
      log "mount=${name} status=${result} directory=\"${dir}\" last=${elapsed_dir}s"
      [ "$result" = "ok" ]
    ) &
    running=$((running + 1))
  done <"$refresh_file"

  while [ "$running" -gt 0 ]; do
    if wait -n; then
      completed=$((completed + 1))
    else
      completed=$((completed + 1))
      failed=$((failed + 1))
    fi
    running=$((running - 1))
    elapsed=$(($(date +%s) - mount_start))
    remaining=$((total_to_refresh - completed))
    if [ "$completed" -gt 0 ]; then
      eta=$((elapsed * remaining / completed))
    else
      eta=0
    fi
    log "mount=${name} status=progress completed=${completed}/${total_to_refresh} running=${running} failed=${failed} elapsed=${elapsed}s eta=${eta}s"
  done

  rm -f "$dirs_file" "$refresh_file"
  total_elapsed=$(($(date +%s) - mount_start))
  log "mount=${name} status=complete directories=${total_to_refresh} failed=${failed} elapsed=${total_elapsed}s"

  # Save current top-level directory state for future update detection
  mkdir -p "$STATE_DIR"
  find "$path" -mindepth 1 -maxdepth 1 -type d -print | sort >"${STATE_DIR}/${name}.dirs"
  # Save mount mtime for rclone-restart detection
  final_mount_mtime="$(stat -c %Y "$path" 2>/dev/null || echo 0)"
  echo "${final_mount_mtime}" > "${STATE_DIR}/${name}.mounttime" 2>/dev/null || true

  [ "$failed" -eq 0 ]
}

log "status=boot-delay seconds=${START_DELAY_SECONDS}"
sleep "$START_DELAY_SECONDS"

overall_start="$(date +%s)"
ok=0
failed=0

printf '%s\n' "$MOUNT_SPECS" | while IFS=: read -r name port path; do
  [ -n "$name" ] || continue
  if warm_mount "$name" "$port" "$path"; then
    ok=$((ok + 1))
  else
    failed=$((failed + 1))
  fi
done

overall_elapsed=$(($(date +%s) - overall_start))
log "status=all-done elapsed=${overall_elapsed}s"

if [ "$KEEP_ALIVE" = "true" ] && [ "$REFRESH_INTERVAL_SECONDS" -gt 0 ]; then
  log "status=loop-mode interval=${REFRESH_INTERVAL_SECONDS}s"
  while true; do
    log "status=sleeping interval=${REFRESH_INTERVAL_SECONDS}s"
    sleep "$REFRESH_INTERVAL_SECONDS"
    log "status=periodic-refresh starting"
    cycle_start="$(date +%s)"
    printf '%s\n' "$MOUNT_SPECS" | while IFS=: read -r name port path; do
      [ -n "$name" ] || continue
      warm_mount "$name" "$port" "$path" || true
    done
    cycle_elapsed=$(($(date +%s) - cycle_start))
    log "status=periodic-refresh-done elapsed=${cycle_elapsed}s"
  done
elif [ "$KEEP_ALIVE" = "true" ]; then
  log "status=idle message=\"pre-cache finished; restart this container to run it again\""
  tail -f /dev/null
fi
