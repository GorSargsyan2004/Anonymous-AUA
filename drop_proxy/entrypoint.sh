#!/bin/sh
set -eu

export DROP_BACKEND_UPSTREAM="${DROP_BACKEND_UPSTREAM:-bot:8000}"
export DROP_INTERNAL_PROXY_SECRET="${DROP_INTERNAL_PROXY_SECRET:-}"

# Validate and sanitize DROP_ONION_HOSTNAME to build DROP_ONION_HEADER_LINE safely
CLEAN_ONION="$(printf '%s' "${DROP_ONION_HOSTNAME:-}" | tr '[:upper:]' '[:lower:]' | sed -E 's#^https?://##; s#/.*$##; s#[[:space:]]##g')"

if printf '%s' "$CLEAN_ONION" | grep -Eq '^[a-z2-7]{16,56}\.onion$'; then
    export DROP_ONION_HEADER_LINE="add_header Onion-Location \"http://${CLEAN_ONION}\$request_uri\" always;"
else
    export DROP_ONION_HEADER_LINE="# Onion-Location disabled (DROP_ONION_HOSTNAME not set)"
fi

# Substitute ONLY our 3 explicit template variables so Nginx runtime vars ($request_uri) stay intact
envsubst '${DROP_INTERNAL_PROXY_SECRET} ${DROP_ONION_HEADER_LINE} ${DROP_BACKEND_UPSTREAM}' \
    < /etc/nginx/templates/nginx.conf.template \
    > /tmp/nginx.conf

nginx -t -c /tmp/nginx.conf
if [ "${1:-}" = "--verify-only" ]; then
    exit 0
fi
exec nginx -c /tmp/nginx.conf -g "daemon off;"
