#!/usr/bin/env bash
# EC2 Systemd Setup & Sudoers Config for Auto-Deploy
set -e

REPO_DIR="/home/ubuntu/crypto-signal-engine"
SERVICE_NAME="crypto-engine"

echo "=========================================="
echo "🔧 Setting up ${SERVICE_NAME} on EC2"
echo "=========================================="

# 1. Ensure systemd service file is copied
echo "-> 📁 Copying systemd unit file..."
sudo cp "${REPO_DIR}/deploy/${SERVICE_NAME}.service" "/etc/systemd/system/${SERVICE_NAME}.service"

# 2. Allow ubuntu user to restart crypto-engine without password prompt (needed for GitHub Actions)
echo "-> 🔐 Configuring passwordless systemctl restart for ubuntu..."
echo "ubuntu ALL=(ALL) NOPASSWD: /bin/systemctl restart ${SERVICE_NAME}, /usr/bin/systemctl restart ${SERVICE_NAME}, /bin/systemctl status ${SERVICE_NAME}, /usr/bin/systemctl status ${SERVICE_NAME}, /bin/systemctl stop ${SERVICE_NAME}, /usr/bin/systemctl stop ${SERVICE_NAME}, /bin/systemctl start ${SERVICE_NAME}, /usr/bin/systemctl start ${SERVICE_NAME}" | sudo tee "/etc/sudoers.d/${SERVICE_NAME}" > /dev/null
sudo chmod 0440 "/etc/sudoers.d/${SERVICE_NAME}"

# 2b. Cap the journal.
# This service logs a structured line per collector tick, 24/7. On the default
# journald config that grows until it has eaten 10% of the disk, and the first
# symptom is writes failing everywhere else on the box rather than anything
# that points at the logs. 500M of history is several weeks at this rate.
echo "-> 🪵 Capping journald disk use at 500M..."
sudo mkdir -p /etc/systemd/journald.conf.d
printf '[Journal]\nSystemMaxUse=500M\nSystemMaxFileSize=50M\n' \
  | sudo tee /etc/systemd/journald.conf.d/99-crypto-engine.conf > /dev/null
sudo systemctl restart systemd-journald

# 3. Reload systemd daemon and enable service
echo "-> 🔄 Reloading systemd and enabling service on boot..."
sudo systemctl daemon-reload
sudo systemctl enable "${SERVICE_NAME}"

# 4. Check if .env exists
if [ ! -f "${REPO_DIR}/.env" ]; then
  echo "⚠️  WARNING: ${REPO_DIR}/.env not found!"
  echo "   Please create .env before starting the service (copy from .env.example)."
else
  echo "-> 🚀 Starting ${SERVICE_NAME}..."
  sudo systemctl restart "${SERVICE_NAME}"
  sleep 2
  sudo systemctl status "${SERVICE_NAME}" --no-pager

  # 5. Optional Telegram ping on setup
  TG_TOKEN=$(grep -E '^TELEGRAM_BOT_TOKEN=' "${REPO_DIR}/.env" | head -n 1 | cut -d '=' -f2- | tr -d '"'\''\r ')
  TG_CHAT=$(grep -E '^TELEGRAM_CHAT_ID=' "${REPO_DIR}/.env" | head -n 1 | cut -d '=' -f2- | tr -d '"'\''\r ')
  if [ -n "$TG_TOKEN" ] && [ -n "$TG_CHAT" ]; then
    echo "-> 📢 Sending setup confirmation to Telegram bot..."
    PUB_IP=$(curl -s --connect-timeout 2 https://checkip.amazonaws.com || curl -s --connect-timeout 2 https://ifconfig.me || echo "EC2")
    MSG="🚀 <b>EC2 Setup Complete!</b>
⚙️ <b>Service:</b> <code>${SERVICE_NAME}</code> is active and running.
🖥️ <b>Host IP:</b> <code>${PUB_IP}</code>
🌐 <b>Dashboard:</b> http://${PUB_IP}:8080"

    curl -s -X POST "https://api.telegram.org/bot${TG_TOKEN}/sendMessage" \
      -d "chat_id=${TG_CHAT}" \
      -d "parse_mode=HTML" \
      --data-urlencode "text=${MSG}" > /dev/null || true
    echo "-> ✅ Telegram test ping sent successfully!"
  fi
fi

echo "=========================================="
echo "✅ Setup complete!"
echo "Useful commands:"
echo "  sudo systemctl status ${SERVICE_NAME}   # View service status"
echo "  sudo journalctl -u ${SERVICE_NAME} -f   # Stream live logs"
echo "  sudo systemctl restart ${SERVICE_NAME}  # Restart manually"
echo "=========================================="
