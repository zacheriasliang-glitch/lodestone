"""Explicit options for one current Loom message; no generation in this dialog."""
from copy import deepcopy
import re
import threading
import weakref
import wx

from basilisk.services.loom_practice import bind_options, validate_options
from .loom_correction_dialog import _display_client_area


def _validated_catalogue(value):
	"""Validate the read-only transport before enabling controls; no native action."""
	if not isinstance(value, dict) or value.get("schema") != "loom-practice-candidates/v1":
		raise ValueError("Unsupported lesson catalogue")
	result = deepcopy(value)
	for key in ("configured_seed_handles", "resolved_seed_handles"):
		if key not in result and key == "resolved_seed_handles":
			continue
		if not isinstance(result.get(key), list) or any(not isinstance(h, str) or not re.fullmatch(r"bezoar:[0-9a-f]{64}", h) for h in result[key]):
			raise ValueError("Invalid configured lesson selection")
	if not isinstance(result.get("candidates"), list):
		raise ValueError("Invalid retained lesson catalogue")
	seen = set()
	for row in result["candidates"]:
		if not isinstance(row, dict) or any(not isinstance(row.get(k), str) for k in ("handle", "id", "state", "correction")):
			raise ValueError("Invalid retained lesson details")
		if not re.fullmatch(r"bezoar:[0-9a-f]{64}", row["handle"]) or row["handle"] in seen or type(row.get("ready")) is not bool:
			raise ValueError("Invalid retained lesson identity or availability")
		seen.add(row["handle"])
		for key in ("conditions", "frame_limits"):
			if not isinstance(row.get(key), list) or any(not isinstance(item, str) for item in row[key]):
				raise ValueError("Invalid retained lesson conditions")
		if row.get("error") is not None and not isinstance(row["error"], str):
			raise ValueError("Invalid retained lesson availability detail")
	return result


