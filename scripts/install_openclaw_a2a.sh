#!/usr/bin/env bash
# scripts/install_openclaw_a2a.sh
# Install the openclaw-a2a plugin on a remote machine so the agent-last
# comm-agent can talk to it over Google A2A v0.3.
#
# Usage:
#   curl -sSL <raw-url> | bash -s -- \
#       --my-peer-id openclaw-home \
#       --your-peer-id agent-last-laptop \
#       --public-host home.example.com \
#       --hmac-secret "$(openssl rand -hex 32)"

set -euo pipefail

MY_PEER_ID=""
YOUR_PEER_ID=""
PUBLIC_HOST=""
HMAC_SECRET=""
OPENCLAW_BIN="${OPENCLAW_BIN:-openclaw}"
A2A_PLUGIN_VERSION="${A2A_PLUGIN_VERSION:-v0.3.0}"
CADDY_PORT="${CADDY_PORT:-8443}"
OPENCLAW_A2A_PORT="${OPENCLAW_A2A_PORT:-19443}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --my-peer-id) MY_PEER_ID="$2"; shift 2;;
    --your-peer-id) YOUR_PEER_ID="$2"; shift 2;;
    --public-host) PUBLIC_HOST="$2"; shift 2;;
    --hmac-secret) HMAC_SECRET="$2"; shift 2;;
    *) echo "unknown flag: $1" >&2; exit 2;;
  esac
done

[[ -z "$MY_PEER_ID" || -z "$YOUR_PEER_ID" || -z "$PUBLIC_HOST" || -z "$HMAC_SECRET" ]] && {
  echo "missing required flag(s); see header for usage" >&2
  exit 2
}

echo "==> [1/7] Checking OpenClaw is installed"
command -v "$OPENCLAW_BIN" >/dev/null 2>&1 || {
  echo "ERROR: '$OPENCLAW_BIN' not on PATH. Install OpenClaw first (https://github.com/openclaw/openclaw) or export OPENCLAW_BIN." >&2
  exit 3
}

echo "==> [2/7] Installing openclaw-a2a plugin @ $A2A_PLUGIN_VERSION"
"$OPENCLAW_BIN" skill install "marketclaw-tech/openclaw-a2a@$A2A_PLUGIN_VERSION"

echo "==> [3/7] Writing OpenClaw A2A config"
OPENCLAW_CONFIG_DIR="$($OPENCLAW_BIN config-dir 2>/dev/null || echo "$HOME/.openclaw")"
mkdir -p "$OPENCLAW_CONFIG_DIR"
cat > "$OPENCLAW_CONFIG_DIR/a2a.yaml" <<EOF
a2a:
  my_peer_id: "$MY_PEER_ID"
  listen_port: $OPENCLAW_A2A_PORT
  hmac_secret_env: A2A_HMAC_SECRET
  allowed_peers:
    - peer_id: "$YOUR_PEER_ID"
      hmac_secret_env: A2A_HMAC_SECRET
EOF
echo "  wrote $OPENCLAW_CONFIG_DIR/a2a.yaml"

echo "==> [4/7] Persisting HMAC secret to env"
ENV_FILE="$OPENCLAW_CONFIG_DIR/a2a.env"
echo "A2A_HMAC_SECRET=$HMAC_SECRET" > "$ENV_FILE"
chmod 600 "$ENV_FILE"
echo "  wrote $ENV_FILE (mode 0600)"

echo "==> [5/7] Generating Caddyfile"
CADDY_DIR="${CADDY_DIR:-/etc/caddy/Caddyfile.d}"
mkdir -p "$CADDY_DIR" 2>/dev/null || CADDY_DIR="$HOME/.caddy"
mkdir -p "$CADDY_DIR"
cat > "$CADDY_DIR/openclaw-a2a.caddy" <<EOF
$PUBLIC_HOST:$CADDY_PORT {
    reverse_proxy localhost:$OPENCLAW_A2A_PORT
}
EOF
echo "  wrote $CADDY_DIR/openclaw-a2a.caddy"

echo "==> [6/7] Reloading Caddy"
if command -v systemctl >/dev/null 2>&1 && systemctl is-active --quiet caddy; then
  sudo systemctl reload caddy
  echo "  caddy reloaded via systemctl"
else
  echo "  systemd caddy not running; you'll need to start caddy manually:"
  echo "    caddy run --config $CADDY_DIR/openclaw-a2a.caddy"
fi

echo "==> [7/7] Self-check"
sleep 2
if curl -sk --max-time 5 "https://localhost:$CADDY_PORT/.well-known/agent.json" >/dev/null; then
  echo "  agent card served OK"
else
  echo "  WARNING: agent card not yet reachable on https://localhost:$CADDY_PORT/"
fi

cat <<EOF

✅ Install complete.

Next step on your laptop:
  In the comm-agent REPL, register this peer:
    comm.add_peer peer_id=$MY_PEER_ID \\
                  url=https://$PUBLIC_HOST:$CADDY_PORT \\
                  hmac_secret_value=$HMAC_SECRET

(Keep that HMAC secret safe — it's the only copy printed.)
EOF
