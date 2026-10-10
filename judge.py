from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Language:
    key: str
    label: str
    extension: str
    aliases: tuple[str, ...] = ()


LANGUAGES = {
    "cpp": Language("cpp", "C++", ".cpp", (".cc", ".cxx")),
    "c": Language("c", "C", ".c"),
    "java": Language("java", "Java", ".java"),
    "kotlin": Language("kotlin", "Kotlin", ".kt"),
    "python3": Language("python3", "Python 3", ".py"),
    "rust": Language("rust", "Rust", ".rs"),
    "ruby": Language("ruby", "Ruby", ".rb"),
}

EXTENSION_LANGUAGE = {}
for key, lang in LANGUAGES.items():
    EXTENSION_LANGUAGE[lang.extension.lower()] = key
    for alias in lang.aliases:
        EXTENSION_LANGUAGE[alias.lower()] = key


def detect_language_from_filename(filename: str | None) -> str | None:
    if not filename:
        return None
    return EXTENSION_LANGUAGE.get(Path(filename).suffix.lower())


def judge_backend() -> str:
    return os.environ.get("INQUISITOR_JUDGE_BACKEND", "local").strip().lower()


def compiler_status() -> dict[str, bool]:
    if judge_backend() == "docker":
        docker_ok = shutil.which("docker") is not None
        return {key: docker_ok for key in LANGUAGES}
    return {
        "cpp": shutil.which("g++") is not None,
        "c": shutil.which("gcc") is not None,
        "java": shutil.which("javac") is not None and shutil.which("java") is not None,
        "kotlin": shutil.which("kotlinc") is not None and shutil.which("java") is not None,
        "python3": bool(sys.executable),
        "rust": shutil.which("rustc") is not None,
        "ruby": shutil.which("ruby") is not None,
    }


def _normalized(text: str) -> str:
    lines = [line.rstrip() for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    while lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines)


def _commands(language: str, work: Path):
    if language == "cpp":
        src = work / "main.cpp"; exe = work / ("main.exe" if os.name == "nt" else "main")
        return src, ["g++", "-std=c++17", "-O2", "-pipe", str(src), "-o", str(exe)], [str(exe)]
    if language == "c":
        src = work / "main.c"; exe = work / ("main.exe" if os.name == "nt" else "main")
        return src, ["gcc", "-std=c17", "-O2", "-pipe", str(src), "-o", str(exe)], [str(exe)]
    if language == "java":
        src = work / "Main.java"; return src, ["javac", str(src)], ["java", "-cp", str(work), "Main"]
    if language == "kotlin":
        src = work / "Main.kt"; jar = work / "main.jar"; return src, ["kotlinc", str(src), "-include-runtime", "-d", str(jar)], ["java", "-jar", str(jar)]
    if language == "python3":
        src = work / "main.py"; return src, None, [sys.executable, "-I", str(src)]
    if language == "rust":
        src = work / "main.rs"; exe = work / ("main.exe" if os.name == "nt" else "main"); return src, ["rustc", "-O", str(src), "-o", str(exe)], [str(exe)]
    if language == "ruby":
        src = work / "main.rb"; return src, None, ["ruby", str(src)]
    raise ValueError("Unsupported language")


def _docker_commands(language: str):
    if language == "cpp": return "main.cpp", "g++ -std=c++17 -O2 -pipe main.cpp -o main", "./main"
    if language == "c": return "main.c", "gcc -std=c17 -O2 -pipe main.c -o main", "./main"
    if language == "java": return "Main.java", "javac Main.java", "java -cp /work Main"
    if language == "kotlin": return "Main.kt", "kotlinc Main.kt -include-runtime -d main.jar", "java -jar main.jar"
    if language == "python3": return "main.py", None, "python3 -I main.py"
    if language == "rust": return "main.rs", "rustc -O main.rs -o main", "./main"
    if language == "ruby": return "main.rb", None, "ruby main.rb"
    raise ValueError("Unsupported language")


