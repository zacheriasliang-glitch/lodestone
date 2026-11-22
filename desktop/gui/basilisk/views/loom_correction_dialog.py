"""Capture a user's conditional correction for one retained Loom encounter."""
import wx


class LoomCorrectionDialog(wx.Dialog):
	def __init__(self, parent, receipt):
		super().__init__(parent, title="Correct the last Loom encounter", size=(640, 590))
		sizer = wx.BoxSizer(wx.VERTICAL)
		purpose = wx.StaticText(self, label="Purpose: " + receipt["purpose"])
		purpose.Wrap(590)
		sizer.Add(purpose, 0, wx.ALL | wx.EXPAND, 10)
		self.fields = {}
		for key, label, height, default in (
			("correction", "What should Loom learn or change?", 100, ""),
			("conditions", "When does this correction apply?", 55, ""),
			("limits", "When should this be reconsidered? (optional)", 55, ""),
			("observation", "What actually happened? (optional)", 55, ""),
			("author", "Author", 28, "Local user"),
		):
			sizer.Add(wx.StaticText(self, label=label), 0, wx.LEFT | wx.RIGHT | wx.TOP, 10)
			control = wx.TextCtrl(self, value=default, size=(-1, height),
				style=wx.TE_MULTILINE if height > 28 else 0)
			self.fields[key] = control
			sizer.Add(control, 0, wx.LEFT | wx.RIGHT | wx.EXPAND, 10)
		sizer.Add(self.CreateButtonSizer(wx.OK | wx.CANCEL), 0, wx.ALL | wx.EXPAND, 10)
		self.SetSizer(sizer)
		self.FindWindowById(wx.ID_OK).SetLabel("Save correction")
		self.Bind(wx.EVT_BUTTON, self._save, id=wx.ID_OK)

	def values(self):
		return {key: value.GetValue().strip() for key, value in self.fields.items()}

	def _save(self, event):
		values = self.values()
		if not all(values[key] for key in ("correction", "conditions", "author")):
			wx.MessageBox("Enter the correction, when it applies, and its author.",
				"Correction incomplete", wx.OK | wx.ICON_INFORMATION, self)
			return
		self.EndModal(wx.ID_OK)
