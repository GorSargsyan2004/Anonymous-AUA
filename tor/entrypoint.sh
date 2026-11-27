#!/bin/sh
set -eu

HS_DIR="/var/lib/tor/auahub_drop"
RUNTIME_TORRC="/tmp/torrc.runtime"

mkdir -p "$HS_DIR"
chmod 700 /var/lib/tor "$HS_DIR"

# Resolve target hostname (e.g. drop_proxy) to an IPv4 address because Tor's HiddenServicePort requires an IP literal
TARGET_HOST="${DROP_PROXY_HOST:-drop_proxy}"
TARGET_PORT="${DROP_PROXY_PORT:-8089}"
TARGET_IP="$(getent ahostsv4 "$TARGET_HOST" 2>/dev/null | awk 'NR==1{print $1}' || true)"
if [ -z "$TARGET_IP" ]; then
    TARGET_IP="127.0.0.1"
fi

sed "s#drop_proxy:8089#${TARGET_IP}:${TARGET_PORT}#g" /etc/tor/torrc.base > "$RUNTIME_TORRC"

# Auto-check whether the installed Tor binary supports Equi-X Proof-of-Work (PoW) DoS defense
POW_TEST_TORRC="/tmp/torrc.powtest"
cp "$RUNTIME_TORRC" "$POW_TEST_TORRC"
cat >> "$POW_TEST_TORRC" <<'EOF'
HiddenServicePoWDefensesEnabled 1
HiddenServicePoWQueueRate 25
HiddenServicePoWQueueBurst 250
EOF

if tor --verify-config -f "$POW_TEST_TORRC" >/dev/null 2>&1; then
    echo "[tor_drop] HiddenServicePoWDefensesEnabled is supported and enabled."
    mv "$POW_TEST_TORRC" "$RUNTIME_TORRC"
else
    echo "[tor_drop] Tor binary built without PoW module; using IntroDoS + MaxStreams defenses."
    rm -f "$POW_TEST_TORRC"
fi

tor --verify-config -f "$RUNTIME_TORRC" >/dev/null

if [ "${1:-}" = "--verify-only" ]; then
    exec tor --verify-config -f "$RUNTIME_TORRC"
fi

# Print the public .onion hostname once keys are generated (never print secret key!)
(
    i=0
    while [ "$i" -lt 30 ]; do
        if [ -s "${HS_DIR}/hostname" ]; then
            echo "================================================================"
            echo "[tor_drop] Tor v3 Hidden Service Hostname: $(cat "${HS_DIR}/hostname")"
            echo "================================================================"
            break
        fi
        i=$((i + 1))
        sleep 1
    done
) &

exec tor -f "$RUNTIME_TORRC"
