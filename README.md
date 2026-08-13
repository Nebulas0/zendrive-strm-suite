# ZenDrive STRM Suite

A complete automation pipeline for syncing STRM libraries from a ZenDRIVE/ClearStreamer S3-backed CDN to a local Emby (or Jellyfin/Plex) media server.

## What This Does

```
MQTT event (file added/changed on ZenDRIVE)
  |
  v
ZenLocalPoller (one per library section)
  |-- refreshes the rclone VFS cache for the affected unionfs path
  |-- notifies Autoscan with the unionfs path  -->  Emby scans the unionfs mount
  |-- notifies strm-bridge with the unionfs path
  |       |
  |       v
  |    strm-bridge
  |       |-- converts unionfs path to local STRM path
  |       |-- counts files on the remote (rclone size)
  |       |-- if < 500 files: rclone sync (with --max-delete 500)
  |       |-- if >= 500 files: rclone copy (avoids --max-delete limits)
  |       |-- waits for rclone to complete successfully
  |       |-- notifies Autoscan with the local STRM path  -->  Emby scans the STRM library
  |
  v
strm-pull.service (systemd, every 6h)
  |-- runs rclone sync for each section in parallel (television, movies, xxx, sports, courses)
  |-- removes stale local files (--max-delete 10000 per section)
  |-- bounded log (1 GB max)
  |
  v
STRM Sync Monitor (web UI)
  |-- shows sync status, run history, live log tail
  |-- Run Sync Now button (starts strm-pull.service)
  |-- Stop Sync button (stops strm-pull.service)
```

## Components

| Component | Purpose |
|-----------|---------|
| **ZenLocalPoller** | Listens to MQTT events from ZenDRIVE, refreshes rclone VFS cache, forwards paths to Autoscan and strm-bridge |
| **strm-bridge** | Converts unionfs paths to STRM paths, runs targeted rclone sync/copy for the changed directory, then triggers Autoscan with the STRM path |
| **strm-pull** | systemd service that runs a full rclone sync of all sections in parallel every 6 hours to catch deletions and any missed files |
| **STRM Sync Monitor** | Web dashboard to monitor and control the strm-pull sync |
| **rclone-rc-proxy** | Fixes a boolean-to-string parameter bug between ZenLocalPoller and rclone RC API |
| **rclone-startup-precache** | Warms up rclone VFS caches on boot with parallel vfs/refresh calls |
| **Autoscan** | Receives scan requests and tells Emby to scan the specified directories |

## Prerequisites

- Ubuntu 22.04+ (or any Linux with systemd)
- Docker + Docker Compose
- rclone installed on the host (for strm-pull and rclone mounts)
- A ZenDRIVE/ClearStreamer account with:
  - S3 credentials (access key + secret)
  - MQTT credentials (username + password)
  - CDN endpoint URL
- An Emby/Jellyfin/Plex server
- Optional: Traefik + Authelia for the STRM Sync Monitor web UI

## Directory Structure

```
zendrive-strm-suite/
├── README.md                          # This file
├── .env.example                       # Copy to .env and fill in your credentials
├── .gitignore
│
├── zenlocalpoller/                    # ZenLocalPoller + related containers
│   ├── docker-compose.yml             # All containers + strm-bridge + rclone-rc-proxy + precache
│   ├── Dockerfile                     # ZenLocalPoller binary container
│   ├── Dockerfile.rclone-startup-precache  # VFS cache warmup container
│   ├── entrypoint.sh                  # Downloads ZenLocalPoller binary on first run
│   ├── rclone-rc-proxy.py             # Fixes rclone RC boolean parameter bug
│   ├── rclone-startup-precache.sh     # Parallel VFS refresh script
│   └── config-examples/               # One config per library section (sanitized)
│       ├── config-television.yml
│       ├── config-television-int.yml
│       ├── config-movies.yml
│       ├── config-movies-int.yml
│       ├── config-xxx.yml
│       ├── config-sports.yml
│       └── config-courses.yml
│
├── strm-bridge/                       # STRM bridge container
│   ├── Dockerfile.strm-bridge         # Python + rclone Alpine container
│   └── strm-bridge.py                 # The bridge script
│
├── strm-sync-monitor/                 # Web dashboard for strm-pull
│   ├── docker-compose.yml             # With Traefik labels (adjust domain)
│   ├── Dockerfile                     # Python + Flask + nsenter
│   └── app.py                         # Flask web app
│
├── strm-pull/                         # Host-side systemd sync scripts
│   ├── strm-pull.sh                   # Parallel section sync script
│   ├── start-sync.sh                  # Used by monitor to start the service
│   └── stop-sync.sh                   # Used by monitor to stop the service
│
├── systemd/                           # systemd unit files
│   ├── strm-pull.service              # One-shot service that runs strm-pull.sh
│   └── strm-pull.timer                # Triggers every 6h + on boot
│
├── autoscan/                          # Autoscan configuration
│   └── config.yml                     # Autoscan webhook + Emby target config
│
└── rclone/                            # rclone configuration
    └── rclone.conf.example            # S3 backend + alias remotes template
```

