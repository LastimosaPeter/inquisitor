# Inquisitor v1.0.7 — Galahad

**The official contest management platform of TACTICS x ACPC 2026**

**Release codename:** **Galahad**

Inquisitor is a reusable programming-contest platform for individual and team competitions. The default demonstration contest is **Ateneo Computer Programming Competition 2026**, but the system can create, clone, finalize, archive, and publish future contests from departmental through international scope.

## Core competition features

- Individual or Team / ICPC-style competition format
- Contest scopes: Department, College, Cross-College / University, City, Regional, National, International
- ICPC, points, and custom-points scoring modes
- Contest lifecycle: Draft, Registration Open, Registration Closed, Practice, Scheduled, Running, automatic Frozen state, Paused, Ended, Finalized, Archived
- Automatic public scoreboard freeze and progressive bottom-up scoreboard resolution
- Gold / silver / bronze podium highlighting and First-to-Solve tracking
- Department and institution identity without adding cluttering leaderboard columns
- Projector, fullscreen, and awarding scoreboard views
- Public contest information and contest archive

## Registration and people

- Institution directory
- Department, college, year level, affiliation and institution metadata
- Eligibility rules and registration windows
- Optional registration approval and mandatory check-in
- Individual/team mode with configurable maximum team size
- Coach/adviser fields
- Contestant status: Registered, Pending, Checked In, Active, Withdrawn, Disqualified, DNS
- Seat/workstation assignment
- CSV contestant import
- Contest Director password reset
- Printable credentials with QR code and one-time temporary-password workflow
- Role assignment for Contest Director, Chief Judge, Judge, Problem Setter, Technical Admin, Registration Staff and Spectator

## Problems and judging

Supported languages:

- Rust (`.rs`)
- Ruby (`.rb`)
- Java (`.java`)
- Kotlin (`.kt`)
- Python 3 (`.py`)
- C (`.c`)
- C++ (`.cpp`, `.cc`, `.cxx`)

The submission page detects the language from the uploaded source-file extension and warns/normalizes the selection on the server as well.

Problems support title/code/API ID, statement, author/contributor(s), tags, source/origin, internal notes, color, time and memory limits, points, samples, hidden tests, reference solution, reference language, optional Python input validator, and enable/disable status.

Inquisitor can import/export ICPC-style ZIP problem packages, validate problem completeness and reference solutions, and preserve hidden judge data outside the public interface.

### Production judge

The included Docker judge backend executes each untrusted submission in a short-lived isolated container with:

- no network
- CPU and memory limits
- PID limit
- dropped Linux capabilities
- `no-new-privileges`
- non-root execution
- read-only root filesystem
- temporary writable work directory

A local subprocess judge remains available for development only. Production mode rejects unsafe local judging unless explicitly overridden.

## Contest communications and discipline

- General clarification requests
- Claims of ambiguity
- Possible statement-error reports
- Private judge replies
- Official broadcast clarifications to all contestants/teams
- Contest announcements with severity, audience and pinning
- Emergency global notice
- Disqualification and reinstatement with reason, evidence/notes and full history
- Team-wide DQ behavior in team mode
- Submission blocking for disqualified entrants
- Manual source-similarity review as a judge aid, never as automatic proof of cheating

## Live operations

- Judge Console with submission queue, verdicts, rejudge and clarification handling
- Pause/resume contest
- Extend contest time
- Open/close submissions
- Enable/disable individual problems
- Requeue judging
- Judge/runtime health and queue status
- Periodic, pre-contest and post-finalization backups
- Searchable audit log
- Contest snapshots
- Balloon / solve-recognition queue
- First-to-Solve history

## Awards and analytics

Configurable award rules include:

- overall rank
- First to Solve by problem
- best contestant/team by department
- best contestant/team by institution
- best contestant/team by year level
- aggregate best department
- aggregate best institution
- manual recipient

The system generates award certificates and participation certificates. Analytics include verdict distribution, acceptance rate, per-problem statistics, department standings and institution standings. Exports include CSV, XLSX and PDF final standings, problem statistics CSV, submission archive ZIP and certificate ZIPs.

## ICPC interoperability

Inquisitor exposes a **draft-aligned ICPC Contest API subset** covering contest, languages, problems, organizations, groups, persons, teams, accounts, submissions, judgements, clarification messages, awards, state, scoreboard and an NDJSON event feed. The API can be disabled, public-read, or protected with a generated API key. The UI includes an implementation-health summary.

This is deliberately described as draft-aligned, not as an official ICPC conformance certification. The bundled event feed is finite/pollable NDJSON rather than a permanently open streaming connection.

## Local development

### Windows

```text
start_windows.bat
```

### Linux/macOS

```bash
./start_linux.sh
```

Open `http://127.0.0.1:5000`. For LAN testing, use the server's LAN IP.

Local demo credentials are enabled by default:

- Contestant: `contestant` / `inquisitor`
- Judge: `judge` / `inquisitor`
- Contest Director: `admin` / `inquisitor`

These credentials are **not enabled by the production `.env.example`**.

## Production deployment

See **DEPLOYMENT.md**. The short version is:

```bash
cp .env.example .env
# Edit .env: secret, bootstrap director credentials, domain/security settings.

docker build -t inquisitor-judge-runtime:1.0 -f Dockerfile.judge-runtime .
docker compose build web worker
docker compose up -d web worker
```

For HTTPS using the included Caddy profile:

```bash
docker compose --profile https up -d
```

The supplied deployment is intentionally a single-node SQLite/WAL release suitable for ordinary on-site contests. For very large national/international events requiring horizontal high availability, migrate persistence to PostgreSQL and deploy multiple application/judge nodes after load testing.

## Before a real event

Run `python manage.py check`, create a manual backup, test every supported runtime, submit known accepted/wrong/TLE solutions for every problem, test clarification broadcast, test scoreboard freeze and reveal, test an emergency pause/extension, verify the public projector screen, and conduct a full contest rehearsal on the actual LAN and hardware.

## Programming language deployment
For compiler/runtime setup, verification, native Windows notes, and the pre-contest certification checklist, see `LANGUAGE_DEPLOYMENT_GUIDE.md`. On the recommended Linux + Docker deployment, run `./verify_judge_runtime.sh` after building `inquisitor-judge-runtime:1.0` to smoke-test all seven supported languages.


### v1.0.7 Galahad UI/account refinements
- Cleaner leaderboard, people-management, problem-generator, and public footer layouts.
- Contestant and staff accounts now support optional email addresses; sign-in accepts either username or email.
- TACTICS footer uses the T-shaped mark.
- Problem difficulty is no longer exposed in the Contest Director problem generator.
