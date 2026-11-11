#!/usr/bin/env python3
"""Generate a Dockerfile with a local LLM and let `docker build` judge it.

The loop: inspect the project, ask the model for a Dockerfile, build it, and
if the build fails, send the model its previous Dockerfile plus the tail of the
build error and try again. A successful `docker build` is the only thing that
counts as success.

The model's RUN steps execute during `docker build`. Use this on projects you
trust, review the Dockerfile it prints, and prefer `--network none` or
`--dry-run` (see README, "Safety").

Usage:
    python3 dockergen.py samples/flask-app
    python3 dockergen.py path/to/app --model qwen2.5-coder:7b --attempts 3
    python3 dockergen.py path/to/app --network none
    python3 dockergen.py path/to/app --dry-run

Exit codes: 0 a Dockerfile was built (or, with --dry-run, written);
1 every attempt failed or was refused (the last Dockerfile is left in place);
2 usage or setup error (bad arguments, Docker missing or its daemon unreachable,
Ollama unreachable or returning an empty or malformed reply, symlinked
Dockerfile).
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

DEFAULT_URL = "http://localhost:11434/api/generate"
DEFAULT_MODEL = "qwen2.5-coder:7b"
DEPENDENCY_FILES = ["requirements.txt", "pyproject.toml", "package.json", "go.mod", "Cargo.toml", "Gemfile"]
MAX_DEPENDENCY_CHARS = 4000
MAX_ERROR_LINES = 40
IGNORED_NAMES = {".git", "node_modules", "__pycache__", ".venv", "venv", "Dockerfile"}
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
DAEMON_DOWN = "Cannot connect to the Docker daemon"

SYSTEM_RULES = (
    "Reply with only the Dockerfile, no explanation. Use an official, pinned base image tag, "
    "install dependencies before copying the rest of the source so layers cache, run as a "
    "non-root user where the base image allows it, and set the command that starts the app."
)

# Dockerfile features that reach beyond a plain build: a custom frontend, extra
# mounts (secrets, SSH, host caches), network or security overrides on RUN, and
# remote downloads through ADD. A Dockerfile using any of them is never built.
REFUSED_PATTERNS = [
    (re.compile(r"^\s*#\s*syntax\s*=", re.I | re.M), "a '# syntax=' directive (custom BuildKit frontend)"),
    (re.compile(r"--mount\s*=", re.I), "a RUN --mount= flag"),
    (re.compile(r"--network\s*=", re.I), "a RUN --network= flag"),
    (re.compile(r"--security\s*=", re.I), "a RUN --security= flag"),
    (re.compile(r"^\s*ADD\b[^\n]*?https?://", re.I | re.M), "ADD from a remote URL"),
]

FENCE_RE = re.compile(r"```[^\n`]*\n(.*?)(?:```|\Z)", re.S)


@dataclass
class ProjectContext:
    files: list[str]
    dependency_file: str | None
    dependencies: str


def warn(message: str) -> None:
    print(f"warning: {message}", file=sys.stderr)


def _listed(name: str) -> bool:
    return name not in IGNORED_NAMES and not name.startswith("Dockerfile.bak-")


def inspect_project(path: Path) -> ProjectContext:
    """List the project's top-level files and read its first safe dependency manifest.

    A manifest that is a symlink, or that resolves outside the project, is skipped so
    that a link like requirements.txt -> ~/.ssh/id_rsa never ends up in the prompt.
    """
    root = path.resolve()
    files = sorted(p.name for p in path.iterdir() if _listed(p.name))
    for name in DEPENDENCY_FILES:
        manifest = path / name
        if manifest.is_symlink():
            warn(f"skipping {name}: it is a symlink")
            continue
        if not manifest.is_file():
            continue
        if not manifest.resolve().is_relative_to(root):
            warn(f"skipping {name}: it resolves outside the project")
            continue
        text = manifest.read_text(encoding="utf-8", errors="replace")
        return ProjectContext(files, name, text[:MAX_DEPENDENCY_CHARS])
    return ProjectContext(files, None, "")


def build_prompt(context: ProjectContext, previous: str | None = None, error: str | None = None) -> str:
    parts = [
        "Write a Dockerfile for this project.",
        f"Top-level files: {', '.join(context.files) or '(none)'}.",
    ]
    if context.dependency_file:
        parts.append(f"{context.dependency_file}:\n{context.dependencies}")
    parts.append(SYSTEM_RULES)
    if previous is not None and error is not None:
        parts.append(
            "Your previous Dockerfile failed.\n"
            f"Previous Dockerfile:\n{previous}\n\n"
            f"Reason or end of the docker build output:\n{error}\n\n"
            "Return a corrected Dockerfile."
        )
    return "\n\n".join(parts)


def extract_dockerfile(reply: str) -> str:
    """Take the first fenced code block if there is one, else the whole reply.

    Handles an unclosed fence, a fence without a language tag, and a one-line
    fence such as ```FROM x```. Returns "" when nothing usable is left.
    """
    start = reply.find("```")
    if start == -1:
        text = reply
    else:
        first_line = reply[start + 3:].split("\n", 1)[0]
        if "```" in first_line:  # one-line fence: ```FROM x``` or ```dockerfile FROM x```
            text = re.sub(r"^\s*dockerfile\s+", "", first_line.split("```", 1)[0], flags=re.I)
        else:
            match = FENCE_RE.search(reply, start)
            text = match.group(1) if match else reply[start + 3:]
    text = text.strip()
    return text + "\n" if text else ""


def check_dockerfile(text: str) -> str | None:
    """Return the reason a Dockerfile must not be built, or None if it may be."""
    for pattern, description in REFUSED_PATTERNS:
        if pattern.search(text):
            return (f"refused by dockergen: the Dockerfile uses {description}, which is not allowed. "
                    "Write a plain Dockerfile without it.")
    return None


def tail(text: str, lines: int = MAX_ERROR_LINES) -> str:
    return "\n".join(text.strip().splitlines()[-lines:])


def ollama_generate(prompt: str, model: str, url: str, timeout: float) -> str:
    body = json.dumps({"model": model, "prompt": prompt, "stream": False}).encode()
    request = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.load(response)
    except (OSError, ValueError, TimeoutError, http.client.HTTPException) as exc:
        # OSError covers URLError, refused/reset connections and socket timeouts;
        # ValueError covers a body that is not JSON.
        reason = getattr(exc, "reason", exc)
        raise RuntimeError(f"could not get a reply from Ollama at {url}: {reason}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("response"), str):
        raise RuntimeError(f"unexpected Ollama reply: {str(data)[:200]}")
    if not data["response"].strip():
        raise RuntimeError(f"Ollama returned an empty reply (model {model})")
    return data["response"]


def docker_build(path: Path, tag: str, timeout: float, network: str = "default") -> tuple[bool, str]:
    try:
        result = subprocess.run(["docker", "build", f"--network={network}", "-t", tag, str(path)],
                                capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, f"docker build timed out after {timeout:.0f}s"
    output = result.stdout + result.stderr
    if result.returncode != 0 and DAEMON_DOWN in output:
        raise RuntimeError(f"Docker daemon is not reachable: {tail(output, 1)}")
    return result.returncode == 0, output


def docker_daemon_error() -> str | None:
    """Return why the Docker daemon cannot be used, or None if `docker info` works."""
    try:
        result = subprocess.run(["docker", "info"], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return str(exc)
    if result.returncode != 0:
        return tail(result.stderr or result.stdout, 3) or f"docker info exited {result.returncode}"
    return None


def backup_path(path: Path) -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    candidate = path / f"Dockerfile.bak-{stamp}"
    n = 0
    while candidate.exists() or candidate.is_symlink():
        n += 1
        candidate = path / f"Dockerfile.bak-{stamp}-{n}"
    return candidate


def back_up(dockerfile_path: Path) -> Path:
    """Copy the existing Dockerfile to a new, never-overwritten backup file."""
    data = dockerfile_path.read_bytes()
    while True:
        target = backup_path(dockerfile_path.parent)
        try:
            with open(target, "xb") as fh:  # O_EXCL: fails instead of overwriting
                fh.write(data)
            return target
        except FileExistsError:
            continue


def write_dockerfile(dockerfile_path: Path, text: str) -> None:
    """Write without following a symlink (O_NOFOLLOW), so a link cannot redirect the write."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(dockerfile_path, flags, 0o644)
    except OSError as exc:
        raise RuntimeError(f"cannot write {dockerfile_path}: {exc}") from exc
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)