## Installation Guide

### 1. Clone the Repository

```bash
git clone https://github.com/Nebulas0/zendrive-strm-suite.git
cd zendrive-strm-suite
```

### 2. Create Your .env File

```bash
cp .env.example .env
nano .env
```

Fill in your credentials:
- MQTT_USERNAME / MQTT_PASSWORD — from your ClearStreamer/ZenDRIVE account
- AUTOSCAN_PASSWORD — choose a password for Autoscan webhooks
- STRM_BRIDGE_AUTH_PASS — must match what ZenLocalPoller sends
- EMBY_TOKEN — Emby API token (Settings > Advanced > API Keys)
- TZ — your timezone (e.g., Europe/Berlin)

### 3. Configure rclone

```bash
mkdir -p ~/.config/rclone
cp rclone/rclone.conf.example ~/.config/rclone/rclone.conf
nano ~/.config/rclone/rclone.conf
```

Replace YOUR_S3_ACCESS_KEY, YOUR_S3_SECRET_KEY, and the endpoint URL with your ZenDRIVE credentials.

Verify it works:
```bash
rclone lsd zendrive:strm-tree/
```

### 4. Set Up rclone Mounts (Saltbox)

If you're using Saltbox, the rclone mounts are already configured. If not, you need read-only rclone mounts for each section:

```bash
# Example for television
rclone mount zendrive-television: /mnt/remote/zendrive/television \
  --allow-other --read-only --daemon \
  --rc --rc-addr 127.0.0.1:5573 \
  --vfs-cache-mode minimal
```

Each mount needs:
- Its own RC port (5572 for movies, 5573 for television, etc.)
- --rc enabled for ZenLocalPoller to refresh the VFS cache

### 5. Set Up mergerfs / unionfs

```bash
# Create the union mount combining local + remote
mergerfs /mnt/local:/mnt/remote/zendrive /mnt/unionfs \
  -o allow_other,use_ino,category.action=ff,category.create=ff
```

Create the anchor file:
```bash
touch /mnt/unionfs/mounted.bin
```

### 6. Create the STRM Library Directory

```bash
mkdir -p /mnt/local/strm/library
```

### 7. Install strm-pull (systemd)

```bash
# Copy scripts
sudo mkdir -p /home/plexuser/strm-pull
sudo cp strm-pull/strm-pull.sh /home/plexuser/strm-pull/
sudo cp strm-pull/start-sync.sh /home/plexuser/strm-pull/
sudo cp strm-pull/stop-sync.sh /home/plexuser/strm-pull/
sudo chown -R plexuser:plexuser /home/plexuser/strm-pull/
sudo chmod +x /home/plexuser/strm-pull/*.sh

# Install systemd units
sudo cp systemd/strm-pull.service /etc/systemd/system/
sudo cp systemd/strm-pull.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable strm-pull.timer
sudo systemctl start strm-pull.timer
```

Note: Adjust the User=plexuser and paths in strm-pull.service if your user is different.

### 8. Configure Autoscan

```bash
sudo mkdir -p /opt/autoscan
sudo cp autoscan/config.yml /opt/autoscan/config.yml
sudo nano /opt/autoscan/config.yml
```

Update:
- authentication.password — your Autoscan password
- targets.emby.url — your Emby URL
- targets.emby.token — your Emby API token

Run Autoscan (using Docker):
```bash
docker run -d \
  --name autoscan \
  --restart unless-stopped \
  --network host \
  -v /opt/autoscan/config.yml:/config.yml \
  -v /mnt/unionfs:/mnt/unionfs:ro \
  -v /mnt/local:/mnt/local:ro \
  cloudbox/autoscan
```

