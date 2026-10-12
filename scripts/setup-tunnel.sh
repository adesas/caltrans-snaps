#!/usr/bin/env bash
# One-shot Cloudflare Tunnel setup for the recordings host.
# Usage: bash scripts/setup-tunnel.sh media.yourdomain.tld
# Requires: a Cloudflare account with your domain added (free plan OK).
set -euo pipefail

HOST="${1:?usage: setup-tunnel.sh media.yourdomain.tld}"
TUNNEL_NAME=caltrans-media

echo ">> Browser login to Cloudflare (one-time)..."
cloudflared tunnel login

echo ">> Creating tunnel '$TUNNEL_NAME'..."
cloudflared tunnel create "$TUNNEL_NAME" 2>/dev/null || true
TUNNEL_ID=$(cloudflared tunnel list | awk -v n="$TUNNEL_NAME" '$2==n {print $1}')
CRED="/etc/cloudflared/$TUNNEL_ID.json"
sudo mkdir -p /etc/cloudflared
sudo cp "$HOME/.cloudflared/$TUNNEL_ID.json" "$CRED"

sudo tee /etc/cloudflared/config.yml >/dev/null <<EOF
tunnel: $TUNNEL_ID
credentials-file: $CRED
ingress:
  - hostname: $HOST
    service: http://localhost:8090
  - service: http_status:404
EOF

echo ">> Routing DNS ($HOST -> tunnel)..."
cloudflared tunnel route dns "$TUNNEL_ID" "$HOST"

echo ">> Installing systemd service..."
sudo tee /etc/systemd/system/caltrans-tunnel.service >/dev/null <<EOF
[Unit]
Description=Cloudflare Tunnel for caltrans-snaps media
After=network-online.target
[Service]
ExecStart=/usr/local/bin/cloudflared tunnel --config /etc/cloudflared/config.yml run
Restart=always
RestartSec=5
[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload && sudo systemctl enable --now caltrans-tunnel

echo ">> Updating site config MEDIA_BASE -> https://$HOST ..."
sed -i "s|MEDIA_BASE: \"https://media.example.com\"|MEDIA_BASE: \"https://$HOST\"|" "$HOME/caltrans_snaps/web/config.js"
cd "$HOME/caltrans_snaps" && git add web/config.js && git commit -m "tunnel: MEDIA_BASE -> $HOST" && git push

echo ">> Done. In ~1 min: https://$HOST/hls/ should list nothing but serve playlists,"
echo "   and https://adesas.github.io/caltrans-snaps/ plays recorded hours."
