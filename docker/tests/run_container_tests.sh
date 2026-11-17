#!/usr/bin/env bash
# Container acceptance tests: run from the repository root on a host with Docker.
#
#   bash docker/tests/run_container_tests.sh            # full run
#   KEEP=1 bash docker/tests/run_container_tests.sh     # keep the test project afterwards
#
# Covers: image build, compose refusing to start without APP_MASTER_KEY, key generation,
# non-root from PID 1, empty capabilities, no privileged / docker socket / devices /
# host network, read-only root filesystem, writes only to /data /state /tmp, port 80
# inside and 7777 on the host, BIND_ADDRESS, ALLOWED_NETWORKS, setup token, login,
# reset-admin-password (with the app running), kill during a scan and during moves with
# correct resume, the whole Python test-suite inside the hardened container (real
# ExifTool + ffmpeg), and MariaDB tests against a throw-away MariaDB container.
# Nothing here touches your real library or state: everything lives in a temp dir.
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
WORK="$(mktemp -d /tmp/twincheck-ct.XXXXXX)"
PROJECT="twincheck-ct-$$"
PASS=0; FAIL=0; RESULTS=()
export COMPOSE_PROJECT_NAME="$PROJECT"

ok()   { PASS=$((PASS+1)); RESULTS+=("PASS  $1"); echo "PASS  $1"; }
ko()   { FAIL=$((FAIL+1)); RESULTS+=("FAIL  $1"); echo "FAIL  $1"; }
check(){ local name="$1"; shift; if "$@" >/dev/null 2>&1; then ok "$name"; else ko "$name"; fi; }
json() { python3 -c "import sys,json; d=json.load(sys.stdin); print($1)"; }

cleanup() {
  dc down -v --remove-orphans >/dev/null 2>&1 || true
  docker rm -f "${PROJECT}-mariadb" >/dev/null 2>&1 || true
  [ -n "${KEEP:-}" ] || rm -rf "$WORK"
}
trap cleanup EXIT

mkdir -p "$WORK/library" "$WORK/state"
cp "$ROOT/.env.example" "$WORK/.env"
chmod 600 "$WORK/.env"
setenv() { # setenv NAME VALUE  (in the test .env)
  if grep -q "^$1=" "$WORK/.env"; then sed -i "s|^$1=.*|$1=$2|" "$WORK/.env"; else echo "$1=$2" >> "$WORK/.env"; fi
}
CUID="$(id -u)"; CGID="$(id -g)"
if [ "$CUID" = 0 ]; then CUID=1000; CGID=1000; chown -R 1000:1000 "$WORK/library" "$WORK/state"; fi   # never run the app as root
setenv PUID "$CUID"; setenv PGID "$CGID"
setenv DATA_PATH "$WORK/library"; setenv STATE_PATH "$WORK/state"
setenv CONTAINER_NAME "$PROJECT"; setenv HOST_LABEL "ct-host"
dc() { docker compose -f "$ROOT/docker/docker-compose.yml" --env-file "$WORK/.env" -p "$PROJECT" "$@"; }

echo "== build"
check "image builds" dc build --pull
IMG="$(dc config --images | head -1)"
check "no compiler in the final image" bash -c "! docker run --rm --entrypoint sh $IMG -c 'command -v gcc || command -v cc || command -v make'"
check "exiftool, ffmpeg, python3 present" docker run --rm --entrypoint sh "$IMG" -c 'command -v exiftool && command -v ffmpeg && command -v python3'

echo "== master key"
sed -i '/^APP_MASTER_KEY=/d' "$WORK/.env"
check "compose refuses to start without APP_MASTER_KEY" bash -c "! docker compose -f $ROOT/docker/docker-compose.yml --env-file $WORK/.env -p $PROJECT config >/dev/null 2>&1"
echo "APP_MASTER_KEY=CHANGE_ME" >> "$WORK/.env"
KEYLINE="$(dc run --rm --no-deps app generate-key 2>/dev/null | grep '^APP_MASTER_KEY=')"
check "generate-key prints a 64-hex key" bash -c "[[ '$KEYLINE' =~ ^APP_MASTER_KEY=[0-9a-f]{64}$ ]]"
dc up -d >/dev/null 2>&1; sleep 4
check "container refuses the placeholder key" bash -c "[ \"\$(docker inspect -f '{{.State.Running}}' $PROJECT 2>/dev/null)\" != true ] || docker logs $PROJECT 2>&1 | grep -q KEY_INVALID"
dc down >/dev/null 2>&1
sed -i "s|^APP_MASTER_KEY=.*|$KEYLINE|" "$WORK/.env"

