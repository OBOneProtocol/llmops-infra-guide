"""Offline tests: no Ollama or Docker needed. The model and the build are fakes."""

import io
import json
import socket
import sys
import urllib.error
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dockergen as dg  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
QUIET = {"log": lambda m: None}


def make_project(tmp_path, files):
    for name, text in files.items():
        (tmp_path / name).write_text(text)
    return tmp_path


class Calls:
    """Records docker builds; fails the test if a build happens when it must not."""

    def __init__(self, results):
        self.results = list(results)
        self.paths = []

    def __call__(self, path, *args, **kwargs):
        self.paths.append(path)
        return self.results.pop(0)


def fake_main_env(monkeypatch, replies, builds):
    """Patch the outside world for main(): Docker present and up, model and build faked."""
    replies = iter(replies)
    build = Calls(builds)
    monkeypatch.setattr(dg.shutil, "which", lambda name: "/usr/bin/docker")
    monkeypatch.setattr(dg, "docker_daemon_error", lambda: None)
    monkeypatch.setattr(dg, "ollama_generate", lambda prompt, model, url, timeout: next(replies))
    monkeypatch.setattr(dg, "docker_build", build)
    return build


# --- project inspection -----------------------------------------------------------

def test_inspect_reads_first_manifest_and_skips_noise(tmp_path):
    p = make_project(tmp_path, {"app.py": "", "requirements.txt": "flask==3.0.3\n",
                                "Dockerfile.bak-20261110-120000": "FROM old\n"})
    (tmp_path / "node_modules").mkdir()
    ctx = dg.inspect_project(p)
    assert ctx.dependency_file == "requirements.txt"
    assert "flask==3.0.3" in ctx.dependencies
    assert "node_modules" not in ctx.files
    assert not any(name.startswith("Dockerfile.bak-") for name in ctx.files)


def test_symlinked_manifest_is_skipped(tmp_path, capsys):
    secret = tmp_path / "secret_key"
    secret.write_text("PRIVATE KEY MATERIAL\n")
    project = tmp_path / "app"
    project.mkdir()
    (project / "requirements.txt").symlink_to(secret)
    (project / "package.json").write_text('{"name": "ok"}\n')
    ctx = dg.inspect_project(project)
    assert ctx.dependency_file == "package.json"
    assert "PRIVATE KEY" not in ctx.dependencies
    assert "PRIVATE KEY" not in dg.build_prompt(ctx)
    assert "symlink" in capsys.readouterr().err


# --- fence extraction ------------------------------------------------------------

@pytest.mark.parametrize("reply, expected", [
    ("Here:\n```dockerfile\nFROM python:3.12-slim\n```\nDone", "FROM python:3.12-slim\n"),
    ("FROM golang:1.22\n", "FROM golang:1.22\n"),
    ("```\nFROM alpine:3.20\nCMD [\"sh\"]\n```", "FROM alpine:3.20\nCMD [\"sh\"]\n"),          # no language tag
    ("```Dockerfile\nFROM alpine:3.20\nRUN true", "FROM alpine:3.20\nRUN true\n"),                # unclosed fence
    ("```FROM alpine:3.20```", "FROM alpine:3.20\n"),                                            # same-line fence
    ("Use this: ```dockerfile FROM alpine:3.20``` ok", "FROM alpine:3.20\n"),                     # same-line + tag
    ("```docker-file\nFROM alpine:3.20\n```", "FROM alpine:3.20\n"),                              # odd tag chars
])
def test_extract_dockerfile_fence_variants(reply, expected):
    assert dg.extract_dockerfile(reply) == expected


def test_extract_dockerfile_empty_block_is_empty():
    assert dg.extract_dockerfile("```dockerfile\n```") == ""
    assert dg.extract_dockerfile("   \n") == ""


# --- retry loop ------------------------------------------------------------------

