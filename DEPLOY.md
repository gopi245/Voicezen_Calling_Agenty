# Deploying to the Hostinger VPS

Target: KVM 4 (4 vCPU / 16 GB) already running Dograh and n8n.

## Why it fits

The worker does **no local inference** — Sarvam STT, Groq LLM and Cartesia TTS
are all cloud APIs, and `turn_detection="stt"` means no VAD or turn-detector
model is loaded. A runtime check confirms neither torch, onnxruntime nor
transformers is imported. It is an I/O-bound process, not an ML workload.

| | expected |
|---|---|
| Idle memory | ~1 GB |
| Per active call | ~200–400 MB |
| Peak (3 concurrent, the Vobiz account ceiling) | ~2.5 GB |
| CPU | well under 1 vCPU |

Compose caps it at 4 GB / 2 vCPU so it can never starve Dograh or n8n.

## Region matters more than specs

The whole latency case rests on this. From a laptop in India every API hop
costs ~220 ms of TCP round-trip. In a Mumbai or Bangalore datacentre that is
5–30 ms.

**Verify before deploying:**

```bash
curl -s -o /dev/null -w 'groq %{time_connect}s\n'     https://api.groq.com
curl -s -o /dev/null -w 'cartesia %{time_connect}s\n' https://api.cartesia.ai
curl -s -o /dev/null -w 'sarvam %{time_connect}s\n'   https://api.sarvam.ai
```

Under ~0.05 s means the win is real. If it comes back at 0.2 s or higher the
VPS is not in India, and deploying there will be **no better than the laptop**
— possibly worse.

## Deploy

```bash
# 1. copy the project up (excluding junk)
rsync -av --exclude __pycache__ --exclude .venv --exclude captured.jsonl \
      ./ user@your-vps:/opt/gundusoodi/

# 2. on the VPS
cd /opt/gundusoodi
docker compose build

# 3. verify BEFORE going live -- catches bad config, dead models,
#    prompt drift and a too-long greeting without placing a call
docker compose run --rm voice-agent python smoke_test.py

# 4. start
docker compose up -d
docker compose logs -f
```

Wait for `registered worker` in the logs before dialling. Startup prewarms two
processes and their provider clients, so allow ~30–60 s.

## Operating it

```bash
docker compose logs -f                       # live call timeline
docker compose logs | grep TRANSCRIPT        # what was actually said
docker compose restart voice-agent           # after an .env change
docker compose run --rm voice-agent python smoke_test.py   # before any restart
```

Captured orders and complaints land in `captured.jsonl` inside the container.
To keep them on the host, add to `docker-compose.yml`:

```yaml
    volumes:
      - ./captured.jsonl:/app/captured.jsonl
```

## Notes

- **No inbound ports.** The agent dials out to LiveKit over WebSocket and
  receives calls through that connection. No firewall rule, no port forward,
  and port 8081 stays inside the container so it cannot clash with n8n.
- **`.env` is never baked into the image** — compose injects it at runtime via
  `env_file`, so secrets stay out of the image layers.
- **Linux uses `fork`, not Windows `spawn`**, so per-call process init and the
  slow restart window both shrink substantially.
- **Rotate the API keys** before this becomes a real deployment; they were
  pasted into a chat transcript.
