# Inquisitor v1.0.8 — Galahad deployment

## Recommended production target
Linux (Ubuntu 24.04 or Debian 12), Docker Engine + Docker Compose plugin, 4+ CPU cores, 8+ GB RAM, SSD storage, and a private LAN for an on-site contest. A public deployment should sit behind HTTPS.

## 1. Prepare the host

```bash
sudo mkdir -p /srv/inquisitor/data/judge-work
sudo chmod 755 /srv/inquisitor /srv/inquisitor/data
sudo chmod 777 /srv/inquisitor/data/judge-work
cp .env.example .env
```

Edit `.env`. Set a unique `INQUISITOR_SECRET`, keep `INQUISITOR_DEMO_ACCOUNTS=0` for a real contest, and set the domain if HTTPS will be used.

## 2. Build the isolated judge image

```bash
docker build -t inquisitor-judge-runtime:1.0 -f Dockerfile.judge-runtime .
```

The runtime contains C, C++, Java, Kotlin, Python 3, Rust, and Ruby. Each submission is executed in a short-lived container with no network, a read-only root filesystem, dropped capabilities, a non-root user, CPU/memory/PID limits, and a temporary writable work directory.

## 3. Start Inquisitor

```bash
docker compose build web worker
docker compose up -d web worker
```

Open `http://SERVER-IP:8080` on the contest LAN.

## 4. HTTPS / internet deployment

Set `INQUISITOR_DOMAIN` to a DNS name pointing at the server, set `INQUISITOR_SECURE_COOKIES=1`, then run:

```bash
docker compose --profile https up -d
```

Caddy terminates HTTPS and proxies to Inquisitor. If your institution already has nginx/HAProxy/Cloudflare, use that instead and expose only the web service internally.

## 5. Create the first director account

Keep `INQUISITOR_DEMO_ACCOUNTS=0` and set `INQUISITOR_BOOTSTRAP_ADMIN_USER` plus a 12+ character `INQUISITOR_BOOTSTRAP_ADMIN_PASSWORD` in `.env` before first start. Inquisitor creates that Contest Director only when the database is empty. Remove the bootstrap password from `.env` after first successful initialization and restart the services. Never enable demo credentials on an internet-facing instance.

## 6. Operational checklist

- Create/clone the contest and set scope, format, scoring model, eligibility, registration window, check-in policy, start time, duration, and freeze.
- Import or register institutions and contestants; approve and check in entrants when required.
- Create/import problems, upload hidden tests, add reference solutions, run validation, and disable unpublished problems.
- Open Practice before the real contest if desired.
- Confirm System Status shows the Docker judge backend and all language runtimes available.
- Create a manual backup before opening submissions.
- Test every problem with the reference solution and at least one intentional wrong solution.
- Put the public leaderboard/projector on a separate display.
- During the contest use announcements, clarifications, emergency controls, audit logs, and judge/rejudge tools.
- At the end, use progressive resolution, compute awards, export official results, generate certificates, finalize, snapshot, back up, then archive.

## Storage and backups
Persistent data lives at `/srv/inquisitor/data` on the host. Back up this entire directory in addition to Inquisitor's SQLite snapshot files. Do not store the database only inside a disposable container layer.

## Scaling note
The supplied deployment is a robust single-node configuration intended for departmental through regional-sized on-site events. The web process uses one Gunicorn worker with threads because SQLite is a single-writer database. For very large national/international events or high-availability multi-server deployment, move the persistence layer to PostgreSQL and use multiple independent judge workers before calling the deployment horizontally scalable.
