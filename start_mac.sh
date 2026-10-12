#!/usr/bin/env bash
set -euo pipefail

# Run on the Mac. SSH_TARGET uses the Mac's existing SSH configuration.
SSH_TARGET="${SSH_TARGET:-haolei@ch-pub-2}"
LOCAL_PORT="${LOCAL_PORT:-18003}"

if [[ "$(uname -s)" != Darwin ]]; then
  echo "请将此脚本下载到 Mac，在 Mac 终端运行。" >&2
  exit 1
fi
if ! [[ "${LOCAL_PORT}" =~ ^[0-9]{1,5}$ ]] || (( 10#${LOCAL_PORT} < 1 || 10#${LOCAL_PORT} > 65535 )); then
  echo "LOCAL_PORT 必须是 1–65535 之间的端口。" >&2
  exit 1
fi

WIFI_DEVICE="$(networksetup -listallhardwareports | awk '/Hardware Port: (Wi-Fi|AirPort)/ {getline; if ($1 == "Device:") print $2; exit}')"
WIFI_IP=""
if [[ -n "${WIFI_DEVICE}" ]]; then
  WIFI_IP="$(ipconfig getifaddr "${WIFI_DEVICE}" 2>/dev/null || true)"
fi

echo "正在连接 ${SSH_TARGET}，启动或复用服务器上的标注服务……"
echo "连接成功且服务就绪后，本机访问：http://localhost:${LOCAL_PORT}"
if [[ -n "${WIFI_IP}" ]]; then
  echo "同一 Wi-Fi 下的其他人访问：http://${WIFI_IP}:${LOCAL_PORT}"
else
  echo "未检测到 Wi-Fi IPv4 地址；请在系统设置中查看 Mac 的 Wi-Fi IP。"
fi
echo "请保持 Mac 唤醒和此终端运行。Ctrl+C 停止共享，服务器服务继续运行。"

# A detached server survives the tunnel. First ensure it is ready over SSH.
ssh -T \
  -o ServerAliveInterval=30 \
  -o ServerAliveCountMax=3 \
  "${SSH_TARGET}" 'bash -l -s' <<'REMOTE'
set -euo pipefail
APP_DIR="${HOME}/value_label_annotator"
DATASET_ROOT="/mnt/data/haolei/2026-09-btin-datasets/2026-09-G1D01-dagger-boin-40mm/boltinsert_value_validate_datasets"
LOG_FILE="${APP_DIR}/.cache/shared-server.log"

service_ready() {
  curl --noproxy '*' --max-time 3 -fsS http://127.0.0.1:8003/api/datasets 2>/dev/null |
    python3 -c 'import json, sys; d = json.load(sys.stdin); sys.exit(0 if "annotation_ui_version" in d and "datasets" in d else 1)' 2>/dev/null
}

if service_ready; then
  echo "已有标注服务运行，复用现有服务及其数据集配置。"
else
  if [[ -n "$(ss -H -ltn 'sport = :8003')" ]]; then
    echo "8003 端口已被占用，但标注服务未就绪。请在服务器检查占用进程。" >&2
    exit 1
  fi
  [[ -f "${APP_DIR}/run.sh" ]] || { echo "未找到 ${APP_DIR}/run.sh" >&2; exit 1; }
  [[ -d "${DATASET_ROOT}" ]] || { echo "数据集目录不存在：${DATASET_ROOT}" >&2; exit 1; }
  mkdir -p "${APP_DIR}/.cache"
  nohup bash "${APP_DIR}/run.sh" --root "${DATASET_ROOT}" --host 127.0.0.1 --port 8003 \
    </dev/null >"${LOG_FILE}" 2>&1 &
  SERVER_PID=$!
  READY=0
  for (( attempt=0; attempt<60; attempt++ )); do
    if service_ready; then READY=1; break; fi
    if ! kill -0 "${SERVER_PID}" 2>/dev/null; then break; fi
    sleep 1
  done
  if (( READY == 0 )); then
    echo "服务尚未就绪，请检查服务器日志：${LOG_FILE}" >&2
    tail -n 20 "${LOG_FILE}" >&2
    exit 1
  fi
  echo "服务器标注服务已启动。日志：${LOG_FILE}"
fi

echo "服务器标注服务已就绪。"
REMOTE

echo "正在建立端口转发；SSH 登录成功且没有报错后，可以打开上方网址。"
exec ssh -N -g \
  -o ExitOnForwardFailure=yes \
  -o ServerAliveInterval=30 \
  -o ServerAliveCountMax=3 \
  -L "0.0.0.0:${LOCAL_PORT}:127.0.0.1:8003" \
  "${SSH_TARGET}"
