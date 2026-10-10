# Inquisitor v1.0.8 — Galahad Vercel Presentation Demo

This package is intentionally configured for a **presentation/demo deployment on Vercel**. It keeps the Inquisitor interface and seeded workflows available for demonstrations without treating Vercel as the production contest judge.

## What works in the demo

- Public home / leaderboard / contest information / archive views
- Contestant sign-in and sign-up screens
- Contest Director and Judge interfaces
- Contest, problem, contestant, institution, staff and role management screens
- Clarifications, announcements, awards and most administrative UI
- Sample problems and seeded leaderboard data/workflows
- Demo accounts:
  - `contestant` / `inquisitor`
  - `judge` / `inquisitor`
  - `admin` / `inquisitor`

## Demo limitations

Vercel runs the Flask application as serverless compute. This build therefore stores its SQLite database and generated demo files under `/tmp`.

- Data is **ephemeral** and can reset when Vercel starts a new function instance.
- The continuously running auto-judge worker is **not started**.
- Docker sandbox judging is **not available** in this Vercel demo.
- Do not use this build for an actual programming contest or persistent production data.

The full Inquisitor deployment should use Render/VPS/Linux with persistent storage/database and a dedicated judge worker.

## Deploy from GitHub

1. Extract this package into your GitHub repository.
2. Commit and push the files.
3. In Vercel, choose **Add New → Project** and import the repository.
4. Keep the project root as the repository root.
5. Vercel currently supports zero-configuration Flask detection, so no custom build/start command is required for this package.
6. Deploy.

Optional environment variable:

```text
INQUISITOR_SECRET=<a long random demo secret>
```

You can also force demo behavior locally with:

```text
INQUISITOR_VERCEL_DEMO=1
```

Then run Flask normally after installing the requirements.

## Important

The presentation demo is deliberately separate from the production contest architecture. A successful Vercel demo proves the UI, navigation and web workflows; it does not validate the secure compiler sandbox or continuous judge infrastructure.