### 9. Configure ZenLocalPoller

```bash
sudo mkdir -p /opt/ZenServer/ZenLocalPoller
cd /opt/ZenServer/ZenLocalPoller

# Copy files from the repo
cp -r zenlocalpoller/* .
cp -r strm-bridge/Dockerfile.strm-bridge .
cp strm-bridge/strm-bridge.py .

# Create config directories (one per section)
for section in television movies xxx sports courses movies-int television-int; do
  mkdir -p config-$section
  cp zenlocalpoller/config-examples/config-$section.yml config-$section/config.yml
done

# Also create the default config (for the main movies poller)
mkdir -p config
cp zenlocalpoller/config-examples/config-movies.yml config/config.yml
```

Edit each config file to match your setup:
- MQTT credentials
- Autoscan/strm-bridge credentials
- rclone RC port (must match your rclone mount)
- Mount paths

Download the ZenLocalPoller binary:
```bash
wget -O zenlocalpoller https://github.com/datahorders/zenlocalpoller-binaries/releases/latest/download/zenlocalpoller
chmod +x zenlocalpoller
```

Start everything:
```bash
docker compose up -d
```

### 10. Set Up the STRM Sync Monitor (Optional)

```bash
sudo mkdir -p /opt/strm-sync-monitor
cd /opt/strm-sync-monitor
cp -r ../../strm-sync-monitor/* .

# Edit docker-compose.yml to change the domain
nano docker-compose.yml
# Change strm-sync.example.com to your domain

docker compose up -d
```

The monitor needs:
- privileged: true and pid: host for nsenter (to control systemd on the host)
- The host scripts mounted (start-sync.sh, stop-sync.sh)
- The log file mounted
- The lock file mounted

## Configuration Reference

### ZenLocalPoller Config (per section)

Each section (television, movies, etc.) has its own config file. Key fields:

```yaml
mosquitto:
  broker: mqtt.clearstreamer.com    # MQTT broker
  username: ${MQTT_USERNAME}        # Your MQTT username
  password: ${MQTT_PASSWORD}        # Your MQTT password

mount:
  path: /mnt/unionfs/television/    # The unionfs path for this section
  inclusions:
    - /mnt/unionfs/television/      # Only process events for this section

rclone:
  enabled: true
  port: '5583'                      # RC port for this section's rclone mount (via proxy)
  remote: zendrive-television        # rclone remote name

autoscan-servers:
  - host: 127.0.0.1
    port: 3031                       # strm-bridge (for STRM sync)
  - host: 127.0.0.1
    port: 3030                       # autoscan (for unionfs scan)
```

### strm-bridge Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| LISTEN_HOST | 127.0.0.1 | Bind address |
| LISTEN_PORT | 3031 | Listen port |
| LOCAL_LIBRARY | /mnt/local/strm/library | Local STRM library root |
| RCLONE_REMOTE | zendrive:strm-tree | rclone remote for STRM tree |
| AUTOSCAN_URL | http://127.0.0.1:3030 | Autoscan URL |
| AUTH_USER | plexuser | Basic auth username |
| AUTH_PASS | changeme | Basic auth password |
| SYNC_FILE_THRESHOLD | 500 | Files below this = sync, above = copy |
| RCLONE_TRANSFERS | 8 | Parallel transfers for targeted sync |
| RCLONE_CHECKERS | 8 | Parallel checkers for targeted sync |

### strm-pull.sh Settings

| Variable | Default | Description |
|----------|---------|-------------|
| STRM_PULL_SECTIONS | television movies xxx sports courses | Space-separated sections to sync in parallel |
| STRM_PULL_TRANSFERS | 32 | Per-section rclone transfers |
| STRM_PULL_CHECKERS | 32 | Per-section rclone checkers |
| STRM_PULL_MAX_DELETE | 10000 | Per-section max deletions |
| STRM_PULL_LOG | /home/plexuser/strm-pull/pull.log | Log file path |
| MAX_LOG_BYTES | 1073741824 (1 GB) | Log size limit before truncation |

### strm-pull.timer Schedule

