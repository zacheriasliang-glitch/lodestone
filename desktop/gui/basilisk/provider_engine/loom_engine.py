"""Loom's GUI boundary: every request uses the native learning lifecycle."""
from __future__ import annotations

from functools import cached_property
import json
import threading

from basilisk import global_vars
from basilisk.conversation import Message, MessageRoleEnum
from basilisk.provider_ai_model import ProviderAIModel
from basilisk.services.loom_records import save_receipt

from .base_engine import BaseEngine, ProviderCapability


def _failure_reason(result):
	"""Expose bounded failure reasons without dumping native evidence packets."""
	observation = result.get("broker", {}).get("outcome", {}).get("observation", {})
	rows = [result]
	if isinstance(observation, dict):
		rows.append(observation)
		rows.extend(observation.get(key, {}) for key in ("worker", "supervisor"))
	values = []
	for row in rows:
		if isinstance(row, dict):
			for key in ("error", "reason"):
				value = row.get(key)
				if isinstance(value, str) and value not in values:
					values.append(value)
	return "; ".join(values)[:2000]


class LoomEngine(BaseEngine):
	"""Use the explicitly injected local bridge; never fall back to another engine."""
	capabilities = {ProviderCapability.TEXT}
	supported_attachment_formats = set()
	uses_terminal_results = True

	def __init__(self, account):
		super().__init__(account)
		self._cancel_event = threading.Event()
		self._run_lock = threading.Lock()
		self.last_receipt = None

	@cached_property
	def client(self):
		if global_vars.loom_bridge is None:
			raise RuntimeError("Open Loom through its configured local launcher")
		return global_vars.loom_bridge

	def _load_models(self):
		return [ProviderAIModel(**row) for row in self.client.models()]

	@property
	def models(self):
		# This metadata is local configuration, not an online catalog. Require
		# the bridge even if an old disk catalog happens to exist.
		return self._load_models()

	def prepare_message_request(self, message):
		super().prepare_message_request(message)
		return {"role": message.role.value, "content": message.content}

	def prepare_message_response(self, response):
		return Message(role=MessageRoleEnum.ASSISTANT, content=response)

	def cancel_completion(self):
		"""Request cancellation; the worker returns only after actual termination."""
		self._cancel_event.set()

	def prepare_completion(self):
		"""Reset before the worker starts, so an immediate Stop cannot be lost."""
		self._cancel_event.clear()
		self.last_receipt = None

	def completion(self, new_block, conversation, system_message, stop_block_index=None,
				   *, loom_purpose=None, **kwargs):
		self.last_receipt = None
		if not isinstance(loom_purpose, str) or not loom_purpose.strip():
			raise ValueError("Set the purpose for this Loom encounter before sending")
		if new_block.request.attachments:
			raise ValueError("Loom currently accepts text encounters; describe the attachment in the message")
		if not self._run_lock.acquire(blocking=False):
			raise RuntimeError("This Loom encounter is still running")
		try:
			history = []
			for index, block in enumerate(conversation.messages):
				if stop_block_index is not None and index >= stop_block_index:
					break
				if block.request.attachments:
					raise ValueError("This conversation contains attachments Loom cannot read; use a text encounter")
				history.append({"request": block.request.content,
					"response": block.response.content if block.response else None})
			context = json.dumps({"conversation": history,
				"additional_context": system_message.content if system_message else ""},
				ensure_ascii=False)
			result = self.client.run(
				encounter=new_block.request.content, purpose=loom_purpose.strip(),
				context=context, route=new_block.model.model_id,
				max_tokens=new_block.max_tokens or None, temperature=new_block.temperature,
				top_p=new_block.top_p, cancel_event=self._cancel_event,
			)
			if result.get("outcome_handle"):
				self.last_receipt = save_receipt(new_block, result, loom_purpose.strip())
			return result
		finally:
			self._run_lock.release()

	def completion_response_without_stream(self, response, new_block, **kwargs):
		status = response["status"]
		text = response.get("response") or ""
		if status != "COMPLETED":
			label = {
				"INTERRUPTED": "Loom stopped this encounter. The interrupted attempt was retained.",
				"FAILED": "Loom could not complete this encounter. The failed attempt was retained.",
				"REFUSED": "Loom declined this encounter. The refusal was retained.",
				"ABSTAINED": "Loom abstained from an answer. The attempt was retained.",
			}.get(status, f"Loom ended with status {status}. The attempt was retained.")
			if not response.get("outcome_handle"):
				label = (f"Loom ended with status {status} before a Bezoar outcome was created."
					+ (" A local error record was saved." if response.get("artifacts_dir") else ""))
			reason = _failure_reason(response)
			if reason:
				label += "\nReason: " + reason
			if response.get("artifacts_dir"):
				label += "\nRecord: " + str(response["artifacts_dir"])
			text = label + ("\n\nPartial response:\n" + text if text else "")
		new_block.response = Message(role=MessageRoleEnum.ASSISTANT, content=text)
		return new_block

	def completion_response_with_stream(self, stream):
		raise ValueError("Loom returns a complete retained outcome; streaming is unavailable")

	def correct(self, receipt, *, correction, author, observation=""):
		return self.client.correct(receipt["outcome_handle"], correction=correction,
			author=author, observation=observation)

	def decision_review_context(self, receipt):
		return self.client.decision_review_context(receipt["outcome_handle"])

	def preview_decision_review(self, receipt, *, case_json, policy=None, author):
		return self.client.preview_decision_review(receipt["outcome_handle"],
			case_json=case_json, policy=policy, author=author)

	def review_decision(self, receipt, *, contract, author):
		return self.client.review_decision(receipt["outcome_handle"],
			contract=contract, author=author)

	def preview_origami(self, receipt, *, root, paths):
		return self.client.preview_origami(receipt["outcome_handle"], root=root, paths=paths)

	def complete_origami(self, handoff_id, curation):
		return self.client.complete_origami(handoff_id, curation)
