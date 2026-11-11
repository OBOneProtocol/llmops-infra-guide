#!/usr/bin/env python3
"""Generate a Dockerfile with a local LLM and let `docker build` judge it.

The loop: inspect the project, ask the model for a Dockerfile, build it, and
if the build fails, send the model its previous Dockerfile plus the tail of the
build error and try again. A successful `docker build` is the only thing that
counts as success.

Usage:
    python3 dockergen.py samples/flask-app
    python3 dockergen.py path/to/app --model qwen2.5-coder:7b --attempts 3

Exit code 0 means a Dockerfile was written and built; 1 means every attempt
failed (the last attempt's Dockerfile is left in place for inspection);
2 means a usage or setup error.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import time
import urllib.error
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

SYSTEM_RULES = (
    "Reply with only the Dockerfile, no explanation. Use an official, pinned base image tag, "
    "install dependencies before copying the rest of the source so layers cache, run as a "
    "non-root user where the base image allows it, and set the command that starts the app."
)


@dataclass
class ProjectContext:
    files: list[str]
    dependency_file: str | None
    dependencies: str


def inspect_project(path: Path) -> ProjectContext:
    """List the project's top-level files and read its first dependency manifest."""
    files = sorted(p.name for p in path.iterdir() if p.name not in IGNORED_NAMES)
    for name in DEPENDENCY_FILES:
        manifest = path / name
        if manifest.is_file():
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
            "Your previous Dockerfile failed to build.\n"
            f"Previous Dockerfile:\n{previous}\n\n"
            f"End of the docker build output:\n{error}\n\n"
            "Return a corrected Dockerfile."
        )
    return "\n\n".join(parts)


def extract_dockerfile(reply: str) -> str:
    """Take the first fenced code block if there is one, else the whole reply."""
    match = re.search(r"```[a-zA-Z]*\n(.*?)```", reply, re.S)
    text = match.group(1) if match else reply
    return text.strip() + "\n"


def tail(text: str, lines: int = MAX_ERROR_LINES) -> str:
    return "\n".join(text.strip().splitlines()[-lines:])


def ollama_generate(prompt: str, model: str, url: str, timeout: float) -> str:
    body = json.dumps({"model": model, "prompt": prompt, "stream": False}).encode()
    request = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.load(response)
    except urllib.error.URLError as exc:
        raise RuntimeError(f"could not reach Ollama at {url}: {exc.reason}") from exc
    if "response" not in data:
        raise RuntimeError(f"unexpected Ollama reply: {str(data)[:200]}")
    return data["response"]


def docker_build(path: Path, tag: str, timeout: float) -> tuple[bool, str]:
    try:
        result = subprocess.run(["docker", "build", "-t", tag, str(path)],
                                capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, f"docker build timed out after {timeout:.0f}s"
    return result.returncode == 0, result.stdout + result.stderr


def run(path: Path, attempts: int, generate: Callable[[str], str],
        build: Callable[[Path], tuple[bool, str]], log: Callable[[str], None] = print) -> bool:
    dockerfile_path = path / "Dockerfile"
    if dockerfile_path.exists():
        backup = path / f"Dockerfile.bak-{time.strftime('%Y%m%d-%H%M%S')}"
        shutil.copy2(dockerfile_path, backup)
        log(f"Existing Dockerfile backed up to {backup.name}")

    context = inspect_project(path)
    previous: str | None = None
    error: str | None = None
    for attempt in range(1, attempts + 1):
        dockerfile = extract_dockerfile(generate(build_prompt(context, previous, error)))
        dockerfile_path.write_text(dockerfile, encoding="utf-8")
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
    parser.add_argument("--url", default=DEFAULT_URL, help="Ollama generate endpoint")
    parser.add_argument("--attempts", type=int, default=3)
    parser.add_argument("--tag", default="dockergen-test", help="image tag used for test builds")
    parser.add_argument("--llm-timeout", type=float, default=300)
    parser.add_argument("--build-timeout", type=float, default=900)
    args = parser.parse_args(argv)

    if not args.project.is_dir():
        print(f"error: {args.project} is not a directory", file=sys.stderr)
        return 2
    if args.attempts < 1:
        print("error: --attempts must be at least 1", file=sys.stderr)
        return 2
    if shutil.which("docker") is None:
        print("error: docker is not on PATH", file=sys.stderr)
        return 2

    try:
        ok = run(
            args.project.resolve(), args.attempts,
            generate=lambda prompt: ollama_generate(prompt, args.model, args.url, args.llm_timeout),
            build=lambda path: docker_build(path, args.tag, args.build_timeout),
        )
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