| Setting | Value | Description |
|---------|-------|-------------|
| OnBootSec | 20min | First run 20 min after boot |
| OnUnitInactiveSec | 6h | Then every 6 hours |
| Persistent | true | Catch up on missed runs after reboot |
| RandomizedDelaySec | 10min | Random delay to avoid spikes |

## How It Works

### Event Flow (real-time)

1. A file is added/changed on ZenDRIVE
2. ZenDRIVE publishes an MQTT event
3. The matching ZenLocalPoller receives it
4. ZenLocalPoller refreshes the rclone VFS cache for the affected directory
5. ZenLocalPoller sends the unionfs path to Autoscan (port 3030) -> Emby scans unionfs
6. ZenLocalPoller sends the unionfs path to strm-bridge (port 3031)
7. strm-bridge converts the path to a local STRM path (strips the filename, uses the parent directory)
8. strm-bridge counts files on the remote with rclone size
9. If < 500 files: runs rclone sync (with --max-delete 500) to also clean up stale files
10. If >= 500 files: runs rclone copy (avoids --max-delete limits from ZenDRIVE duplicate dirs)
11. If sync hits --max-delete limit: falls back to rclone copy automatically
12. After rclone completes successfully: strm-bridge sends the STRM path to Autoscan -> Emby scans STRM library
13. Duplicate events for the same directory wait for the running sync, then trigger Autoscan

### Full Sync (every 6 hours)

1. strm-pull.timer triggers strm-pull.service
2. strm-pull.sh launches 5 parallel rclone sync processes (one per section)
3. Each section syncs independently with --fast-list, --max-delete 10000, --ignore-errors
4. Small sections (sports, courses) finish in minutes
5. Large sections (television, movies) take longer but run in parallel
6. Log is truncated to 1 GB at the start of each run
7. The STRM Sync Monitor shows live progress

### Why Both Unionfs and STRM Paths?

Autoscan receives BOTH paths because:
- Some media servers use the unionfs/mergerfs mount directly
- Some media servers use the local STRM library
- "No target libraries found" for one path is expected and not an error

### Why sync for small dirs and copy for large dirs?

ZenDRIVE's S3 gateway returns duplicate directory entries in LIST responses. With rclone sync, this causes rclone to see "extra" files that don't exist in the chosen copy, triggering deletions. For small directories (< 500 files), --max-delete 500 is sufficient. For large directories, rclone copy avoids the issue entirely. The full 6-hour sync handles deletions across the whole tree with --max-delete 10000 per section.

## Troubleshooting

### strm-bridge shows "permission denied" on .partial files

This happens when directories were created by a different user (e.g., root). Fix:
```bash
sudo find /mnt/local/strm/library -type d -not -user plexuser -exec chown plexuser:plexuser {} +
sudo find /mnt/local/strm/library -type d -not -perm 775 -exec chmod 775 {} +
```

### strm-pull log is empty / shows no progress

The log uses --log-level INFO which shows stats every 2 minutes. With --fast-list, rclone reads the entire tree into RAM before transferring. For large sections (television ~5M files), this can take 30-60 minutes before transfers start. The parallel section approach means small sections finish quickly while large ones scan in parallel.

### "Duplicate directory found in source - ignoring"

This is a NOTICE, not an error. ZenDRIVE's S3 gateway returns duplicate directory prefixes in LIST responses. Both entries point to the same S3 prefix — there's only one set of files. rclone correctly deduplicates and syncs all content.

### Monitor Stop button doesn't work

The monitor uses nsenter to control systemd on the host. Ensure:
- Container has privileged: true and pid: host
- util-linux is installed in the container (for nsenter)
- The host scripts (start-sync.sh, stop-sync.sh) are mounted

### Movie events not reaching strm-bridge

Each ZenLocalPoller config must list strm-bridge (port 3031) as an autoscan-server. If movies are skipped, check that config-movies/config.yml has port 3031 in its autoscan-servers list.

## Credits

- [ZenLocalPoller](https://github.com/datahorders/zenlocalpoller-binaries) by datahorders
- [Autoscan](https://github.com/cloudbox/autoscan) by Cloudbox
- [rclone](https://rclone.org) by Nick Craig-Wood
- [ClearStreamer](https://clearstreamer.com) / ZenDRIVE CDN

## License

This configuration and tooling is provided as-is for personal use. The underlying tools (ZenLocalPoller, Autoscan, rclone) have their own licenses.
