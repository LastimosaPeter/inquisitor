#!/usr/bin/env bash
# Inquisitor setup for a Docker host. Safe to run again: it keeps your .env and your data.
#
#   bash setup.sh
#
# Optional settings (environment variables):
#   INQ_HOME=$HOME/inquisitor      where everything lives
#   INQ_BRANCH=judging-workflow    git branch to deploy
#   INQ_HTTP_PORT=8080             port the site listens on
#   INQ_BIND_ADDR=0.0.0.0          0.0.0.0 = reachable on the LAN, 127.0.0.1 = only this machine (use once the tunnel is up)
#   INQ_PROFILE=dedicated          dedicated = this machine is for Inquisitor.  shared = it also runs other things (e.g. a
#                                  production system): fewer judge threads, memory and CPU caps, smaller compile memory.
#   INQ_JUDGE_WORKERS=3            parallel judge threads (about half the CPU cores; the shared profile defaults to 2)
#   INQ_PROJECT=inquisitor         docker compose project name
set -euo pipefail

INQ_HOME="${INQ_HOME:-$HOME/inquisitor}"
BRANCH="${INQ_BRANCH:-judging-workflow}"
REPO_URL="${INQ_REPO:-https://github.com/LastimosaPeter/inquisitor.git}"
HTTP_PORT="${INQ_HTTP_PORT:-8080}"
BIND_ADDR="${INQ_BIND_ADDR:-0.0.0.0}"
PROFILE="${INQ_PROFILE:-dedicated}"
case "$PROFILE" in dedicated|shared) ;; *) echo "INQ_PROFILE must be dedicated or shared"; exit 1 ;; esac
if [ "$PROFILE" = "shared" ]; then WORKERS="${INQ_JUDGE_WORKERS:-2}"; else WORKERS="${INQ_JUDGE_WORKERS:-3}"; fi
PROJECT="${INQ_PROJECT:-inquisitor}"

say() { printf '\n==> %s\n' "$*"; }
die() { printf '\nERROR: %s\n' "$*" >&2; exit 1; }

say "Checking this machine"
command -v docker >/dev/null || die "docker is not installed"
command -v git    >/dev/null || die "git is not installed (sudo apt-get install -y git)"
docker compose version >/dev/null 2>&1 || die "docker compose is not installed"
docker info >/dev/null 2>&1 || die "this user cannot talk to docker. Add it to the docker group (sudo usermod -aG docker \$USER, then log out and in) or run with sudo."
if ss -ltn 2>/dev/null | awk '{print $4}' | grep -q "[:.]${HTTP_PORT}\$"; then
  # Fine if it is our own stack from an earlier run; otherwise something else owns the port.
  if ! docker ps --format '{{.Names}}' | grep -q "^${PROJECT}-web-1\$"; then
    die "port ${HTTP_PORT} is already used by something else. Re-run with INQ_HTTP_PORT=<free port>."
  fi
fi
echo "docker $(docker --version | cut -d' ' -f3 | tr -d ,), $(nproc) CPU cores, $(free -g | awk '/Mem:/{print $2}') GB RAM, profile: $PROFILE"
if [ "$PROFILE" = "shared" ]; then
  echo "already running here: $(docker ps --format '{{.Names}}' | grep -vc '^'"$PROJECT"'-' || true) other container(s); load average $(cut -d' ' -f1-3 /proc/loadavg); $(free -m | awk '/Mem:/{print $7}') MB memory available"
fi

say "Getting the code (branch ${BRANCH})"
mkdir -p "$INQ_HOME/data/judge-work"
if [ -d "$INQ_HOME/app/.git" ]; then
  git -C "$INQ_HOME/app" fetch --quiet origin "$BRANCH"
  git -C "$INQ_HOME/app" checkout --quiet "$BRANCH"
  git -C "$INQ_HOME/app" pull --quiet --ff-only origin "$BRANCH"
else
  git clone --quiet --depth 1 --branch "$BRANCH" "$REPO_URL" "$INQ_HOME/app"