def run(path: Path, attempts: int, generate: Callable[[str], str],
        build: Callable[[Path], tuple[bool, str]], log: Callable[[str], None] = print,
        dry_run: bool = False) -> bool:
    dockerfile_path = path / "Dockerfile"
    if dockerfile_path.is_symlink():
        raise RuntimeError(f"{dockerfile_path} is a symlink; refusing to read or overwrite it")
    if dockerfile_path.exists():
        backup = back_up(dockerfile_path)
        log(f"Existing Dockerfile backed up to {backup.name}")

    context = inspect_project(path)
    previous: str | None = None
    error: str | None = None
    for attempt in range(1, attempts + 1):
        dockerfile = extract_dockerfile(generate(build_prompt(context, previous, error)))
        if not dockerfile:
            raise RuntimeError("the model reply contained no Dockerfile")
        write_dockerfile(dockerfile_path, dockerfile)
        refusal = check_dockerfile(dockerfile)
        log(f"--- Dockerfile, attempt {attempt} ---\n{dockerfile.rstrip()}\n--- end Dockerfile ---")
        if dry_run:
            if refusal:
                warn(refusal)
            log(f"Dry run: Dockerfile written to {dockerfile_path}; not built.")
            return True
        if refusal:
            previous, error = dockerfile, refusal
            log(f"Attempt {attempt} not built: {refusal}")
            continue
        ok, output = build(path)
        if ok:
            log(f"Build succeeded on attempt {attempt}. Dockerfile written to {dockerfile_path}")
            return True
        previous, error = dockerfile, tail(output)
        log(f"Attempt {attempt} failed:\n{tail(output, 5)}")
    log(f"No working Dockerfile after {attempts} attempts; the last one is left at {dockerfile_path}")
    return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("project", type=Path, help="project directory to containerize")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--url", default=DEFAULT_URL,
                        help="Ollama generate endpoint; a remote host receives file names and the manifest")
    parser.add_argument("--attempts", type=int, default=3)
    parser.add_argument("--tag", default="dockergen-test", help="image tag used for test builds")
    parser.add_argument("--network", choices=["default", "none"], default="default",
                        help="network for RUN steps during docker build (none blocks downloads)")
    parser.add_argument("--dry-run", action="store_true",
                        help="write and print one Dockerfile, do not build it")
    parser.add_argument("--llm-timeout", type=float, default=300)
    parser.add_argument("--build-timeout", type=float, default=900)
    args = parser.parse_args(argv)

    if not args.project.is_dir():
        print(f"error: {args.project} is not a directory", file=sys.stderr)
        return 2
    if args.attempts < 1:
        print("error: --attempts must be at least 1", file=sys.stderr)
        return 2
    if (args.project / "Dockerfile").is_symlink():
        print(f"error: {args.project / 'Dockerfile'} is a symlink; refusing to read or overwrite it",
              file=sys.stderr)
        return 2
    host = urllib.parse.urlparse(args.url).hostname
    if host not in LOCAL_HOSTS:
        warn(f"--url host {host!r} is not local; file names and the dependency manifest will be sent there")
    if not args.dry_run:
        if shutil.which("docker") is None:
            print("error: docker is not on PATH", file=sys.stderr)
            return 2
        daemon_error = docker_daemon_error()
        if daemon_error:
            print(f"error: Docker daemon is not reachable: {daemon_error}", file=sys.stderr)
            return 2

    try:
        ok = run(
            args.project.resolve(), args.attempts,
            generate=lambda prompt: ollama_generate(prompt, args.model, args.url, args.llm_timeout),
            build=lambda path: docker_build(path, args.tag, args.build_timeout, args.network),
            dry_run=args.dry_run,
        )
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