def test_retry_prompt_includes_previous_dockerfile_and_error_tail(tmp_path):
    p = make_project(tmp_path, {"app.py": "", "requirements.txt": "flask\n"})
    prompts = []
    replies = iter(["FROM bad:tag\n", "FROM python:3.12-slim\n"])
    build = Calls([(False, "\n".join(f"line {i}" for i in range(100)) + "\nERROR: pull access denied"), (True, "")])

    ok = dg.run(p, 3, generate=lambda pr: (prompts.append(pr), next(replies))[1], build=build, **QUIET)
    assert ok
    assert len(prompts) == 2
    assert "FROM bad:tag" in prompts[1]
    assert "pull access denied" in prompts[1]
    # MAX_ERROR_LINES = 40: the last 40 lines are "line 61".."line 99" plus the ERROR line.
    assert "line 99" in prompts[1]
    assert "line 61" in prompts[1]
    assert "line 59" not in prompts[1]
    assert "line 0\n" not in prompts[1]
    assert (p / "Dockerfile").read_text() == "FROM python:3.12-slim\n"


def test_empty_model_reply_raises_without_building(tmp_path):
    p = make_project(tmp_path, {"main.go": ""})
    build = Calls([])
    with pytest.raises(RuntimeError, match="no Dockerfile"):
        dg.run(p, 3, generate=lambda pr: "```dockerfile\n   \n```", build=build, **QUIET)
    assert build.paths == []


def test_dockerfile_is_printed_before_each_build(tmp_path):
    p = make_project(tmp_path, {"main.go": ""})
    events = []
    build = lambda path: (events.append("build"), (False, "boom"))[1]  # noqa: E731
    dg.run(p, 2, generate=lambda pr: "FROM golang:1.22\n", build=build, log=lambda m: events.append(m))
    first_build = events.index("build")
    assert any("--- Dockerfile, attempt 1 ---" in e and "FROM golang:1.22" in e for e in events[:first_build])
    assert sum(1 for e in events if e == "build") == 2


# --- backups ---------------------------------------------------------------------

def test_existing_dockerfile_is_backed_up_and_failure_returns_false(tmp_path):
    p = make_project(tmp_path, {"Dockerfile": "FROM mine\n", "main.go": ""})
    ok = dg.run(p, 2, generate=lambda pr: "FROM x\n", build=lambda path: (False, "boom"), **QUIET)
    assert not ok
    backups = list(p.glob("Dockerfile.bak-*"))
    assert len(backups) == 1 and backups[0].read_text() == "FROM mine\n"


def test_backup_never_overwrites_within_the_same_second(tmp_path, monkeypatch):
    monkeypatch.setattr(dg.time, "strftime", lambda fmt: "20261110-120000")
    p = make_project(tmp_path, {"main.go": ""})
    contents = ["FROM first\n", "FROM second\n", "FROM third\n"]
    for text in contents:
        (p / "Dockerfile").write_text(text)
        dg.run(p, 1, generate=lambda pr: "FROM new\n", build=lambda path: (True, ""), **QUIET)
    names = sorted(b.name for b in p.glob("Dockerfile.bak-*"))
    assert names == ["Dockerfile.bak-20261110-120000", "Dockerfile.bak-20261110-120000-1",
                     "Dockerfile.bak-20261110-120000-2"]
    assert sorted(b.read_text() for b in p.glob("Dockerfile.bak-*")) == sorted(contents)


# --- check_dockerfile refusals ---------------------------------------------------

REFUSED = {
    "syntax": "# syntax=docker/dockerfile:1\nFROM alpine:3.20\n",
    "syntax-spaced": "  #  SYNTAX = evil/frontend\nFROM alpine:3.20\n",
    "mount": "FROM alpine:3.20\nRUN --mount=type=secret,id=x cat /run/secrets/x\n",
    "network": "FROM alpine:3.20\nRUN --network=host wget http://10.0.0.1\n",
    "security": "FROM alpine:3.20\nRUN --security=insecure true\n",
    "add-url": "FROM alpine:3.20\nADD https://example.com/x.tgz /x\n",
    "add-url-flags": "FROM alpine:3.20\nadd --chown=1000 http://example.com/x /x\n",
}