def _docker_base(work: Path, memory_mb: int) -> list[str]:
    image = os.environ.get("INQUISITOR_JUDGE_IMAGE", "inquisitor-judge-runtime:1.0")
    return [
        "docker", "run", "--rm", "--network", "none", "--cpus", os.environ.get("INQUISITOR_JUDGE_CPUS", "1"),
        "--memory", f"{max(32, memory_mb)}m", "--memory-swap", f"{max(32, memory_mb)}m", "--pids-limit", "64",
        "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--user", "65534:65534", "--read-only", "--tmpfs", "/tmp:rw,noexec,nosuid,size=64m",
        "-v", f"{work.resolve()}:/work:rw", "-w", "/work", image,
    ]


def _judge_docker(*, language: str, source: str, tests_dir: Path, time_limit_ms: int, memory_limit_mb: int) -> dict:
    if shutil.which("docker") is None:
        return {"verdict": "JE", "runtime_ms": None, "detail": "Docker judge backend selected but docker CLI is unavailable."}
    inputs = sorted(tests_dir.glob("input*.txt"))
    if not inputs:
        return {"verdict": "JE", "runtime_ms": None, "detail": "No hidden test cases found."}
    factor = {"cpp":1.0,"c":1.0,"java":2.0,"kotlin":2.5,"python3":2.5,"rust":1.0,"ruby":3.0}[language]
    timeout=max(.10,(time_limit_ms/1000.0)*factor); max_runtime=0.0
    work_root = os.environ.get("INQUISITOR_JUDGE_WORK_ROOT")
    if work_root:
        Path(work_root).mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="inquisitor_docker_", dir=work_root or None) as tmp:
        work=Path(tmp); os.chmod(work,0o777); filename, compile_cmd, run_cmd=_docker_commands(language); src_path=(work/filename); src_path.write_text(source,encoding="utf-8"); os.chmod(src_path,0o666)
        if compile_cmd:
            cmd=_docker_base(work,memory_limit_mb)+["bash","-lc",compile_cmd]
            try: cp=subprocess.run(cmd,capture_output=True,text=True,timeout=45)
            except subprocess.TimeoutExpired: return {"verdict":"CE","runtime_ms":None,"detail":"Compilation exceeded 45 seconds."}
            if cp.returncode!=0: return {"verdict":"CE","runtime_ms":None,"detail":((cp.stderr or cp.stdout or "Compilation failed.")[-6000:])}
        for index,input_path in enumerate(inputs,1):
            output_path=tests_dir/input_path.name.replace("input","output",1)
            if not output_path.exists(): return {"verdict":"JE","runtime_ms":None,"detail":f"Missing expected output for test {index}."}
            cmd=_docker_base(work,memory_limit_mb)+["bash","-lc",run_cmd]
            started=time.perf_counter()
            try:
                rp=subprocess.run(cmd,input=input_path.read_text(encoding="utf-8"),capture_output=True,text=True,timeout=timeout+1.5)
            except subprocess.TimeoutExpired:
                return {"verdict":"TLE","runtime_ms":int(timeout*1000),"detail":f"Time limit exceeded on test {index}."}
            elapsed=(time.perf_counter()-started)*1000; max_runtime=max(max_runtime,elapsed)
            if rp.returncode in {137,143}: return {"verdict":"TLE","runtime_ms":int(max_runtime),"detail":f"Resource/time limit exceeded on test {index}."}
            if rp.returncode!=0: return {"verdict":"RTE","runtime_ms":int(max_runtime),"detail":f"Test {index}: {(rp.stderr or rp.stdout or f'Program exited with code {rp.returncode}.')[-6000:]}"}
            if _normalized(rp.stdout)!=_normalized(output_path.read_text(encoding="utf-8")): return {"verdict":"WA","runtime_ms":int(max_runtime),"detail":f"Wrong answer on hidden test {index}."}
    return {"verdict":"AC","runtime_ms":int(max_runtime),"detail":"All hidden tests passed in isolated Docker sandbox."}


