import os
import requests
import subprocess
import json

OLLAMA_URL = "http://localhost:11434/api/generate"
MODEL = "qwen2.5-coder:7b"

def inspect_project(path):
    files = os.listdir(path)
    context = {"files": files, "dependencies": ""}
    if "requirements.txt" in files:
        with open(os.path.join(path, "requirements.txt"), "r") as f:
            context["dependencies"] = f.read()
    elif "package.json" in files:
        with open(os.path.join(path, "package.json"), "r") as f:
            context["dependencies"] = f.read()
    elif "go.mod" in files:
        with open(os.path.join(path, "go.mod"), "r") as f:
            context["dependencies"] = f.read()
    return context

def generate_dockerfile(context, error=None):
    prompt = f"Generate a professional Dockerfile for a project with these files: {context['files']}. Dependencies: {context['dependencies']}. "
    if error:
        prompt += f"\n\nThe previous build failed with this error: {error}\nPlease fix the Dockerfile."
    
    payload = {"model": MODEL, "prompt": prompt, "stream": False}
    response = requests.post(OLLAMA_URL, json=payload)
    return response.json()['response'].strip().replace('```dockerfile', '').replace('```', '').strip()

def validate_build(path, dockerfile_content):
    with open(os.path.join(path, "Dockerfile"), "w") as f:
        f.write(dockerfile_content)
    
    result = subprocess.run(["docker", "build", "-t", "llmops-test", path], capture_output=True, text=True)
    return result.returncode == 0, result.stderr

def main(project_path):
    context = inspect_project(project_path)
    dockerfile = generate_dockerfile(context)
    
    for i in range(3):
        success, error = validate_build(project_path, dockerfile)
        if success:
            print(f"Build successful on attempt {i+1}!")
            return True
        print(f"Build attempt {i+1} failed. Retrying...")
        dockerfile = generate_dockerfile(context, error)
    
    return False

if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        main(sys.argv[1])
