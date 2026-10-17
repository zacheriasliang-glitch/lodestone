"""Transport checks against an actual local Git remote; no network or model calls."""
import base64
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location(
    "lodestone_mailbox_transport", Path(__file__).with_name("mailbox.py"))
transport = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(transport)


def git(cwd, *args):
    return subprocess.run(
        ["git", "-C", str(cwd), *args], check=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout.strip()


class PacketTests(unittest.TestCase):
    def test_bytes_and_json_roundtrip(self):
        data = bytes(range(256)) + "Mrrp — 月\n".encode("utf-8")
        packet = transport.pack(data, "request-001", "mac")
        self.assertEqual(data, transport.validate(json.loads(json.dumps(packet))))

    def test_corruption_and_path_mismatch(self):
        good = transport.pack(b"original\r\n", "request-001", "mac")
        bad = dict(good, payload_b64=base64.b64encode(b"modified\r\n").decode())
        for packet, kwargs in [
            (bad, {}),
            (dict(good, size=True), {}),
            (dict(good, size=999), {}),
            (dict(good, kind="execute"), {}),
            (dict(good, schema="unknown"), {}),
            (dict(good, payload_b64="%%%"), {}),
            (dict(good, id="../escape"), {}),
            (good, {"expected_id": "other"}),
            (good, {"expected_to": "cloud"}),
        ]:
            with self.subTest(packet=packet, kwargs=kwargs):
                with self.assertRaises(transport.MailboxError):
                    transport.validate(packet, **kwargs)

    def test_payload_limit(self):
        with self.assertRaises(transport.MailboxError):
            transport.pack(b"x" * (1024 * 1024 + 1), "large", "mac")


class GitTransportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.remote = self.root / "remote.git"
        self.seed = self.root / "seed"
        self.seed.mkdir()
        git(self.root, "init", "--bare", str(self.remote))
        git(self.seed, "init")
        git(self.seed, "config", "user.name", "Mailbox test")
        git(self.seed, "config", "user.email", "mailbox-test@example.invalid")
        (self.seed / "unrelated.txt").write_bytes(b"preserve this file\n")
        git(self.seed, "add", "unrelated.txt")
        git(self.seed, "commit", "-m", "Fixture")
        git(self.seed, "push", str(self.remote), "HEAD:refs/heads/mailbox-test")
        self.cloud = transport.Mailbox(str(self.remote), "mailbox-test", self.root / "cloud")
        self.mac = transport.Mailbox(str(self.remote), "mailbox-test", self.root / "mac")

    def remote_packet(self, side, packet_id):
        raw = git(self.remote, "show", "refs/heads/mailbox-test:mailbox/to-{}/{}.json".format(side, packet_id))
        return json.loads(raw)

    def test_bidirectional_exact_files_idempotency_and_conflict(self):
        data = b"\x00\xff exact bytes\r\n" + "月".encode()
        packet = transport.pack(data, "request-001", "mac")
        self.assertTrue(self.cloud.send(packet))
        self.assertFalse(self.cloud.send(packet))
        received = self.mac.receive("mac")
        self.assertEqual(len(received), 1)
        self.assertEqual(Path(received[0]).read_bytes(), data)
        self.assertEqual(self.mac.receive("mac"), [])
        with self.assertRaises(transport.MailboxError):
            self.cloud.send(transport.pack(b"changed", "request-001", "mac"))
        response = b'{"request_id":"request-001", "response":"ok"}\n'
        self.mac.send(transport.pack(response, "response-001", "cloud"))
        replies = self.cloud.receive("cloud")
        self.assertEqual(Path(replies[0]).read_bytes(), response)
        self.assertEqual(git(self.remote, "show", "refs/heads/mailbox-test:unrelated.txt"), b"preserve this file")

    def test_ping_pong_does_not_execute_payload(self):
        target = self.root / "MUST_NOT_EXIST"
        payload = ("touch " + str(target)).encode()
        self.cloud.send(transport.pack(payload, "probe", "mac", "ping"))
        self.mac.receive("mac")
        pong = self.remote_packet("cloud", "probe-ack")
        self.assertEqual(pong["kind"], "pong")
        self.assertEqual(transport.validate(pong), payload)
        self.assertFalse(target.exists())
        self.mac.receive("mac")
        self.assertEqual(len(self.cloud.receive("cloud")), 1)
        self.assertEqual(self.cloud.receive("cloud"), [])

    def test_competing_writer_is_preserved_on_push_retry(self):
        real_run = subprocess.run
        interleaved = []

        def run_with_race(command, *args, **kwargs):
            if isinstance(command, (list, tuple)) and "push" in command and not interleaved:
                interleaved.append(True)
                self.mac.send(transport.pack(b"second writer", "second", "cloud"))
            return real_run(command, *args, **kwargs)

        with patch.object(transport.subprocess, "run", side_effect=run_with_race):
            self.cloud.send(transport.pack(b"first writer", "first", "mac"))
        self.assertTrue(interleaved, "The test must intercept an actual push.")
        self.assertEqual(transport.validate(self.remote_packet("mac", "first")), b"first writer")
        self.assertEqual(transport.validate(self.remote_packet("cloud", "second")), b"second writer")

    def test_remote_corruption_never_reaches_inbox(self):
        bad = transport.pack(b"original", "broken", "mac")
        bad["sha256"] = "0" * 64
        dest = self.seed / "mailbox/to-mac/broken.json"
        dest.parent.mkdir(parents=True)
        dest.write_text(json.dumps(bad))
        git(self.seed, "add", "mailbox")
        git(self.seed, "commit", "-m", "Intentionally corrupt fixture")
        git(self.seed, "push", str(self.remote), "HEAD:refs/heads/mailbox-test")
        with self.assertRaises(transport.MailboxError):
            self.mac.receive("mac")
        self.assertFalse((self.root / "mac/inbox/mac/broken/payload").exists())

    def test_changed_local_payload_is_not_silently_reused(self):
        self.cloud.send(transport.pack(b"original", "request", "mac"))
        received = self.mac.receive("mac")
        Path(received[0]).write_bytes(b"changed on disk")
        with self.assertRaises(transport.MailboxError):
            self.mac.receive("mac")


if __name__ == "__main__":
    unittest.main()
