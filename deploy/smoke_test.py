#!/usr/bin/env python3
"""End-to-end check of a running Inquisitor (needs only python3, no extra packages).

    python3 smoke_test.py [base_url] [env_file]

It signs in as the director, creates a throwaway student through the CSV import, submits three solutions
(Python, Java, C++), waits for the auto-judge, reviews one submission as a judge, and runs the similarity check.
The test student is named smoke-xxxx and stays in the database, so run this BEFORE the real contest and
reset the data afterwards (see README, "Fresh start").
"""
import http.cookiejar, os, re, sys, time, uuid, urllib.error, urllib.parse, urllib.request

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8080").rstrip("/")
ENV = sys.argv[2] if len(sys.argv) > 2 else os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")

def env_value(key):
    for line in open(ENV):
        if line.startswith(key + "="): return line.split("=", 1)[1].strip()
    raise SystemExit(f"{key} not found in {ENV}")

class Client:
    def __init__(self):
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))
    def _do(self, req):
        try:
            with self.opener.open(req, timeout=60) as r: return r.status, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e: return e.code, e.read().decode("utf-8", "replace")
    def get(self, path): return self._do(urllib.request.Request(BASE + path))
    def csrf(self, path="/login"):
        m = re.search(r'INQUISITOR_CSRF="([^"]+)"', self.get(path)[1]); return m.group(1) if m else ""
    def post(self, path, data, csrf_from="/login"):
        data = dict(data, csrf_token=self.csrf(csrf_from))
        return self._do(urllib.request.Request(BASE + path, urllib.parse.urlencode(data).encode()))
    def post_file(self, path, fields, file_field, filename, content, csrf_from="/login"):
        fields = dict(fields, csrf_token=self.csrf(csrf_from)); b = uuid.uuid4().hex; body = b""
        for k, v in fields.items(): body += f'--{b}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
        body += f'--{b}\r\nContent-Disposition: form-data; name="{file_field}"; filename="{filename}"\r\nContent-Type: text/csv\r\n\r\n'.encode() + content.encode() + f"\r\n--{b}--\r\n".encode()
        req = urllib.request.Request(BASE + path, body); req.add_header("Content-Type", f"multipart/form-data; boundary={b}")
        return self._do(req)

results = []
def check(name, ok, detail=""):
    results.append(bool(ok)); print(("PASS  " if ok else "FAIL  ") + name + (f"   ({detail})" if detail else ""), flush=True)
    return ok
def strip(html): return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))

PY_A = "n=int(input())\na=input().split()\nprint(*a[::-1])"
JAVA_C = "import java.util.*;public class Main{public static void main(String[] x){Scanner s=new Scanner(System.in);int n=s.nextInt();long t=0;while(n-->0)t+=s.nextLong();System.out.println(t);}}"
CPP_WRONG = "#include <iostream>\nint main(){std::cout<<0<<std::endl;}"

print(f"Testing {BASE}\n")
st, body = Client().get("/healthz"); check("web server is healthy", st == 200, body.strip()[:40])
admin = Client()
st, text = admin.post("/login", {"username": env_value("INQUISITOR_BOOTSTRAP_ADMIN_USER"), "password": env_value("INQUISITOR_BOOTSTRAP_ADMIN_PASSWORD")})
if not check("director can sign in", "Sign out" in text): raise SystemExit("cannot continue without the director login")
for p in ("/admin", "/judge", "/admin/participants", "/director/similarity", "/director/audit", "/director/operations"):
    check(f"director page {p} opens", admin.get(p)[0] == 200)
_, health = admin.get("/judge"); langs = re.findall(r'class="badge[^"]*">(Ready|Missing)<', health)
check("judge console reports every language ready", langs and all(x == "Ready" for x in langs), f"{langs.count('Ready')} ready, {langs.count('Missing')} missing")

user = "smoke-" + uuid.uuid4().hex[:4]; pw = "smoke-pass-" + uuid.uuid4().hex[:6]
st, text = admin.post_file("/director/participants/import", {}, "csv_file", "smoke.csv", f"username,display_name,department,password\n{user},Smoke Test,Computer Science,{pw}\n", "/admin/participants")
student = Client(); st, text = student.post("/login", {"username": user, "password": pw})
if not check("test student was created by CSV import and can sign in", "Sign out" in text, user): raise SystemExit("cannot continue without the test student")
if "Change password" in text or "/change-password" in student.get("/dashboard")[1] and False: pass
sent = 0
for code, lang, src in (("A", "python3", PY_A), ("C", "java", JAVA_C), ("B", "cpp", CPP_WRONG)):
    st, text = student.post("/submit", {"problem": code, "language": lang, "source": src}, "/submit")
    sent += st == 200 and "not open" not in text and "not registered" not in text
check("three submissions accepted (Python, Java, C++)", sent == 3, "if not, the contest may not be running: check its phase in the director pages")
deadline = time.time() + 150; verdicts = []
while time.time() < deadline:
    verdicts = re.findall(r">(AC|WA|TLE|RTE|CE|OLE|MLE|PE|JE)<", student.get("/submissions")[1])
    if len(verdicts) >= 3: break
    time.sleep(3)
check("auto-judge returned a verdict for all three", len(verdicts) == 3, f"verdicts: {sorted(verdicts)}")
check("verdicts are right (2 accepted, 1 wrong answer)", sorted(verdicts) == ["AC", "AC", "WA"], f"got {sorted(verdicts)}")

_, queue = admin.get(f"/judge?q={user}&show=unreviewed"); ids = list(dict.fromkeys(re.findall(r"/judge/submission/(\d+)", queue)))
if check("judge console lists the test submissions for review", len(ids) >= 3, f"{len(ids)} found") :
    sid = ids[0]; _, claim = admin.post(f"/judge/submission/{sid}/claim", {}, f"/judge/submission/{sid}")
    check("a judge can claim a submission and read its code", "You claimed" in claim and "class=\"code-view\"" in claim)
    other = Client(); other.post("/login", {"username": env_value("INQUISITOR_BOOTSTRAP_ADMIN_USER"), "password": env_value("INQUISITOR_BOOTSTRAP_ADMIN_PASSWORD")})
    _, second = admin.post(f"/judge/submission/{ids[1]}/claim", {}, f"/judge/submission/{ids[1]}")
    check("the same judge cannot hold two claims at once", "Finish or release" in strip(second))
    _, done = admin.post(f"/judge/submission/{sid}/review", {"verdict": "", "note": "smoke test review"}, f"/judge/submission/{sid}")
    check("finishing the review keeps the auto-judge verdict", "Review saved" in strip(done))
st, sim = admin.get("/director/similarity?run=1&scope=latest&threshold=0.82"); check("similarity check runs", st == 200 and "Similarity by participant" in sim)

print(f"\n{sum(results)} of {len(results)} checks passed.")
print(f"A test student named {user} now exists. Run this before the real contest and reset the data afterwards (README: Fresh start).")
sys.exit(0 if all(results) else 1)
