#!/usr/bin/env python3
"""Allowlist tracked files; reject accidental credentials or deployment state."""
import json
from pathlib import Path
import re
import subprocess

root = Path(__file__).resolve().parents[1]
allowed = {
    ".gitignore", ".github/workflows/check.yml", "README.md", "SECURITY.md", "COMPATIBILITY.md",
    "deploy.example.json", "manage.py", "security/apparmor/komari-agent.profile.in",
    "security/seccomp/amd64.json", "tests/test_manage.py", "tests/container_smoke.py",
    "scripts/check_public_files.py",
}
tracked = subprocess.check_output(["git", "ls-files", "-z"], cwd=root).decode().split("\0")
for name in filter(None, tracked):
    if name not in allowed:
        raise SystemExit(f"Unreviewed public file: {name}")
    text = (root / name).read_text()
    if re.search(r"-----BEGIN (?:OPENSSH |RSA |EC )?PRIVATE KEY-----|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}", text):
        raise SystemExit(f"Potential credential in {name}")
example = json.loads((root / "deploy.example.json").read_text())
assert example["agent"]["token"] == "REPLACE_WITH_NODE_TOKEN"
assert example["agent"]["endpoint"] == "https://komari.example.com"
print("PASS: public file allowlist and known credential patterns; manual review still required")
