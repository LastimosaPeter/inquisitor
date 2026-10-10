# Inquisitor v1.0.7 — Galahad
## Programming Language Deployment Guide

Inquisitor supports these contest languages:

| Inquisitor language | Source extensions | Production command/runtime |
|---|---|---|
| C++ | `.cpp`, `.cc`, `.cxx` | `g++ -std=c++17 -O2 -pipe` |
| C | `.c` | `gcc -std=c17 -O2 -pipe` |
| Java | `.java` | `javac` + `java` |
| Kotlin | `.kt` | `kotlinc` + `java` |
| Python 3 | `.py` | `python3 -I` |
| Rust | `.rs` | `rustc -O` |
| Ruby | `.rb` | `ruby` |

## Short answer

**On the recommended Linux + Docker production deployment: yes.** All seven languages are included in `Dockerfile.judge-runtime` and are available to the judge **after the judge runtime image has been built successfully**.

The production judge image installs:

```text
gcc
g++
openjdk-21-jdk-headless
python3
ruby-full
rustc
kotlin
```

The web container does not compile contestant programs itself. The worker launches a fresh isolated judge container for each compile/run operation.

**On native Windows/local development: not automatically.** Inquisitor can start with only Python installed, but C/C++, Java, Kotlin, Rust, and Ruby submissions will only work if those compilers/runtimes are installed on Windows and visible in `PATH`.

---

# A. Recommended production setup — Linux + Docker

Use Ubuntu 24.04 or Debian 12 on the contest server.

## 1. Install Docker Engine and the Compose plugin

Confirm:

```bash
docker --version
docker compose version
```

Both commands must work before continuing.

## 2. Prepare Inquisitor

From the extracted Inquisitor directory:

```bash
cp .env.example .env
```

Edit `.env` and make sure the judging settings include:

```text
INQUISITOR_JUDGE_BACKEND=docker
INQUISITOR_JUDGE_IMAGE=inquisitor-judge-runtime:1.0
INQUISITOR_ALLOW_UNSAFE_LOCAL_JUDGE=0
```

For a real contest, also keep:

```text
INQUISITOR_DEMO_ACCOUNTS=0
```

## 3. Build the judge runtime

This is the step that actually installs all seven programming languages:

```bash
docker build --pull -t inquisitor-judge-runtime:1.0 -f Dockerfile.judge-runtime .
```

Do not skip this step. The worker can be healthy while submissions still fail with Judge Error if the runtime image does not exist.

Confirm the image exists:

```bash
docker image inspect inquisitor-judge-runtime:1.0 >/dev/null && echo "Judge image ready"
```

## 4. Verify all seven languages before starting a contest

Run the included verifier:

```bash
chmod +x verify_judge_runtime.sh
./verify_judge_runtime.sh
```

It checks that the image contains every required compiler/runtime and performs a tiny compile/run smoke test for C++, C, Java, Kotlin, Python 3, Rust, and Ruby.

You can also inspect versions manually:

```bash
docker run --rm inquisitor-judge-runtime:1.0 bash -lc '
  echo "=== C++ ==="; g++ --version | head -n1
  echo "=== C ==="; gcc --version | head -n1
  echo "=== Java ==="; javac -version; java -version 2>&1 | head -n1
  echo "=== Kotlin ==="; kotlinc -version 2>&1 | head -n1
  echo "=== Python ==="; python3 --version
  echo "=== Rust ==="; rustc --version
  echo "=== Ruby ==="; ruby --version
'
```

Save this output with your contest records. It establishes the exact compiler/runtime versions used during the event.

## 5. Start Inquisitor

```bash
sudo mkdir -p /srv/inquisitor/data/judge-work
sudo chmod 755 /srv/inquisitor /srv/inquisitor/data
sudo chmod 777 /srv/inquisitor/data/judge-work

docker compose build web worker
docker compose up -d web worker
```

Check service health:

```bash
docker compose ps
```

Then inspect logs:

```bash
docker compose logs --tail=100 web
docker compose logs --tail=100 worker
```

## 6. Make sure the worker can see the judge image

The worker talks to the host Docker daemon through `/var/run/docker.sock`.

Run:

```bash
docker compose exec worker docker image inspect inquisitor-judge-runtime:1.0 >/dev/null \
  && echo "Worker can see judge runtime"
```

If that succeeds, the production worker can launch the judge containers.

---

# B. Language-specific requirements

## C++

Accepted extensions:

```text
.cpp
.cc
.cxx
```

Compilation:

```bash
g++ -std=c++17 -O2 -pipe main.cpp -o main
```

Inquisitor currently judges C++ as **C++17**.

## C

Extension:

```text
.c
```

Compilation:

```bash
gcc -std=c17 -O2 -pipe main.c -o main
```

Inquisitor currently judges C as **C17**.

## Java

Extension:

```text
.java
```

The submitted source is stored as `Main.java`, so contestants should normally use:

```java
public class Main {
    public static void main(String[] args) {
    }
}
```

The judge runtime uses OpenJDK 21.

For Java problems, avoid extremely small memory limits. **256 MB or more** is a sensible baseline because the JVM itself requires memory in addition to the program's objects.

## Kotlin

Extension:

```text
.kt
```

Typical entry point:

```kotlin
fun main() {
}
```

Compilation:

```bash
kotlinc Main.kt -include-runtime -d main.jar
```

Execution:

```bash
java -jar main.jar
```