@pytest.mark.parametrize("name", sorted(REFUSED))
def test_check_dockerfile_refuses(name):
    reason = dg.check_dockerfile(REFUSED[name])
    assert reason and reason.startswith("refused by dockergen")


def test_check_dockerfile_allows_plain_dockerfile():
    text = "FROM python:3.12-slim\nCOPY requirements.txt .\nRUN pip install -r requirements.txt\nADD app.tgz /app\n"
    assert dg.check_dockerfile(text) is None


@pytest.mark.parametrize("name", sorted(REFUSED))
def test_refused_dockerfile_is_never_built_and_reason_is_fed_back(tmp_path, name):
    p = make_project(tmp_path, {"main.go": ""})
    prompts = []
    build = Calls([])
    ok = dg.run(p, 2, generate=lambda pr: (prompts.append(pr), REFUSED[name])[1], build=build, **QUIET)
    assert not ok
    assert build.paths == []
    assert "refused by dockergen" in prompts[1]


def test_refused_then_clean_dockerfile_builds_only_the_clean_one(tmp_path):
    p = make_project(tmp_path, {"main.go": ""})
    replies = iter([REFUSED["mount"], "FROM golang:1.22\n"])
    build = Calls([(True, "")])
    assert dg.run(p, 3, generate=lambda pr: next(replies), build=build, **QUIET)
    assert len(build.paths) == 1
    assert (p / "Dockerfile").read_text() == "FROM golang:1.22\n"


# --- symlinked Dockerfile --------------------------------------------------------

def test_symlinked_dockerfile_exits_2_and_target_untouched(tmp_path, monkeypatch, capsys):
    target = tmp_path / "outside.txt"
    target.write_text("do not touch\n")
    project = tmp_path / "app"
    project.mkdir()
    (project / "Dockerfile").symlink_to(target)
    build = fake_main_env(monkeypatch, ["FROM x\n"], [(True, "")])
    assert dg.main([str(project)]) == 2
    assert target.read_text() == "do not touch\n"
    assert (project / "Dockerfile").is_symlink()
    assert build.paths == []
    assert "symlink" in capsys.readouterr().err


def test_run_refuses_symlinked_dockerfile_directly(tmp_path):
    target = tmp_path / "outside.txt"
    target.write_text("do not touch\n")
    project = tmp_path / "app"
    project.mkdir()
    (project / "Dockerfile").symlink_to(target)
    with pytest.raises(RuntimeError, match="symlink"):
        dg.run(project, 1, generate=lambda pr: "FROM x\n", build=lambda path: (True, ""), **QUIET)
    assert target.read_text() == "do not touch\n"


def test_write_dockerfile_does_not_follow_symlink(tmp_path):
    target = tmp_path / "outside.txt"
    target.write_text("do not touch\n")
    (tmp_path / "Dockerfile").symlink_to(target)
    with pytest.raises(RuntimeError):
        dg.write_dockerfile(tmp_path / "Dockerfile", "FROM x\n")
    assert target.read_text() == "do not touch\n"


# --- main() exit codes -----------------------------------------------------------

def test_main_rejects_bad_arguments(tmp_path):
    assert dg.main([str(tmp_path / "missing")]) == 2
    assert dg.main([str(tmp_path), "--attempts", "0"]) == 2


def test_main_exit_0_when_build_succeeds(tmp_path, monkeypatch, capsys):
    p = make_project(tmp_path, {"main.go": ""})
    build = fake_main_env(monkeypatch, ["```dockerfile\nFROM golang:1.22\n```"], [(True, "")])
    assert dg.main([str(p), "--network", "none"]) == 0
    assert len(build.paths) == 1
    assert (p / "Dockerfile").read_text() == "FROM golang:1.22\n"
    assert "FROM golang:1.22" in capsys.readouterr().out


def test_main_passes_network_to_docker_build(tmp_path, monkeypatch):
    p = make_project(tmp_path, {"main.go": ""})
    seen = []
    fake_main_env(monkeypatch, ["FROM golang:1.22\n"], [])
    monkeypatch.setattr(dg, "docker_build", lambda path, tag, timeout, network: (seen.append(network), (True, ""))[1])
    assert dg.main([str(p), "--network", "none"]) == 0
    assert seen == ["none"]


