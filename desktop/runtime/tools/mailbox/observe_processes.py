#!/usr/bin/env python3
"""Passively sample a fixed process tree; labels do not prove executable identity."""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import selectors
import subprocess
import tempfile
import time


MAX_PS_BYTES = 2 * 1024 * 1024
MAX_PS_SECONDS = 3.0
MAX_LOG_BYTES = 1024 * 1024
END_RESERVE = 1024
INTERVAL = 0.25


class ObservationError(Exception):
    pass


def encode(value):
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def parse_processes(raw):
    rows = {}
    for line in raw.decode("utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        fields = line.split(None, 3)
        if len(fields) != 4 or not all(field.isascii() and field.isdecimal() for field in fields[:3]):
            raise ObservationError("ps_invalid_output")
        pid, ppid, pgid = map(int, fields[:3])
        if pid in rows:
            raise ObservationError("ps_duplicate_pid")
        rows[pid] = {"pid": pid, "ppid": ppid, "pgid": pgid,
                     "comm_basename": fields[3].rstrip("/").rsplit("/", 1)[-1]}
    return rows


def read_processes(timeout):
    # Keep the bounded, unfiltered table only in memory. Cleanup signals only
    # this sampler-owned ps child, never the observed processes or their groups.
    if not 0 < timeout <= MAX_PS_SECONDS:
        raise ValueError("ps timeout is out of bounds")
    deadline = time.monotonic() + timeout
    try:
        process = subprocess.Popen(["/bin/ps", "-axo", "pid=,ppid=,pgid=,comm="],
                                   stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, bufsize=0)
    except OSError:
        raise ObservationError("ps_spawn_failed")
    raw = bytearray()
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ObservationError("ps_timeout")
                if not selector.select(remaining):
                    raise ObservationError("ps_timeout")
                chunk = os.read(process.stdout.fileno(),
                                min(65536, MAX_PS_BYTES + 1 - len(raw)))
                if not chunk:
                    break
                if len(raw) + len(chunk) > MAX_PS_BYTES:
                    raise ObservationError("ps_output_limit")
                raw.extend(chunk)
        if process.wait(timeout=max(0, deadline - time.monotonic())):
            raise ObservationError("ps_nonzero_exit")
    except subprocess.TimeoutExpired:
        raise ObservationError("ps_timeout")
    except OSError:
        raise ObservationError("ps_read_failed")
    finally:
        process.stdout.close()
        if process.poll() is None:
            process.kill()
            try:
                process.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                raise ObservationError("ps_cleanup_timeout")
    return parse_processes(raw)


def descendants(rows, root_pid):
    if root_pid not in rows:
        return []
    selected = {root_pid}
    while True:
        children = {pid for pid, row in rows.items() if row["ppid"] in selected}
        expanded = selected | children
        if expanded == selected:
            return [rows[pid] for pid in sorted(selected)]
        selected = expanded


def observe(root_pid, output_path, ready_path, seconds=600, ps_timeout=1.0):
    if root_pid <= 0 or not 0 < seconds <= 600:
        raise ValueError("root PID and observation duration are out of bounds")
    if not 0 < ps_timeout <= MAX_PS_SECONDS:
        raise ValueError("ps timeout is out of bounds")
    if Path(output_path).resolve() == Path(ready_path).resolve():
        raise ValueError("output and readiness paths must differ")
    started = time.monotonic()
    count, observed_root, reason = 0, False, "deadline"
    with open(output_path, "xb", buffering=0) as output:
        output.write(encode({"type": "start", "schema": "lodestone-process-observation/v1",
                             "root_pid": root_pid, "seconds": seconds,
                             "ps_timeout_seconds": ps_timeout,
                             "utc": datetime.now(timezone.utc).isoformat(),
                             "minimum_gap_seconds": INTERVAL,
                             "comm_is_untrusted_label": True}))
        while True:
            remaining = seconds - (time.monotonic() - started)
            if remaining <= 0:
                break
            before = time.monotonic()
            record = {"type": "sample", "sample": count}
            try:
                rows = descendants(read_processes(min(ps_timeout, remaining)), root_pid)
                record.update(status="ok", rows=rows)
            except ObservationError as exc:
                rows = []
                record.update(status="failed", error=str(exc))
            after = time.monotonic()
            record.update(utc=datetime.now(timezone.utc).isoformat(),
                          elapsed_seconds=round(after - started, 6),
                          duration_seconds=round(after - before, 6))
            raw = encode(record)
            if output.tell() + len(raw) > MAX_LOG_BYTES - END_RESERVE:
                reason = "observation_output_limit"
                break
            output.write(raw)
            count += 1
            if record["status"] != "ok":
                reason = record["error"]
                break
            if not rows:
                reason = "root_not_seen" if observed_root else "root_missing_at_start"
                break
            if not observed_root:
                observed_root = True
                try:
                    os.fsync(output.fileno())
                    with tempfile.NamedTemporaryFile(dir=str(Path(ready_path).parent)) as ready:
                        ready.write(encode({"schema": "lodestone-process-observer-ready/v1",
                                            "root_pid": root_pid, "sample": count - 1}))
                        ready.flush()
                        os.fsync(ready.fileno())
                        os.link(ready.name, ready_path)
                except OSError:
                    reason = "ready_file_failed"
                    break
            remaining = seconds - (time.monotonic() - started)
            if remaining > 0:
                time.sleep(min(INTERVAL, remaining))
        output.write(encode({"type": "end", "reason": reason, "sample_count": count,
                             "observed_root": observed_root,
                             "utc": datetime.now(timezone.utc).isoformat(),
                             "elapsed_seconds": round(time.monotonic() - started, 6)}))
    return 0 if reason == "root_not_seen" else 2


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root-pid", required=True, type=int)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--ready-file", required=True, type=Path)
    parser.add_argument("--seconds", default=600, type=float)
    parser.add_argument("--ps-timeout", default=1.0, type=float,
                        help="Per-ps deadline in seconds (>0 and <=3; default: 1)")
    args = parser.parse_args(argv)
    if args.root_pid <= 0 or not 0 < args.seconds <= 600:
        parser.error("--root-pid must be positive; --seconds must be >0 and <=600")
    if not 0 < args.ps_timeout <= MAX_PS_SECONDS:
        parser.error("--ps-timeout must be >0 and <=3")
    os.umask(0o077)
    try:
        return observe(args.root_pid, args.output, args.ready_file, args.seconds,
                       args.ps_timeout)
    except (OSError, ValueError):
        print("Observation failed: output/setup unavailable.", flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
