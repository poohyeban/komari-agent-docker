#!/usr/bin/env python3
"""Isolated Linux CI smoke test. Never registers an agent or contacts a panel."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import uuid

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("manage", ROOT / "manage.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def run(args):
    return subprocess.check_output(args, text=True, stderr=subprocess.STDOUT)


with tempfile.TemporaryDirectory(prefix="komari-smoke-") as tmp:
    root = Path(tmp)
    shutil.copytree(ROOT / "security", root / "security")
    cfg = json.loads((ROOT / "deploy.example.json").read_text())
    cfg["image"] = os.environ.get("KOMARI_TEST_IMAGE", cfg["image"])
    cfg["project_name"] = "smoke-" + uuid.uuid4().hex[:12]
    cfg["agent"].update(endpoint="https://panel.example.invalid", token="test-only-not-a-real-token", include_nics="lo")
    m.validate(cfg); m.render(cfg, root)
    model = json.loads(run(m.compose(cfg, "config", "--format", "json", root=root)))
    svc = model["services"]["agent"]
    assert svc["network_mode"] == "host" and svc["read_only"]
    assert "NET_RAW" in svc["cap_add"] and "ALL" in svc["cap_drop"]
    assert svc["logging"]["options"] == {"max-size": "10m", "max-file": "3"}
    profile = root / ".generated/apparmor.profile"
    run(["apparmor_parser", "-Q", "-T", str(profile)])
    run(["docker", "pull", cfg["image"]])
    loaded = False
    try:
        run(["apparmor_parser", "-r", "-T", str(profile)]); loaded = True
        check = r'''
set -eu
/app/komari-agent --help >/dev/null
grep -q '^Seccomp:[[:space:]]*2' /proc/self/status
grep -q '^NoNewPrivs:[[:space:]]*1' /proc/self/status
grep -q '^CapEff:[[:space:]]*0000000000002000' /proc/self/status
grep -q '(enforce)' /proc/self/attr/current
test -r /host/proc/stat
if cat /host/proc/1/environ >/dev/null 2>&1; then exit 11; fi
if sh -c ': > /etc/komari-smoke-write' 2>/dev/null; then exit 12; fi
if sh -c ': < /dev/ptmx' 2>/dev/null; then exit 13; fi
ping -c 1 -W 2 127.0.0.1 >/dev/null
echo 'PASS: isolated image/help, local ICMP and kernel-enforced restrictions'
'''
        print(run(["docker", "run", "--rm", "--network", "none", "--read-only", "--user", "0:0",
                   "--cap-drop", "ALL", "--cap-add", "NET_RAW", "--security-opt", "no-new-privileges:true",
                   "--security-opt", "apparmor=" + m.profile_name(cfg),
                   "--security-opt", "seccomp=" + str(root / ".generated/seccomp.json"),
                   "--mount", "type=bind,src=/proc,dst=/host/proc,readonly",
                   "--tmpfs", "/tmp:size=16m,noexec,nosuid,nodev", "--entrypoint", "/bin/sh",
                   cfg["image"], "-ec", check]))
    finally:
        if loaded:
            run(["apparmor_parser", "-R", "-T", str(profile)])