def test_docker_build_command_carries_network(monkeypatch, tmp_path):
    captured = {}

    class Result:
        returncode, stdout, stderr = 0, "ok", ""

    monkeypatch.setattr(dg.subprocess, "run", lambda cmd, **kw: (captured.setdefault("cmd", cmd), Result())[1])
    assert dg.docker_build(tmp_path, "t", 10, "none") == (True, "ok")
    assert "--network=none" in captured["cmd"]


def test_main_exit_1_when_every_attempt_fails(tmp_path, monkeypatch, capsys):
    p = make_project(tmp_path, {"main.go": ""})
    build = fake_main_env(monkeypatch, ["FROM a\n", "FROM b\n"], [(False, "err a"), (False, "err b")])
    assert dg.main([str(p), "--attempts", "2"]) == 1
    assert len(build.paths) == 2
    assert "No working Dockerfile after 2 attempts" in capsys.readouterr().out


def test_main_exit_2_when_docker_missing(tmp_path, monkeypatch):
    p = make_project(tmp_path, {"main.go": ""})
    fake_main_env(monkeypatch, [], [])
    monkeypatch.setattr(dg.shutil, "which", lambda name: None)
    assert dg.main([str(p)]) == 2


def test_main_exit_2_when_docker_daemon_down(tmp_path, monkeypatch, capsys):
    p = make_project(tmp_path, {"main.go": ""})
    build = fake_main_env(monkeypatch, ["FROM x\n"], [])
    monkeypatch.setattr(dg, "docker_daemon_error", lambda: "Cannot connect to the Docker daemon")
    assert dg.main([str(p)]) == 2
    assert build.paths == []
    assert "daemon" in capsys.readouterr().err


@pytest.mark.parametrize("stderr", [
    "ERROR: Cannot connect to the Docker daemon at unix:///var/run/docker.sock.",
    "ERROR: failed to connect to the docker API at unix:///var/run/docker.sock",
])
def test_docker_build_daemon_down_raises(monkeypatch, tmp_path, stderr):
    class Result:
        returncode, stdout = 1, ""

    Result.stderr = stderr

    monkeypatch.setattr(dg.subprocess, "run", lambda cmd, **kw: Result())
    with pytest.raises(RuntimeError, match="not reachable"):
        dg.docker_build(tmp_path, "t", 10)


def test_main_exit_2_when_model_reply_empty(tmp_path, monkeypatch):
    p = make_project(tmp_path, {"main.go": ""})
    build = fake_main_env(monkeypatch, [], [])

    def empty(*a):
        raise RuntimeError("Ollama returned an empty reply")

    monkeypatch.setattr(dg, "ollama_generate", empty)
    assert dg.main([str(p)]) == 2
    assert build.paths == []


def test_main_exit_2_when_reply_has_only_an_empty_fence(tmp_path, monkeypatch):
    p = make_project(tmp_path, {"main.go": ""})
    build = fake_main_env(monkeypatch, ["```dockerfile\n```"], [])
    assert dg.main([str(p)]) == 2
    assert build.paths == []


def test_main_exit_2_when_ollama_unreachable(tmp_path, monkeypatch, capsys):
    p = make_project(tmp_path, {"main.go": ""})
    build = Calls([])  # the real ollama_generate is used; only Docker is faked
    monkeypatch.setattr(dg.shutil, "which", lambda name: "/usr/bin/docker")
    monkeypatch.setattr(dg, "docker_daemon_error", lambda: None)
    monkeypatch.setattr(dg, "docker_build", build)
    with socket.socket() as s:  # a port nothing listens on
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    assert dg.main([str(p), "--url", f"http://127.0.0.1:{port}/api/generate", "--llm-timeout", "5"]) == 2
    assert build.paths == []
    assert "could not get a reply from Ollama" in capsys.readouterr().err


# --- ollama_generate error handling ----------------------------------------------

