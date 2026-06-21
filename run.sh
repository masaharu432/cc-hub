#!/usr/bin/env bash
# Start the launcher. Idempotent: refuses to double-bind the port.
set -euo pipefail
cd "$(dirname "$0")"
exec python3 server.py
