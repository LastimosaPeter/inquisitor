# Inquisitor deployment

Sets up Inquisitor (the contest site plus the automatic judge) on a machine that has Docker, and checks that it works.
Everything it creates lives in one folder (`~/inquisitor`), and removing it is one command.
The code you cloned (`~/inquisitor-src`) is only the installer; the running copy is kept in `~/inquisitor/app`.

## What you need
- Ubuntu with Docker and Docker Compose, and a user that can run `docker` (member of the `docker` group).
- `git` and `curl`. Internet access for the first install (it downloads the code and builds the images).
- A free port (8080 by default).

If Docker is not installed yet (Ubuntu, same versions this was tested with):

```bash
sudo apt update
sudo apt install -y docker.io docker-compose-v2 git curl
sudo usermod -aG docker $USER      # then log out and back in so the group applies
docker run --rm hello-world        # should print "Hello from Docker!"
```

Note that Docker adds its own network (172.17.0.0/16 and up) and firewall rules, and a port it publishes is not
filtered by `ufw`. If the server has other firewall rules or already uses 172.x addresses, check them first.
- About 4 GB of free disk for the images, and a few minutes for the first build.

## Install

Everything below is typed on the server itself. The code is downloaded from GitHub, so nothing needs to be copied over.

```bash
git clone --branch judging-workflow https://github.com/LastimosaPeter/inquisitor.git ~/inquisitor-src
```

On a server that also runs something else (for example a production system), use the shared profile:

```bash
INQ_PROFILE=shared bash ~/inquisitor-src/deploy/setup.sh
```

On a machine that is only for Inquisitor:

```bash
bash ~/inquisitor-src/deploy/setup.sh
```

The script prints the site address, the director login and, the first time only, the director password
(it is also saved in `~/inquisitor/.env`, readable only by you). Write the password down.

If port 8080 is taken, add `INQ_HTTP_PORT=8090` (any free port) in front of the command.

## Check it works

```bash
python3 ~/inquisitor/smoke_test.py
```

It signs in as the director, creates a test student, submits Python, Java and C++ solutions, waits for the verdicts,
reviews one submission as a judge and runs the similarity check. Expect `18 of 18 checks passed`.
The test student stays in the database, so see "Fresh start" below before the real contest.

## Day to day

```bash
~/inquisitor/inq status      # is everything up?
~/inquisitor/inq logs -f     # watch the logs (Ctrl+C to stop watching)
~/inquisitor/inq restart     # restart the website and the judge
~/inquisitor/inq down        # stop everything now (frees all CPU and memory; your data is kept)
~/inquisitor/inq up -d       # start it again
~/inquisitor/inq backup      # take a backup now (also taken automatically every 30 minutes)
```

Backups are saved in `~/inquisitor/data/backups`. Copy them off the server before and after the contest.

## Before the contest (in the website, as the director)
1. Create or edit the contest: name, start time, length.
2. Import the problems (ZIP packages, or add them by hand) and check that each reference solution passes.
3. Open registration, let students sign up (or import a CSV of students), then close registration.
4. Create judge accounts under Staff & Roles.
5. Tell students: Java classes must be named `Main`; programs read standard input and write standard output.

## Fresh start (removes everything, including the test student)

```bash
~/inquisitor/inq down
rm -rf ~/inquisitor/data
~/inquisitor/inq up -d
```

## Sign in with Google (ADNU accounts)

Students sign in with their ADNU Google account (`@adnu.edu.ph` or `@gbox.adnu.edu.ph`) and fill in a short form the first time.
Their contest account is created automatically while registration is open. Only addresses on the allowed domains get in.

1. In Google Cloud (`console.cloud.google.com`), open **Google Auth Platform** and create an **OAuth client** of type **Web application**.
   Add this **Authorized redirect URI** (use your own address if it differs):
   `https://inq.pel47.cc/auth/google/callback`
2. On the server, run the helper. It asks for the Client ID, the Client secret (typing is hidden) and the site address:
   ```bash
   ~/inquisitor/inq google-setup
   ```
   It turns Google sign-in on, switches the password form off on the public page, and prints the **staff sign-in address**.
3. Directors and judges sign in at that hidden staff address (username and password). It is long and random on purpose.
   Bookmark it, and print it again any time with `~/inquisitor/inq staff-url`. Don't share it.