echo "== start"
dc up -d >/dev/null 2>&1
for i in $(seq 1 60); do [ "$(docker inspect -f '{{.State.Health.Status}}' "$PROJECT" 2>/dev/null)" = healthy ] && break; sleep 2; done
check "container healthy (healthcheck on internal port 80)" bash -c "[ \"\$(docker inspect -f '{{.State.Health.Status}}' $PROJECT)\" = healthy ]"

echo "== no special permissions"
I() { docker inspect -f "$1" "$PROJECT"; }
CFGUSER="$(I '{{.Config.User}}')"
check "configured user is non-root ($CFGUSER)" bash -c "[ '$CFGUSER' = '$CUID:$CGID' ] && [ '${CFGUSER%%:*}' != 0 ]"
check "PID 1 runs as non-root" bash -c "docker exec $PROJECT awk '/^Uid:/{exit (\$2==0)}' /proc/1/status"
check "effective capabilities empty" bash -c "docker exec $PROJECT awk '/^CapEff:/{exit (\$2!=\"0000000000000000\")}' /proc/1/status"
check "cap_drop ALL, no cap_add" bash -c "[[ \"$(I '{{.HostConfig.CapDrop}}')\" == *ALL* ]] && [ \"$(I '{{.HostConfig.CapAdd}}')\" = '[]' ]"
check "not privileged" bash -c "[ \"$(I '{{.HostConfig.Privileged}}')\" = false ]"
check "no-new-privileges" bash -c "[[ \"$(I '{{.HostConfig.SecurityOpt}}')\" == *no-new-privileges* ]]"
check "read-only root filesystem" bash -c "[ \"$(I '{{.HostConfig.ReadonlyRootfs}}')\" = true ]"
check "no host network" bash -c "[ \"$(I '{{.HostConfig.NetworkMode}}')\" != host ]"
check "no devices" bash -c "[ \"$(I '{{.HostConfig.Devices}}')\" = '[]' ]"
check "no docker socket mounted" bash -c "! docker inspect $PROJECT | grep -q docker.sock"
check "only sysctl is ip_unprivileged_port_start" bash -c "[[ \"$(I '{{.HostConfig.Sysctls}}')\" == 'map[net.ipv4.ip_unprivileged_port_start:0]' ]]"
check "write to / fails" bash -c "! docker exec $PROJECT sh -c 'touch /x' 2>/dev/null"
check "write to /opt fails" bash -c "! docker exec $PROJECT sh -c 'touch /opt/twincheck/x' 2>/dev/null"
check "write to /data, /state, /tmp works" docker exec "$PROJECT" sh -c 'touch /data/.w /state/.w /tmp/.w && rm /data/.w /state/.w /tmp/.w'
check "no .pyc written" bash -c "[ -z \"\$(docker exec $PROJECT find / -xdev -name '*.pyc' -newer /proc/1/cmdline 2>/dev/null | head -1)\" ]"
check "no secret in state files" bash -c "! grep -rqa 'admin\|scrypt' $WORK/state --include=vault.enc"

echo "== network"
check "port 80 inside the container" docker exec "$PROJECT" wget -q -O /dev/null http://127.0.0.1/healthz
check "port 7777 on the host" curl -fsS -o /dev/null http://127.0.0.1:7777/healthz
check "no TLS listener (HTTPS on 7777 fails)" bash -c "! curl -ksS -o /dev/null https://127.0.0.1:7777/ 2>/dev/null"
HOSTIP="$(hostname -I 2>/dev/null | awk '{print $1}')"
[ -n "$HOSTIP" ] && check "reachable on the host LAN address $HOSTIP:7777" curl -fsS -o /dev/null "http://$HOSTIP:7777/healthz"

