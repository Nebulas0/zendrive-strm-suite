#!/bin/sh
set -eu

BIN_PATH="/usr/local/bin/zenlocalpoller"
DOWNLOAD_URL="${ZLP_DOWNLOAD_URL:-https://github.com/datahorders/zenlocalpoller-binaries/releases/latest/download/zenlocalpoller}"
CONFIG_DIR="${ZLP_CONFIG_DIR:-/config}"
CONFIG_FILE="${CONFIG_DIR}/config.yml"

if [ ! -x "${BIN_PATH}" ]; then
  echo "[ZenLocalPoller] Downloading binary from ${DOWNLOAD_URL}"
  TMP_BIN="${BIN_PATH}.tmp"
  if ! wget -O "${TMP_BIN}" "${DOWNLOAD_URL}"; then
    echo "[ZenLocalPoller] Failed to download binary. Check ZLP_DOWNLOAD_URL and network access."
    exit 1
  fi
  chmod +x "${TMP_BIN}"
  mv "${TMP_BIN}" "${BIN_PATH}"
fi

if [ ! -f "${CONFIG_FILE}" ]; then
  echo "[ZenLocalPoller] Missing config file: ${CONFIG_FILE}"
  exit 1
fi

cd "${CONFIG_DIR}"
exec "${BIN_PATH}"
