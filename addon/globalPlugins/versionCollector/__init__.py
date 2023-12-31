# versionCollector/__init__.py
# A part of the Version Collector NVDA add-on.
# Copyright (C) 2023, Luke Davis, Open Source Systems, Ltd. <XLTechie@newanswertech.com>, all rights reserved.
# This file is covered by the GNU General Public License version 2.
# See the file COPYING for more details.

import wx
from datetime import datetime
from dataclasses import dataclass, field
from typing import Callable, Optional, List, Dict, Any

import globalPluginHandler
import addonHandler
import api
import ui
from logHandler import log
from NVDAObjects import NVDAObject
from scriptHandler import script, getLastScriptRepeatCount
from globalCommands import SCRCAT_TOOLS
from appModuleHandler import post_appSwitch
from core import postNvdaStartup, callLater as core_callLater

#from . import toolsGUI


@dataclass(repr=False, eq=True)
class _AppData:
	"""Properties representing a piece of software by its metadata.
	Metadata includes name, version, bitness, and last seen timestamp.
	Because we also track NVDA add-ons, various other metadata is stored.
	"""
	name: str
	version: str = None
	is64bit: Optional[bool] = None
	lastSeen: datetime.timestamp = field(compare=False, default_factory=lambda : datetime.timestamp(datetime.now()))
	firstSeen: datetime.timestamp = field(compare=False, default_factory=lambda : datetime.timestamp(datetime.now()))
	isAddon: bool = False  # Set True if this record represents an NVDA add-on
	extra: Optional[Any] = field(compare=False, default=None)

	@property
	def isAddonEnabled(self) -> bool:
		"""A property that checks whether an NVDA add-on is enabled, and
		returns the status. Raises ValueError if not an add-on.
		"""
		if not self.isAddon:
			raise ValueError(f"Tried to check running/enablement status for an app that is not an add-on ({self.name}).")
		else:
			return self.extra["enabled"]


_appDataCache: List[_AppData] = []
"""The main in-memory listing of per-app metadata"""

_dirtyCache: bool = False
"""Represents whether the LHS of the cache needs to be updated on disk. Forces an immediate cache save."""

_dirtyDates: bool = False
"""Represents whether the RHS of the cache needs to be updated on disk. Doesn't force a cache save cycle."""

def isCached(app: _AppData) -> bool:
	if getCacheIndexOf(app) < 0:
		return False
	else:
		return True

def getCacheIndexOf(app: _AppData) -> int:
	try:
		ind = _appDataCache.index(app)
	except ValueError:
		ind = -1
	return ind

def updateLastDate(app: _AppData, index: int) -> None:
	global _dirtyDates, _appDataCache
	if index < 0:  # We weren't given an index, yet some how the caller knows app is cached
		index = getCacheIndexOf(app)
		if index < 0:  # Something weird is going on
			raise RuntimeError(f"Was asked to update date for an item not in the cache! {app}")
	_appDataCache[index].lastSeen = app.lastSeen
	if (
		_appDataCache[index].firstSeen == None
		and app.firstSeen == None
	):
		_appDataCache[index].firstSeen = datetime.timestamp(datetime.now())
	_dirtyDates = True

def addToCache(app: _AppData, checked: bool = False) -> None:
	global _dirtyCache, _appDataCache
	if not checked:
		if isCached(app):
			raise RuntimeError(f"Tried to add an already cached app to the cache! {app.name}")
	# Adding . . .
	_appDataCache.append(app)
	_dirtyCache = True
	log.debug(f'Added an {"add-on" if app.isAddon else "app"} to the cache: {app.name}.')

def _showState(message: Optional[str] = None) -> None:
	"""A debugging function which writes everything the add-on knows to a browseableMessage.

	@param message An optional message to put at the top.
	"""
	#return  # Comment to disable this function
	ui.browseableMessage("\n".join((
		"" if message is None else message,
		"The dates are " + ("" if _dirtyDates else "not ") + "dirty.",
		"The cache is " + ("" if _dirtyCache else "not ") + "dirty.",
		"\nThe cache contains:\n",
		"\n".join(
			f"{app.name}:\nis64bit: {app.is64bit}\nVersion: {app.version}\nIs Addon: {app.isAddon}\n"
			f"First seen: {app.firstSeen}\nLast seen: {app.lastSeen}"
			for app in _appDataCache
		)
	)), title="Cache report")

