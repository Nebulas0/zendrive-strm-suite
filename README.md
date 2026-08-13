# ZenDrive STRM Suite

A complete automation pipeline for syncing STRM libraries from a ZenDRIVE/ClearStreamer S3-backed CDN to a local Emby (or Jellyfin/Plex) media server. Designed for [Saltbox](https://docs.saltbox.dev/) deployments.

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
  |-- removes stale local files (--max-delete 50000 per section)
  |-- bounded log (1 GB max)
  |
  v
STRM Sync Monitor (web UI behind Traefik + Authelia)
  |-- shows sync status, run history, live log tail
  |-- Run Sync Now button (starts strm-pull.service via nsenter)
  |-- Stop Sync button (stops strm-pull.service via nsenter)
```

## Components

| Component | Purpose |
|-----------|---------|
| **ZenLocalPoller** | Listens to MQTT events from ZenDRIVE, refreshes rclone VFS cache, forwards paths to Autoscan and strm-bridge |
| **strm-bridge** | Converts unionfs paths to STRM paths, runs targeted rclone sync/copy for the changed directory, then triggers Autoscan with the STRM path |
| **strm-pull** | systemd service that runs a full rclone sync of all sections in parallel every 6 hours to catch deletions and any missed files |
| **STRM Sync Monitor** | Web dashboard to monitor and control the strm-pull sync (behind Traefik + Authelia) |
| **rclone-rc-proxy** | Fixes a boolean-to-string parameter bug between ZenLocalPoller v0.1.0 and rclone v1.75.0 RC API |
| **rclone-startup-precache** | Warms up rclone VFS directory caches on boot with parallel vfs/refresh calls |
| **Autoscan** | Receives scan requests and tells Emby to scan the specified directories (Saltbox built-in) |

## Prerequisites

### Base requirements

- Ubuntu 22.04+ (or any Linux with systemd)
- Docker + Docker Compose
- rclone installed on the host
- A ZenDRIVE/ClearStreamer account with:
  - S3 credentials (access key + secret)
  - MQTT credentials (username + password)
  - CDN endpoint URL
- An Emby/Jellyfin/Plex server

### Saltbox requirements

This suite is designed to run on top of a working [Saltbox](https://docs.saltbox.dev/) installation. Saltbox provides:

- **Traefik** reverse proxy with TLS
- **Authelia** authentication middleware
- **mergerfs/unionfs** at `/mnt/unionfs` combining `/mnt/local` + `/mnt/remote`
- **rclone mounts** at `/mnt/remote/zendrive/<section>` with RC enabled
- **Autoscan** at `/opt/autoscan` (installed via `sb install autoscan`)
- **Emby** (or Jellyfin/Plex) as a Docker container on the `saltbox` network
- The `saltbox` Docker network for inter-container communication
- User `plexuser` (UID/GID 1000) as the service account

If you don't use Saltbox, see the [Standalone Setup](#standalone-setup-non-saltbox) section below.

## Directory Structure

```
zendrive-strm-suite/
├── README.md                          # This file
├── .env.example                       # Copy to .env and fill in your credentials
├── .gitignore
│
├── saltbox/                           # Saltbox-specific configuration
│   ├── inventory-overrides.yml        # Copy into localhost.yml inventory
│   ├── settings-rclone.yml            # rclone remote definitions for settings.yml
│   └── generic-scan.j2                # Custom rclone mount template (scan-optimized)
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

## Saltbox Setup Guide

This is the recommended installation path. It assumes you already have a working Saltbox with Traefik, Authelia, Emby, and rclone mounts.

### Step 1: Clone the Repository

```bash
cd /opt
git clone https://github.com/Nebulas0/zendrive-strm-suite.git ZenServer/ZendriveStrmSuite
cd ZenServer/ZendriveStrmSuite
```

> Or clone anywhere and symlink — the docker-compose files use relative paths.

### Step 2: Create Your .env File

```bash
cp .env.example .env
nano .env
```

Fill in:
- `MQTT_USERNAME` / `MQTT_PASSWORD` — from your ClearStreamer welcome email
- `AUTOSCAN_PASSWORD` — must match the password in your Autoscan config
- `STRM_BRIDGE_AUTH_PASS` — must match what ZenLocalPoller sends (use same as AUTOSCAN_PASSWORD)
- `EMBY_TOKEN` — Emby API token (Emby Dashboard > Advanced > API Keys)
- `TZ` — your timezone (e.g., `Europe/Berlin`)

### Step 3: Configure rclone

Saltbox already installs rclone and manages the config at `~/.config/rclone/rclone.conf`. Add the ZenDRIVE S3 backend and alias remotes:

```bash
nano ~/.config/rclone/rclone.conf
```

Add (replacing with your credentials):

```ini
[zendrive]
type = s3
provider = Other
access_key_id = YOUR_S3_ACCESS_KEY
secret_access_key = YOUR_S3_SECRET_KEY
endpoint = https://YOUR-ENDPOINT/
acl = private
force_path_style = true

[zendrive-movies]
type = alias
remote = zendrive:movies

[zendrive-television]
type = alias
remote = zendrive:television

# ... one alias per section (see rclone/rclone.conf.example)
```

Verify:
```bash
rclone lsd zendrive:strm-tree/
```

### Step 4: Add Saltbox Inventory Overrides

Edit your Saltbox inventory:
```bash
sb edit inventory
```

Add the overrides from `saltbox/inventory-overrides.yml`. Key settings:

```yaml
# Custom mount branch for ZenDRIVE
custom_mount_branch: "/mnt/remote/zendrive=NC"

# Emby instance named "clean" (or use default "emby")
emby_instances: ["clean"]
clean_docker_image_repo: "lscr.io/linuxserver/emby"
clean_docker_image_tag: "version-4.9.5.0"

# Emby: LSIO Docker mod for tuned libsqlite3 (faster scans)
clean_docker_envs_custom:
  DOCKER_MODS: "ghcr.io/darthshadow/linuxserver-mod-sqlite3-emby:2026.07.20-r2"

# Emby: Increase DB connections and cache for better scan performance
clean_config_settings_custom:
  - xpath: 'MaxLibraryDatabaseConnections'
    value: '20'
  - xpath: 'DatabaseCacheSizeMB'
    value: '192'
  - xpath: 'EnableSqLiteMmio'
    value: 'true'

# Autoscan: bind to localhost only
autoscan_role_docker_ports:
  - "127.0.0.1:3030:3030"
```

### Step 5: Add rclone Mount Definitions to Saltbox

Edit Saltbox settings:
```bash
nano /srv/git/saltbox/settings.yml
```

Add the rclone remotes from `saltbox/settings-rclone.yml`. Each remote uses the custom scan-optimized template:

```yaml
rclone:
  version: latest
  enabled: yes
  remotes:
    - remote: zendrive-movies
      settings:
        mount: yes
        template: /opt/mount-templates/custom/generic-scan.j2
        union: no
        upload: no
        vfs_cache:
          enabled: yes
          max_age: 72h
          size: 150G
    - remote: zendrive-television
      settings:
        mount: yes
        template: /opt/mount-templates/custom/generic-scan.j2
        union: no
        vfs_cache:
          enabled: yes
          max_age: 72h
          size: 100G
    # ... one per section (see saltbox/settings-rclone.yml)
```

### Step 6: Install the Custom Mount Template

The `generic-scan.j2` template is an rclone mount systemd unit optimized for ZenDRIVE S3 scanning:
- Read-only mount
- `--use-server-modtime` (correct modtime for STRM files)
- `--no-checksum` / `--s3-disable-checksum` / `--s3-no-head` (skip unnecessary S3 HEAD requests)
- `--fast-list` (batched LIST calls)
- `--dir-cache-time 9999h` (long cache, refreshed by ZenLocalPoller)
- `--vfs-cache-mode full` with configurable size
- RC endpoint enabled on port 5572+ (one per mount)

```bash
sudo mkdir -p /opt/mount-templates/custom
sudo cp saltbox/generic-scan.j2 /opt/mount-templates/custom/generic-scan.j2
```

### Step 7: Deploy Saltbox rclone Mounts

```bash
sb install mounts
```

This creates systemd rclone mount services for each ZenDRIVE section at `/mnt/remote/zendrive/<section>` with RC ports:
- movies: 5572
- television: 5573
- sports: 5574
- xxx: 5575
- courses: 5576
- movies-int: 5577
- television-int: 5578

### Step 8: Install Autoscan

```bash
sb install autoscan
```

Then edit the config:
```bash
sudo nano /opt/autoscan/config.yml
```

Use the config from `autoscan/config.yml` in this repo as a reference. Key points:
- `port: 3030`
- `authentication` with your username/password
- `anchors: [/mnt/unionfs/mounted.bin]` — prevents scans when mount is down
- `triggers`: sonarr, radarr, lidarr webhooks
- `targets`: your Emby URL + API token

### Step 9: Create the STRM Library Directory

```bash
sudo mkdir -p /mnt/local/strm/library
sudo chown plexuser:plexuser /mnt/local/strm/library
sudo chmod 775 /mnt/local/strm/library
```

### Step 10: Install strm-pull (systemd)

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

The service runs as `plexuser` (UID 1000) — the same user Saltbox uses. This ensures rclone can read `~/.config/rclone/rclone.conf` and all STRM files are owned by `plexuser`.

### Step 11: Deploy ZenLocalPoller + strm-bridge + precache

```bash
sudo mkdir -p /opt/ZenServer/ZenLocalPoller
cd /opt/ZenServer/ZenLocalPoller

# Copy all files from the repo
cp -r /opt/ZenServer/ZendriveStrmSuite/zenlocalpoller/* .
cp /opt/ZenServer/ZendriveStrmSuite/strm-bridge/Dockerfile.strm-bridge .
cp /opt/ZenServer/ZendriveStrmSuite/strm-bridge/strm-bridge.py .

# Create config directories (one per section)
for section in television movies xxx sports courses movies-int television-int; do
  mkdir -p config-$section
  cp zenlocalpoller/config-examples/config-$section.yml config-$section/config.yml
done

# Default config (main movies poller)
mkdir -p config
cp zenlocalpoller/config-examples/config-movies.yml config/config.yml
```

Edit each config to match your setup (MQTT credentials, rclone RC port, etc.).

Download the ZenLocalPoller binary:
```bash
wget -O zenlocalpoller https://github.com/datahorders/zenlocalpoller-binaries/releases/latest/download/zenlocalpoller
chmod +x zenlocalpoller
```

Start everything:
```bash
docker compose up -d
```

This starts:
- 8 ZenLocalPoller containers (one per section + int sections)
- strm-bridge (port 3031)
- rclone-rc-proxy (ports 5582-5588 -> 5572-5578)
- rclone-startup-precache (warms VFS on boot)

### Step 12: Deploy the STRM Sync Monitor

```bash
sudo mkdir -p /opt/strm-sync-monitor
cd /opt/strm-sync-monitor
cp -r /opt/ZenServer/ZendriveStrmSuite/strm-sync-monitor/* .

# Edit docker-compose.yml to change the domain
nano docker-compose.yml
# Change strm-sync.example.com to your domain (e.g., strm-sync.yourdomain.com)

docker compose up -d
```

The monitor is behind Traefik + Authelia (same as other Saltbox apps). It needs:
- `privileged: true` and `pid: host` for nsenter (controls host systemd)
- Host scripts mounted (`start-sync.sh`, `stop-sync.sh`)
- The pull log mounted for live tailing
- The lock file mounted

### Step 13: Configure Emby Libraries

In Emby (your `clean` instance), add libraries pointing at the **local STRM paths** (not mounts):

1. Settings > Library > Add Media Library
2. Content type: Movies / Shows (one per section)
3. Folder: `/mnt/local/strm/library/movies`, `/mnt/local/strm/library/television`, etc.
4. Set these options for fast scans:
   - **Enable real time monitoring: OFF**
   - **Metadata downloaders (internet): uncheck all** (NFO files are already local)
   - **Metadata readers: Nfo first**
   - **Save artwork/metadata into media folders: OFF**
   - **Chapter image extraction: OFF**
5. Save

See the [ClearStreamer STRM wiki](https://wiki.clearstreamer.com/getting-started/strm-library) for detailed Emby/Jellyfin settings.

## Standalone Setup (Non-Saltbox)

If you're not using Saltbox, you need to provide:

1. **mergerfs/unionfs** at `/mnt/unionfs`:
   ```bash
   mergerfs /mnt/local:/mnt/remote/zendrive /mnt/unionfs -o allow_other,use_ino,category.action=ff,category.create=ff
   touch /mnt/unionfs/mounted.bin
   ```

2. **rclone mounts** for each section with RC enabled:
   ```bash
   rclone mount zendrive-television: /mnt/remote/zendrive/television \
     --allow-other --read-only --daemon \
     --rc --rc-addr 127.0.0.1:5573 \
     --vfs-cache-mode minimal
   ```

3. **Autoscan** as a Docker container:
   ```bash
   docker run -d --name autoscan --restart unless-stopped --network host \
     -v /opt/autoscan/config.yml:/config.yml \
     -v /mnt/unionfs:/mnt/unionfs:ro \
     -v /mnt/local:/mnt/local:ro \
     cloudbox/autoscan
   ```

4. **Emby/Jellyfin** with access to both `/mnt/unionfs` and `/mnt/local/strm/library`

5. Skip the Saltbox inventory/settings steps (Steps 4-7 above) and configure rclone mounts manually.

## Configuration Reference

### ZenLocalPoller Config (per section)

Each section has its own config file and its own poller container. Key fields:

```yaml
mosquitto:
  broker: mqtt.clearstreamer.com    # MQTT broker (from ClearStreamer)
  port: "443"                       # WSS/TLS
  client_id: my-unique-poller-id    # MUST be unique per poller
  username: your-mqtt-username
  password: your-mqtt-password
  topic: s3
  qos: 2
  tls:
    enabled: true

mount:
  path: /mnt/unionfs/television/    # unionfs path for this section
  inclusions:
    - /mnt/unionfs/television/      # only process events for this section

rclone:
  enabled: true
  host: 127.0.0.1
  port: '5583'                      # RC port (via rclone-rc-proxy -> 5573)
  remote: zendrive-television        # rclone remote name

autoscan-servers:
  - host: 127.0.0.1
    port: 3031                       # strm-bridge (for STRM sync)
  - host: 127.0.0.1
    port: 3030                       # autoscan (for unionfs scan)
```

> **Important:** Each config must list BOTH port 3031 (strm-bridge) and port 3030 (autoscan) in `autoscan-servers`. This ensures both the unionfs path and the STRM path get scanned.

### rclone-rc-proxy Port Mapping

The proxy translates ZenLocalPoller's boolean `recursive` parameter to the string format rclone v1.75.0 expects, and forwards to the correct RC port:

| Proxy Port | Rclone RC Port | Section |
|-----------|---------------|---------|
| 5582 | 5572 | movies |
| 5583 | 5573 | television |
| 5584 | 5574 | sports |
| 5585 | 5575 | xxx |
| 5586 | 5576 | courses |
| 5587 | 5577 | movies-int |
| 5588 | 5578 | television-int |

ZenLocalPoller configs point at the proxy ports (5582-5588), not the rclone RC ports directly.

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
| SYNC_FILE_THRESHOLD | 500 | Files below this = sync, at/above = copy |
| RCLONE_TRANSFERS | 8 | Parallel transfers for targeted sync |
| RCLONE_CHECKERS | 8 | Parallel checkers for targeted sync |

### strm-pull.sh Settings

| Variable | Default | Description |
|----------|---------|-------------|
| STRM_PULL_SECTIONS | television movies xxx sports courses | Space-separated sections to sync in parallel |
| STRM_PULL_TRANSFERS | 32 | Per-section rclone transfers |
| STRM_PULL_CHECKERS | 32 | Per-section rclone checkers |
| STRM_PULL_MAX_DELETE | 50000 | Per-section max deletions |
| STRM_PULL_LOG | /home/plexuser/strm-pull/pull.log | Log file path |
| MAX_LOG_BYTES | 1073741824 (1 GB) | Log size limit before truncation |

### strm-pull.timer Schedule

| Setting | Value | Description |
|---------|-------|-------------|
| OnBootSec | 20min | First run 20 min after boot |
| OnUnitInactiveSec | 6h | Then every 6 hours after last run finishes |
| Persistent | true | Catch up on missed runs after reboot |
| RandomizedDelaySec | 10min | Random delay to avoid spikes |

> `OnUnitInactiveSec` (not `OnUnitActiveSec`) is deliberate: it measures from when the last run **finished**, so runs never overlap regardless of duration. See the [ClearStreamer wiki](https://wiki.clearstreamer.com/getting-started/strm-library#keep-it-updated-automatically-systemd).

### Saltbox Paths Used

| Path | Purpose |
|------|---------|
| /opt/autoscan/ | Autoscan config (Saltbox default) |
| /opt/ZenServer/ZenLocalPoller/ | ZenLocalPoller containers + configs |
| /opt/strm-sync-monitor/ | STRM Sync Monitor container |
| /opt/clean/ | Emby config (Saltbox instance named "clean") |
| /mnt/unionfs/ | mergerfs union (local + remote) |
| /mnt/remote/zendrive/ | rclone mount roots (one per section) |
| /mnt/local/strm/library/ | Local STRM library |
| /home/plexuser/strm-pull/ | strm-pull scripts + log |
| /home/plexuser/.config/rclone/ | rclone config |
| /srv/git/saltbox/inventories/host_vars/localhost.yml | Saltbox inventory overrides |
| /srv/git/saltbox/settings.yml | Saltbox rclone settings |
| /opt/mount-templates/custom/generic-scan.j2 | Custom rclone mount template |

## How It Works

### Event Flow (real-time)

1. A file is added/changed on ZenDRIVE
2. ZenDRIVE publishes an MQTT event to `mqtt.clearstreamer.com:443` topic `s3`
3. The matching ZenLocalPoller receives it (each poller filters by unionfs path)
4. ZenLocalPoller calls `vfs/refresh` on the rclone RC (via proxy) to update the VFS cache
5. ZenLocalPoller sends the unionfs path to Autoscan (port 3030) -> Emby scans unionfs
6. ZenLocalPoller sends the unionfs path to strm-bridge (port 3031)
7. strm-bridge converts the path: strips the filename, maps unionfs -> STRM library path
8. strm-bridge counts files on the remote with `rclone size`
9. If < 500 files: runs `rclone sync` (with `--max-delete 500`) to also clean up stale files
10. If >= 500 files: runs `rclone copy` (avoids `--max-delete` limits from ZenDRIVE duplicate dirs)
11. If sync hits `--max-delete` limit: falls back to `rclone copy` automatically
12. After rclone completes successfully: strm-bridge sends the STRM path to Autoscan -> Emby scans STRM library
13. Duplicate events for the same directory wait for the running sync, then trigger Autoscan

### Full Sync (every 6 hours)

1. `strm-pull.timer` triggers `strm-pull.service`
2. `strm-pull.sh` acquires a flock (prevents overlapping runs)
3. Trims the log to 1 GB if needed
4. Launches 5 parallel `rclone sync` processes (one per section)
5. Each section syncs independently with `--fast-list`, `--max-delete 50000`, `--ignore-errors`
6. Excludes local-only directories: `.recyclebin/**`, `.downloads/**`, `.inbound/**`
7. Each section's log lines are prefixed with `[section]` (e.g. `[movies] INFO ...`)
8. Small sections (sports, courses) finish in minutes
9. Large sections (television, movies) take longer but run in parallel
10. Waits for all sections, reports per-section OK/FAILED
11. Returns nonzero if any section failed

### Startup Cache Warmup

On boot, `rclone-startup-precache` container:
1. Waits for each rclone mount's RC endpoint to be ready
2. Refreshes the root directory listing (non-recursive)
3. Discovers immediate child directories
4. Refreshes each top-level directory (non-recursive, limited parallelism)
5. Watchdog kills stuck refreshes after 10 minutes

This makes the first browse of the library feel instant after a reboot. See the [ClearStreamer cache warming wiki](https://wiki.clearstreamer.com/updates/startup-precache).

### Why Both Unionfs and STRM Paths?

Autoscan receives BOTH paths because:
- Some Emby libraries use the unionfs/mergerfs mount directly
- Some Emby libraries use the local STRM library
- "No target libraries found" for one path is expected and not an error
- Future media servers may use either mount type

### Why sync for small dirs and copy for large dirs?

ZenDRIVE's S3 gateway returns duplicate directory entries in LIST responses (both `CommonPrefixes` and zero-byte directory marker `Key` entries). With `rclone sync`, this can cause rclone to see "extra" files, triggering deletions. For small directories (< 500 files), `--max-delete 500` is sufficient. For large directories, `rclone copy` avoids the issue entirely. The full 6-hour sync handles deletions across the whole tree with `--max-delete 50000` per section.

### Custom rclone Mount Template

The `generic-scan.j2` template is optimized for ZenDRIVE S3 scanning:

| Flag | Why |
|------|-----|
| `--read-only` | ZenDRIVE is read-only |
| `--use-server-modtime` | Correct modtime for STRM files (enables "Recently Added" sorting) |
| `--no-checksum` / `--s3-disable-checksum` / `--s3-no-head` | Skip unnecessary S3 HEAD requests |
| `--fast-list` | Batched LIST calls (essential for 8.9M files) |
| `--dir-cache-time 9999h` | Long cache (ZenLocalPoller refreshes it on changes) |
| `--poll-interval 0` | Disable polling (S3 doesn't support it) |
| `--vfs-cache-mode full` | Full VFS cache for streaming |
| `--rc --rc-addr 127.0.0.1:PORT` | RC endpoint for ZenLocalPoller cache refresh |
| `--buffer-size 32M` / `--vfs-read-ahead 128M` | Read-ahead for smooth playback |

## Troubleshooting

### strm-bridge shows "permission denied" on .partial files

Directories were created by a different user (e.g., root). Fix:
```bash
sudo find /mnt/local/strm/library -type d -not -user plexuser -exec chown plexuser:plexuser {} +
sudo find /mnt/local/strm/library -type d -not -perm 775 -exec chmod 775 {} +
```

### strm-pull log is empty / shows no progress

The log uses `--log-level INFO` which shows stats every 2 minutes. With `--fast-list`, rclone reads the entire tree into RAM before transferring. For large sections (television ~5M files), this can take 30-60 minutes before transfers start. The parallel section approach means small sections finish quickly while large ones scan in parallel.

### "Duplicate directory found in source - ignoring"

This is a NOTICE, not an error. ZenDRIVE's S3 gateway returns duplicate directory prefixes in LIST responses. Both entries point to the same S3 prefix. rclone correctly deduplicates and syncs all content.

### Monitor Stop button doesn't work

The monitor uses `nsenter` to control systemd on the host. Ensure:
- Container has `privileged: true` and `pid: host`
- `util-linux` is installed in the container (for `nsenter`)
- The host scripts (`start-sync.sh`, `stop-sync.sh`) are mounted

### Movie events not reaching strm-bridge

Each ZenLocalPoller config must list strm-bridge (port 3031) as an autoscan-server. If movies are skipped, check that `config-movies/config.yml` has port 3031 in its `autoscan-servers` list.

### "corrupted on transfer: sizes differ" on .strm files

You're missing `--ignore-size`. Each `.strm` file is personalized with your CDN node URL, making the delivered file a different size than the catalog listing. `--ignore-size` tells rclone to compare modification time instead. This is already included in both strm-pull.sh and strm-bridge.py.

### rclone config not found inside container

The strm-bridge container runs as UID 1000 and mounts `/home/plexuser/.config/rclone` read-only. Ensure your rclone config is at that path and readable by UID 1000.

### ZenLocalPoller "Unmapped Path" warnings

These appear when a poller receives an MQTT event for a path outside its scope. For example, the movies poller logs "Unmapped Path" for television paths — this is normal, as the television poller handles those. Each poller only processes events matching its `mount.inclusions`.

## Useful Commands

```bash
# Check strm-pull timer status
systemctl list-timers strm-pull.timer

# Check strm-pull service status
systemctl status strm-pull.service

# Force a sync run now
sudo systemctl start strm-pull.service

# View pull log
tail -f /home/plexuser/strm-pull/pull.log

# Check which rclone sync sections are running
pgrep -f "rclone sync zendrive:strm-tree/" | while read pid; do
  ps -o cmd= -p $pid | grep -oP 'zendrive:strm-tree/\K\w+'
done

# Check strm-bridge logs
docker logs -f strm-bridge

# Check ZenLocalPoller logs (per section)
docker logs -f zenlocalpoller-television

# Check Autoscan logs
docker logs -f autoscan

# Check rclone mount status
systemctl status rclone-zendrive-television.service

# Check rclone RC endpoint
curl -s http://127.0.0.1:5573/rc/noop -X POST -d '{}'

# Verify STRM library permissions
find /mnt/local/strm/library -type d -not -user plexuser | wc -l  # should be 0
find /mnt/local/strm/library -type d -not -perm 775 | wc -l       # should be 0
```

## References

- [ClearStreamer STRM Library Wiki](https://wiki.clearstreamer.com/getting-started/strm-library) — the base STRM sync setup
- [ClearStreamer ZenLocalPoller Wiki](https://wiki.clearstreamer.com/updates/poller) — poller configuration
- [ClearStreamer Cache Warming Wiki](https://wiki.clearstreamer.com/updates/startup-precache) — startup VFS warmup
- [Saltbox Docs](https://docs.saltbox.dev/) — base Saltbox installation
- [Saltbox: Adding Your Own Containers](https://docs.saltbox.dev/advanced/your-own-containers) — how to integrate custom containers
- [Saltbox: Inventory](https://docs.saltbox.dev/saltbox/inventory) — overriding role variables
- [Saltbox: Autoscan](https://docs.saltbox.dev/apps/autoscan) — Autoscan role
- [Saltbox: Feeder Mount](https://docs.saltbox.dev/advanced/feeder) — feeder mount setup
- [ZenLocalPoller Releases](https://github.com/datahorders/zenlocalpoller-binaries/releases) — binary downloads
- [Autoscan GitHub](https://github.com/cloudbox/autoscan) — Autoscan source
- [rclone](https://rclone.org) — rclone documentation

## Credits

- [ZenLocalPoller](https://github.com/datahorders/zenlocalpoller-binaries) by datahorders
- [Autoscan](https://github.com/cloudbox/autoscan) by Cloudbox
- [rclone](https://rclone.org) by Nick Craig-Wood
- [ClearStreamer](https://clearstreamer.com) / ZenDRIVE CDN
- [Saltbox](https://docs.saltbox.dev/) by saltyorg

## License

This configuration and tooling is provided as-is for personal use. The underlying tools (ZenLocalPoller, Autoscan, rclone, Saltbox) have their own licenses.
