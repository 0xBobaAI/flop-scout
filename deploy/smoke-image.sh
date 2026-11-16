#!/usr/bin/env bash
# Runs the published image and talks to it over MCP and HTTP.
#
# The registry and Glama both point consumers at this artefact, so "it built" is
# not enough: an image that crashes on start, or serves no tools, is worse than
# publishing no package at all. Everything here is asserted against the image
# that was pushed, not a fresh local build, so a cache miss cannot hide a defect.
#
# --network host puts uvicorn's peer at 127.0.0.1, which is the topology the
# FORWARDED_ALLOW_IPS default assumes: a proxy on the same machine.
set -euo pipefail

IMAGE="${1:?usage: smoke-image.sh <image:tag>}"
PORT="${PORT:-8080}"
BASE="http://127.0.0.1:${PORT}"
NAME=scout-smoke

fail() {
  echo "::error::$*" >&2
  docker logs --tail 120 "$NAME" 2>&1 || true
  exit 1
}

cleanup() { docker rm -f "$NAME" >/dev/null 2>&1 || true; }
trap cleanup EXIT

docker rm -f "$NAME" >/dev/null 2>&1 || true
docker pull "$IMAGE"
docker run -d --name "$NAME" --network host "$IMAGE"

# /robots.txt rather than /api/health: health reaches the FLOP gateway and can
# take a couple of seconds, which would make a healthy container look sick.
ready=0
for _ in $(seq 1 30); do
  if curl -fsS "$BASE/robots.txt" >/dev/null 2>&1; then ready=1; break; fi
  sleep 2
done
[ "$ready" = 1 ] || fail "container did not become ready in 60s"
echo "  ready: $IMAGE"

MCP_HDR=(-H 'content-type: application/json' -H 'Accept: application/json, text/event-stream')

INIT=$(curl -fsS "${MCP_HDR[@]}" -X POST "$BASE/mcp" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"ci","version":"1"}}}')
echo "$INIT" | python3 -c '
import json, sys
d = json.load(sys.stdin)["result"]
si = d["serverInfo"]
assert si["name"] == "flop-scout", si
assert "protocolVersion" in d, d
print("  initialize:", si)
' || fail "MCP initialize did not return flop-scout"

curl -fsS "${MCP_HDR[@]}" -X POST "$BASE/mcp" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}' | python3 -c '
import json, sys
t = json.load(sys.stdin)["result"]["tools"]
# A floor, not an equality: adding a tool must not fail CI, dropping one must.
assert len(t) >= 10, f"expected at least 10 tools, got {len(t)}"
assert "scout_read" in {x["name"] for x in t}, "scout_read is missing"
bad = [x["name"] for x in t if not x.get("description") or not x.get("inputSchema")]
assert not bad, f"tools missing description or schema: {bad}"
print(f"  tools: {len(t)}, all described and schematised")
' || fail "MCP tools/list is wrong"

# A GET must not be able to open an SSE stream or claim a session: the server is
# stateless by design, and a client that sees 404 thinks the endpoint is missing.
CODE=$(curl -s -o /dev/null -w '%{http_code}' "$BASE/mcp")
[ "$CODE" = 405 ] || fail "GET /mcp returned $CODE, expected 405"
echo "  GET /mcp: 405 as a stateless server should answer"

QUOTE=$(curl -fsS -X POST "$BASE/api/quote" -H 'content-type: application/json' \
  -d '{"sources":["keenable"],"q":"how many miners","model":"flop:qwen/qwen2.5-0.5b-instruct"}')
echo "$QUOTE" | python3 -c '
import json, sys
d = json.load(sys.stdin)
assert d["total_flop"] > 0, d
inf = d.get("inference") or {}
assert inf.get("floor"), f"no inference estimate: {inf}"
print("  quote: %s FLOP fee, miner floor %s FLOP" % (d["total_flop"], inf["floor"]["cost_flop"]))
' || fail "/api/quote did not price the request"

# A URL that is never fetched: the validator must refuse it before any network
# call, so this checks the guard without depending on the reader being up.
CODE=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$BASE/api/read" \
  -H 'content-type: application/json' -d '{"url":"http://169.254.169.254/latest/meta-data/"}')
[ "$CODE" = 422 ] || fail "/api/read accepted a link-local address (HTTP $CODE)"
echo "  /api/read refuses link-local addresses"

# --proxy-headers is what makes client_id() see the caller behind a same-host
# proxy; without it every caller shares one rate-limit bucket.
#
# /api/quote only reads the quota (peek_quota), it never consumes a slot, so
# comparing remaining counts across two IPs proves nothing: both are 20. The
# evidence is what uvicorn records as the peer. With --proxy-headers and
# --forwarded-allow-ips=127.0.0.1 the access log shows the forwarded address;
# without them it shows 127.0.0.1 for everything.
H2=$(curl -fsS -o /dev/null -D - -X POST "$BASE/api/quote"       -H 'content-type: application/json' -H 'x-forwarded-for: 203.0.113.1'       -d '{"sources":["keenable"]}')
echo "$H2" | grep -qi '^x-ratelimit-remaining:' || fail "no X-RateLimit-remaining header"
echo "  quote carries X-RateLimit-* headers"
docker logs "$NAME" 2>&1 | grep -q '203\.0\.113\.1'   || fail "uvicorn did not record the forwarded peer: --proxy-headers is not in effect"
echo "  uvicorn logged the forwarded peer 203.0.113.1"

UID_IN=$(docker exec "$NAME" id -u)
[ "$UID_IN" != 0 ] || fail "container runs as root"
echo "  runs as uid $UID_IN (unprivileged)"

docker exec "$NAME" sh -c 'touch /data/.probe && rm -f /data/.probe' \
  || fail "/data is not writable by the container user"
echo "  /data writable"

# A secret that survived into the image would be readable from inside it. Check
# the path the Dockerfile is supposed to clear, not the build context. The names
# are exact: static/wallet.js is the public sr25519 bundle and matches a loose
# "wallet" pattern, which would fail this every time.
LEAK=$(docker exec "$NAME" sh -c 'ls -A /app | grep -iE "\.wallet|\.pem$|priv\.hex|\.db$|\.seed$|mcp-key" || true')
[ -z "$LEAK" ] || fail "secret-looking files in the image: $LEAK"
echo "  no secret-bearing files in /app"

echo "OK $IMAGE"
