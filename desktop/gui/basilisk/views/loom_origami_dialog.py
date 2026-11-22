"""Explicit source selection and human curation; never automatic model execution."""
import json
import wx


class LoomOrigamiDialog(wx.Dialog):

	def __init__(self, parent, proposal):
		super().__init__(parent, title="Preserve this encounter with Origami", size=(760, 700))
		self.proposal = proposal
		outer = wx.BoxSizer(wx.VERTICAL)
		note = wx.StaticText(self, label="Choose source support and curate the relationship. The original Loom answer is advice. "
			"A saved answer remains provisional until independently reviewed. Choose Unresolved if a live alternative could change the answer.")
		note.Wrap(710)
		outer.Add(note, 0, wx.ALL | wx.EXPAND, 10)
		tabs = wx.Notebook(self)
		advice = wx.Panel(tabs)
		layout = wx.BoxSizer(wx.VERTICAL)
		original = proposal["original_outcome"]
		advice_text = ("Original status: " + original["status"] + "\nPurpose: " + proposal["purpose"]
			+ "\n\nOriginal contribution:\n" + (original.get("response") or "(No response)")
			+ "\n\nComplete learning advice, prior outcomes and corrections:\n"
			+ json.dumps(proposal["learning_advice"], ensure_ascii=False, indent=2))
		layout.Add(wx.TextCtrl(advice, value=advice_text, style=wx.TE_MULTILINE | wx.TE_READONLY), 1, wx.EXPAND | wx.ALL, 8)
		advice.SetSizer(layout)
		tabs.AddPage(advice, "Contribution and corrections")
		sources = wx.Panel(tabs)
		layout = wx.BoxSizer(wx.VERTICAL)
		label = f"Select supporting passages. {proposal['omitted_passages']} passages omitted from this bounded preview."
		layout.Add(wx.StaticText(sources, label=label), 0, wx.ALL, 8)
		self.support = wx.CheckListBox(sources, choices=[f"{row['source']} : {row['start_line']}–{row['end_line']}"
			for row in proposal["evidence"]], size=(-1, 170))
		layout.Add(self.support, 0, wx.EXPAND | wx.ALL, 8)
		self.source_text = wx.TextCtrl(sources, style=wx.TE_MULTILINE | wx.TE_READONLY)
		layout.Add(self.source_text, 1, wx.EXPAND | wx.ALL, 8)
		self.support.Bind(wx.EVT_LISTBOX, self.on_source)
		self.support.SetSelection(0)
		self.on_source(None)
		sources.SetSizer(layout)
		tabs.AddPage(sources, "Source support")
		form = wx.ScrolledWindow(tabs)
		form.SetScrollRate(0, 12)
		layout = wx.BoxSizer(wx.VERTICAL)
		self.fields = {}
		for key, label, height, default in (
			("curator", "Your name as curator", 28, ""),
			("checker", "Intended independent checker (review remains pending)", 28, ""),
			("answer", "Your curated answer or unresolved proposal", 90, ""),
			("relationship", "What relationship should a later reader retain?", 55, ""),
			("conditions", "Under which conditions does it apply?", 55, ""),
			("action", "What action follows under those conditions?", 55, ""),
			("effect", "Actual result, or explicitly label a predicted / unobserved effect", 55, ""),
			("uncertainty", "What remains uncertain?", 55, ""),
			("limits", "Scope, source qualifications and when to reconsider", 55, ""),
		):
			layout.Add(wx.StaticText(form, label=label), 0, wx.LEFT | wx.TOP, 8)
			self.fields[key] = wx.TextCtrl(form, value=default, size=(-1, height),
				style=wx.TE_MULTILINE if height > 28 else 0)
			layout.Add(self.fields[key], 0, wx.LEFT | wx.RIGHT | wx.EXPAND, 8)
		layout.Add(wx.StaticText(form, label="Curation status"), 0, wx.LEFT | wx.TOP, 8)
		self.decision = wx.Choice(form, choices=["Unresolved", "Provisional answer supported by selected passages"])
		self.decision.SetSelection(0)
		layout.Add(self.decision, 0, wx.EXPAND | wx.ALL, 8)
		form.SetSizer(layout)
		tabs.AddPage(form, "Curate")
		outer.Add(tabs, 1, wx.EXPAND | wx.ALL, 8)
		outer.Add(self.CreateButtonSizer(wx.OK | wx.CANCEL), 0, wx.ALL | wx.EXPAND, 10)
		self.SetSizer(outer)
		self.FindWindowById(wx.ID_OK).SetLabel("Submit to Origami")
		self.Bind(wx.EVT_BUTTON, self.on_submit, id=wx.ID_OK)

	def on_source(self, event):
		index = self.support.GetSelection()
		if index != wx.NOT_FOUND:
			self.source_text.SetValue(self.proposal["evidence"][index]["text"])

	def values(self):
		return {**{key: control.GetValue().strip() for key, control in self.fields.items()},
			"decision": "ANSWER" if self.decision.GetSelection() == 1 else "UNRESOLVED",
			"support_ids": [self.proposal["evidence"][index]["id"] for index in self.support.GetCheckedItems()]}

	def on_submit(self, event):
		values = self.values()
		if not all(values.values()) or values["curator"] == values["checker"]:
			wx.MessageBox("Complete the curation, select source passages, and name a separate intended checker.",
				"Curation incomplete", wx.OK | wx.ICON_INFORMATION, self)
			return
		self.EndModal(wx.ID_OK)
