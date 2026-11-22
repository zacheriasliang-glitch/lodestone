"""Capture a user's conditional correction for one retained Loom encounter."""
from copy import deepcopy
import json
import wx


def _display_client_area(window):
	index = wx.Display.GetFromWindow(window) if window is not None else wx.NOT_FOUND
	return wx.Display(0 if index == wx.NOT_FOUND else index).GetClientArea()


class LoomCorrectionDialog(wx.Dialog):
	def __init__(self, parent, receipt, *, engine=None):
		super().__init__(parent, title="Correct the last Loom encounter", size=(780, 800),
			style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
		self.receipt, self.engine = deepcopy(receipt), engine
		self.last_saved_review = None
		self.review_context = None
		self.decision_preview = None
		self.saved_review = None
		self._populating = False
		sizer = wx.BoxSizer(wx.VERTICAL)
		purpose = wx.StaticText(self, label="Purpose: " + receipt["purpose"])
		purpose.Wrap(730)
		sizer.Add(purpose, 0, wx.ALL | wx.EXPAND, 10)
		self.pages = wx.Notebook(self)
		self.pages.SetMinSize((1, 1))
		ordinary = wx.ScrolledWindow(self.pages)
		ordinary.SetScrollRate(0, 12)
		ordinary_sizer = wx.BoxSizer(wx.VERTICAL)
		self.fields = {}
		for key, label, height, default in (
			("correction", "What should Loom learn or change?", 100, ""),
			("conditions", "When does this correction apply?", 55, ""),
			("limits", "When should this be reconsidered? (optional)", 55, ""),
			("observation", "What actually happened? (optional)", 55, ""),
			("author", "Author", 28, "Local user"),
		):
			ordinary_sizer.Add(wx.StaticText(ordinary, label=label), 0, wx.LEFT | wx.RIGHT | wx.TOP, 10)
			control = wx.TextCtrl(ordinary, value=default, size=(-1, height),
				style=wx.TE_MULTILINE if height > 28 else 0)
			self.fields[key] = control
			ordinary_sizer.Add(control, 0, wx.LEFT | wx.RIGHT | wx.EXPAND, 10)
		ordinary.SetSizer(ordinary_sizer)
		self.pages.AddPage(ordinary, "Correction", select=True)
		self._build_decision_page()
		sizer.Add(self.pages, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 10)
		sizer.Add(self.CreateButtonSizer(wx.OK | wx.CANCEL), 0, wx.ALL | wx.EXPAND, 10)
		self.SetSizer(sizer)
		self.FindWindowById(wx.ID_OK).SetLabel("Save correction")
		self.Bind(wx.EVT_BUTTON, self._save, id=wx.ID_OK)
		self.pages.Bind(wx.EVT_NOTEBOOK_PAGE_CHANGED, self._page_changed)
		# GetClientArea excludes the menu bar and Dock. Keep the footer outside
		# both scrolling forms so Save/Cancel remain reachable on small screens.
		area = _display_client_area(parent)
		width, height = min(780, max(1, area.width - 32)), min(800, max(1, area.height - 32))
		self.SetSize(width, height)
		self.SetPosition((area.x + (area.width - width) // 2, area.y + (area.height - height) // 2))
		self.Layout()

	def _build_decision_page(self):
		page = wx.ScrolledWindow(self.pages)
		page.SetScrollRate(0, 12)
		layout = wx.BoxSizer(wx.VERTICAL)
		note = wx.StaticText(page, label="Optional check for an explicitly structured case. It uses the saved answer, "
			"not a new model call. Declared interpretations remain attributed; a passing contract check does not establish learning.")
		note.Wrap(710)
		layout.Add(note, 0, wx.ALL | wx.EXPAND, 8)
		self.native_status = wx.StaticText(page, label="Native record is loaded when this tab opens.")
		layout.Add(self.native_status, 0, wx.ALL | wx.EXPAND, 8)
		layout.Add(wx.StaticText(page, label="Retained reviews for this exact encounter"), 0, wx.LEFT, 8)
		self.review_selection = wx.Choice(page, choices=["New review"])
		self.review_selection.SetSelection(0)
		layout.Add(self.review_selection, 0, wx.ALL | wx.EXPAND, 8)
		sources = wx.Notebook(page)
		self.original_encounter = wx.TextCtrl(sources, style=wx.TE_MULTILINE | wx.TE_READONLY, size=(-1, 130))
		self.original_response = wx.TextCtrl(sources, style=wx.TE_MULTILINE | wx.TE_READONLY, size=(-1, 130))
		sources.AddPage(self.original_encounter, "Original encounter")
		sources.AddPage(self.original_response, "Original raw answer")
		layout.Add(sources, 0, wx.EXPAND | wx.ALL, 8)
		self.copy_case_btn = wx.Button(page, label="Use selected encounter text as case JSON")
		layout.Add(self.copy_case_btn, 0, wx.LEFT | wx.RIGHT, 8)
		self.decision_fields = {}
		for key, label, height, default in (
			("case_json", "Exact case JSON from the saved encounter", 100, ""),
			("policy_json", "Declared condition/action policy (optional JSON; blank leaves support unresolved)", 85, ""),
			("author", "Review author / interpretation curator", 28, "Local user"),
		):
			layout.Add(wx.StaticText(page, label=label), 0, wx.LEFT | wx.RIGHT | wx.TOP, 8)
			control = wx.TextCtrl(page, value=default, size=(-1, height), style=wx.TE_MULTILINE if height > 28 else 0)
			self.decision_fields[key] = control
			control.Bind(wx.EVT_TEXT, self._decision_changed)
			layout.Add(control, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)
		self.preview_btn = wx.Button(page, label="Preview contract check")
		layout.Add(self.preview_btn, 0, wx.ALL, 8)
		self.decision_summary = wx.StaticText(page, label="No check has been previewed or saved.")
		layout.Add(self.decision_summary, 0, wx.ALL | wx.EXPAND, 8)
		self.decision_details = wx.TextCtrl(page, style=wx.TE_MULTILINE | wx.TE_READONLY, size=(-1, 160))
		layout.Add(self.decision_details, 0, wx.EXPAND | wx.ALL, 8)
		page.SetSizer(layout)
		self.pages.AddPage(page, "Structured case review")
		self.review_selection.Bind(wx.EVT_CHOICE, self._select_review)
		self.copy_case_btn.Bind(wx.EVT_BUTTON, self._copy_case)
		self.preview_btn.Bind(wx.EVT_BUTTON, self._preview_decision)

	def _page_changed(self, event):
		if self.pages.GetSelection() == 1 and self.review_context is None:
			try:
				if self.engine is None:
					raise ValueError("The configured native review route is unavailable.")
				context = self.engine.decision_review_context(self.receipt)
				if context["outcome_handle"] != self.receipt["outcome_handle"]:
					raise ValueError("Native history belongs to another outcome.")
				self.review_context = deepcopy(context)
				self.original_encounter.SetValue(context["encounter"])
				self.original_response.SetValue(context["response"] if context["response"] is not None else "")
				self.native_status.SetLabel("Native status: " + context["native_status"]
					+ ("; no raw response was returned." if context["response"] is None else "; original answer is unchanged."))
				self._refresh_review_choices()
			except Exception as exc:
				self.native_status.SetLabel("Native review unavailable: " + str(exc))
				self.native_status.Wrap(710)
		self.preview_btn.Enable(self.review_context is not None)
		self.copy_case_btn.Enable(self.review_context is not None)
		self._update_save_button()
		event.Skip()

	def _refresh_review_choices(self, selected_handle=None):
		rows = self.review_context["reviews"]
		self.review_selection.Set(["New review"] + [row["assessment"]["status"] + " — "
			+ row["reviewer"] + " — " + row["review_handle"][-12:] for row in rows])
		selection = next((i + 1 for i, row in enumerate(rows) if row["review_handle"] == selected_handle), 0)
		self.review_selection.SetSelection(selection)
		if selection:
			self._select_review(None)

	def _select_review(self, event):
		self.decision_preview = None
		index = self.review_selection.GetSelection()
		self.saved_review = deepcopy(self.review_context["reviews"][index - 1]) if index > 0 else None
		if self.saved_review is not None:
			row = self.saved_review
			self._populating = True
			try:
				self.decision_fields["case_json"].SetValue(row["contract"]["case_json"])
				self.decision_fields["policy_json"].SetValue(json.dumps(row["contract"]["policy"],
					ensure_ascii=False, indent=2) if row["contract"]["policy"] is not None else "")
				self.decision_fields["author"].SetValue(row["reviewer"])
			finally:
				self._populating = False
			self.decision_summary.SetLabel("Saved contract check: " + row["assessment"]["status"]
				+ "\nLearning review: " + row["learning_verdict"] + "; original answer unchanged.")
			self.decision_details.SetValue(json.dumps(row, ensure_ascii=False, indent=2))
		else:
			self.decision_summary.SetLabel("Preview these fields before saving a new review.")
			self.decision_details.SetValue("")
		self._update_save_button()

	def _input_snapshot(self):
		return (self.decision_fields["case_json"].GetValue(), self.decision_fields["policy_json"].GetValue(),
			self.decision_fields["author"].GetValue().strip())

	def _decision_changed(self, event):
		if not self._populating:
			self.decision_preview = None
			self.saved_review = None
			self.review_selection.SetSelection(0)
			self.decision_summary.SetLabel("Fields changed. Preview the contract check before saving.")
			self.decision_details.SetValue("")
			self._update_save_button()
		event.Skip()

	def _copy_case(self, event):
		selected = self.original_encounter.GetStringSelection()
		if selected:
			self.decision_fields["case_json"].SetValue(selected)
		else:
			self.decision_summary.SetLabel("Select the exact case JSON in Original encounter, then use this button.")

	@staticmethod
	def _policy(text):
		if not text.strip():
			return None
		if len(text.encode("utf-8")) > 65536:
			raise ValueError("Policy JSON exceeds 65536 bytes.")
		def pairs(items):
			result = {}
			for key, value in items:
				if key in result:
					raise ValueError("Policy JSON contains a duplicate key: " + key)
				result[key] = value
			return result
		def constant(value):
			raise ValueError("Policy JSON contains a nonfinite number.")
		return json.loads(text, object_pairs_hook=pairs, parse_constant=constant)

	def _preview_decision(self, event):
		self.decision_preview = None
		self.saved_review = None
		self._update_save_button()
		if self.review_context is None:
			return
		try:
			case_json, policy_json, author = self._input_snapshot()
			if not author:
				raise ValueError("Enter the review author / interpretation curator.")
			result = self.engine.preview_decision_review(self.receipt, case_json=case_json,
				policy=self._policy(policy_json), author=author)
			if result["outcome_handle"] != self.receipt["outcome_handle"]:
				raise ValueError("Preview belongs to another native outcome.")
			self.decision_preview = {"inputs": (case_json, policy_json, author), "result": deepcopy(result)}
			self.review_selection.SetSelection(0)
			self.decision_summary.SetLabel("Preview contract check: " + result["assessment"]["status"]
				+ "\nNot saved. Saving retains an unresolved learning review; original answer stays unchanged.")
			self.decision_details.SetValue(json.dumps(result, ensure_ascii=False, indent=2))
		except Exception as exc:
			self.decision_summary.SetLabel("Check was not previewed or saved: " + str(exc))
			self.decision_details.SetValue("")
		self.decision_summary.Wrap(710)
		self._update_save_button()

	def _update_save_button(self):
		button = self.FindWindowById(wx.ID_OK)
		if button is None:
			return
		structured = self.pages.GetSelection() == 1
		button.SetLabel("Save structured review" if structured else "Save correction")
		button.Enable(not structured or (self.decision_preview is not None
			and self.decision_preview["inputs"] == self._input_snapshot()))
		self.Layout()

	def _save_decision(self):
		preview = self.decision_preview
		if preview is None or preview["inputs"] != self._input_snapshot():
			self._update_save_button()
			return
		try:
			contract = deepcopy(preview["result"]["contract"])
			result = self.engine.review_decision(self.receipt, contract=contract, author=preview["inputs"][2])
		except Exception as exc:
			self.decision_summary.SetLabel("Review save did not complete: " + str(exc))
			self.decision_summary.Wrap(710)
			return
		self.last_saved_review = deepcopy(result)
		self.decision_preview = None
		# Read the actual retained assessment, not a recomputed check or a synthetic
		# review. Its raw answer is held separately in the immutable outcome.
		try:
			context = self.engine.decision_review_context(self.receipt)
			if context["outcome_handle"] != self.receipt["outcome_handle"]:
				raise ValueError("Native history belongs to another outcome.")
			handle = result["retention"]["review_handle"]
			if not any(row["review_handle"] == handle for row in context["reviews"]):
				raise ValueError("The saved review is not available in native history.")
			self.review_context = deepcopy(context)
			self._refresh_review_choices(handle)
		except Exception as exc:
			self.decision_summary.SetLabel("Review was retained, but its history could not be reopened: " + str(exc))
			self.decision_summary.Wrap(710)
			self.decision_details.SetValue(json.dumps({"retained_result": result, "contract": contract},
				ensure_ascii=False, indent=2))
			self._update_save_button()

	def values(self):
		return {key: value.GetValue().strip() for key, value in self.fields.items()}

	def _save(self, event):
		if self.pages.GetSelection() == 1:
			self._save_decision()
			return
		values = self.values()
		if not all(values[key] for key in ("correction", "conditions", "author")):
			wx.MessageBox("Enter the correction, when it applies, and its author.",
				"Correction incomplete", wx.OK | wx.ICON_INFORMATION, self)
			return
		self.EndModal(wx.ID_OK)
