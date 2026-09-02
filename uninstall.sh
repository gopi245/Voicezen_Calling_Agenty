#!/usr/bin/env bash
# Remove this project from the VPS completely, touching nothing else.
#
# Scoped by the compose project name "gundusoodi", so it cannot match the
# containers, networks or images belonging to Dograh, n8n, or anything else
# on the host. It deliberately does NOT run `docker system prune`, which
# would reach outside this project.
set -euo pipefail

cd "$(dirname "$0")"

echo "Stopping and removing the gundusoodi stack..."
# --rmi local drops the image we built; --volumes drops volumes we declared.
docker compose --project-name gundusoodi down --rmi local --volumes --remove-orphans

echo
echo "Remaining gundusoodi artefacts (should be none):"
docker ps -a    --filter "name=gundusoodi" --format '  container: {{.Names}}'  || true
docker images   --filter "reference=gundusoodi-agent" --format '  image: {{.Repository}}:{{.Tag}}' || true
docker network ls --filter "name=gundusoodi_net" --format '  network: {{.Name}}' || true

echo
echo "Untouched (confirm your other services are still running):"
docker ps --format '  {{.Names}}  ({{.Status}})'

echo
echo "Docker artefacts removed. To delete the files as well:"
echo "  cd .. && rm -rf $(basename "$PWD")"
