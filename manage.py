#!/usr/bin/env python3
"""Render and operate a single hardened Komari Agent deployment. Stdlib only."""
import argparse
import fnmatch
import json
import math
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent
REPO = "ghcr.io/komari-monitor/komari-agent"
IMAGE_RE = re.compile(re.escape(REPO) + r":(\d+\.\d+\.\d+(?:[-.][A-Za-z0-9.-]+)?)@sha256:[0-9a-f]{64}\Z")
AGENT_KEYS = {
    "endpoint", "token", "include_nics", "include_mountpoints", "month_rotate",
    "interval", "info_report_interval", "max_retries", "reconnect_interval",
    "disable_web_ssh", "disable_auto_update",
}
RESOURCE_LIMITS = {
    "memory_mb": (64, 65536), "cpus": (0.1, 256), "pids_limit": (16, 65536),
    "tmpfs_mb": (1, 1024), "nofile": (128, 1048576),
    "stop_grace_seconds": (5, 300), "log_max_size_mb": (1, 1024), "log_max_files": (1, 20),
}


class Error(Exception):
    pass


def require(condition, message):
    if not condition:
        raise Error(message)


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, "Duplicate JSON key; remove duplicate settings.")
        result[key] = value
    return result


def read_json(path):
    try:
        return json.loads(path.read_text(), object_pairs_hook=unique_object)
    except (ValueError, OSError) as exc:
        # JSON error text may contain a credential; do not echo its input.
        raise Error(f"Cannot read valid JSON from {path.name} ({type(exc).__name__}).") from None


def exact_keys(value, keys, label):
    require(isinstance(value, dict) and set(value) == set(keys),
            f"{label}: fields must exactly match deploy.example.json.")


def number(value, minimum, maximum, label, integer=True):
    require(type(value) in ((int,) if integer else (int, float)) and
            math.isfinite(value) and minimum <= value <= maximum,
            f"{label}: expected {'integer' if integer else 'number'} in {minimum}..{maximum}.")


def validate(cfg):
    exact_keys(cfg, {"schema_version", "project_name", "image", "agent", "resources"}, "deployment")
    require(type(cfg["schema_version"]) is int and cfg["schema_version"] == 1, "Unsupported schema_version.")
    require(isinstance(cfg["project_name"], str) and
            re.fullmatch(r"[a-z][a-z0-9-]{0,39}", cfg["project_name"]), "Invalid project_name.")
    require(isinstance(cfg["image"], str) and IMAGE_RE.fullmatch(cfg["image"]),
            "image must be an official numeric version tag plus sha256 repository digest.")
    a = cfg["agent"]
    exact_keys(a, AGENT_KEYS, "agent")
    for field in ("endpoint", "token", "include_nics", "include_mountpoints"):
        require(isinstance(a[field], str) and a[field] and not any(ord(c) < 32 for c in a[field]),
                f"agent.{field}: expected a nonempty string without control characters.")
    try:
        endpoint = urlsplit(a["endpoint"])
        _ = endpoint.port
    except ValueError:
        raise Error("Invalid endpoint URL.") from None
    require(endpoint.scheme == "https" and endpoint.hostname and not endpoint.username and
            not endpoint.password and not endpoint.query and not endpoint.fragment,
            "endpoint must use HTTPS, without credentials, query or fragment.")
    require(endpoint.hostname != "komari.example.com", "Replace the example endpoint.")
    require(not a["token"].startswith("REPLACE_"), "Replace the example node token.")
    require(not a["include_nics"].startswith("REPLACE_"), "Select the actual host NIC(s).")
    require(all(re.fullmatch(r"[A-Za-z0-9_.:*?\[\]-]+", n) for n in a["include_nics"].split(",")),
            "include_nics must contain comma-separated NIC names/patterns without spaces.")
    require(a["include_mountpoints"] == "/", "This profile supports root filesystem statistics only.")
    require(a["disable_web_ssh"] is True and a["disable_auto_update"] is True,
            "This metrics-only profile requires remote control and self-update to stay disabled.")
    for key, lo, hi in [("month_rotate", 0, 31), ("info_report_interval", 1, 1440),
                        ("max_retries", 0, 100), ("reconnect_interval", 1, 3600)]:
        number(a[key], lo, hi, f"agent.{key}")
    number(a["interval"], 1, 3600, "agent.interval", integer=False)
    exact_keys(cfg["resources"], RESOURCE_LIMITS, "resources")
    for key, (lo, hi) in RESOURCE_LIMITS.items():
        number(cfg["resources"][key], lo, hi, key, integer=key != "cpus")
    return cfg


def load(root=ROOT):
    path = root / "deploy.json"
    require(path.is_file() and not path.is_symlink(), "Create deploy.json with: python3 manage.py init")
    require(stat.S_IMODE(path.stat().st_mode) & 0o077 == 0, "deploy.json must be private: chmod 600 deploy.json")
    return validate(read_json(path))