class LoomPracticeDialog(wx.Dialog):
	def __init__(self, parent, *, engine, encounter, purpose, options=None):
		super().__init__(parent, title="Practice options for this encounter",
			style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
		self.encounter, self.purpose = encounter, purpose
		self.result = None
		options = validate_options(options)
		self._saved_options = options
		self._catalogue_cancel = threading.Event()
		self._catalogue_ready = False
		self.candidates = []
		self.configured_handles = []
		selected = options["seed_handles"] if options else None
		layout = wx.BoxSizer(wx.VERTICAL)
		self.loading = wx.StaticText(self, label="Loading retained lessons… You can cancel while this runs.")
		layout.Add(self.loading, 0, wx.ALL | wx.EXPAND, 8)
		body = wx.ScrolledWindow(self)
		body.SetScrollRate(0, 12)
		body.SetMinSize((1, 1))
		fields = wx.BoxSizer(wx.VERTICAL)
		note = wx.StaticText(body, label="Optional settings apply only to this message and purpose. "
			"Changing either requires review. Retained lessons remain candidate advice; selecting them does not establish learning.")
		note.Wrap(650)
		fields.Add(note, 0, wx.ALL | wx.EXPAND, 8)
		self.configured = wx.CheckBox(body, label="Use configured lesson selection")
		self.configured.SetValue(selected is None)
		fields.Add(self.configured, 0, wx.ALL, 8)
		self.lessons = wx.CheckListBox(body, choices=[], size=(-1, 120))
		fields.Add(self.lessons, 0, wx.ALL | wx.EXPAND, 8)
		self.lesson_details = wx.TextCtrl(body, style=wx.TE_MULTILINE | wx.TE_READONLY, size=(-1, 115))
		fields.Add(self.lesson_details, 0, wx.ALL | wx.EXPAND, 8)
		self.window_enabled = wx.CheckBox(body, label="Limit visible practice history to the newest runs (original records stay retained)")
		self.window_enabled.SetValue(bool(options and options["history_window"] is not None))
		fields.Add(self.window_enabled, 0, wx.ALL, 8)
		self.history_window = wx.SpinCtrl(body, min=1, max=32, initial=options["history_window"] if options and options["history_window"] else 1)
		fields.Add(self.history_window, 0, wx.LEFT | wx.RIGHT, 8)
		fields.Add(wx.StaticText(body, label="Advanced: exact case JSON selected from the current message (blank keeps open-ended responses)"), 0, wx.ALL, 8)
		self.case_json = wx.TextCtrl(body, value=(options["decision_case_json"] or "") if options else "",
			style=wx.TE_MULTILINE, size=(-1, 105))
		fields.Add(self.case_json, 0, wx.ALL | wx.EXPAND, 8)
		self.strategy = wx.Choice(body, choices=["Flat decision (default)", "Evidence first, then decision"])
		self.strategy.SetSelection(1 if options and options["decision_strategy"] == "evidence_first" else 0)
		fields.Add(self.strategy, 0, wx.ALL | wx.EXPAND, 8)
		fields.Add(wx.StaticText(body, label="Optional: machine finding handles from Structured case review (one per line). "
			"Only definite rejections can enter this encounter as named knots."), 0, wx.ALL, 8)
		self.knot_handles = wx.TextCtrl(body,
			value="\n".join(options.get("decision_knot_handles", []) or []) if options else "",
			style=wx.TE_MULTILINE, size=(-1, 65))
		fields.Add(self.knot_handles, 0, wx.ALL | wx.EXPAND, 8)
		self.message = wx.StaticText(body, label="")
		fields.Add(self.message, 0, wx.ALL | wx.EXPAND, 8)
		self.defaults = wx.Button(body, label="Clear options and use ordinary defaults")
		fields.Add(self.defaults, 0, wx.ALL, 8)
		body.SetSizer(fields)
		layout.Add(body, 1, wx.EXPAND)
		layout.Add(self.CreateButtonSizer(wx.OK | wx.CANCEL), 0, wx.ALL | wx.EXPAND, 8)
		self.SetSizer(layout)
		self.FindWindow(wx.ID_OK).SetLabel("Use for this encounter")
		self.Bind(wx.EVT_BUTTON, self._save, id=wx.ID_OK)
		self.configured.Bind(wx.EVT_CHECKBOX, self._controls_changed)
		self.window_enabled.Bind(wx.EVT_CHECKBOX, self._controls_changed)
		self.lessons.Bind(wx.EVT_LISTBOX, self._selected)
		self.lessons.Bind(wx.EVT_CHECKLISTBOX, self._lesson_checked)
		self.defaults.Bind(wx.EVT_BUTTON, self._clear)
		self._loading_controls = (self.configured, self.lessons, self.window_enabled, self.history_window,
			self.case_json, self.strategy, self.knot_handles, self.defaults, self.FindWindow(wx.ID_OK))
		for control in self._loading_controls:
			control.Disable()
		self.Bind(wx.EVT_CLOSE, self._cancel_loading)
		self.Bind(wx.EVT_BUTTON, self._cancel_loading, id=wx.ID_CANCEL)
		self.Bind(wx.EVT_WINDOW_DESTROY, self._destroyed)
		area = _display_client_area(parent)
		width, height = min(720, max(1, area.width - 32)), min(780, max(1, area.height - 32))
		self.SetSize(width, height)
		self.SetPosition((area.x + (area.width - width) // 2, area.y + (area.height - height) // 2))
		self.Layout()
		reference, cancelled = weakref.ref(self), self._catalogue_cancel
		def deliver(catalogue, error):
			dialog = reference()
			if not cancelled.is_set() and dialog is not None and bool(dialog):
				dialog._catalogue_loaded(catalogue, error)
		def load():
			try:
				catalogue = _validated_catalogue(engine.practice_candidates())
				error = None
			except Exception as exc:
				catalogue, error = None, str(exc)
			if not cancelled.is_set():
				try:
					wx.CallAfter(deliver, catalogue, error)
				except (RuntimeError, AssertionError):
					# The application can close between the cancellation check and
					# posting its event. The read-only result then has no recipient.
					cancelled.set()
		self._catalogue_thread = threading.Thread(target=load, name="Loom lesson catalogue", daemon=True)
		self._catalogue_thread.start()

	def _cancel_loading(self, event):
		self._catalogue_cancel.set()
		event.Skip()

	def _destroyed(self, event):
		if event.GetEventObject() is self:
			self._catalogue_cancel.set()
		event.Skip()

	def _catalogue_loaded(self, catalogue, error):
		if error is not None:
			self.loading.SetLabel("Retained lessons could not be loaded: " + error)
			self.loading.Wrap(max(1, self.GetClientSize().width - 16))
			self.Layout()
			return
		self.candidates = catalogue["candidates"]
		self.configured_handles = catalogue.get("resolved_seed_handles", catalogue["configured_seed_handles"])
		selected = self._saved_options["seed_handles"] if self._saved_options else None
		known = {row["handle"] for row in self.candidates}
		for handle in selected or []:
			if handle not in known:
				self.candidates.append({"handle": handle, "id": "Unavailable saved lesson", "ready": False,
					"state": "UNAVAILABLE", "conditions": [], "frame_limits": [], "correction": "",
					"error": "This saved lesson is unavailable in the configured learning memory."})
		self.lessons.Set([row["id"] + " — " + row["state"] + ("" if row["ready"] else " (unavailable)")
			+ " — " + row["handle"][-10:] for row in self.candidates])
		for index, row in enumerate(self.candidates):
			self.lessons.Check(index, row["handle"] in (self.configured_handles if selected is None else selected))
		self._catalogue_ready = True
		self.loading.SetLabel("Retained lessons loaded. Review their conditions before choosing.")
		for control in self._loading_controls:
			control.Enable()
		self._controls_changed(None)
		if self.candidates:
			self.lessons.SetSelection(0)
			self._show_details()
		self.Layout()

	def _controls_changed(self, event):
		if self.configured.GetValue():
			for index, row in enumerate(self.candidates):
				self.lessons.Check(index, row["handle"] in self.configured_handles)
		self.history_window.Enable(self.window_enabled.GetValue())
		if event:
			event.Skip()

	def _lesson_checked(self, event):
		# The list remains readable while configured selection is in use.
		# Checking a lesson explicitly opts into a per-encounter selection.
		self.configured.SetValue(False)
		event.Skip()

	def _selected(self, event):
		self._show_details()
		event.Skip()

	def _show_details(self):
		index = self.lessons.GetSelection()
		if index != wx.NOT_FOUND:
			row = self.candidates[index]
			lines = [row["id"], "", "Correction", row["correction"], "", "Conditions"]
			for field in ("conditions", "frame_limits"):
				if field == "frame_limits":
					lines.extend(["", "Limits"])
				value = row[field]
				lines.extend(["• " + str(item) for item in value] if isinstance(value, list) else [str(value)])
			lines.extend(["", "Availability", "Paired retained candidate; benefit remains unproven."
				if row["ready"] else row.get("error") or "Unavailable for selection."])
			replacement = row.get("superseded_by") or row.get("resolved_handle")
			if replacement and replacement != row["handle"]:
				name = next((item["id"] for item in self.candidates if item["handle"] == replacement), replacement)
				lines.append("Choose its current replacement: " + name)
			self.lesson_details.SetValue("\n".join(lines))

	def _clear(self, event):
		self.configured.SetValue(True)
		self.window_enabled.SetValue(False)
		self.case_json.SetValue("")
		self.knot_handles.SetValue("")
		self.strategy.SetSelection(0)
		self._controls_changed(None)

	def _save(self, event):
		if not self._catalogue_ready or self._catalogue_cancel.is_set():
			return
		try:
			seeds = None
			if not self.configured.GetValue():
				rows = [self.candidates[i] for i in self.lessons.GetCheckedItems()]
				if any(not row["ready"] for row in rows):
					raise ValueError("A selected lesson is unavailable or has no usable recurring pattern")
				seeds = [row["handle"] for row in rows]
			case = self.case_json.GetValue()
			self.result = bind_options(self.encounter, self.purpose, seed_handles=seeds,
				history_window=self.history_window.GetValue() if self.window_enabled.GetValue() else None,
				decision_case_json=case if case.strip() else None,
				decision_strategy="evidence_first" if self.strategy.GetSelection() else "flat",
				decision_knot_handles=self.knot_handles.GetValue().split() or None)
		except (ValueError, TypeError) as exc:
			self.message.SetLabel(str(exc))
			self.message.Wrap(650)
			self.Layout()
			return
		self._catalogue_cancel.set()
		self.EndModal(wx.ID_OK)
