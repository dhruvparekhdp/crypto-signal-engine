#!/usr/bin/env bash
# EC2 Systemd Setup & Sudoers Config for Auto-Deploy
set -e

REPO_DIR="/home/ubuntu/crypto-signal-engine"
SERVICE_NAME="crypto-engine"

echo "=========================================="
echo "🔧 Setting up ${SERVICE_NAME} on EC2"
echo "=========================================="

# 1. Ensure systemd service file is copied
echo "-> Copying systemd unit file..."
sudo cp "${REPO_DIR}/deploy/${SERVICE_NAME}.service" "/etc/systemd/system/${SERVICE_NAME}.service"

# 2. Allow ubuntu user to restart crypto-engine without password prompt (needed for GitHub Actions)
echo "-> Configuring passwordless systemctl restart for ubuntu..."
echo "ubuntu ALL=(ALL) NOPASSWD: /bin/systemctl restart ${SERVICE_NAME}, /bin/systemctl status ${SERVICE_NAME}, /bin/systemctl stop ${SERVICE_NAME}, /bin/systemctl start ${SERVICE_NAME}" | sudo tee "/etc/sudoers.d/${SERVICE_NAME}" > /dev/null
sudo chmod 0440 "/etc/sudoers.d/${SERVICE_NAME}"

# 3. Reload systemd daemon and enable service
echo "-> Reloading systemd and enabling service on boot..."
sudo systemctl daemon-reload
sudo systemctl enable "${SERVICE_NAME}"

# 4. Check if .env exists
if [ ! -f "${REPO_DIR}/.env" ]; then
  echo "⚠️  WARNING: ${REPO_DIR}/.env not found!"
  echo "   Please create .env before starting the service (copy from .env.example)."
else
  echo "-> Starting ${SERVICE_NAME}..."
  sudo systemctl restart "${SERVICE_NAME}"
  sleep 2
  sudo systemctl status "${SERVICE_NAME}" --no-pager
fi

echo "=========================================="
echo "✅ Setup complete!"
echo "Useful commands:"
echo "  sudo systemctl status ${SERVICE_NAME}   # View service status"
echo "  sudo journalctl -u ${SERVICE_NAME} -f   # Stream live logs"
echo "  sudo systemctl restart ${SERVICE_NAME}  # Restart manually"
echo "=========================================="
