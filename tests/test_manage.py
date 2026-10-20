import importlib.util
import json
from pathlib import Path
import shutil
import stat
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("manage", ROOT / "manage.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def config():
    c = json.loads((ROOT / "deploy.example.json").read_text())
    c["agent"].update(endpoint="https://panel.example.invalid", token="unit-test-token-$literal",
                      include_nics="eth0")
    return c


class ConfigTests(unittest.TestCase):
    def test_valid_and_version_independent(self):
        c = config()
        self.assertIs(m.validate(c), c)
        c["image"] = m.REPO + ":1.5.11@sha256:" + "a" * 64
        m.validate(c)
        self.assertNotIn("protocol_version", c["agent"])

    def test_rejects_mutable_or_foreign_image(self):
        for value in [m.REPO + ":latest", m.REPO + ":1.2.60", "evil.invalid/agent:1.0.0@sha256:" + "a" * 64]:
            with self.subTest(value=value), self.assertRaises(m.Error):
                c = config(); c["image"] = value; m.validate(c)

    def test_no_silent_security_downgrade(self):
        for field in ("disable_web_ssh", "disable_auto_update"):
            c = config(); c["agent"][field] = False
            with self.assertRaises(m.Error): m.validate(c)

    def test_unknown_and_duplicate_fields_rejected(self):
        c = config(); c["agent"]["protocol_version"] = 2
        with self.assertRaises(m.Error): m.validate(c)
        with self.assertRaises(m.Error):
            json.loads('{"token":"a","token":"b"}', object_pairs_hook=m.unique_object)

    def test_errors_do_not_echo_credentials(self):
        c = config(); secret = "secret-do-not-print"
        c["agent"]["endpoint"] = f"https://user:{secret}@panel.example.invalid"
        with self.assertRaises(m.Error) as caught: m.validate(c)
        self.assertNotIn(secret, str(caught.exception))

    def test_numeric_and_path_validation(self):
        for group, key, value in [("resources", "cpus", float("nan")), ("resources", "pids_limit", True),
                                  ("agent", "month_rotate", 32), ("agent", "include_mountpoints", "/host")]:
            with self.subTest(key=key), self.assertRaises(m.Error):
                c = config(); c[group][key] = value; m.validate(c)
        c = config(); c["project_name"] = "../other"
        with self.assertRaises(m.Error): m.validate(c)

    def test_example_cannot_accidentally_deploy(self):
        with self.assertRaises(m.Error): m.validate(json.loads((ROOT / "deploy.example.json").read_text()))


class RenderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "path with spaces $literal"
        self.root.mkdir()
        shutil.copytree(ROOT / "security", self.root / "security")

    def tearDown(self): self.temp.cleanup()

    def test_private_render_and_no_credentials_in_compose(self):
        c = config(); m.render(c, self.root)
        generated = self.root / ".generated"
        compose = (generated / "compose.json").read_text()
        self.assertNotIn(c["agent"]["token"], compose)
        self.assertNotIn(c["agent"]["endpoint"], compose)
        self.assertEqual(json.loads((generated / "agent.json").read_text()), c["agent"])
        for path, mode in [(generated, 0o700), (self.root / "data", 0o700),
                           (generated / "agent.json", 0o400), (generated / "compose.json", 0o600)]:
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), mode)
        self.assertIn("$$literal", compose)
        self.assertNotIn("@PROFILE_NAME@", (generated / "apparmor.profile").read_text())

    def test_render_keeps_existing_state_and_refuses_symlink(self):
        c = config(); m.render(c, self.root)
        data = self.root / "data/net_static.json"; data.write_text('{"sentinel":true}')
        m.render(c, self.root)
        self.assertEqual(data.read_text(), '{"sentinel":true}')
        target = self.root / ".generated/agent.json"
        target.unlink(); target.symlink_to(data)
        with self.assertRaises(m.Error): m.render(c, self.root)
        self.assertEqual(data.read_text(), '{"sentinel":true}')

    def test_security_contract(self):
        svc = m.compose_model(config(), self.root)["services"]["agent"]
        self.assertEqual(svc["cap_drop"], ["ALL"])
        self.assertEqual(svc["cap_add"], ["NET_RAW"])
        self.assertTrue(svc["read_only"])
        self.assertEqual(svc["network_mode"], "host")
        self.assertEqual(svc["mem_limit"], svc["memswap_limit"])
        self.assertEqual(svc["logging"]["options"], {"max-size": "10m", "max-file": "3"})
        self.assertNotIn("privileged", svc)
        self.assertNotIn("pid", svc)
        self.assertNotIn("ports", svc)
        writable = [v["target"] for v in svc["volumes"] if not v["read_only"]]
        self.assertEqual(writable, ["/var/lib/komari-agent"])
        self.assertFalse(any("docker.sock" in v["source"] for v in svc["volumes"]))

    def test_readable_config_refused(self):
        path = self.root / "deploy.json"
        path.write_text(json.dumps(config())); path.chmod(0o644)
        with self.assertRaises(m.Error): m.load(self.root)
        path.chmod(0o600); m.load(self.root)

    def test_log_redaction(self):
        c = config()
        text = c["agent"]["token"] + " " + c["agent"]["endpoint"] + " wss://panel.invalid/rpc?token=other token=another"
        result = m.redact(text, c)
        for value in [c["agent"]["token"], c["agent"]["endpoint"], "token=other", "token=another"]:
            self.assertNotIn(value, result)


class OwnershipTests(unittest.TestCase):
    def test_refuses_existing_unrelated_project(self):
        with patch.object(m, "run", side_effect=["", "id", json.dumps([{"Config": {"Labels": {}}}])]):
            with self.assertRaisesRegex(m.Error, "another deployment"):
                m.owned_containers(config())

    def test_refuses_project_rename_with_existing_container(self):
        item = [{"Config": {"Labels": {"com.docker.compose.project": "previous-name"}}}]
        with patch.object(m, "run", side_effect=["id", json.dumps(item)]):
            with self.assertRaisesRegex(m.Error, "Keep project_name stable"):
                m.owned_containers(config())


class UpgradeTests(unittest.TestCase):
    def test_pull_failure_does_not_touch_policy_or_generated_config(self):
        with patch.object(m, "check_host"), patch.object(m, "run", side_effect=m.Error("pull failed")), \
                patch.object(m, "render") as render, patch.object(m, "install_policy") as policy:
            with self.assertRaises(m.Error): m.up(config())
            render.assert_not_called(); policy.assert_not_called()

    def test_wrong_image_metadata_is_rejected_before_mutation(self):
        image = [{"Architecture": "arm64", "Os": "linux", "Config": {"Labels": {}}}]
        with patch.object(m, "check_host"), patch.object(m, "run", return_value=json.dumps(image)), \
                patch.object(m, "render") as render, patch.object(m, "install_policy") as policy:
            with self.assertRaises(m.Error): m.up(config(), pull="never")
            render.assert_not_called(); policy.assert_not_called()


if __name__ == "__main__": unittest.main()
