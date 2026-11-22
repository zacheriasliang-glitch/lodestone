"""Private GUI receipt indexes; native Bezoar records remain authoritative."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile

from basilisk import global_vars

DEFAULT_LOOM_PURPOSE = "Find a useful next contribution to this encounter."


def block_key(block) -> str:
	"""Use fields already preserved by the conversation database and export."""
	value = {
		"created_at": block.created_at.isoformat(),
		"request": block.request.content,
		"model": block.model.model_dump(mode="json"),
	}
	return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def _root() -> Path:
	if global_vars.user_data_path is None:
		raise RuntimeError("Loom requires the unified installation's private data directory")
	return Path(global_vars.user_data_path) / "loom_receipts"


def save_receipt(block, result: dict, purpose: str) -> dict:
	"""Retain an immutable reference to this exact native practice outcome."""
	key = block_key(block)
	if not result.get("outcome_handle"):
		raise ValueError("Loom returned no retained outcome handle")
	receipt = {
		"schema": "loom-gui-receipt/v1",
		"block_key": key,
		"recorded_at": datetime.now(timezone.utc).isoformat(),
		"purpose": purpose,
		"status": result["status"],
		"run_id": result.get("run_id"),
		"run_handle": result.get("run_handle"),
		"outcome_handle": result["outcome_handle"],
		"terminal_handle": result.get("terminal_handle"),
		"reader": result.get("reader"),
	}
	directory = _root() / key
	directory.mkdir(mode=0o700, parents=True, exist_ok=True)
	identity = hashlib.sha256(result["outcome_handle"].encode()).hexdigest()
	target = directory / f"{identity}.json"
	if target.exists():
		return json.loads(target.read_text())
	fd, temporary = tempfile.mkstemp(dir=directory, prefix=".receipt-")
	try:
		with os.fdopen(fd, "w") as stream:
			json.dump(receipt, stream, indent=2)
			stream.write("\n")
			stream.flush()
			os.fsync(stream.fileno())
		os.replace(temporary, target)
	finally:
		if os.path.exists(temporary):
			os.unlink(temporary)
	return receipt


def receipt_for(block) -> dict | None:
	"""Recover the latest retained attempt for this specific displayed block."""
	key = block_key(block)
	directory = _root() / key
	if not directory.is_dir():
		return None
	rows = []
	for path in directory.glob("*.json"):
		row = json.loads(path.read_text())
		if row.get("schema") == "loom-gui-receipt/v1" and row.get("block_key") == key:
			rows.append(row)
	return max(rows, key=lambda row: row["recorded_at"], default=None)


def latest_receipt(conversation) -> dict | None:
	for block in reversed(conversation.messages):
		if block.model.provider_id == "loom":
			# A latest encounter without a native outcome cannot be corrected
			# through an unrelated earlier attempt's handle.
			return receipt_for(block)
	return None