def private_dir(path):
    require(not path.is_symlink(), f"Refusing symlink directory: {path.name}")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path, 0o700)


def atomic_write(path, content, mode=0o600):
    require(not path.is_symlink(), f"Refusing symlink file: {path.name}")
    fd, temporary = tempfile.mkstemp(prefix=".tmp-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(content)
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def profile_name(cfg):
    return "komari-agent-docker-" + cfg["project_name"]


def compose_model(cfg, root):
    r = cfg["resources"]
    # Docker Compose interpolates '$' even in JSON/YAML strings.
    def path(value):
        return str(root / value).replace("$", "$$")
    def mount(source, target, readonly):
        return {"type": "bind", "source": source, "target": target, "read_only": readonly,
                "bind": {"create_host_path": False}}
    return {"name": cfg["project_name"], "services": {"agent": {
        "image": cfg["image"], "platform": "linux/amd64", "restart": "unless-stopped",
        "init": True, "hostname": "localhost", "network_mode": "host", "ipc": "none",
        "user": "0:0", "read_only": True, "working_dir": "/var/lib/komari-agent",
        "volumes": [mount(path("data"), "/var/lib/komari-agent", False),
                    mount("/proc", "/host/proc", True), mount("/etc/os-release", "/etc/os-release", True),
                    mount(path(".generated/agent.json"), "/run/komari-agent/config.json", True)],
        "environment": {"HOST_PROC": "/host/proc"},
        "tmpfs": [f"/tmp:size={r['tmpfs_mb']}m,noexec,nosuid,nodev"],
        "cap_drop": ["ALL"], "cap_add": ["NET_RAW"],
        "security_opt": ["no-new-privileges:true", "apparmor=" + profile_name(cfg),
                         "seccomp=" + path(".generated/seccomp.json")],
        "pids_limit": r["pids_limit"], "mem_limit": f"{r['memory_mb']}m",
        "memswap_limit": f"{r['memory_mb']}m", "cpus": r["cpus"],
        "ulimits": {"core": {"soft": 0, "hard": 0}, "nofile": {"soft": r["nofile"], "hard": r["nofile"]}},
        "stop_grace_period": f"{r['stop_grace_seconds']}s",
        "command": ["--config", "/run/komari-agent/config.json"],
        "labels": {"com.centurylinklabs.watchtower.enable": "false",
                   "io.github.komari-agent-docker.root": str(root).replace("$", "$$")},
        "logging": {"driver": "json-file", "options": {
            "max-size": f"{r['log_max_size_mb']}m", "max-file": str(r["log_max_files"])}}
    }}}


def render(cfg, root=ROOT):
    generated = root / ".generated"
    private_dir(generated)
    private_dir(root / "data")
    policy = (root / "security/apparmor/komari-agent.profile.in").read_text()
    policy = policy.replace("@PROFILE_NAME@", profile_name(cfg))
    atomic_write(generated / "apparmor.profile", policy)
    atomic_write(generated / "seccomp.json", (root / "security/seccomp/amd64.json").read_text())
    atomic_write(generated / "agent.json", json.dumps(cfg["agent"], indent=2) + "\n", 0o400)
    atomic_write(generated / "compose.json", json.dumps(compose_model(cfg, root), indent=2) + "\n")


def run(args, capture=True):
    try:
        result = subprocess.run(args, text=True, stdout=subprocess.PIPE if capture else None,
                                stderr=subprocess.PIPE if capture else None, check=False)
    except FileNotFoundError:
        raise Error(f"Required command missing: {args[0]}") from None
    require(result.returncode == 0, f"Command failed: {args[0]} {args[1] if len(args) > 1 else ''} (exit {result.returncode}).")
    return result.stdout if capture else ""


def compose(cfg, *args, root=ROOT):
    # Explicit file/project and empty env file avoid accidental local overrides.
    return ["docker", "compose", "--env-file", "/dev/null", "-p", cfg["project_name"],
            "-f", str(root / ".generated/compose.json"), *args]


def container_ids(cfg):
    return run(["docker", "ps", "-aq", "--filter", "label=com.docker.compose.project=" + cfg["project_name"]]).split()


def owned_containers(cfg, root=ROOT):
    local = run(["docker", "ps", "-aq", "--filter", "label=io.github.komari-agent-docker.root=" + str(root)]).split()
    for cid in local:
        labels = json.loads(run(["docker", "inspect", cid]))[0]["Config"].get("Labels") or {}
        require(labels.get("com.docker.compose.project") == cfg["project_name"],
                "This directory already manages another project name. Keep project_name stable.")
    ids = container_ids(cfg)
    for cid in ids:
        item = json.loads(run(["docker", "inspect", cid]))[0]
        labels = item["Config"].get("Labels") or {}
        require(labels.get("io.github.komari-agent-docker.root") == str(root),
                "Compose project already belongs to another deployment. Choose an unused project_name; do not overwrite it.")
    return ids


def check_host(cfg, root=ROOT):
    require(sys.platform == "linux" and os.uname().machine in ("x86_64", "amd64"),
            "Validated profile requires native Linux amd64; ARM64 is not supported by this seccomp policy.")
    require(os.geteuid() == 0, "Host preparation and verification require root (sudo python3 manage.py ...).")
    endpoint = os.environ.get("DOCKER_HOST")
    if not endpoint:
        endpoint = json.loads(run(["docker", "context", "inspect", "--format", "{{json .Endpoints.docker.Host}} "]))
    require(endpoint.startswith("unix://"), "Use a local rootful Docker Engine, not a remote context.")
    info = json.loads(run(["docker", "info", "--format", "{{json .}} "]))
    require(info.get("OSType") == "linux" and info.get("Architecture") in ("x86_64", "amd64"), "Docker architecture mismatch.")
    options = " ".join(info.get("SecurityOptions", []))
    require("rootless" not in options and "userns" not in options, "Rootless Docker and userns-remap are not supported by this profile.")
    require("apparmor" in options and "seccomp" in options, "Docker must support AppArmor and seccomp.")
    require(Path("/sys/module/apparmor/parameters/enabled").read_text().strip() == "Y", "AppArmor must be enabled.")
    require(shutil.which("apparmor_parser"), "Install the host AppArmor tools first.")
    run(["systemctl", "is-enabled", "apparmor.service"])
    run(["systemctl", "is-active", "apparmor.service"])
    run(["docker", "compose", "version"])
    docker_root = Path(info["DockerRootDir"])
    require(docker_root.stat().st_dev == Path("/").stat().st_dev,
            "Docker storage is on another filesystem: '/' would not reliably represent the host root disk. Review disk mounts before deploying.")
    interfaces = [p.name for p in Path("/sys/class/net").iterdir()]
    for pattern in cfg["agent"]["include_nics"].split(","):
        require(any(fnmatch.fnmatchcase(n, pattern) for n in interfaces), "Configured NIC pattern matches no host interface.")
    owned_containers(cfg, root)
    print("Host checks passed: local rootful amd64 Docker, AppArmor, seccomp, root filesystem and NIC selection.")


def install_policy(cfg, root=ROOT):
    target = Path("/etc/apparmor.d") / profile_name(cfg)
    header = f"# Managed by komari-agent-docker; root={root}\n"
    require(not target.is_symlink(), "Existing AppArmor policy is a symlink; review ownership before replacing it.")
    if target.exists():
        require(target.read_text().startswith(header), "AppArmor profile name is already in use by another installation.")
    source = (root / ".generated/apparmor.profile").read_text()
    # Parse before writing or loading a policy; -Q skips loading into the kernel.
    run(["apparmor_parser", "-Q", "-T", str(root / ".generated/apparmor.profile")])
    atomic_write(target, header + source, 0o644)
    run(["apparmor_parser", "-r", "-T", str(target)])


def up(cfg, pull="always"):
    check_host(cfg)
    # Pull/inspect before modifying generated files or the running container.
    if pull == "always":
        run(["docker", "pull", cfg["image"]], capture=False)
    image = json.loads(run(["docker", "image", "inspect", cfg["image"]]))[0]
    require(image["Architecture"] == "amd64" and image["Os"] == "linux", "Unexpected image architecture.")
    expected_version = IMAGE_RE.fullmatch(cfg["image"]).group(1)
    actual_version = (image["Config"].get("Labels") or {}).get("org.opencontainers.image.version", "")
    require(actual_version.lstrip("v") == expected_version, "Image version label does not match its configured tag.")
    render(cfg)
    run(compose(cfg, "config", "--quiet"))
    install_policy(cfg)
    run(compose(cfg, "up", "-d", "--no-deps", "--pull", "never", "--force-recreate", "agent"), capture=False)
    print("Container started. Run verify, then check fresh metrics and probe results in your Komari panel.")


def inspect_agent(cfg):
    ids = owned_containers(cfg)
    require(len(ids) == 1, "Expected exactly one managed container; deploy first or inspect the project.")
    return json.loads(run(["docker", "inspect", ids[0]]))[0]


def verify(cfg, seconds):
    initial = inspect_agent(cfg)
    start = time.monotonic()
    rounds = 0
    while True:
        item = inspect_agent(cfg)
        state, host = item["State"], item["HostConfig"]
        require(item["Id"] == initial["Id"] and item["RestartCount"] == initial["RestartCount"], "Container changed or restarted during verification.")
        require(state["Running"] and not state["OOMKilled"], "Agent is stopped or OOM-killed.")
        require(item["Config"]["Image"] == cfg["image"], "Running image differs from deploy.json.")
        require(host["ReadonlyRootfs"] and not host["Privileged"] and host["NetworkMode"] == "host", "Unexpected isolation settings.")
        require(not host.get("PidMode"), "Host PID namespace must not be shared.")
        policies = host.get("SecurityOpt") or []
        seccomp = next((s[len("seccomp="):] for s in policies if s.startswith("seccomp=")), "")
        try:
            require(json.loads(seccomp) == read_json(ROOT / "security/seccomp/amd64.json"), "Running seccomp policy differs from this deployment.")
        except ValueError:
            raise Error("Cannot verify the active seccomp policy.") from None
        pid = state["Pid"]
        child_file = Path(f"/proc/{pid}/task/{pid}/children")
        children = child_file.read_text().split()
        agents = [n for n in children if Path(f"/proc/{n}/comm").read_text().strip() == "komari-agent"]
        require(len(agents) == 1, "Expected a running Agent child under docker-init.")
        for proc in [str(pid), *agents]:
            require(Path(f"/proc/{proc}/attr/current").read_text().strip() == profile_name(cfg) + " (enforce)", "AppArmor is not enforcing the expected profile.")
            fields = dict(line.split(":", 1) for line in Path(f"/proc/{proc}/status").read_text().splitlines() if ":" in line)
            require(fields["NoNewPrivs"].strip() == "1" and fields["Seccomp"].strip() == "2", "Missing NNP/seccomp enforcement.")
            require(all(int(fields[key].strip(), 16) == 0x2000 for key in ("CapEff", "CapPrm", "CapBnd")), "Capabilities must be NET_RAW only.")
        mounted = Path(f"/proc/{pid}/root/run/komari-agent/config.json")
        require(read_json(mounted) == cfg["agent"], "Running config differs from deploy.json; recreate after changing config.")
        rounds += 1
        elapsed = time.monotonic() - start
        if elapsed >= seconds:
            break
        time.sleep(min(5, seconds - elapsed))
    print(f"Local runtime checks passed: {rounds} rounds, {elapsed:.1f}s, no restart/OOM, expected config and security active.")
    print("No Docker healthcheck is defined. Panel ingestion and actual ICMP/TCP/HTTP tasks must be checked separately.")


def redact(text, cfg):
    for key in ("token", "endpoint"):
        text = text.replace(cfg["agent"][key], "<redacted>")
    text = re.sub(r"(?:https?|wss?)://[^\s<>\"']+", "<redacted-url>", text)
    return re.sub(r"(?i)(token[=:]\s*)[^\s&]+", r"\1<redacted>", text)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["init", "validate", "render", "check", "up", "verify", "status", "logs", "stop", "down"])
    parser.add_argument("--seconds", type=int, default=60, help="verify duration, 0..300 seconds")
    parser.add_argument("--pull", choices=["always", "never"], default="always", help="up: allow cached-image recovery with never")
    args = parser.parse_args()
    if args.command == "init":
        path = ROOT / "deploy.json"
        require(not path.exists() and not path.is_symlink(), "deploy.json already exists; refusing to overwrite it.")
        with path.open("x", opener=lambda p, flags: os.open(p, flags, 0o600)) as stream:
            stream.write((ROOT / "deploy.example.json").read_text())
        print("Created private deploy.json. Set your endpoint, node token and NIC before deployment.")
        return
    cfg = load()
    if args.command == "validate":
        print("Configuration valid. Credentials were not printed.")
    elif args.command == "render":
        render(cfg)
        print("Rendered private .generated files. No Docker or host configuration changed.")
    elif args.command == "check":
        check_host(cfg)
    elif args.command == "up":
        up(cfg, args.pull)
    elif args.command == "verify":
        number(args.seconds, 0, 300, "seconds")
        verify(cfg, args.seconds)
    elif args.command == "status":
        item = inspect_agent(cfg)
        print(json.dumps({"image": item["Config"]["Image"], "status": item["State"]["Status"],
                          "restarts": item["RestartCount"], "oom": item["State"]["OOMKilled"]}, indent=2))
    elif args.command == "logs":
        item = inspect_agent(cfg)
        result = subprocess.run(["docker", "logs", "--tail", "80", "--timestamps", item["Id"]], text=True, capture_output=True)
        require(result.returncode == 0, "Cannot read container logs.")
        print(redact(result.stdout + result.stderr, cfg))
    else:
        owned_containers(cfg)
        require((ROOT / ".generated/compose.json").is_file(), "No rendered deployment found.")
        run(compose(cfg, args.command), capture=False)
        print("Data and config retained. The installed AppArmor profile is also retained.")


if __name__ == "__main__":
    try:
        main()
    except (Error, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
