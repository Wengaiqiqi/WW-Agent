#!/usr/bin/env bash
# scripts/install_hermes_a2a.sh
# Provision the W&W Agent Hermes A2A<->ACP bridge on a remote machine so the
# W&W Agent comm-agent can delegate to / chat with a local `hermes acp` over A2A v0.3.
#
# Two ways to run:
#   1) Interactive (no flags): prompts for each value (Enter accepts the default).
#        bash install_hermes_a2a.sh
#   2) Non-interactive (flags / piped): pass values explicitly.
#        curl -sSL <raw-url> | bash -s -- \
#            --my-peer-id hermes-home \
#            --your-peer-id ww-agent \
#            --public-host home.example.com \
#            --hmac-secret "$(openssl rand -hex 32)"

set -euo pipefail

MY_PEER_ID=""
YOUR_PEER_ID=""
PUBLIC_HOST=""
HMAC_SECRET=""
HERMES_BIN="${HERMES_BIN:-hermes}"
WW_AGENT_REPO="${WW_AGENT_REPO:-https://github.com/ww-agent/ww-agent.git}"
WW_AGENT_DIR="${WW_AGENT_DIR:-$HOME/.hermes-a2a/ww-agent}"
CADDY_PORT="${CADDY_PORT:-8443}"
BRIDGE_PORT="${BRIDGE_PORT:-19444}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --my-peer-id) MY_PEER_ID="$2"; shift 2;;
    --your-peer-id) YOUR_PEER_ID="$2"; shift 2;;
    --public-host) PUBLIC_HOST="$2"; shift 2;;
    --hmac-secret) HMAC_SECRET="$2"; shift 2;;
    -h|--help)
      echo "usage: install_hermes_a2a.sh [--my-peer-id X] [--your-peer-id X] [--public-host X] [--hmac-secret X]"
      echo "  run with no flags for interactive prompts."
      exit 0;;
    *) echo "unknown flag: $1" >&2; exit 2;;
  esac
done

have_tty() { [[ -e /dev/tty ]]; }

# ask VARNAME "prompt" "default" — only prompts if the var is still empty and a TTY exists.
ask() {
  local __var="$1" __prompt="$2" __default="$3" __ans=""
  if [[ -n "${!__var}" ]]; then return 0; fi
  if ! have_tty; then return 0; fi
  if [[ -n "$__default" ]]; then
    read -r -p "$__prompt [$__default]: " __ans < /dev/tty || true
    printf -v "$__var" '%s' "${__ans:-$__default}"
  else
    read -r -p "$__prompt: " __ans < /dev/tty || true
    printf -v "$__var" '%s' "$__ans"
  fi
}

ask MY_PEER_ID   "Remote (this machine) peer id" "hermes-home"
ask YOUR_PEER_ID "Your laptop's W&W Agent peer id (must equal its COMM_AGENT_MY_PEER_ID)" "ww-agent"
ask PUBLIC_HOST  "Public host name (e.g. home.example.com)" ""
if [[ -z "$HMAC_SECRET" ]] && have_tty; then
  read -r -p "HMAC secret (blank = auto-generate): " HMAC_SECRET < /dev/tty || true
fi

# Defaults for any value still empty (non-interactive path).
MY_PEER_ID="${MY_PEER_ID:-hermes-home}"
YOUR_PEER_ID="${YOUR_PEER_ID:-ww-agent}"
if [[ -z "$HMAC_SECRET" ]]; then
  HMAC_SECRET="$(openssl rand -hex 32)"
  echo "  generated HMAC secret: $HMAC_SECRET"
fi
if [[ -z "$PUBLIC_HOST" ]]; then
  echo "ERROR: --public-host is required (no value given and no TTY to prompt)." >&2
  exit 2
fi

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

echo "==> [2/7] Fetching W&W Agent (for the reused A2A server modules)"
if [[ -d "$WW_AGENT_DIR/.git" ]]; then
  git -C "$WW_AGENT_DIR" pull --ff-only || echo "  (pull skipped)"
else
  mkdir -p "$(dirname "$WW_AGENT_DIR")"
  git clone --depth 1 "$WW_AGENT_REPO" "$WW_AGENT_DIR"
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
echo "  Start the bridge (loads env, runs from the W&W Agent checkout):"
echo "    cd $WW_AGENT_DIR && set -a && . $ENV_FILE && set +a && python3 -m bridge.hermes_a2a"
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

IMPORTANT — peer id must match:
  On your laptop, COMM_AGENT_MY_PEER_ID must equal '$YOUR_PEER_ID'
  (its default is already 'ww-agent'). If it differs, the bridge will reject
  the call with 'caller peer not allowed'.

Next step on your W&W Agent machine — register this peer:
    comm.add_peer peer_id=$MY_PEER_ID \\
                  url=https://$PUBLIC_HOST:$CADDY_PORT \\
                  hmac_secret_value=$HMAC_SECRET

(Keep that HMAC secret safe — it's the only copy printed.)
EOF
