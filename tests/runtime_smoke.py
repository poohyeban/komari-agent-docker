#!/usr/bin/env python3
"""Run the real Agent against a mock HTTPS/WSS panel without external networking."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("manage", ROOT / "manage.py")
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)


def run(args):
    r = subprocess.run(args, capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError(f"Command failed: {args[:3]}\n{r.stdout}\n{r.stderr}")
    return r.stdout


def events(root):
    path = root / "fixture/events.jsonl"
    if not path.exists(): return []
    data = []
    for line in path.read_text().splitlines():
        try: data.append(json.loads(line))
        except ValueError: pass  # The server may be writing its final line.
    return data


with tempfile.TemporaryDirectory(prefix="komari-runtime-") as tmp:
    root = Path(tmp)
    fixture = root / "fixture"; fixture.mkdir()
    shutil.copytree(ROOT / "security", root / "security")
    shutil.copy(ROOT / "tests/mock_panel.py", fixture / "server.py")
    cfg = json.loads((ROOT / "deploy.example.json").read_text())
    cfg["image"] = os.environ.get("KOMARI_TEST_IMAGE", cfg["image"])
    cfg["project_name"] = "runtime-" + uuid.uuid4().hex[:12]
    cfg["agent"].update(endpoint="https://127.0.0.1:8443", token="test-only-not-a-real-token",
                        include_nics="lo", month_rotate=17, interval=1, info_report_interval=1)
    m.validate(cfg); m.render(cfg, root)
    run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
         "-subj", "/CN=127.0.0.1", "-addext", "subjectAltName=IP:127.0.0.1",
         "-keyout", str(fixture / "key.pem"), "-out", str(fixture / "cert.pem")])
    panel = cfg["project_name"] + "-panel"
    profile = root / ".generated/apparmor.profile"
    loaded = False
    agent_id = None
    try:
        run(["docker", "pull", cfg["image"]])
        run(["docker", "pull", "python:3.12-alpine"])
        run(["apparmor_parser", "-r", "-T", str(profile)]); loaded = True
        run(["docker", "run", "-d", "--name", panel, "--hostname", "localhost", "--network", "none", "--read-only",
             "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
             "--mount", f"type=bind,src={fixture},dst=/fixture", "python:3.12-alpine", "python", "/fixture/server.py"])
        for _ in range(30):
            if (fixture / "ready").exists(): break
            time.sleep(1)
        assert (fixture / "ready").exists(), "Mock panel did not start"
        model = m.compose_model(cfg, root)
        service = model["services"]["agent"]
        # Only networking and test CA differ from the production model. No public route exists.
        service["network_mode"] = "container:" + panel
        service.pop("hostname")  # Shared network mode inherits the fixture hostname.
        service["restart"] = "no"
        service["volumes"].append({"type": "bind", "source": str(fixture / "cert.pem"),
                                   "target": "/etc/ssl/certs/ca-certificates.crt", "read_only": True})
        (root / ".generated/compose.json").write_text(json.dumps(model))
        run(m.compose(cfg, "up", "-d", "--no-deps", "--pull", "never", "agent", root=root))
        agent_id = run(m.compose(cfg, "ps", "-q", "agent", root=root)).strip()
        assert agent_id
        version = m.IMAGE_RE.fullmatch(cfg["image"]).group(1)
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            messages = events(root)
            reports = [x for x in messages if x.get("method") == "agent.report"]
            pings = {x["params"]["ping_type"]: x["params"]["value"] for x in messages if x.get("method") == "agent.pingResult"}
            tasks = [x["params"] for x in messages if x.get("method") == "agent.taskResult"]
            files = [x["params"] for x in messages if x.get("method") == "agent.file.result"]
            if len(reports) >= 10 and len(pings) == 3 and tasks and (version == "1.2.60" or files): break
            item = json.loads(run(["docker", "inspect", agent_id]))[0]
            assert item["State"]["Running"], "Agent exited during startup/reporting"
            time.sleep(1)
        assert len(reports) >= 10, "No sustained v2 reports"
        assert set(pings) == {"icmp", "tcp", "http"} and all(v >= 0 for v in pings.values()), pings
        assert any(x.get("exit_code") == -1 and "disabled" in x.get("result", "").lower() for x in tasks), tasks
        if version != "1.2.60":
            assert any(x.get("ok") is False and "disabled" in x.get("error", "").lower() for x in files), files
        item = json.loads(run(["docker", "inspect", agent_id]))[0]
        assert not item["State"]["OOMKilled"] and item["RestartCount"] == 0
        pid = item["State"]["Pid"]
        children = Path(f"/proc/{pid}/task/{pid}/children").read_text().split()
        agent_pids = [p for p in children if Path(f"/proc/{p}/comm").read_text().strip() == "komari-agent"]
        assert len(agent_pids) == 1
        for p in [str(pid), *agent_pids]:
            assert Path(f"/proc/{p}/attr/current").read_text().strip() == m.profile_name(cfg) + " (enforce)"
            status = dict(line.split(":", 1) for line in Path(f"/proc/{p}/status").read_text().splitlines() if ":" in line)
            assert status["Seccomp"].strip() == "2" and status["NoNewPrivs"].strip() == "1"
            assert int(status["CapEff"].strip(), 16) == 0x2000
        run(m.compose(cfg, "stop", "agent", root=root))
        history = root / "data/net_static.json"
        assert history.exists() and "lo" in json.loads(history.read_text())["interfaces"]
        before = len(reports)
        run(m.compose(cfg, "start", "agent", root=root))
        for _ in range(30):
            resumed = sum(x.get("method") == "agent.report" for x in events(root))
            if resumed > before: break
            time.sleep(1)
        assert resumed > before, "Reporting did not resume after restart"
        print(f"PASS {version}: {before} v2 reports, ICMP/TCP/HTTP, disabled remote exec, persistence/restart, active security")
        if version != "1.2.60": print("PASS: remote file operations rejected")
    except Exception:
        if agent_id:
            r = subprocess.run(["docker", "logs", "--tail", "60", agent_id], capture_output=True, text=True)
            print(m.redact(r.stdout + r.stderr, cfg))
        raise
    finally:
        if agent_id: subprocess.run(["docker", "rm", "-f", agent_id], capture_output=True)
        subprocess.run(m.compose(cfg, "down", root=root), capture_output=True)
        subprocess.run(["docker", "rm", "-f", panel], capture_output=True)
        if loaded: run(["apparmor_parser", "-R", "-T", str(profile)])