class GlobalPlugin(globalPluginHandler.GlobalPlugin):

	def __init__(self, *args, **kwargs):
		super().__init__(*args, **kwargs)
		self.currentApp: Optional[_AppData] = None
		# Run our handler whenever the application changes
		post_appSwitch.register(self.onAppSwitch)
		# Seed the pond
		postNvdaStartup.register(self.collectInitialApp)
		# Become aware of all NVDA add-ons
		postNvdaStartup.register(self.retrieveInstalledAddons)

	def terminate(self) -> None:
		# Unregister the extensionPoints
		post_appSwitch.unregister(self.onAppSwitch)
		postNvdaStartup.unregister(self.onAppSwitch)
		postNvdaStartup.unregister(self.retrieveInstalledAddons)
		super().terminate()

	@script(
		gesture="kb:NVDA+control+shift+v",
		# Translators: An input help description of the report view and copy script.
		description=_("Copy the Version Collector report to the clipboard. Press twice to view it instead."),
		category=SCRCAT_TOOLS
	)
	def script_versionCollectorReport(self, gesture) -> None:
		presses = getLastScriptRepeatCount()
		if presses == 0:  # Pressed once
			# Use wx.CallLater to delay firing of the first potential action long enough to determine
			# whether the second action is what is actually being requested.
			# If so, the wx.Timer is cancelled, but left available for later.
			try:
				self.firstScriptActionTimer.Start(505)
			except AttributeError:
				self.firstScriptActionTimer = wx.CallLater(505, self.copyTextReport)
		elif presses == 1:  # Pressed twice
			self.firstScriptActionTimer.Stop()
			self.showHTMLReport()
		else:  # Pressed more than twice. Do nothing
			self.firstScriptActionTimer.Stop()
			return

	def onAppSwitch(self) -> None:
		"""Called as a registered extensionPoint, whenever appModuleHandler detects an application switch."""
		obj = api.getForegroundObject()
		# Handle a strange case. This is mentioned in core code. May not be complete solution. FixMe
		if obj.processHandle == 0:
			log.debug("\t\tRan into the obj.processHandle == 0 situation for {obj}--trying to use last child.")
			obj = obj.simpleLastChild
		try:
			currentApp: _AppData = self.normalizeAppInfo(
				getattr(obj.appModule, "appName", None),
				getattr(obj.appModule, "productName", None),
				getattr(obj.appModule, "productVersion", None),
				getattr(obj.appModule, "is64BitProcess", None)
			)
		except Exception as e:
			log.debug(f"Couldn't get module info for object {obj} ({e}).")
			return
		# If the current app is not the same as the previously known app, we have a new app
		if currentApp != self.currentApp:
			self.currentApp = currentApp
			self.addToCacheOrUpdateDate(currentApp)

	def collectInitialApp(self) -> None:
		"""Called as a registered extensionPoint, when NVDA first finishes loading."""
		log.debug("Collecting the initial app after a delay.")
		core_callLater(1000, self.onAppSwitch)
		#postNvdaStartup.unregister(self.collectInitialApp)

	def addToCacheOrUpdateDate(self, subject: _AppData) -> None:
		ind = getCacheIndexOf(subject)
		if ind < 0:
			addToCache(subject, True)
		else:  # It's in the cache already, update the date only
			updateLastDate(subject, ind)

	@staticmethod
	def normalizeAppInfo(shortName: str, longName: str, version: str, is64bit: bool) -> _AppData:
		"""Returns an AppData representation of the passed app metadata.
		It converts short and long names into a single (hopefully) non-repetitive name.
		"""
		if longName is None or longName == "":  # No longName
			# If neither kind of name is set, this is a bad conversion, and we fail
			if shortName is None or shortName == "":
				raise ValueError("Names not set: probably not a module.")
			else:  # We can only go with the shortName
				appName = shortName.title()
		else:  # We have a longName; assume we have a shortName as well
			# If the shortName appears inside the longName, we can throw away the redundant shortName
			if longName.lower().find(shortName.lower()) >= 0:
				appName = longName
			else:  # We need both, such as for Windows Notepad
				appName = f"{shortName.title()} ({longName})"
		# Did we get a version?
		if version is not None and version != "":
			appVersion = version
		else:
			appVersion = None
		return _AppData(
			name=appName, version=appVersion, is64bit=is64bit
		)

	def retrieveInstalledAddons(self) -> None:
		"""Processes the currently installed NVDA add-ons as if they were apps.
		"""
		for addon in addonHandler.getAvailableAddons():
			self.addToCacheOrUpdateDate(_AppData(
				name=addon.manifest["summary"],
				version=addon.version, isAddon=True, is64bit=False,
				extra={"name": addon.name, "author": addon.manifest["author"], "enabled": not addon.isDisabled}
			))

	@staticmethod
	def createStructuredList(
			func: Callable,
			useHTML: bool = False,
			*,
			hideFields: tuple = (),
			transformFields: Dict[str, Callable] = {}
	) -> str:
		"""Takes a generator of _AppData records, and returns their data in a structured way."""
		if useHTML:
			lineStart = "<tr><TD>&#8611;</TD>"
			fieldStart = "<td>"
			fieldEnd = "</td>"
			lineEnd = "</tr>\n"
		else:
			lineStart = "- "
			fieldStart = ""
			fieldEnd = "\t"
			lineEnd = "\n"
		returnable: str = ""
		for appData in func():
			line: str = ""
			for property in ("name", "version", "is64bit", "isAddon", "isAddonEnabled", "extra"):
				fields: list = []
				if property not in hideFields:
					if property in transformFields:
						transformed = (transformFields[property])(getattr(appData, property))
						if isinstance(transformed, list):
							fields.extend(transformed)
						else:
							fields.append(transformed)
					else:
						fields.append(getattr(appData, property))
				for field in fields:
					line += f"{fieldStart}{field}{fieldEnd}"
			if line != "":
				returnable += f"{lineStart}{line}{lineEnd}"
		return returnable

	@staticmethod
	def generateAppsOnly() -> str:
		for app in _appDataCache:
			if not app.isAddon:
				yield app
		else:
			return None

	@staticmethod
	def generateAddonsOnly() -> str:
		for app in _appDataCache:
			if app.isAddon:
				yield app
		else:
			return None

	def getStructuredAppList(self, useHTML=False) -> str:
		return self.createStructuredList(
			self.generateAppsOnly, useHTML, hideFields=("isAddon", "isAddonEnabled", "extra"),
			transformFields={"is64bit": lambda x: "[64 bit]" if x else "[32 bit]"}
		)

	def getStructuredAddonList(self, useHTML=False) -> str:
		return self.createStructuredList(
			self.generateAddonsOnly, useHTML, hideFields=("isAddon", "is64bit"),
			transformFields={
				"extra": lambda x: [ x["author"], f'({x["name"]})' ],
				"isAddonEnabled": lambda x: "[enabled]" if x else "[disabled]"
			}
		)

	def showHTMLReport(self) -> None:
		output = """<style>
		table {
		table-layout: auto;
		width: 100%;
		border-collapse: separate;
		border-spacing: 80px 0;
		border-left: 100px solid transparent;
		}
		td, th{
		padding: 10px 0;
		}
		tr td:first-child {padding-left:0px;}
		tr td:last-child { margin-right: 0; }
		</style>
		"""
		# Translators: Suggestions on how a user can interact with the Version Report.
		output += "<p>" + _("Use shift+arrow keys to select, ctrl+c to copy to clipboard.")
		output += """</p>\n<br><h1>Detected Applications:</h1>\n<table>
		<tr><TH>&nbsp;</TH> <TH>NAME</TH> <TH>VERSION</TH> <TH>BITNESS</TH> </tr>
		"""
		output += self.getStructuredAppList(True)
		output += """</table><br>
		<h1>Detected NVDA Add-ons:</h1>\n<table>
		<tr><TH>&nbsp;</TH><TH>NAME</TH> <TH>VERSION</TH> <TH>STATUS</TH> <TH>AUTHOR/PUBLISHER</TH> <TH>Add-on ID</TH></tr>
		"""
		output += self.getStructuredAddonList(True)
		output += "</table><br>\n<p>"
		# Translators: Instruction to press escape to leave the report window.
		output += _("Press escape when done.") + "</p>"
		# Translators: Title of the Application Versions Report when shown in a webpage style.
		ui.browseableMessage(output, _("Detected apps, add-ons, and versions"), True)

	def copyTextReport(self) -> None:
		output = "Applications:\n" + self.getStructuredAppList()
		output += "\nNVDA Add-ons:\n" + self.getStructuredAddonList()
		api.copyToClip(output)
		# Translators: Message spoken when the text report has been copied to the clipboard.
		ui.message(_("Application version report copied."))