Kotlin/JVM has greater startup and compiler overhead than C/C++. For contest problems that permit Kotlin, prefer **256–512 MB or higher** memory limits and account for language-specific time multipliers when validating reference solutions.

## Python 3

Extension:

```text
.py
```

Execution:

```bash
python3 -I main.py
```

`-I` enables isolated Python mode, reducing dependence on user/environment configuration.

## Rust

Extension:

```text
.rs
```

Compilation:

```bash
rustc -O main.rs -o main
```

Typical entry point:

```rust
fn main() {
}
```

## Ruby

Extension:

```text
.rb
```

Execution:

```bash
ruby main.rb
```

Ruby is interpreted and commonly needs a larger time allowance than compiled languages. Inquisitor already applies a language time multiplier internally, but problem setters should still test official solutions in the real judge environment.

---

# C. Smart file detection

Inquisitor maps uploaded filenames as follows:

```text
solution.cpp / .cc / .cxx -> C++
solution.c                 -> C
Main.java                  -> Java
Main.kt                    -> Kotlin
solution.py                -> Python 3
solution.rs                -> Rust
solution.rb                -> Ruby
```

Detection happens in the UI and is checked again server-side. The file extension is a convenience, not a security boundary; the judge always uses Inquisitor's selected/detected language configuration.

---

# D. Native Windows/local mode

`start_windows.bat` uses:

```text
INQUISITOR_JUDGE_BACKEND=local
```

In local mode, Inquisitor checks for these commands on your Windows `PATH`:

```text
C++       g++
C         gcc
Java      javac + java
Kotlin    kotlinc + java
Python 3  the Python interpreter running Inquisitor
Rust      rustc
Ruby      ruby
```

You can check manually in PowerShell:

```powershell
Get-Command g++ -ErrorAction SilentlyContinue
Get-Command gcc -ErrorAction SilentlyContinue
Get-Command javac -ErrorAction SilentlyContinue
Get-Command java -ErrorAction SilentlyContinue
Get-Command kotlinc -ErrorAction SilentlyContinue
Get-Command python -ErrorAction SilentlyContinue
Get-Command rustc -ErrorAction SilentlyContinue
Get-Command ruby -ErrorAction SilentlyContinue
```

If one is missing, install the corresponding toolchain and restart the terminal so the updated `PATH` is loaded.

For an actual ACPC event, **do not use native local execution for untrusted contestant code**. Use the Docker judge backend on Linux.

---

# E. Pre-contest language certification checklist

Before registration opens, create one trivial problem such as adding two integers and submit an accepted solution in every enabled language.

Verify:

- [ ] C++ compiles and receives AC.
- [ ] C compiles and receives AC.
- [ ] Java compiles and receives AC.
- [ ] Kotlin compiles and receives AC.
- [ ] Python 3 receives AC.
- [ ] Rust compiles and receives AC.
- [ ] Ruby receives AC.
- [ ] Wrong solutions receive WA.
- [ ] Infinite loops receive TLE.
- [ ] Invalid source receives CE where applicable.
- [ ] Runtime failures receive RTE.
- [ ] The worker remains healthy after repeated submissions.
- [ ] Judge containers have no network access.
- [ ] Compiler/runtime versions have been recorded.
- [ ] Every official problem has been tested using each language you promise contestants will be supported for.

Do this again after any server update, Docker image rebuild, or language/toolchain update.

---

# F. Common failures

## `Judge Error: image not found`

Build the runtime:

```bash
docker build -t inquisitor-judge-runtime:1.0 -f Dockerfile.judge-runtime .
```

Then restart the worker:

```bash
docker compose restart worker
```

## `docker: command not found` inside worker

Rebuild the web/worker image because the supplied `Dockerfile` installs the Docker CLI:

```bash
docker compose build --no-cache web worker
docker compose up -d web worker
```

## Worker cannot access Docker daemon

Confirm the socket exists on the Linux host:

```bash
ls -l /var/run/docker.sock
```

Then confirm the worker has the mount:

```bash
docker compose exec worker docker info
```

## A compiler is missing from the judge image

Rebuild without cache:

```bash
docker build --pull --no-cache -t inquisitor-judge-runtime:1.0 -f Dockerfile.judge-runtime .
```

Run `./verify_judge_runtime.sh` again.

## Java/Kotlin receive runtime or memory failures on otherwise-correct solutions

Increase that problem's memory limit. JVM languages need runtime overhead that C/C++ do not.

## Compiler versions changed unexpectedly

The default runtime image uses Ubuntu packages. Once your contest environment is certified, avoid rebuilding the judge image immediately before or during the contest. Keep the tested image and record its image ID:

```bash
docker image inspect inquisitor-judge-runtime:1.0 --format '{{.Id}}'
```

For stricter reproducibility in future releases, pin compiler/runtime versions in `Dockerfile.judge-runtime` and tag the image per contest, for example:

```text
inquisitor-judge-runtime:acpc-2026
```

---

# Recommended ACPC configuration

For Ateneo Computer Programming Competition 2026, the safest deployment is:

```text
Ubuntu/Debian server
        |
Docker Compose
        |
+-- Inquisitor web
+-- Inquisitor worker
        |
Docker socket
        |
Inquisitor judge runtime
        |
+-- C++17
+-- C17
+-- Java 21
+-- Kotlin/JVM
+-- Python 3
+-- Rust
+-- Ruby
```

Build and certify this environment **before contest day**, then do not update the judge image during the event.
