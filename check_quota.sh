#!/usr/bin/env bash
# Daily LiveKit quota check. Writes QUOTA_ALERT.txt at 90%.
cd /opt/gundusoodi
docker compose run --rm --no-deps -v /opt/gundusoodi/usage.py:/app/usage.py \
  -v /opt/gundusoodi:/opt/gundusoodi voice-agent python /app/usage.py \
  >> /opt/gundusoodi/quota.log 2>&1
