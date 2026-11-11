"""Offline tests: no Ollama or Docker needed. The model and the build are fakes."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dockergen as dg  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def make_project(tmp_path, files):
    for name, text in files.items():
        (tmp_path / name).write_text(text)
    return tmp_path


def test_inspect_reads_first_manifest_and_skips_noise(tmp_path):
    p = make_project(tmp_path, {"app.py": "", "requirements.txt": "flask==3.0.3\n"})
    (tmp_path / "node_modules").mkdir()
    ctx = dg.inspect_project(p)
    assert ctx.dependency_file == "requirements.txt"
    assert "flask==3.0.3" in ctx.dependencies
    assert "node_modules" not in ctx.files


def test_extract_dockerfile_handles_fences_and_plain_text():
    assert dg.extract_dockerfile("Here:\n```dockerfile\nFROM python:3.12-slim\n```\nDone") == "FROM python:3.12-slim\n"
    assert dg.extract_dockerfile("FROM golang:1.22\n") == "FROM golang:1.22\n"


def test_retry_prompt_includes_previous_dockerfile_and_error_tail(tmp_path):
    p = make_project(tmp_path, {"app.py": "", "requirements.txt": "flask\n"})
    prompts = []
    replies = iter(["FROM bad:tag\n", "FROM python:3.12-slim\n"])
    builds = iter([(False, "\n".join(f"line {i}" for i in range(100)) + "\nERROR: pull access denied"), (True, "")])

    ok = dg.run(p, 3, generate=lambda pr: (prompts.append(pr), next(replies))[1],
                build=lambda path: next(builds), log=lambda m: None)
    assert ok
    assert len(prompts) == 2
    assert "FROM bad:tag" in prompts[1]
    assert "pull access denied" in prompts[1]
    assert "line 0\n" not in prompts[1]  # only the tail of the error is sent
    assert (p / "Dockerfile").read_text() == "FROM python:3.12-slim\n"


def test_existing_dockerfile_is_backed_up_and_failure_returns_false(tmp_path):
    p = make_project(tmp_path, {"Dockerfile": "FROM mine\n", "main.go": ""})
    ok = dg.run(p, 2, generate=lambda pr: "FROM x\n", build=lambda path: (False, "boom"), log=lambda m: None)
    assert not ok
    backups = list(p.glob("Dockerfile.bak-*"))
    assert len(backups) == 1 and backups[0].read_text() == "FROM mine\n"


def test_main_rejects_bad_arguments(tmp_path, capsys):
    assert dg.main([str(tmp_path / "missing")]) == 2
    assert dg.main([str(tmp_path), "--attempts", "0"]) == 2


def test_samples_have_valid_manifests():
    go_mod = (ROOT / "samples/go-app/go.mod").read_text().splitlines()
    assert go_mod[0] == "module go-app" and "go 1.21" in go_mod
    import json
    assert "express" in json.loads((ROOT / "samples/node-app/package.json").read_text())["dependencies"]
