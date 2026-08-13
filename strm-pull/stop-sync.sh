#!/usr/bin/env bash
# Stop the strm-pull systemd service on the host
nsenter -t 1 -m -u -i -n -- /usr/bin/systemctl stop strm-pull.service
# Kill any stray rclone sync processes
PIDS=$(nsenter -t 1 -m -u -i -n -- pgrep -f 'rclone sync zendrive:strm-tree ' 2>/dev/null)
if [ -n "$PIDS" ]; then
  for pid in $PIDS; do
    nsenter -t 1 -m -u -i -n -- kill -KILL "$pid" 2>/dev/null
  done
fi
