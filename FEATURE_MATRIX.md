# Inquisitor v1.0.8 — Galahad feature matrix

This matrix maps the v1.0.8 Vercel presentation/demo release to the complete feature set requested during design.

| # | Requested capability | v1.0.8 implementation |
|---:|---|---|
| 1 | Contest scope | Department, College, Cross-College / University, City, Regional, National, International in Contest Director → Contests |
| 2 | Individual / Team format | Per-contest competition mode with team aggregation and ICPC API compatibility mapping |
| 3 | Eligibility & registration rules | Registration windows, department/college/institution/year filters, max team size, coach/adviser, approval and check-in policies |
| 4 | Institutions management | Reusable institution directory with active/inactive status |
| 5 | Contest phases | Draft, Registration Open, Registration Closed, Practice, Scheduled, Running, **automatic Frozen state at the configured threshold**, Paused, Ended, Finalized, Archived |
| 6 | Multiple scoring systems | ICPC solved/penalty, Points, Custom points; per-problem points and custom scoring notes |
| 7 | Problem metadata | Difficulty, tags, contributor(s), source, color, limits, points, internal notes and enabled status |
| 8 | Problem package import/export | ICPC-style ZIP layout with statement, `problem.yaml`, secret data and accepted reference solution; Inquisitor metadata round-trip |
| 9 | Problem validation | Statement, limits, judge data, samples, reference solution and optional validator checks with validation history |
| 10 | Reference solutions | Hidden reference source + language stored for problem validation |
| 11 | Multiple staff roles | Contest Director, Chief Judge, Judge, Problem Setter, Technical Admin, Registration Staff, Spectator with permission gates |
| 12 | Audit log | Searchable contest audit log records administrative/operational actions |
| 13 | DQ history | DQ/reinstate history with actor, reason and evidence/notes; team-wide in team mode |
| 14 | Announcements | Audience, severity, pinning and public/participant delivery |
| 15 | Clarification categories | General Question, Ambiguity Claim, Statement Error; private reply or broadcast official clarification |
| 16 | Smart submission detection | `.cpp/.cc/.cxx`, `.c`, `.java`, `.kt`, `.py`, `.rs`, `.rb` auto-detection and server-side normalization |
| 17 | Similarity review | Post-contest source similarity scan with manual review status; explicitly an aid, not automatic proof |
| 18 | Contestant statuses | Registered, Pending, Checked In, Active, Withdrawn, Disqualified, DNS |
| 19 | Check-in | Required/optional check-in policy and staff check-in controls |
| 20 | Seat/workstation | Per-registration seat and workstation assignment |
| 21 | Printable credentials | PDF credentials with QR, identity, assignment and temporary-password support |
| 22 | Password reset | Contest Director/registration staff reset flow and forced change on next sign-in |
| 23 | Practice contest | Practice lifecycle phase and practice mode using the same judge infrastructure |
| 24 | Balloon / award queue | Accepted-solve queue with Pending/Claimed/Delivered and FTS indicator |
| 25 | First-to-Solve history | Earliest accepted solution by problem, freeze-safe public display, official history |
| 26 | Award computation | Rank, FTS, department, institution, year-level, aggregate department/institution and manual rules |
| 27 | Department standings | Analytics aggregation by department |
| 28 | Institution standings | Analytics aggregation by institution/affiliation |
| 29 | Scoreboard presentations | Standard, Projector, Fullscreen and Awarding modes |
| 30 | Progressive frozen-board reveal | Bottom-up queue with per-entity resolution; final top ranks naturally resolve last |
| 31 | Contest statistics | Submission/verdict totals, acceptance rate, per-problem data and activity analytics |
| 32 | Post-contest analytics | Final problem, department and institution analysis retained after finalization |
| 33 | Contest archive | Finalized/archived public contest archive and read-only final results page |
| 34 | Clone Contest | Clone settings, problems and judge data while excluding contestants/submissions |
| 35 | Contestant CSV import | Bulk participant import with registration metadata |
| 36 | Result exports | Scoreboard CSV/XLSX/PDF, submissions ZIP, problem-statistics CSV and certificates |
| 37 | Automatic backups | Pre-contest, periodic running-contest, post-finalization and manual SQLite backups |
| 38 | Network/status page | DB, disk, queue, judge backend, host and compiler/runtime health |
| 39 | Emergency controls | Pause, resume, extend, stop/open submissions, disable/enable problem, requeue judging, emergency broadcast |
| 40 | Contest API controls | Per-contest enable/public-private policy, rotating hashed API key and implementation-health status |
| 41 | Public contest information | Scope, schedule, venue, registration state, organizers, rules, languages, problems, announcements, countdown and final results |
| 42 | Certificates/results | Award and participation PDFs plus bulk participation certificate ZIP |
| 43 | Deployable judging/security | Docker sandbox backend, production-safe configuration, Gunicorn web service, dedicated worker, Caddy option, CSRF/security headers and production bootstrap credentials |

## Additional v1.0.7 capabilities

- Contest snapshots
- Official problem colors and podium/FTS visual cues
- Department labels embedded under contestant/team identity rather than a separate leaderboard column
- Seven-language compiler/runtime health checks
- API organizations/groups/accounts/awards resources
- One-time API token display with hash-only storage
- SQLite WAL persistence and configurable data directory
- Local offline/LAN mode and public HTTPS deployment path