echo "== setup and login"
TOKEN="$(docker logs "$PROJECT" 2>&1 | sed -n 's/.*\[SETUP_TOKEN\] Setup token: //p' | tail -1)"
check "setup token printed in the log" test -n "$TOKEN"
APW='Container-Test-Pass-42'
CJ="$WORK/cookies"
H=(-H 'Content-Type: application/json' -H 'X-Requested-With: twincheck')
curl -fsS "${H[@]}" -d "{\"token\":\"$TOKEN\",\"database\":{\"type\":\"sqlite\"},\"password\":\"$APW\",\"password_confirm\":\"$APW\",\"lang\":\"en\"}" \
  http://127.0.0.1:7777/api/setup/complete >/dev/null && ok "wizard completes" || ko "wizard completes"
check "wizard locked after use" bash -c "[ \"\$(curl -s -o /dev/null -w '%{http_code}' -H 'X-Requested-With: twincheck' -H 'Content-Type: application/json' -d '{\"token\":\"$TOKEN\"}' http://127.0.0.1:7777/api/setup/check-token)\" = 410 ]"
login() { curl -s -c "$CJ" -b "$CJ" "${H[@]}" -d "{\"username\":\"$1\",\"password\":\"$2\"}" http://127.0.0.1:7777/api/login; }
CSRF="$(login admin "$APW" | json "d['csrf']")"
check "admin login" test -n "$CSRF"
# fresh request (no cookie jar) so only the response headers are inspected
LOGIN_HDRS="$(curl -s -D - -o /dev/null "${H[@]}" -d "{\"username\":\"admin\",\"password\":\"$APW\"}" http://127.0.0.1:7777/api/login | tr -d '\r')"
SESSION_SET="$(printf '%s\n' "$LOGIN_HDRS" | grep -i '^set-cookie: tc_session=' || true)"
check "session cookie HttpOnly + SameSite=Strict, no Secure" bash -c 'grep -q "HttpOnly" <<<"$1" && grep -qi "SameSite=Strict" <<<"$1" && ! grep -qi "secure" <<<"$1"' _ "$SESSION_SET"
CSRF="$(login admin "$APW" | json "d['csrf']")"
api() { local m="$1" u="$2"; shift 2; curl -s -b "$CJ" -c "$CJ" "${H[@]}" -H "X-CSRF-Token: $CSRF" -X "$m" "http://127.0.0.1:7777$u" "$@"; }
check "metrics after login" bash -c "curl -s -b $CJ http://127.0.0.1:7777/api/sysinfo | grep -q ct-host"
check "no metrics before login" bash -c "[ \"\$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:7777/api/sysinfo)\" = 401 ]"
MEM_LIMIT="$(docker exec "$PROJECT" cat /sys/fs/cgroup/memory.max 2>/dev/null || echo max)"
echo "   cgroup memory.max=$MEM_LIMIT ; header memory: $(curl -s -b "$CJ" http://127.0.0.1:7777/api/sysinfo | json "d['memory']")"
echo "   df /data in container: $(docker exec "$PROJECT" df -k /data | tail -1)"

echo "== kill during a scan, then during moves"
python3 - "$WORK/library/u" <<'PY'
import os, sys, random
root = sys.argv[1]
for i in range(4000):
    d = os.path.join(root, f"d{i % 40}")
    os.makedirs(d, exist_ok=True)
    data = os.urandom(20000) if i % 10 else b"DUPLICATE" * 2000
    with open(os.path.join(d, f"IMG_{i:05d}.jpg"), "wb") as fh:
        fh.write(b"\xff\xd8" + data + b"\xff\xd9")
PY
BEFORE="$(find "$WORK/library/u" -type f | wc -l)"
api POST /api/admin/users -d '{"username":"ctuser","role":"user","password":"Container-Test-Pass-42","sources":[{"path":"u"}]}' >/dev/null
UCJ="$WORK/ucookies"
CJ_SAVE="$CJ"; CJ="$UCJ"
UCSRF="$(login ctuser 'Container-Test-Pass-42' | json "d['csrf']")"
NEWPW='Container-Test-Pass-43'
curl -s -b "$UCJ" -c "$UCJ" "${H[@]}" -H "X-CSRF-Token: $UCSRF" -d "{\"current\":\"Container-Test-Pass-42\",\"password\":\"$NEWPW\",\"password_confirm\":\"$NEWPW\"}" http://127.0.0.1:7777/api/me/password >/dev/null
UCSRF="$(login ctuser "$NEWPW" | json "d['csrf']")"
uapi() { local m="$1" u="$2"; shift 2; curl -s -b "$UCJ" -c "$UCJ" "${H[@]}" -H "X-CSRF-Token: $UCSRF" -X "$m" "http://127.0.0.1:7777$u" "$@"; }
SID="$(uapi GET /api/sources | json "d['sources'][0]['id']")"
SCAN="$(uapi POST /api/scan -d "{\"source_id\":\"$SID\"}" | json "d['job_id']")"
sleep 1.5
docker kill "$PROJECT" >/dev/null; dc up -d >/dev/null 2>&1; sleep 6
UCSRF="$(login ctuser "$NEWPW" | json "d['csrf']")"
for i in $(seq 1 120); do S="$(uapi GET /api/jobs/$SCAN | json "d['status']")"; [ "$S" = completed ] && break; sleep 1; done
check "scan resumed automatically after kill and completed" test "$S" = completed
check "every file indexed exactly once" bash -c "[ \"\$(curl -s -b $UCJ 'http://127.0.0.1:7777/api/report' | python3 -c 'import sys,json; print(sum(x[\"n\"] for x in json.load(sys.stdin)[\"files\"]))')\" = $BEFORE ]"
uapi POST /api/metadata/analyze -d '{}' >/dev/null; sleep 20
PLAN="$(uapi POST /api/organize/plan -d "{\"source_id\":\"$SID\"}" | json "d['job_id']")"
for i in $(seq 1 60); do [ "$(uapi GET /api/jobs/$PLAN | json "d['status']")" = completed ] && break; sleep 1; done
APPLY="$(uapi POST /api/plans/$PLAN/confirm -d '{"confirm":true}' | json "d['job_id']")"
sleep 2
docker kill "$PROJECT" >/dev/null; dc up -d >/dev/null 2>&1; sleep 6
UCSRF="$(login ctuser "$NEWPW" | json "d['csrf']")"
check "move job interrupted, not resumed without confirmation" bash -c "[ \"\$(curl -s -b $UCJ http://127.0.0.1:7777/api/jobs/$APPLY | python3 -c 'import sys,json;print(json.load(sys.stdin)[\"status\"])')\" = interrupted ]"
uapi POST /api/jobs/$APPLY/resume >/dev/null
for i in $(seq 1 180); do [ "$(uapi GET /api/jobs/$APPLY | json "d['status']")" = completed ] && break; sleep 1; done
AFTER="$(find "$WORK/library/u" -type f | wc -l)"
check "moves completed after kill: no file lost or duplicated ($BEFORE -> $AFTER)" test "$BEFORE" = "$AFTER"
check "no leftover temp/partial files" bash -c "[ -z \"\$(find $WORK/library -name '.twincheck-*' | head -1)\" ]"
CJ="$CJ_SAVE"

echo "== reset-admin-password with the app running"
CSRF="$(login admin "$APW" | json "d['csrf']")"
check "admin session valid before the reset" bash -c "[ \"\$(curl -s -o /dev/null -w '%{http_code}' -b $CJ http://127.0.0.1:7777/api/me)\" = 200 ]"
OUT="$(docker exec "$PROJECT" reset-admin-password --generate 2>&1)"
NEWADMIN="$(echo "$OUT" | sed -n '3p' | tr -d '[:space:]')"
check "reset command succeeded" test -n "$NEWADMIN"
check "old admin session invalidated" bash -c "[ \"\$(curl -s -o /dev/null -w '%{http_code}' -b $CJ http://127.0.0.1:7777/api/me)\" = 401 ]"
NEWLOGIN="$(curl -s -c "$WORK/c2" "${H[@]}" -d "{\"username\":\"admin\",\"password\":\"$NEWADMIN\"}" http://127.0.0.1:7777/api/login | json "d.get('csrf', '')" 2>/dev/null || true)"
check "new password works" test -n "$NEWLOGIN"
check "password never in container logs" bash -c "! docker logs $PROJECT 2>&1 | grep -qF '$NEWADMIN'"
check "reset fails with a wrong key, nothing modified" bash -c "H1=\$(sha256sum $WORK/state/vault.enc); ! docker exec -e APP_MASTER_KEY=\$(printf '0%.0s' {1..64}) $PROJECT reset-admin-password --generate >/dev/null 2>&1; [ \"\$H1\" = \"\$(sha256sum $WORK/state/vault.enc)\" ]"

echo "== ALLOWED_NETWORKS and BIND_ADDRESS"
setenv ALLOWED_NETWORKS "203.0.113.0/24"; dc up -d >/dev/null 2>&1; sleep 6
check "ALLOWED_NETWORKS blocks the host (403 on login page)" bash -c "[ \"\$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:7777/)\" = 403 ]"
check "healthcheck still OK inside (loopback)" docker exec "$PROJECT" wget -q -O /dev/null http://127.0.0.1/healthz
setenv ALLOWED_NETWORKS ""; setenv BIND_ADDRESS 127.0.0.1; dc up -d >/dev/null 2>&1; sleep 6
check "BIND_ADDRESS=127.0.0.1 publishes only on loopback" bash -c "docker port $PROJECT 80 | grep -q '^127.0.0.1:7777' && ! docker port $PROJECT 80 | grep -q '0.0.0.0'"
[ -n "$HOSTIP" ] && check "not reachable on $HOSTIP:7777 any more" bash -c "! curl -fsS -m 3 -o /dev/null http://$HOSTIP:7777/healthz"
setenv BIND_ADDRESS 0.0.0.0; dc up -d >/dev/null 2>&1; sleep 4

echo "== Python test-suite inside a hardened container (real ExifTool and ffmpeg)"
docker run -d --name "${PROJECT}-mariadb" -e MARIADB_ROOT_PASSWORD=test-only-password --tmpfs /var/lib/mysql \
  mariadb:11.4 --max-allowed-packet=64M >/dev/null 2>&1
for i in $(seq 1 60); do docker exec "${PROJECT}-mariadb" mariadb-admin -uroot -ptest-only-password ping >/dev/null 2>&1 && break; sleep 2; done
MIP="$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' "${PROJECT}-mariadb")"
docker run --rm --user "$CUID:$CGID" --read-only --tmpfs /tmp:rw,size=1g --cap-drop ALL --security-opt no-new-privileges:true \
  -v "$ROOT/app/tests:/opt/twincheck/app/tests:ro" -v "$ROOT/app/tools:/opt/twincheck/app/tools:ro" \
  -e TWINCHECK_TEST_MARIADB_HOST="$MIP" -e TWINCHECK_TEST_MARIADB_PASSWORD=test-only-password -e TWINCHECK_BIG_ROWS="${TWINCHECK_BIG_ROWS:-1000000}" \
  --entrypoint python3 "$IMG" -B -m unittest discover -s /opt/twincheck/app/tests -v > "$WORK/unittest.log" 2>&1 \
  && ok "unit/integration tests in the container (SQLite + MariaDB, real ExifTool/ffmpeg)" || { ko "unit/integration tests in the container"; tail -40 "$WORK/unittest.log"; }
grep -E "^Ran |^OK|^FAILED|large dataset" "$WORK/unittest.log" | sed 's/^/   /'

echo
echo "================ RESULTS ================"
printf '%s\n' "${RESULTS[@]}"
echo "PASSED: $PASS   FAILED: $FAIL"
echo "Manual check left: open http://<this-host-LAN-IP>:7777 from another machine."
[ "$FAIL" -eq 0 ]