fi
echo "at commit $(git -C "$INQ_HOME/app" log --oneline -1)"

say "Settings and secrets"
rand() { head -c 64 /dev/urandom | base64 | tr -d '/+=\n' | cut -c1-"$1"; }
NEW_ENV=0
if [ ! -f "$INQ_HOME/.env" ]; then
  NEW_ENV=1
  ADMIN_PW="$(rand 20)"
  umask 077
  cat > "$INQ_HOME/.env" <<EOF
# Inquisitor settings. Keep this file private. Edit it, then run: $INQ_HOME/inq up -d
INQUISITOR_SECRET=$(rand 64)
INQUISITOR_DEMO_ACCOUNTS=0
INQUISITOR_BOOTSTRAP_ADMIN_USER=director
INQUISITOR_BOOTSTRAP_ADMIN_PASSWORD=$ADMIN_PW
INQUISITOR_BOOTSTRAP_ADMIN_NAME=Contest Director
# Set to 1 once the site is served over HTTPS (the tunnel): $INQ_HOME/inq https-on
INQUISITOR_SECURE_COOKIES=0
INQUISITOR_JUDGE_BACKEND=docker
INQUISITOR_JUDGE_IMAGE=inquisitor-judge-runtime:1.0
INQUISITOR_JUDGE_WORKERS=$WORKERS
INQUISITOR_JUDGE_CPUS=1
INQUISITOR_ALLOW_UNSAFE_LOCAL_JUDGE=0
INQUISITOR_REVIEW_CLAIM_MINUTES=20
INQUISITOR_BACKUP_INTERVAL_MINUTES=30
EOF
  chmod 600 "$INQ_HOME/.env"
  echo "created $INQ_HOME/.env"
else
  echo "keeping existing $INQ_HOME/.env"
fi
set_env_default() { grep -q "^$1=" "$INQ_HOME/.env" || echo "$1=$2" >> "$INQ_HOME/.env"; }
if [ "$PROFILE" = "shared" ]; then
  set_env_default INQUISITOR_JUDGE_COMPILE_MEMORY_MB 768   # a compile may use at most this much memory (default 1536)
  set_env_default INQUISITOR_JUDGE_CPU_SHARES 256          # judge containers yield the CPU to other services when it is contended (default 1024)
fi

say "Writing the compose override and the inq helper"
CAPS_WEB=""; CAPS_WORKER=""
if [ "$PROFILE" = "shared" ]; then
  CAPS_WEB=$'    cpus: 1.5\n    cpu_shares: 512\n    mem_limit: 768m'   # website: at most 1.5 cores and 768 MB, lower CPU priority than other services
  CAPS_WORKER=$'    cpus: 0.5\n    cpu_shares: 256\n    mem_limit: 384m' # worker process itself (the judge containers it starts are capped separately)
fi
DOCKER_GID="$(getent group docker | cut -d: -f3 || true)"
[ -n "$DOCKER_GID" ] || die "no 'docker' group found; the judge worker needs access to the docker socket"
cat > "$INQ_HOME/compose.local.yml" <<EOF
# Generated by setup.sh. Re-run setup.sh to regenerate.
# Data lives in $INQ_HOME/data at the SAME path inside and outside the containers (the Docker judge needs that).
# Containers run as $(id -un) so the data folder stays yours.
services:
  web:
${CAPS_WEB}
    user: "$(id -u):$(id -g)"
    env_file: !override
      - $INQ_HOME/.env
    environment:
      INQUISITOR_DATA_DIR: $INQ_HOME/data
    volumes: !override
      - $INQ_HOME/data:$INQ_HOME/data
    ports: !override
      - "${BIND_ADDR}:${HTTP_PORT}:8000"
  worker:
${CAPS_WORKER}
    user: "$(id -u):$(id -g)"
    group_add: ["$DOCKER_GID"]
    env_file: !override
      - $INQ_HOME/.env
    environment:
      INQUISITOR_DATA_DIR: $INQ_HOME/data
      INQUISITOR_JUDGE_WORK_ROOT: $INQ_HOME/data/judge-work
    volumes: !override
      - $INQ_HOME/data:$INQ_HOME/data
      - /var/run/docker.sock:/var/run/docker.sock
