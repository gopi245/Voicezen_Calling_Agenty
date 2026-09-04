# Slim, not alpine: the livekit wheels are manylinux and alpine's musl libc
# forces a source build of the rtc bindings.
FROM python:3.13-slim

# Audio plugins link against libstdc++ and need CA certs for the provider APIs.
# git is only for requirements.txt's livekit-plugins-raya line -- it isn't on
# PyPI yet, pip installs it straight from GitHub at build time.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates libstdc++6 git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy requirements first so a code change does not re-download the wheels.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY agent.py stack.py voicezen.py smoke_test.py stt_test.py ./
COPY clients/ ./clients/

# Unbuffered so `docker logs` shows the call timeline live rather than in
# 8KB chunks -- this is the only way to watch a call in progress.
ENV PYTHONUNBUFFERED=1

# Two idle processes covers the Vobiz account's 3-concurrent-call ceiling
# with a spare, and keeps idle memory near 1GB.
ENV NUM_IDLE_PROCESSES=2

# `start` is production mode. `dev` keeps ZERO idle processes, which adds a
# full process spawn to the front of every call.
CMD ["python", "agent.py", "start"]