**Who gets in as what.** Everyone who signs in with an ADNU Google account is a **contestant** by default and lands on the
contestant pages. Only people on the **staff list** get staff pages, and each lands on the page their role uses. Add staff either way:

- **On the website**, as a director: open **Staff & Roles**, type their ADNU email, pick a role, click **Add to staff**.
  No password is needed. Their name is filled in from Google the first time they sign in.
- **On the server**, which is also how directors are created (the website cannot create directors):
  ```bash
  ~/inquisitor/inq add-staff ktapel@gbox.adnu.edu.ph director ktapel@adnu.edu.ph   # a director; the 3rd value is a second address that also works
  ~/inquisitor/inq add-staff ilastimosa@adnu.edu.ph director
  ~/inquisitor/inq add-staff someone@adnu.edu.ph judge
  ```
  Roles: `director`, `chief_judge`, `judge`, `problem_setter`, `technical_admin`, `registration_staff`, `spectator`.

**Contest form options** (director pages, **Contests**, when creating or editing a contest):
- **Staff for this contest:** ADNU emails of chief judges, judges, problem setters and registration staff, one per line (commas also work).
- **Allowed contestants:** optional list of ADNU emails. Blank means any ADNU account can register while registration is open; with a list, only
  those emails can register. Students who already registered keep their access, and staff are never blocked by the list.
- **Open entry:** off by default, so only ADNU accounts can sign in. Switch it on for a contest that welcomes non-Ateneo participants: any
  verified Google account can then register (personal Gmail included), and the contestant list may contain any email. Control who gets in with
  **Require staff approval** or the contestant list. Keep it on for the whole contest, because outside participants can only sign in while it is on.
  Directors and judges are always ADNU emails.

Staff roles other than director belong to the active contest; add them again for a new contest.
If a contestant is added to the staff list later, the same account becomes staff (no duplicate).

Turn it off again by removing the `INQUISITOR_GOOGLE_CLIENT_ID` line from `~/inquisitor/.env` and running `~/inquisitor/inq up -d`.
Never put the Client secret in chat, email or the repository. If it leaks, add a new secret in Google Cloud, delete the old one, and run `inq google-setup` again.

## Writing problem statements (Markdown, math and pictures)

Statements accept a small, safe set of Markdown plus LaTeX math. The problem editor shows a live preview under the statement box.

| Write | You get |
|---|---|
| `**bold**` and `*italic*` | **bold** and *italic* (they can be nested) |
| `- item` or `* item` on separate lines | a bullet list |
| `1. step` on separate lines | a numbered list |
| `` `code` `` and a block fenced by three backticks | code (nothing inside is treated as math or bold) |
| a blank line | a new paragraph (single line breaks are kept) |
| `$x^2$` | a formula inside a sentence |
| `$$\sum_{i=1}^{n} a_i$$` | a formula on its own line |
| `\$` | a plain dollar sign |
| `![the graph](graph.png)` | a picture you uploaded to that problem |

Pictures: in **Problems**, open **Edit problem**, find **Pictures for the statement**, upload PNG, JPEG, GIF or WebP files (up to 2 MB each, 12 per problem), then
paste the shown `![description](file.png)` into the statement. Only pictures uploaded to the problem can be shown, so contestants' computers never fetch anything
from outside. Pictures are checked by their content (not their name), cleaned file names are used, and they are included when the problem package is exported.
SVG files are refused. There are no links or raw HTML in statements, so a statement can never run a script. A formula with a mistake is shown in red, and an
unfinished one such as `$x` is left as typed. The math library (KaTeX) is bundled with the site, so no internet access is needed.

## Adding a problem

In the director pages, open **Problems** and fill the form (or open **Edit problem** on an existing one).

| Field | What to put |
|---|---|
| Statement | What contestants read. Markdown, math and pictures are supported (see above). |
| Sample input / output | Shown to contestants. They are not used for judging. |
| Time limit (ms) and Memory (MB) | Per problem. The time limit is for C, C++ and Rust. Other languages get more: Java 2x, Kotlin 2.5x, Python 2.5x, Ruby 3x. |
| Hidden tests ZIP | The real tests, never shown to contestants. See below. |
| Reference solution and language | A correct solution. **Validate** runs it on every hidden test, so you know the tests and the limits are right. |
| Published | Contestants only see published problems. |

