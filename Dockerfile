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

# --- Cloudflare WARP tools, built from pinned releases (wgcf v2.3.1 registers
# a free WARP account; wireproxy v1.1.3 turns it into a local SOCKS proxy —
# Render can't create network interfaces, so it runs in userspace).
FROM golang:1.26-bookworm AS warp
ENV CGO_ENABLED=0
RUN git clone -q --depth 1 --branch v2.3.1 https://github.com/ViRb3/wgcf /wgcf \
 && test "$(git -C /wgcf rev-parse HEAD)" = de6bba7147dc965a1b8e4de113a709750697ccea \
 && cd /wgcf && go build -trimpath -o /out/wgcf .
RUN git clone -q --depth 1 --branch v1.1.3 https://github.com/whyvl/wireproxy /wireproxy \
 && test "$(git -C /wireproxy rev-parse HEAD)" = 31a9a34498267d7fb8aeff05aba79d929b15eb39 \
 && cd /wireproxy && go build -trimpath -o /out/wireproxy ./cmd/wireproxy

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
# yt-dlp plugin: the original visionOS client (visionos_old)
COPY agent/plugins /app/plugins
COPY --from=pot /src/server/package.json /pot/package.json
COPY --from=pot /src/server/build /pot/build
COPY --from=pot /src/server/node_modules /pot/node_modules
COPY --from=warp /out/wgcf /out/wireproxy /usr/local/bin/
COPY agent/start.sh /app/start.sh
ENV OUT_DIR=/tmp/downloads PYTHONUNBUFFERED=1
# start.sh runs the token server and WARP in the background, then the agent.
CMD ["sh", "/app/start.sh"]
