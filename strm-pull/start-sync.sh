#!/usr/bin/env bash
# Start the strm-pull systemd service on the host
nsenter -t 1 -m -u -i -n -- /usr/bin/systemctl reset-failed strm-pull.service 2>/dev/null
sleep 1
nsenter -t 1 -m -u -i -n -- /usr/bin/systemctl start --no-block strm-pull.service
