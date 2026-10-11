# Download agent for Render (free "Web Service", Docker runtime).
# Render: New → Web Service → this repo → Language: Docker. See README.md.
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
ENV OUT_DIR=/tmp/downloads PYTHONUNBUFFERED=1
CMD ["python", "/app/agent.py"]