def _judge_local(*, language: str, source: str, tests_dir: Path, time_limit_ms: int) -> dict:
    if language not in LANGUAGES:
        return {"verdict": "CE", "runtime_ms": None, "detail": "Unsupported language."}
    if not compiler_status().get(language):
        return {"verdict": "CE", "runtime_ms": None, "detail": f"Required compiler/runtime for {LANGUAGES[language].label} is not installed."}
    inputs=sorted(tests_dir.glob("input*.txt"))
    if not inputs: return {"verdict":"JE","runtime_ms":None,"detail":"No hidden test cases found."}
    factor={"cpp":1.0,"c":1.0,"java":2.0,"kotlin":2.5,"python3":2.5,"rust":1.0,"ruby":3.0}[language]; timeout=max(.10,(time_limit_ms/1000.0)*factor); max_runtime=0.0
    with tempfile.TemporaryDirectory(prefix="inquisitor_") as tmp:
        work=Path(tmp); src,compile_cmd,run_cmd=_commands(language,work); src.write_text(source,encoding="utf-8")
        if compile_cmd:
            try: cp=subprocess.run(compile_cmd,cwd=work,capture_output=True,text=True,timeout=30,shell=False)
            except subprocess.TimeoutExpired: return {"verdict":"CE","runtime_ms":None,"detail":"Compilation exceeded 30 seconds."}
            except OSError as exc: return {"verdict":"CE","runtime_ms":None,"detail":f"Compiler error: {exc}"}
            if cp.returncode!=0: return {"verdict":"CE","runtime_ms":None,"detail":(cp.stderr or cp.stdout or "Compilation failed.")[-6000:]}
        for index,input_path in enumerate(inputs,1):
            output_path=tests_dir/input_path.name.replace("input","output",1)
            if not output_path.exists(): return {"verdict":"JE","runtime_ms":None,"detail":f"Missing expected output for test {index}."}
            started=time.perf_counter()
            try: rp=subprocess.run(run_cmd,cwd=work,input=input_path.read_text(encoding="utf-8"),capture_output=True,text=True,timeout=timeout,shell=False)
            except subprocess.TimeoutExpired: return {"verdict":"TLE","runtime_ms":int(timeout*1000),"detail":f"Time limit exceeded on test {index}."}
            except OSError as exc: return {"verdict":"RTE","runtime_ms":None,"detail":f"Execution error: {exc}"}
            elapsed=(time.perf_counter()-started)*1000; max_runtime=max(max_runtime,elapsed)
            if rp.returncode!=0: return {"verdict":"RTE","runtime_ms":int(max_runtime),"detail":f"Test {index}: {(rp.stderr or rp.stdout or f'Program exited with code {rp.returncode}.')[-6000:]}"}
            if _normalized(rp.stdout)!=_normalized(output_path.read_text(encoding="utf-8")): return {"verdict":"WA","runtime_ms":int(max_runtime),"detail":f"Wrong answer on hidden test {index}."}
    return {"verdict":"AC","runtime_ms":int(max_runtime),"detail":"All hidden tests passed."}


def judge_submission(*, language: str, source: str, tests_dir: Path, time_limit_ms: int, memory_limit_mb: int = 256) -> dict:
    if language not in LANGUAGES:
        return {"verdict":"CE","runtime_ms":None,"detail":"Unsupported language."}
    if judge_backend()=="docker":
        return _judge_docker(language=language,source=source,tests_dir=tests_dir,time_limit_ms=time_limit_ms,memory_limit_mb=memory_limit_mb)
    if os.environ.get("INQUISITOR_PRODUCTION","0")=="1" and os.environ.get("INQUISITOR_ALLOW_UNSAFE_LOCAL_JUDGE","0")!="1":
        return {"verdict":"JE","runtime_ms":None,"detail":"Production mode requires INQUISITOR_JUDGE_BACKEND=docker. Unsafe local execution is disabled."}
    return _judge_local(language=language,source=source,tests_dir=tests_dir,time_limit_ms=time_limit_ms)
