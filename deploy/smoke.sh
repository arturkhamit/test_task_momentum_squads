#!/usr/bin/env bash
# Smoke through the loopback ports the shared Caddy proxies to: deploy/smoke.sh
# Used by deploy.sh and rollback.sh. The full check from outside is tests/smoke.py (deploy/README.md).
set -euo pipefail

for url in http://127.0.0.1:18030/health http://127.0.0.1:18030/ready http://127.0.0.1:18031/replies; do
    curl -fsS --max-time 10 --retry 10 --retry-delay 3 --retry-connrefused "$url" > /dev/null
    echo "smoke $url ok"
done
