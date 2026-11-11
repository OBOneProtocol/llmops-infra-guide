# dockergen: Dockerfiles from a local LLM, judged by `docker build`

A small, self-correcting loop. A local model (via [Ollama](https://ollama.com)) writes a Dockerfile, `docker build` decides whether it works, and when it doesn't, the model gets its previous Dockerfile plus the tail of the build error and tries again. A successful build is the only thing that counts as success.

## How it works

1. **Inspect** the project: top-level files and the first dependency manifest found (`requirements.txt`, `pyproject.toml`, `package.json`, `go.mod`, `Cargo.toml`, `Gemfile`).
2. **Generate** a Dockerfile with fixed rules in the prompt: pinned base image, dependencies installed before the source is copied, non-root user where possible, explicit start command.
3. **Build** it with `docker build`.
4. **Correct**: on failure, the next prompt carries the previous Dockerfile and the last 40 lines of build output. Repeat up to `--attempts` times.

An existing `Dockerfile` in the project is backed up (`Dockerfile.bak-<timestamp>`) before anything is written.

## Usage

```bash
ollama pull qwen2.5-coder:7b
python3 dockergen.py samples/flask-app
python3 dockergen.py path/to/app --model qwen2.5-coder:1.5b --attempts 5
```

| Option | Default | Meaning |
|---|---|---|
| `--model` | `qwen2.5-coder:7b` | Ollama model name |
| `--url` | `http://localhost:11434/api/generate` | Ollama endpoint |
| `--attempts` | `3` | generate/build rounds before giving up |
| `--tag` | `dockergen-test` | image tag for test builds |
| `--llm-timeout`, `--build-timeout` | `300`, `900` s | per-call limits |

Exit codes: `0` a Dockerfile was written and built, `1` every attempt failed (the last Dockerfile is left for inspection), `2` usage or setup error (missing directory, Docker not on PATH, Ollama unreachable).

No third-party Python packages are needed; the tool uses only the standard library.

## Samples

`samples/` holds three minimal "hello" services to try it on: Flask (`flask==3.0.3`), Express (`express 4.18.2`) and Go's `net/http` (Go 1.21).

## Results so far

- Original runs with `qwen2.5-coder:7b` (September 2026) are recorded in [FAILURES.md](FAILURES.md), including the Node.js `.dockerignore` correction.
- November 2026, after the rewrite, with the much smaller `qwen2.5-coder:1.5b` on the Go sample: one run built on the first attempt; another failed all three attempts (the model reached for `pip` in a Go project). The loop behaved correctly both times. Small models are unreliable here; use 7B or larger.

## Tests

```bash
python3 -m pytest -q tests
```

The tests run offline: the model and `docker build` are replaced by fakes, so they check the loop itself (retry prompt contents, error truncation, Dockerfile backup, exit codes) and the sample manifests. CI runs them on every push.

## Limits

- One project directory at a time; monorepos need a path per service.
- The generated image is built, not run. A build that succeeds can still start a broken app.
- The prompt sees file names and one manifest, not the source code.