class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.mark.parametrize("error", [
    urllib.error.URLError("refused"),
    TimeoutError("timed out"),
    ConnectionResetError("reset by peer"),
    socket.timeout("read timed out"),
])
def test_ollama_generate_network_errors_become_runtime_error(monkeypatch, error):
    def boom(*a, **k):
        raise error
    monkeypatch.setattr(dg.urllib.request, "urlopen", boom)
    with pytest.raises(RuntimeError, match="could not get a reply"):
        dg.ollama_generate("p", "m", "http://localhost:1/api/generate", 1)


@pytest.mark.parametrize("body, message", [
    (b"<html>not json</html>", "could not get a reply"),
    (b'["a list"]', "unexpected Ollama reply"),
    (b'{"error": "model not found"}', "unexpected Ollama reply"),
    (b'{"response": 42}', "unexpected Ollama reply"),
    (b'{"response": "   \\n"}', "empty reply"),
])
def test_ollama_generate_bad_replies_become_runtime_error(monkeypatch, body, message):
    monkeypatch.setattr(dg.urllib.request, "urlopen", lambda *a, **k: FakeResponse(body))
    with pytest.raises(RuntimeError, match=message):
        dg.ollama_generate("p", "m", "http://localhost:1/api/generate", 1)


def test_ollama_generate_returns_response(monkeypatch):
    monkeypatch.setattr(dg.urllib.request, "urlopen",
                        lambda *a, **k: FakeResponse(json.dumps({"response": "FROM x"}).encode()))
    assert dg.ollama_generate("p", "m", "http://localhost:1/api/generate", 1) == "FROM x"


# --- dry run and --url warning ---------------------------------------------------

def test_dry_run_never_calls_build(tmp_path, monkeypatch, capsys):
    p = make_project(tmp_path, {"main.go": ""})
    build = fake_main_env(monkeypatch, ["FROM golang:1.22\n"], [])
    monkeypatch.setattr(dg, "docker_daemon_error", lambda: pytest.fail("dry run must not touch Docker"))
    assert dg.main([str(p), "--dry-run"]) == 0
    assert build.paths == []
    assert (p / "Dockerfile").read_text() == "FROM golang:1.22\n"
    out = capsys.readouterr().out
    assert "FROM golang:1.22" in out and "not built" in out


def test_dry_run_warns_on_refused_dockerfile_without_building(tmp_path, monkeypatch, capsys):
    p = make_project(tmp_path, {"main.go": ""})
    build = fake_main_env(monkeypatch, [REFUSED["network"]], [])
    assert dg.main([str(p), "--dry-run"]) == 1
    assert build.paths == []
    assert "refused by dockergen" in capsys.readouterr().err


@pytest.mark.parametrize("url, warned", [
    ("http://localhost:11434/api/generate", False),
    ("http://127.0.0.1:11434/api/generate", False),
    ("http://[::1]:11434/api/generate", False),
    ("http://gpu-box.example.com:11434/api/generate", True),
])
def test_remote_url_warns(tmp_path, monkeypatch, capsys, url, warned):
    p = make_project(tmp_path, {"main.go": ""})
    fake_main_env(monkeypatch, ["FROM x\n"], [])
    assert dg.main([str(p), "--dry-run", "--url", url]) == 0
    assert ("is not local" in capsys.readouterr().err) is warned


# --- samples ---------------------------------------------------------------------

def test_samples_have_valid_manifests():
    go_mod = (ROOT / "samples/go-app/go.mod").read_text().splitlines()
    assert go_mod[0] == "module go-app" and "go 1.21" in go_mod
    assert "express" in json.loads((ROOT / "samples/node-app/package.json").read_text())["dependencies"]


def test_dry_run_with_refused_dockerfile_returns_false_and_never_builds(tmp_path):
    p = make_project(tmp_path, {"main.go": ""})
    built = []
    ok = dg.run(p, 3, generate=lambda pr: "# syntax=evil/frontend\nFROM x\n",
                build=lambda path: (built.append(1), (True, ""))[1], log=lambda m: None, dry_run=True)
    assert ok is False and not built
