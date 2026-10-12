#!/bin/sh
# Starts the helpers next to the agent. Each one is optional: if it fails,
# the agent still runs (and says so in the site's log).

# 1. PO-token server (bgutil) — the "proof of origin" YouTube asks servers for.
node /pot/build/main.js --host 127.0.0.1 --port 4416 > /tmp/pot.log 2>&1 &

# 2. Cloudflare WARP as a local SOCKS proxy: the server's own traffic to
#    YouTube leaves through Cloudflare's network instead of Render's address.
#    (Nothing changes for the computer at home — it still only talks to the
#    Worker and to this server.) USE_WARP=0 turns it off.
if [ "${USE_WARP:-1}" = "1" ]; then
  (
    cd /tmp || exit 1
    if [ ! -f wgcf-profile.conf ]; then
      wgcf register --accept-tos > /tmp/warp.log 2>&1 && wgcf generate >> /tmp/warp.log 2>&1 || exit 1
    fi
    printf 'WGConfig = /tmp/wgcf-profile.conf\n\n[Socks5]\nBindAddress = 127.0.0.1:40000\n' > /tmp/wireproxy.conf
    exec wireproxy -c /tmp/wireproxy.conf >> /tmp/warp.log 2>&1
  ) &
fi

exec python /app/agent.py