**Hidden tests ZIP.** A ZIP with the files at its root (no folders), named in pairs: `input01.txt` with `output01.txt`, `input02.txt` with `output02.txt`, and so on.
Each input is fed to the contestant's program on standard input, and its output is compared with the matching output file. The two files of a pair must
have the same number. See `deploy/examples/example-hidden-tests.zip`. Uploading a new ZIP replaces all the old tests.

**How answers are checked.** The program's output must equal the expected output line by line. Spaces at the end of a line and blank lines at the end are ignored, and
nothing else is. There is no special checker, so each test must have exactly one correct output. Avoid "print any valid answer" problems, and print decimals with a
fixed number of places (for example `%.3f`).

**Before the contest.** Press **Validate** on each problem and make sure every check is green, read the statement in the live preview, and keep the limits generous
enough for slower languages. Contestants in Java must name their class `Main`. Examples of a statement and a reference solution are in `deploy/examples/`.

## Light theme

Every page has a sun/moon button in the top bar. The site is dark by default, and the choice is remembered in the browser. The light theme is a warm "dirty white"
and is derived automatically from the dark stylesheet (`theme.py`), so it stays in sync when the dark styles change. Buttons and links use a deep blue accent; set
`INQUISITOR_LIGHT_ACCENT=ink` in `~/inquisitor/.env` (then `~/inquisitor/inq up -d`) for graphite buttons instead. The projector scoreboard stays dark.

## Putting it on the internet (Cloudflare Tunnel)

The site listens on port 8080 of this machine. A tunnel connects a public address to it, so no router ports are opened.
If this server already runs a Cloudflare Tunnel, add an ingress rule to that tunnel's `config.yml` instead of creating a second one:

```yaml
ingress:
  - hostname: inq.pel47.cc
    service: http://localhost:8080
  # ...your other rules...
  - service: http_status:404
```

For a new tunnel:

```bash
cloudflared tunnel login
cloudflared tunnel create inquisitor
cloudflared tunnel route dns inquisitor inq.pel47.cc
# put the ingress rule above in ~/.cloudflared/config.yml, then:
sudo cloudflared service install
```

Once the site opens at `https://inq.pel47.cc`, do these two things:

```bash
~/inquisitor/inq https-on                                        # cookies only travel over https
INQ_PROFILE=shared INQ_BIND_ADDR=127.0.0.1 bash ~/inquisitor-src/deploy/setup.sh   # only the tunnel can reach the site
```

(Leave out `INQ_PROFILE=shared` on a machine that is only for Inquisitor. Running setup again keeps your data and settings.)

## Shared server: what the limits are
With `INQ_PROFILE=shared`:
- 2 judge threads, each limited to 1 CPU core, so judging uses at most 2 cores.
- The website is limited to 1.5 cores and 768 MB; the worker to half a core and 384 MB.
- All of the contest's containers run at a lower CPU priority than the rest of the server. When the CPU is busy, the other
  services get it first and the contest slows down instead of starving them. When the CPU is idle, the contest can use it.
- A compile may use at most 768 MB; running a submission is limited by the problem's memory limit (256 MB by default).
- Submissions run with no network, no extra privileges, a read-only filesystem, a process limit and a time limit.

Measured on a test install with these limits, with 100 students all submitting every 5 seconds (far busier than a real contest),
10 judges reviewing code and 10 director sessions browsing: every submission was judged correctly with no errors, the typical wait
for a verdict was about 53 seconds (worst 67 seconds), the website used up to about 1.6 cores and 300 MB, the worker about 0.2 cores
and 60 MB, and the judge containers about 2 cores. That is the most the contest can take; a normal contest is much lighter.

`~/inquisitor/inq down` stops the contest immediately and frees all of it.

## Update to a newer version

```bash
git -C ~/inquisitor-src pull
INQ_PROFILE=shared bash ~/inquisitor-src/deploy/setup.sh   # pulls the branch again and rebuilds; keeps data and settings
```

## Remove it completely

```bash
~/inquisitor/inq down
docker rmi inquisitor-web:1.0.0 inquisitor-judge-runtime:1.0
rm -rf ~/inquisitor
```

## If something is wrong
- **Every submission shows JE (judge error):** open the judge console; it lists which languages are ready. Check `~/inquisitor/inq logs worker`.
- **"port is already used":** run setup with `INQ_HTTP_PORT=<free port>`.
- **"this user cannot talk to docker":** add the user to the docker group (`sudo usermod -aG docker $USER`), log out and in, run setup again.
- **Cannot sign in after turning on https:** `~/inquisitor/inq https-off` until the site really opens over https.
