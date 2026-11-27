"""Module for the about dialog of the basiliskLLM application.

This module provides functionality to display application information, including
version, description, website, license, developers, and translators using wx.adv.AboutDialog.
"""

import sys
import json
from pathlib import Path

import wx.adv

from basilisk.consts import (
	APP_AUTHORS,
	APP_NAME,
	APP_SOURCE_URL,
	APP_TRANSLATORS,
)

if getattr(sys, "frozen", False):
	from BUILD_CONSTANTS import BUILD_RELEASE_STRING  # type: ignore

	app_version = BUILD_RELEASE_STRING
else:
	# The installer verifies this payload manifest before launching the GUI.
	# Source checkouts intentionally have no packaged release identity or Git
	# build-tool dependency in their runtime environment.
	manifest_path = Path(__file__).resolve().parents[3] / "MANIFEST.json"
	if manifest_path.is_file():
		manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
		if (manifest.get("schema") != "lodestone-desktop-payload/v1"
			or not isinstance(manifest.get("version"), str)
			or not manifest["version"].strip()):
			raise ValueError("Invalid desktop release version manifest")
		app_version = manifest["version"]
	else:
		app_version = "Source checkout (unpackaged)"


def display_about_dialog(parent: wx.Window):
	"""Display the about dialog of the application.

	This function uses wx.adv.AboutDialogInfo to display application details including
	version, description, website, license, developers, and translators.

	Args:
		parent: The parent window for the dialog.
	"""
	info = wx.adv.AboutDialogInfo()
	info.SetName(APP_NAME)
	info.SetVersion(app_version)
	info.SetDescription(_("Where LLMs Unite"))
	info.SetWebSite(APP_SOURCE_URL)
	info.SetLicense(_("GPLV2"))
	info.SetDevelopers(APP_AUTHORS)
	info.SetTranslators(APP_TRANSLATORS)
	wx.adv.AboutBox(info)
