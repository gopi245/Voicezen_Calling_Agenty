#!/usr/bin/env bash
# Deploy the voice agent to the VPS, isolated from everything already there.
#
# Run this ON THE VPS from inside /opt/gundusoodi after copying the files up.
# It never edits a shared config, never opens a port, and never restarts
# another container.
set -euo pipefail

cd "$(dirname "$0")"

echo "==> Pre-flight"

command -v docker >/dev/null || { echo "docker not installed"; exit 1; }
docker compose version >/dev/null || { echo "docker compose v2 not available"; exit 1; }

[ -f .env ] || { echo ".env missing -- copy it up before deploying"; exit 1; }

# Never clobber a name another stack already uses.
if docker ps -a --format '{{.Names}}' | grep -qx gundusoodi-agent; then
  echo "note: an existing gundusoodi-agent container will be replaced"
fi

echo
echo "==> Existing services (these must be untouched afterwards)"
docker ps --format '  {{.Names}}  ({{.Status}})'

echo
echo "==> Region check -- the latency case depends on this"
for host in api.groq.com api.cartesia.ai api.sarvam.ai generativelanguage.googleapis.com; do
  t=$(curl -s -o /dev/null -w '%{time_connect}' "https://$host" || echo "n/a")
  echo "  $host  tcp_connect=${t}s"
done
echo "  (under ~0.05s means this box is genuinely close; a laptop in India sees ~0.22s)"

echo
echo "==> Build"
touch captured.jsonl
docker compose build

echo
echo "==> Smoke test (no phone call needed)"
# Catches bad config ranges, dead model names, prompt drift and a too-long
# greeting BEFORE the worker goes live.
docker compose run --rm --no-deps voice-agent python smoke_test.py

echo
echo "==> Start"
docker compose up -d

echo
echo "==> Waiting for the worker to register (up to 120s)"
for i in $(seq 1 40); do
  if docker compose logs 2>/dev/null | grep -q "registered worker"; then
    echo "  registered after ~$((i*3))s -- safe to call now"
    break
  fi
  sleep 3
done

echo
echo "==> Final state"
docker ps --format '  {{.Names}}  ({{.Status}})'
echo
echo "Logs:      docker compose logs -f"
echo "Transcript: docker compose logs | grep TRANSCRIPT"
echo "Remove:    ./uninstall.sh"
