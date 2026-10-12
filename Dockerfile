# Download agent for Render (free "Web Service", Docker runtime).
# Render: New → Web Service → this repo → Language: Docker. See README.md.

# --- PO-token server (bgutil, pinned to a reviewed commit = version 2.0.2).
# YouTube asks cloud servers for a "proof of origin" token; this generates
# it, and the yt-dlp plugin (requirements.txt) fetches it from 127.0.0.1:4416.
# This is the piece of the zmani anti-bot stack that was missing here.
FROM node:22-bookworm-slim AS pot
RUN apt-get update -qq && apt-get install -y -qq git ca-certificates >/dev/null \
 && rm -rf /var/lib/apt/lists/*
ARG BGUTIL_COMMIT=26475e9d3665b972c4aeb83b4dbaafcf64b88438
RUN git clone -q https://github.com/Brainicism/bgutil-ytdlp-pot-provider /src \
 && git -C /src checkout -q "$BGUTIL_COMMIT"
WORKDIR /src/server
RUN npm ci --no-audit --no-fund && npx tsc && npm prune --omit=dev --no-audit --no-fund

FROM python:3.12-slim-bookworm
RUN apt-get update -qq && apt-get install -y -qq ffmpeg ca-certificates >/dev/null \
 && rm -rf /var/lib/apt/lists/*
# A real `node` on PATH: yt-dlp needs it (with yt-dlp[default]'s solver
# script) to pass YouTube's JS challenge.
COPY --from=node:22-bookworm-slim /usr/local/bin/node /usr/local/bin/node
WORKDIR /app
COPY agent/requirements.txt .
RUN pip install --no-cache-dir -U -r requirements.txt
COPY agent/agent.py .
COPY --from=pot /src/server/package.json /pot/package.json
COPY --from=pot /src/server/build /pot/build
COPY --from=pot /src/server/node_modules /pot/node_modules
ENV OUT_DIR=/tmp/downloads PYTHONUNBUFFERED=1
# The token server runs in the background; if it ever fails, the agent still
# works (yt-dlp just goes without tokens).
CMD ["sh", "-c", "node /pot/build/main.js --host 127.0.0.1 --port 4416 > /tmp/pot.log 2>&1 & exec python /app/agent.py"]
