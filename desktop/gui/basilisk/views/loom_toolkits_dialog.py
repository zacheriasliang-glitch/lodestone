"""User-selected local operations with exact preview and retained results."""
import json
import threading
import wx


class LoomToolkitsDialog(wx.Dialog):
	def __init__(self, parent, bridge):
		super().__init__(parent, title="Lodestone toolkits", size=(820, 720))
		self.shutdown_owner = wx.GetTopLevelParent(parent)
		self.bridge = bridge
		self.catalogue = bridge.tool_catalogue()
		self.operations = self.catalogue["operations"]
		self.request = None
		self.worker = None
		self.cancel_event = threading.Event()
		layout = wx.BoxSizer(wx.VERTICAL)
		self.operation = wx.Choice(self, choices=[row["id"] for row in self.operations])
		layout.Add(wx.StaticText(self, label="Choose an operation"), 0, wx.ALL, 8)
		layout.Add(self.operation, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)
		self.description = wx.StaticText(self)
		layout.Add(self.description, 0, wx.EXPAND | wx.ALL, 8)
		layout.Add(wx.StaticText(self, label="Named inputs (JSON; paths and selections must be explicit)"), 0, wx.LEFT, 8)
		self.inputs = wx.TextCtrl(self, style=wx.TE_MULTILINE, size=(-1, 125))
		layout.Add(self.inputs, 0, wx.EXPAND | wx.ALL, 8)
		layout.Add(wx.StaticText(self, label="Workspace for this operation"), 0, wx.LEFT, 8)
		self.workspace = wx.DirPickerCtrl(self, message="Choose an operation workspace")
		layout.Add(self.workspace, 0, wx.EXPAND | wx.ALL, 8)
		tabs = wx.Notebook(self)
		self.preview = wx.TextCtrl(tabs, style=wx.TE_MULTILINE | wx.TE_READONLY)
		tabs.AddPage(self.preview, "Exact request and result")
		inventory = wx.TextCtrl(tabs, value=json.dumps(self.catalogue, ensure_ascii=False, indent=2),
			style=wx.TE_MULTILINE | wx.TE_READONLY)
		tabs.AddPage(inventory, "Programme and toolkit coverage")
		layout.Add(tabs, 1, wx.EXPAND | wx.ALL, 8)
		buttons = wx.BoxSizer(wx.HORIZONTAL)
		self.prepare_btn = wx.Button(self, label="Preview operation")
		self.run_btn = wx.Button(self, label="Run displayed operation")
		self.cancel_btn = wx.Button(self, wx.ID_CANCEL, label="Close")
		for button in (self.prepare_btn, self.run_btn, self.cancel_btn):
			buttons.Add(button, 0, wx.ALL, 5)
		layout.Add(buttons, 0, wx.ALIGN_RIGHT)
		self.SetSizer(layout)
		self.Bind(wx.EVT_CHOICE, self.on_selection, self.operation)
		self.Bind(wx.EVT_TEXT, self.invalidate, self.inputs)
		self.Bind(wx.EVT_DIRPICKER_CHANGED, self.invalidate, self.workspace)
		self.Bind(wx.EVT_BUTTON, self.on_prepare, self.prepare_btn)
		self.Bind(wx.EVT_BUTTON, self.on_run, self.run_btn)
		self.Bind(wx.EVT_BUTTON, self.on_close, self.cancel_btn)
		self.Bind(wx.EVT_CLOSE, self.on_close)
		self.timer = wx.Timer(self)
		self.Bind(wx.EVT_TIMER, self.on_poll, self.timer)
		self.operation.SetSelection(0)
		self.on_selection(None)

	def invalidate(self, event):
		self.request = None
		self.run_btn.Enable(False)

	def on_selection(self, event):
		item = self.operations[self.operation.GetSelection()]
		self.description.SetLabel(item["description"] + "\nEffects: " + str(item["effects"]))
		self.description.Wrap(770)
		self.inputs.SetValue(json.dumps({key: [] if spec["type"] in ("strings", "sources") else ""
			for key, spec in item["inputs"].items()}, indent=2))
		self.invalidate(None)
		self.Layout()

	def on_prepare(self, event):
		self.invalidate(None)
		try:
			self.request = self.bridge.prepare_tool(self.operations[self.operation.GetSelection()]["id"],
				json.loads(self.inputs.GetValue()), self.workspace.GetPath())
			self.preview.SetValue(json.dumps(self.request, ensure_ascii=False, indent=2))
			self.run_btn.Enable(True)
		except Exception as exc:
			self.preview.SetValue("Operation was not prepared: " + str(exc))

	def on_run(self, event):
		if not self.request or self.worker is not None:
			return
		request = self.request
		self.cancel_event.clear()
		self.result = None
		for control in (self.operation, self.inputs, self.workspace, self.prepare_btn, self.run_btn):
			control.Enable(False)
		self.cancel_btn.SetLabel("Stop operation")
		def execute():
			try:
				self.result = self.bridge.run_tool(request, approved_digest=request["digest"], cancel_event=self.cancel_event)
			except Exception as exc:
				self.result = {"status": "NOT_COMPLETED", "error": str(exc)}
		self.worker = threading.Thread(target=execute, daemon=False)
		self.worker.start()
		self.timer.Start(100)

	def on_poll(self, event):
		if self.worker is None or self.worker.is_alive():
			return
		self.worker.join()
		self.worker = None
		self.timer.Stop()
		self.preview.AppendText("\n\nActual result:\n" + json.dumps(self.result, ensure_ascii=False, indent=2))
		self.invalidate(None)
		for control in (self.operation, self.inputs, self.workspace, self.prepare_btn):
			control.Enable(True)
		self.cancel_btn.SetLabel("Close")

	def request_shutdown(self):
		"""Cancel and collect a finished result before the owning frame exits."""
		if getattr(self, "_shutdown_closing", False):
			return True  # Let ShowModal and its context manager finish first.
		if self.worker is not None:
			self.cancel_event.set()
			self.cancel_btn.SetLabel("Stopping; waiting for receipt")
			self.on_poll(None)
		if self.worker is not None:
			return True
		if self.IsModal():
			self._shutdown_closing = True
			self.timer.Stop()
			self.EndModal(wx.ID_CANCEL)
			return True
		return False

	def on_close(self, event):
		if self.worker is not None:
			self.cancel_event.set()
			self.cancel_btn.SetLabel("Stopping; waiting for receipt")
			if isinstance(event, wx.CloseEvent) and event.CanVeto():
				event.Veto()
			return
		self.timer.Stop()
		self.EndModal(wx.ID_CANCEL)