EOF
cat > "$INQ_HOME/inq" <<EOF
#!/usr/bin/env bash
# inq up -d | down | ps | logs -f | restart | status | https-on | https-off | backup
# (anything else is passed to docker compose)
set -e
HOME_DIR="$INQ_HOME"
cd "\$HOME_DIR/app"
dc() { docker compose --project-name "$PROJECT" -f docker-compose.yml -f "\$HOME_DIR/compose.local.yml" "\$@"; }
case "\${1:-}" in
  status)    dc ps; echo; curl -fsS "http://127.0.0.1:${HTTP_PORT}/healthz" && echo " <- web is healthy" ;;
  https-on)  sed -i 's/^INQUISITOR_SECURE_COOKIES=.*/INQUISITOR_SECURE_COOKIES=1/' "\$HOME_DIR/.env"; dc up -d; echo "secure cookies ON (use this once the site is served over https)" ;;
  https-off) sed -i 's/^INQUISITOR_SECURE_COOKIES=.*/INQUISITOR_SECURE_COOKIES=0/' "\$HOME_DIR/.env"; dc up -d; echo "secure cookies OFF" ;;
  backup)    dc exec -T worker python manage.py backup ;;
  *)         dc "\$@" ;;
esac
EOF
chmod +x "$INQ_HOME/inq"

# keep the smoke test next to the install
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
[ -f "$SCRIPT_DIR/smoke_test.py" ] && cp "$SCRIPT_DIR/smoke_test.py" "$INQ_HOME/smoke_test.py"

say "Building the judge image (compilers for C, C++, Java, Python, Kotlin, Rust, Ruby). This takes a few minutes the first time."
if [ "${INQ_REBUILD:-0}" = "1" ] || ! docker image inspect inquisitor-judge-runtime:1.0 >/dev/null 2>&1; then
  docker build -q -f "$INQ_HOME/app/Dockerfile.judge-runtime" -t inquisitor-judge-runtime:1.0 "$INQ_HOME/app" >/dev/null
fi
echo "judge image ready"

say "Building and starting the website and the judge worker"
"$INQ_HOME/inq" build
"$INQ_HOME/inq" up -d

say "Waiting for the website to come up"
for i in $(seq 1 60); do
  if curl -fsS "http://127.0.0.1:${HTTP_PORT}/healthz" >/dev/null 2>&1; then OK=1; break; fi
  sleep 2
done
[ "${OK:-0}" = "1" ] || { "$INQ_HOME/inq" logs --tail 40; die "the website did not become healthy in 2 minutes"; }

ADMIN_USER="$(grep '^INQUISITOR_BOOTSTRAP_ADMIN_USER=' "$INQ_HOME/.env" | cut -d= -f2)"
IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
say "Done"
echo "Site (this machine):  http://127.0.0.1:${HTTP_PORT}"
[ "$BIND_ADDR" = "0.0.0.0" ] && [ -n "$IP" ] && echo "Site (your network):  http://${IP}:${HTTP_PORT}"
echo "Director login:       $ADMIN_USER"
if [ "$NEW_ENV" = "1" ]; then
  echo "Director password:    $ADMIN_PW"
  echo "  (shown once. It is also saved in $INQ_HOME/.env, readable only by you.)"
else
  echo "Director password:    in $INQ_HOME/.env"
fi
echo
if [ "$PROFILE" = "shared" ]; then
  echo "Shared-host limits: $WORKERS judge thread(s) of 1 CPU each, website capped at 1.5 CPU / 768 MB, compiles capped at 768 MB, and low CPU priority so other services win when the CPU is busy."
  echo "To stop the contest instantly and free everything:  $INQ_HOME/inq down"
fi
echo "Next: python3 $INQ_HOME/smoke_test.py     (checks signup, judging, review and similarity end to end)"
echo "Day to day: $INQ_HOME/inq status | logs -f | restart | down"
