#!/usr/bin/env python3
"""Carry immutable files through a Git branch. Never execute their contents."""

import argparse
import base64
from contextlib import contextmanager
try:
    import fcntl
except ImportError:  # CLI help remains readable on unsupported platforms.
    fcntl = None
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time


SCHEMA = "lodestone-mailbox/v1"
MAX_PAYLOAD = 1024 * 1024
MAX_ENVELOPE = 4 * ((MAX_PAYLOAD + 2) // 3) + 2048
ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,119}")
FIELDS = {"schema", "id", "to", "kind", "size", "sha256", "payload_b64"}


class MailboxError(Exception):
    pass


def validate(packet, expected_id=None, expected_to=None):
    """Validate one envelope and return its original bytes, without coercion."""
    if type(packet) is not dict or set(packet) != FIELDS:
        raise MailboxError("Envelope has missing or unknown fields")
    if packet["schema"] != SCHEMA:
        raise MailboxError("Unknown envelope schema")
    name = packet["id"]
    if not isinstance(name, str) or not ID.fullmatch(name):
        raise MailboxError("Invalid envelope id")
    if packet["to"] not in ("mac", "cloud"):
        raise MailboxError("Invalid envelope destination")
    if packet["kind"] not in ("file", "ping", "pong"):
        raise MailboxError("Unknown envelope kind")
    if packet["kind"] == "ping" and len(name) > 116:
        raise MailboxError("Ping id must leave four characters for -ack")
    if expected_id is not None and name != expected_id:
        raise MailboxError("Envelope id does not match its filename")
    if expected_to is not None and packet["to"] != expected_to:
        raise MailboxError("Envelope destination does not match its directory")
    size = packet["size"]
    if type(size) is not int or not 0 <= size <= MAX_PAYLOAD:
        raise MailboxError("Payload size must be between 0 and 1048576 bytes")
    digest = packet["sha256"]
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise MailboxError("Invalid SHA-256")
    encoded = packet["payload_b64"]
    if not isinstance(encoded, str) or len(encoded) > 4 * ((MAX_PAYLOAD + 2) // 3):
        raise MailboxError("Invalid or oversized base64 payload")
    try:
        payload = base64.b64decode(encoded, validate=True)
    except (ValueError, base64.binascii.Error) as exc:
        raise MailboxError("Invalid base64 payload") from exc
    if len(payload) != size or hashlib.sha256(payload).hexdigest() != digest:
        raise MailboxError("Payload size or SHA-256 mismatch")
    if base64.b64encode(payload).decode("ascii") != encoded:
        raise MailboxError("Noncanonical base64 payload")
    return payload


def pack(payload, packet_id, to, kind="file"):
    if not isinstance(payload, bytes):
        raise MailboxError("Payload must be bytes")
    if len(payload) > MAX_PAYLOAD:
        raise MailboxError("Payload exceeds 1048576 bytes")
    packet = dict(schema=SCHEMA, id=packet_id, to=to, kind=kind,
                  size=len(payload), sha256=hashlib.sha256(payload).hexdigest(),
                  payload_b64=base64.b64encode(payload).decode("ascii"))
    validate(packet)
    return packet


def _encode(packet):
    return (json.dumps(packet, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _decode(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise MailboxError("Duplicate JSON field")
            result[key] = value
        return result
    if len(raw) > MAX_ENVELOPE:
        raise MailboxError("Envelope exceeds size limit")
    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=unique)
    except (ValueError, UnicodeError) as exc:
        raise MailboxError("Invalid JSON envelope") from exc


class Mailbox:
    def __init__(self, remote, branch, state):
        if fcntl is None:
            raise MailboxError("Mailbox transport requires POSIX file locks (macOS/Linux).")
        if not isinstance(remote, str) or not remote.strip():
            raise MailboxError("An explicit Git remote is required")
        if not isinstance(branch, str) or not branch.strip():
            raise MailboxError("An explicit branch is required")
        if not state or not str(state).strip():
            raise MailboxError("An explicit private state directory is required")
        self.remote, self.branch = remote, branch
        self.state = Path(state).expanduser().absolute()
        self.state.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.state.is_symlink() or self.state.stat().st_mode & 0o077:
            raise MailboxError("State must be a private directory (mode 700)")
        self.cache = self.state / "git"
        with self._lock():
            self._git(["check-ref-format", "refs/heads/" + branch], bare=False)
            binding = {"remote": remote, "branch": branch}
            config = self.state / "binding.json"
            try:
                with config.open("x", encoding="utf-8") as handle:
                    json.dump(binding, handle)
            except FileExistsError:
                if json.loads(config.read_text(encoding="utf-8")) != binding:
                    raise MailboxError("State belongs to another remote or branch; use a new --state")
            if not self.cache.exists():
                self._git(["init", "--bare", str(self.cache)], bare=False)
                self._git(["remote", "add", "origin", remote])
                self._git(["config", "remote.origin.promisor", "true"])
                self._git(["config", "remote.origin.partialclonefilter", "blob:none"])
            if self._git(["config", "--get", "remote.origin.url"]).strip().decode() != remote:
                raise MailboxError("Git cache remote differs from the configured mailbox")

    @contextmanager
    def _lock(self):
        with (self.state / "operation.lock").open("a") as handle:
            deadline = time.monotonic() + 60
            while True:
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise MailboxError("Another mailbox operation is busy; retry shortly")
                    time.sleep(0.05)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def _git(self, args, data=None, index=None, bare=True, check=True):
        env = os.environ.copy()
        for name in list(env):
            if name.startswith("GIT_"):
                env.pop(name)
        env.update(GIT_TERMINAL_PROMPT="0", GIT_AUTHOR_NAME="Lodestone Mailbox",
                   GIT_AUTHOR_EMAIL="mailbox@localhost", GIT_COMMITTER_NAME="Lodestone Mailbox",
                   GIT_COMMITTER_EMAIL="mailbox@localhost")
        if index is not None:
            env["GIT_INDEX_FILE"] = str(index)
        cmd = ["git", "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgSign=false"]
        if bare:
            cmd.append("--git-dir=" + str(self.cache))
        try:
            result = subprocess.run(cmd + args, input=data, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, env=env, timeout=60)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise MailboxError("Git unavailable or timed out") from exc
        if check and result.returncode:
            detail = result.stderr.decode("utf-8", errors="replace").strip()
            detail = detail.replace(self.remote, "<remote>")
            raise MailboxError("Git %s failed: %s" % (args[0], detail))
        return result.stdout if check else result

    def _head(self):
        self._git(["fetch", "--quiet", "--no-tags", "--depth=1", "--filter=blob:none",
                   "origin", "refs/heads/" + self.branch])
        return self._git(["rev-parse", "FETCH_HEAD"]).strip().decode("ascii")

    def _entries(self, head, path):
        raw = self._git(["ls-tree", "-r", "-z", head, "--", path])
        for entry in raw.split(b"\0"):
            if not entry:
                continue
            metadata, name = entry.split(b"\t", 1)
            mode, kind, oid = metadata.decode("ascii").split()
            if mode != "100644" or kind != "blob":
                raise MailboxError("Mailbox entries must be regular data files")
            yield name.decode("utf-8"), oid

    def _blob(self, oid):
        if int(self._git(["cat-file", "-s", oid])) > MAX_ENVELOPE:
            raise MailboxError("Envelope exceeds size limit")
        return self._git(["cat-file", "blob", oid])

    def send(self, packet):
        """Append one packet, retrying concurrent pushes; identical resend is a no-op."""
        with self._lock():
            return self._send(packet)

    def _send(self, packet):
        validate(packet)
        path = "mailbox/to-%s/%s.json" % (packet["to"], packet["id"])
        for attempt in range(4):
            parent = self._head()
            entries = list(self._entries(parent, path))
            if entries:
                if len(entries) != 1 or entries[0][0] != path:
                    raise MailboxError("Envelope path is already occupied")
                existing = _decode(self._blob(entries[0][1]))
                validate(existing, packet["id"], packet["to"])
                if existing != packet:
                    raise MailboxError("Immutable envelope id already has different contents")
                return False
            with tempfile.TemporaryDirectory(prefix="index-", dir=str(self.state)) as tmp:
                index = Path(tmp) / "index"
                self._git(["read-tree", parent], index=index)
                oid = self._git(["hash-object", "-w", "--stdin"], data=_encode(packet)).strip().decode()
                self._git(["update-index", "--add", "--cacheinfo", "100644," + oid + "," + path], index=index)
                tree = self._git(["write-tree"], index=index).strip().decode()
                commit = self._git(["commit-tree", tree, "-p", parent],
                                   data=("Mailbox: " + path + "\n").encode()).strip().decode()
                pushed = self._git(["push", "--quiet", "origin", commit + ":refs/heads/" + self.branch], check=False)
            if pushed.returncode == 0:
                return True
        raise MailboxError("Mailbox push failed after four attempts; check Git authentication and branch access")

    def _deliver(self, packet, raw, payload):
        inbox = self.state / "inbox" / packet["to"]
        inbox.mkdir(mode=0o700, parents=True, exist_ok=True)
        destination = inbox / packet["id"]
        if not destination.exists():
            temporary = Path(tempfile.mkdtemp(prefix=".pending-", dir=str(inbox)))
            try:
                (temporary / "payload").write_bytes(payload)
                (temporary / "envelope.json").write_bytes(raw)
                try:
                    temporary.rename(destination)
                    return destination / "payload"
                except OSError:
                    if not destination.exists():
                        raise
            finally:
                if temporary.exists():
                    shutil.rmtree(temporary)
        if destination.is_symlink() or (destination / "payload").read_bytes() != payload or (destination / "envelope.json").read_bytes() != raw:
            raise MailboxError("Existing inbox copy differs from its immutable envelope")
        return None

    def receive(self, side):
        """Store verified files locally. A ping receives a same-bytes pong only."""
        with self._lock():
            return self._receive(side)

    def _receive(self, side):
        if side not in ("mac", "cloud"):
            raise MailboxError("Side must be mac or cloud")
        head = self._head()
        prefix = "mailbox/to-" + side + "/"
        delivered = []
        for path, oid in self._entries(head, prefix):
            name = path[len(prefix):]
            if not name.endswith(".json") or not ID.fullmatch(name[:-5]):
                raise MailboxError("Unexpected file in mailbox directory")
            raw = self._blob(oid)
            packet = _decode(raw)
            payload = validate(packet, name[:-5], side)
            saved = self._deliver(packet, raw, payload)
            if saved is not None:
                delivered.append(saved)
            if packet["kind"] == "ping":
                self._send(pack(payload, packet["id"] + "-ack", "cloud" if side == "mac" else "mac", "pong"))
        return delivered


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--remote", required=True, help="Your Git remote URL or local bare repository")
    parser.add_argument("--branch", required=True, help="Existing dedicated branch on that remote")
    parser.add_argument("--state", required=True, help="Private local cache/inbox directory; never share between remotes")
    commands = parser.add_subparsers(dest="command", required=True)
    sender = commands.add_parser("send", help="Publish exact file bytes")
    sender.add_argument("--to", choices=("mac", "cloud"), required=True)
    sender.add_argument("--id", required=True)
    sender.add_argument("--file", type=Path, required=True)
    sender.add_argument("--kind", choices=("file", "ping", "pong"), default="file")
    for name in ("receive", "watch"):
        receiver = commands.add_parser(name, help="Collect files; acknowledge pings")
        receiver.add_argument("--side", choices=("mac", "cloud"), required=True)
        if name == "watch":
            receiver.add_argument("--interval", type=int, default=30)
    args = parser.parse_args(argv)
    if args.command == "watch" and args.interval < 5:
        parser.error("--interval must be at least 5 seconds")
    os.umask(0o077)
    try:
        mailbox = Mailbox(args.remote, args.branch, args.state)
        if args.command == "send":
            with args.file.open("rb") as handle:
                payload = handle.read(MAX_PAYLOAD + 1)
            packet = pack(payload, args.id, args.to, args.kind)
            result = "sent" if mailbox.send(packet) else "already present"
            print("%s: %s (%d bytes, sha256 %s)" % (result, args.id, len(payload), packet["sha256"]))
            return 0
        if args.command == "watch":
            print("Watching %s; inbox %s. Ctrl-C stops pickup." % (args.side, mailbox.state / "inbox" / args.side), flush=True)
            print("Script: %s\nState: %s" % (Path(__file__).resolve(), mailbox.state), flush=True)
        while True:
            try:
                for path in mailbox.receive(args.side):
                    print("Received: " + str(path), flush=True)
            except (MailboxError, OSError) as exc:
                if args.command != "watch":
                    raise
                print("Pickup error: " + str(exc), file=sys.stderr, flush=True)
            if args.command != "watch":
                return 0
            time.sleep(args.interval)
    except KeyboardInterrupt:
        return 0
    except (MailboxError, OSError, ValueError) as exc:
        print("Mailbox error: " + str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
