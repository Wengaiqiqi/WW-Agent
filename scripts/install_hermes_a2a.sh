#!/usr/bin/env bash
# scripts/install_hermes_a2a.sh
# Provision the Hermes A2A<->ACP bridge on a remote machine so the agent-last
# comm-agent can delegate to / chat with a local `hermes acp` over A2A v0.3.
#
# Usage:
#   curl -sSL <raw-url> | bash -s -- \
#       --my-peer-id hermes-home \
#       --your-peer-id agent-last-laptop \
#       --public-host home.example.com \
#       --hmac-secret "$(openssl rand -hex 32)"

set -euo pipefail

MY_PEER_ID=""
YOUR_PEER_ID=""
PUBLIC_HOST=""
HMAC_SECRET=""
HERMES_BIN="${HERMES_BIN:-hermes}"
AGENT_LAST_REPO="${AGENT_LAST_REPO:-https://github.com/<your-repo>/agent-last.git}"
AGENT_LAST_DIR="${AGENT_LAST_DIR:-$HOME/.hermes-a2a/agent-last}"
CADDY_PORT="${CADDY_PORT:-8443}"
BRIDGE_PORT="${BRIDGE_PORT:-19444}"

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

echo "==> [1/7] Checking Hermes ACP is available"
command -v "$HERMES_BIN" >/dev/null 2>&1 || {
  echo "ERROR: '$HERMES_BIN' not on PATH. Install Hermes (https://github.com/NousResearch/hermes-agent) or set HERMES_BIN." >&2
  exit 3
}
python3 -c "import acp" 2>/dev/null || {
  echo "  NOTE: python package 'acp' not importable. Install Hermes' ACP extra in the Hermes checkout:"
  echo "        pip install -e '.[acp]'"
  echo "  (the bridge itself does not need it, but \`hermes acp\` does)"
}

echo "==> [2/7] Fetching agent-last (for the reused A2A server modules)"
if [[ -d "$AGENT_LAST_DIR/.git" ]]; then
  git -C "$AGENT_LAST_DIR" pull --ff-only || echo "  (pull skipped)"
else
  mkdir -p "$(dirname "$AGENT_LAST_DIR")"
  git clone --depth 1 "$AGENT_LAST_REPO" "$AGENT_LAST_DIR"
fi

echo "==> [3/7] Installing bridge python deps"
python3 -m pip install --quiet fastapi uvicorn pyjwt httpx

echo "==> [4/7] Writing bridge env file"
ENV_DIR="$HOME/.hermes-a2a"
mkdir -p "$ENV_DIR"
ENV_FILE="$ENV_DIR/bridge.env"
cat > "$ENV_FILE" <<EOF
HERMES_A2A_HMAC=$HMAC_SECRET
HERMES_A2A_MY_PEER_ID=$MY_PEER_ID
HERMES_A2A_ALLOWED_PEER=$YOUR_PEER_ID
HERMES_A2A_PORT=$BRIDGE_PORT
HERMES_A2A_PUBLIC_HOST=$PUBLIC_HOST
HERMES_A2A_PUBLIC_PORT=$CADDY_PORT
HERMES_ACP_CMD=$HERMES_BIN acp
EOF
chmod 600 "$ENV_FILE"
echo "  wrote $ENV_FILE (mode 0600)"

echo "==> [5/7] Generating Caddyfile"
CADDY_DIR="${CADDY_DIR:-/etc/caddy/Caddyfile.d}"
mkdir -p "$CADDY_DIR" 2>/dev/null || CADDY_DIR="$HOME/.caddy"
mkdir -p "$CADDY_DIR"
cat > "$CADDY_DIR/hermes-a2a.caddy" <<EOF
$PUBLIC_HOST:$CADDY_PORT {
    reverse_proxy localhost:$BRIDGE_PORT
}
EOF
echo "  wrote $CADDY_DIR/hermes-a2a.caddy"

echo "==> [6/7] Starting the bridge + reloading Caddy"
echo "  Start the bridge (loads env, runs from the agent-last checkout):"
echo "    cd $AGENT_LAST_DIR && set -a && . $ENV_FILE && set +a && python3 -m bridge.hermes_a2a"
echo "  (For a long-running service, wrap that in a systemd unit or 'nohup ... &'.)"
if command -v systemctl >/dev/null 2>&1 && systemctl is-active --quiet caddy; then
  sudo systemctl reload caddy && echo "  caddy reloaded via systemctl"
else
  echo "  systemd caddy not running; start caddy manually:"
  echo "    caddy run --config $CADDY_DIR/hermes-a2a.caddy"
fi

echo "==> [7/7] Self-check hint"
echo "  After starting the bridge AND caddy, verify:"
echo "    curl -sk https://localhost:$CADDY_PORT/.well-known/agent.json"

cat <<EOF

✅ Bridge files installed.

Next step on your agent-last machine — register this peer:
    comm.add_peer peer_id=$MY_PEER_ID \\
                  url=https://$PUBLIC_HOST:$CADDY_PORT \\
                  hmac_secret_value=$HMAC_SECRET

(Keep that HMAC secret safe — it's the only copy printed.)
EOF
