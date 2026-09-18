# Automatic Dockerfile Generation with Local LLM

## Objective
Create a tool that automatically generates validated Dockerfiles for various project stacks using a local LLM.

## Architecture
- **Model**: `qwen2.5-coder:7b` via Ollama.
- **Logic**: The script inspects the root directory for dependency markers, prompts the LLM, and enters a feedback loop where `docker build` stderr is fed back into the model for iterative correction.

## Validation Results
- **Python Flask**: Success (1st attempt).
- **Node.js**: Success (2nd attempt - corrected `.dockerignore` issues).
- **Go**: Success (1st attempt).

## Setup
1. Install Ollama.
2. `ollama pull qwen2.5-coder:7b`
3. `python3 dockergen.py ./samples/your-app`
