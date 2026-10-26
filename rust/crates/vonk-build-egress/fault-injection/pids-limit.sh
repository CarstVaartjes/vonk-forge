#!/usr/bin/env bash
# Spawn-failure evidence for vonk-build-egress: run the proxy (built like the
# release profile) in a container whose task limit is too small for 64 parallel
# CONNECT tunnels, so thread creation fails mid-load, and check that it
# survives, answers 503, releases slots and admits a fresh tunnel afterwards.
# Needs docker, rustc, python3 and outbound access to pypi.org.
#   PIDS="90 94 98 100 104" rust/crates/vonk-build-egress/fault-injection/pids-limit.sh [old.rs]
# An optional proxy source (for example `git show origin/main:...main.rs`) shows
# the pre-fix proxy aborting (exit 133) at some limits.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
work=$(mktemp -d)
trap 'docker rm -f egress-fi >/dev/null 2>&1 || true; rm -rf "$work"' EXIT
source=${1:-$here/../src/main.rs}
rustc --edition=2024 -C panic=abort -C opt-level=3 "$source" -o "$work/proxy"
cat > "$work/client.py" <<'PY'
import socket, sys, time
ip, count = sys.argv[1], int(sys.argv[2])
def tunnel():
    s = socket.create_connection((ip, 18080), timeout=10)
    s.sendall(b"CONNECT pypi.org:443 HTTP/1.1\r\nHost: pypi.org:443\r\n\r\n")
    return s, s.recv(12)
held, codes = [], {}
for _ in range(count):
    try:
        s, r = tunnel()
    except OSError:
        codes["refused"] = codes.get("refused", 0) + 1
        continue
    code = r[9:12].decode(errors="replace")
    codes[code] = codes.get(code, 0) + 1
    (held if r.startswith(b"HTTP/1.1 200") else []).append(s)
print("statuses", codes, "held", len(held))
for s in held:
    s.close()
deadline = time.time() + 15
admitted = False
while time.time() < deadline and not admitted:
    try:
        admitted = tunnel()[1].startswith(b"HTTP/1.1 200")
    except OSError:
        pass
    time.sleep(0.5)
print("fresh admission after release:", admitted)
sys.exit(0 if admitted else 1)
PY
status=0
for pids in ${PIDS:-90 94 98 100 104}; do
  docker rm -f egress-fi >/dev/null 2>&1 || true
  docker run -d --name egress-fi --pids-limit="$pids" --memory=128m \
    -v "$work:/b:ro" rust:1.98.1-slim /b/proxy --allow-host pypi.org >/dev/null
  sleep 1
  ip=$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' egress-fi)
  echo "== pids-limit=$pids"
  python3 "$work/client.py" "$ip" 64 || status=1
  echo "proxy: $(docker inspect -f 'running={{.State.Running}} exit={{.State.ExitCode}}' egress-fi);" \
    "$(docker logs egress-fi 2>&1 | grep -c 'no thread for a connection') refused for lack of a thread"
  [ "$(docker inspect -f '{{.State.Running}}' egress-fi)" = true ] || status=1
done
exit $status
