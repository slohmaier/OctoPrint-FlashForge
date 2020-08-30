# coding=utf-8
from __future__ import absolute_import

import threading
import usb1
import re
import octoprint.plugin
from octoprint.settings import default_settings
from octoprint.util import dict_merge
from . import flashforge

'''
Special case support:

G91 (relative positioning) appears not to be supported by Finder 2, Guider 2 (F2G2). Made into printer profile option
(ff.noG91) so users can enable as appropriate. Flag is also used to special case other F2G2 issues such as:
- F2G2 does not appear to support "G28 X Y"
'''

class FlashForgePlugin(octoprint.plugin.SettingsPlugin,
                       octoprint.plugin.AssetPlugin,
                       octoprint.plugin.TemplatePlugin):


	VENDOR_IDS = {0x0315: "PowerSpec", 0x2a89: "Dremel", 0x2b71: "FlashForge"}
	PRINTER_IDS = {
		"PowerSpec": {0x0001: "Ultra 3DPrinter (C)"},
		"Dremel": {0x8889: "Dremel IdeaBuilder 3D20", 0x888d: "Dremel IdeaBuilder 3D45"},
		"FlashForge": {0x0001: "Dreamer", 0x0002: "Finder v1",  0x0004: "Guider II", 0x0005: "Inventor",
					   0x0007: "Finder v2", 0x0009: "Guider IIs", 0x000A: "Dreamer NX",
					   0x00e7: "Creator Max", 0x00ee: "Finder v2.12",
					   0x00f6: "PowerSpec Ultra 3DPrinter (B)", 0x00ff: "PowerSpec Ultra 3DPrinter (A)"}}
	FILE_PACKET_SIZE = 1024


	def __init__(self):
		import logging

		self._logger = logging.getLogger("octoprint.plugins.flashforge")
		self._logger.debug("__init__")
		self._comm = None
		self._serial_obj = None
		self._currentFile = None
		self._upload_percent = 0
		self._printers = {}
		# FlashForge friendly default connection settings
		self._conn_settings = {
			'firmwareDetection': False,				# do not try to auto detect firmware
			'sdAlwaysAvailable': True,				# FF printers always(?) have the internal SD card available
			'neverSendChecksum': True,				# FF protocol does not use command checksums
			'helloCommand': "M601 S0",				# FF hello command and set communication to USB
			'abortHeatupOnCancel': False			# prevent sending of M108 command which doesn't work
		}
		self._feature_settings = {
			'autoUppercaseBlacklist': ['M146']		# LED control requires lowercase r,g,b
		}
		default_settings["serial"] = dict_merge(default_settings["serial"], self._conn_settings)
		default_settings["feature"] = dict_merge(default_settings["feature"], self._feature_settings)


	##~~ SettingsPlugin mixin
	def get_settings_defaults(self):
		# add default value ff.noG91 to printer profiles or the setting won't get saved by OctoPrint
		profiles = self._printer_profile_manager.get_all()
		self._printer_profile_manager.default["ff"] = dict(noG91=False)
		for k, profile in profiles.items():
			profile = dict_merge(self._printer_profile_manager.default, profile)
			self._printer_profile_manager.save(profile, True)

		# plugin default settings here
		return dict(
			ledStatus=1,
			ledColor=[255, 255, 255]
		)


	##~~ AssetPlugin mixin
	def get_assets(self):
		# Define your plugin's asset files to automatically include in the
		# core UI here.
		return dict(
			js=["js/flashforge.js", "js/color-picker.min.js"],
			css=["css/color-picker.min.css"]
		)


	##~~ Softwareupdate hook
	def get_update_information(self):
		# Define the configuration for your plugin to use with the Software Update
		# Plugin here. See https://github.com/foosel/OctoPrint/wiki/Plugin:-Software-Update
		# for details.
		return dict(
			flashforge=dict(
				displayName="FlashForge Plugin",
				displayVersion=self._plugin_version,

				# version check: github repository
				type="github_release",
				user="Mrnt",
				repo="OctoPrint-FlashForge",
				current=self._plugin_version,

				# update method: pip
				pip="https://github.com/Mrnt/OctoPrint-FlashForge/archive/{target_version}.zip"
			)
		)


	# Look for a supported printer
	def detect_printer(self):
		self._logger.debug("detect_printer()")
		if self._serial_obj:
			return self._printers

		self._printers = {}
		with usb1.USBContext() as usbcontext:
			for device in usbcontext.getDeviceIterator(skip_on_error=True):
				vendor_id = device.getVendorID()
				device_id = device.getProductID()
				try:
					device_name = device.getProduct()
				except:
					device_name = 'unknown'
				self._logger.debug("Found device '{}' with Vendor ID: {:#06X}, USB ID: {:#06X}".format(device_name, vendor_id, device_id))

				if vendor_id in self.VENDOR_IDS:
					vendor_name = self.VENDOR_IDS[vendor_id]
					self._logger.info("Found a {} {}".format(vendor_name, self.PRINTER_IDS[vendor_name][device_id]))
					self._printers[device_name] = {'vid': vendor_id, 'vname': vendor_name, 'did': device_id}
					# get USB interface details to diagnose connectivity issues
					for configuration in device.iterConfigurations():
						for interface in configuration:
							for setting in interface:
								self._logger.debug(
									" setting number: 0x{:02x}, class: 0x{:02x}, subclass: 0x{:02x}, protocol: 0x{:02x}, #endpoints: {}, descriptor: {}".format(
									setting.getNumber(), setting.getClass(), setting.getSubClass(),
									setting.getProtocol(), setting.getNumEndpoints(), setting.getDescriptor()))
								for endpoint in setting:
									self._logger.debug(
										"  endpoint address: 0x{:02x}, attributes: 0x{:02x}, max packet size: {}".format(
										endpoint.getAddress(), endpoint.getAttributes(),
										endpoint.getMaxPacketSize()))


	def printer_factory(self, comm, port, baudrate, read_timeout, *args, **kwargs):
		""" OctoPrint hook - Called when creating printer connection

			Test for presence of a supported printer and then try to connect
		"""
		if port not in self._printers:
			# requested port not in our list
			return None

		self._comm = comm
		serial_obj = flashforge.FlashForge(self, comm, self._printers[port]['vid'], self._printers[port]['did'], read_timeout=float(read_timeout))
		return serial_obj


	def get_additional_port_names(self, *args, **kwargs):
		""" OctoPrint hook - Called when populating Serial Port list
		"""
		self.detect_printer()
		printers = self._printers.keys()
		return printers


	def get_extension_tree(self, *args, **kwargs):
		""" OctoPrint hook - Return supported file extensions for SD upload

			Note not called when printer connects, only when starting up and when the printer disconnects
		"""
		self._logger.debug("get_extension_tree()")
		return dict(
			machinecode=dict(
				g3drem=["g3drem"],	# Dremel
				gx=["gx"]			# Every other FlashForge based printer
			)
		)


	def on_connect(self, serial_obj):
		self._logger.debug("on_connect()")
		self._serial_obj = serial_obj

		thread = threading.Thread(target=serial_obj.keep_alive, name="FlashForge.Keep_Alive")
		thread.daemon = True
		thread.start()


	def on_disconnect(self):
		self._logger.debug("on_disconnect()")
		self._serial_obj = None


	# Flag F2G2
	def G91_disabled(self):
		profile = self._printer_profile_manager.get_current_or_default()
		return "ff" in profile and "noG91" in profile["ff"] and profile["ff"]["noG91"]


	# Called when gcode commands are being placed in the queue by OctoPrint:
	# Mostly important for control panel or translating and printing non FlashPrint file directly from OctoPrint
	def rewrite_gcode(self, comm_instance, phase, cmd, cmd_type, gcode, *args, **kwargs):
		if self._serial_obj:

			if not re.match(r'^[GMT]\d+', cmd):
				# most likely part of the header in a .gx FlashPrint file
				self._logger.debug("rewrite_gcode(): unrecognized command")
				return []

			self._logger.debug("rewrite_gcode(): gcode:{}, cmd:{}".format(gcode, cmd))

			#TODO: detect printer state earlier in connection process and dont send M146, etc if the printer
			# is already busy when we connect
			#TODO: filter M146 and other commands? when printing from SD because they cause comms to hang

			# allow a very limited set of commands while printing from SD to minimize problems...
			if self._serial_obj.is_sd_printing() and gcode not in ["M24", "M25", "M26", "M27", "M104", "M105", "M112", "M114", "M117", "M400"]:
				cmd = []

			# homing
			elif gcode == "G28":
				cmd = cmd.replace('0', '')
				if self.G91_disabled() and cmd == "G28 X Y":
					# F2G2: does not support "G28 X Y"?
					# F2 needs first G28 to finish or it will ignore the second one
					cmd = ["G28 X", "G28 Y"]

			# relative positioning
			elif gcode == "G91":
				if self.G91_disabled():
					# F2G2: try to convert relative positioning to absolute so add in some commands
					self._serial_obj.disable_G91(True)
					cmd = [("G91", cmd_type), "M114"]
				else:
					self._serial_obj.disable_G91(False)

			# M20 list SD card, M21 init SD card - do not do if we are busy, seems to cause issues
			elif (gcode == "M20" or gcode == "M21") and not self._serial_obj.is_ready():
				cmd = []

			# M25 = pause
			# there may be an issue on some printers issuing a pause before a cancel, however OctoPrint needs to think
			# it sent the pause before it will generate a M26 (cancel)...

			# M26 is sent by OctoPrint during SD prints:
			# M26 in Marlin = set SD card position : FlashForge = cancel
			elif gcode == "M26":
				# M26 S0 generated during OctoPrint cancel - use it to send cancel
				if cmd == "M26 S0" and comm_instance.isCancelling():
					cmd = [("M26", cmd_type)]
				else:
					cmd = []

			# M82 in Marlin = extruder abs positioning : FlashForge = undefined?
			elif gcode == "M82":
				cmd = []

			# M83 in Marlin = extruder rel positioning : FlashForge = undefined?
			elif gcode == "M83":
				cmd = []

			# M84 by default sent when OctoPrint cancelling print
			# M84 in Marlin = disable steppers : M18 is FlashForge equivalent
			elif gcode == "M84":
				cmd = ["M18"]

			# M106 S0 is sent by OctoPrint control panel:
			# M106 S0 in Marlin = fan off : M107 is FlashForge equivalent
			elif gcode == "M106":
				if "S0" in cmd:
					cmd = ["M107"]

			# M108 is sent by OctoPrint during SD cancel if abortHeatupOnCancel is set:
			# M108 in Marlin = stop heat wait & continue : FlashForge M108 Tx = change toolhead (no equivalent?),
			# drop if this is the command
			elif cmd == "M108":
				cmd = []

			# M109 in Marlin = wait for extruder temp : M6 in FlashForge (this may need to be moved to the write() method)
			elif gcode == "M109":
				cmd = [cmd.replace("M109", "M6")]

			# M110 is sent by OctoPrint as default hello but also when connected:
			# M110 Set line number/hello in Marlin : FlashForge uses M601 S0 to take control via USB
			elif gcode == "M110":
				cmd = []

			# M119 get status we generate automatically so skip this
			elif gcode == "M119":
				cmd = []

			# M146 = set LED colors: do not send while printing from SD (does not work, may cause issues)
			elif gcode == "M146" and self._serial_obj.is_printing():
				cmd = []

			# M190 in Marlin = wait for bed temp : M7 in FlashForge
			elif gcode == "M190":
				cmd = [cmd.replace("M190", "M7")]

			# Tx = select extruder : FlashForge uses M108
			elif gcode == "T":
				cmd = [("M108 %s" % cmd, cmd_type)]

			if cmd == []:
				self._logger.debug("rewrite_gcode(): dropping command")

		return cmd


	# Uploading files directly to internal SD card
	def upload_to_sd(self, printer, filename, path, sd_upload_started, sd_upload_succeeded, sd_upload_failed, *args,
						 **kwargs):

		if not self._serial_obj:
			return

		def process_upload():
			error = ""

			# rewrite:
			self._upload_percent = 0
			chunk_start_index = 0

			self._serial_obj.makeexclusive(True)
			error = "could not start tx"

			# make sure heaters are off
			self._serial_obj.sendcommand(b"M104 S0 T0")
			self._serial_obj.sendcommand(b"M104 S0 T1")
			self._serial_obj.sendcommand(b"M140 S0")

			ok, answer = self._serial_obj.sendcommand(b"M28 %d 0:/user/%s" % (file_size, remote_name.encode()), 5000)
			if not ok:
				error = "file transfer not started {}".format(answer)
			else:
				self._logger.debug("M28 file tx started")
				error = ""

			try:
				while chunk_start_index < file_size:
					chunk_end_index = min(chunk_start_index + self.FILE_PACKET_SIZE, file_size)
					chunk = bgcode[chunk_start_index:chunk_end_index]
					if not chunk:
						error = "unexpected eof"
						break

					if self._serial_obj.writeraw(chunk, False):
						upload_percent = 100.0 * chunk_end_index / file_size
						self.upload_percent = int(upload_percent)
						self._logger.debug("Sent: %.2f%% %d/%d" % (self.upload_percent, chunk_end_index, file_size))
					else:
						error = "File transfer interrupted"
						break

					chunk_start_index += self.FILE_PACKET_SIZE

				if not error:
					result, response = self._serial_obj.sendcommand(b"M29", 10000)
					if result and b"CMD M28" in response:
						response = self._serial_obj.readraw(1000)
					if result and b"failed" not in response:
						sd_upload_succeeded(filename, remote_name, 10)
					else:
						error = "File transfer incomplete"

			except flashforge.FlashForgeError as error:
				error = "File transfer incomplete"
				pass

			if error:
				self._logger.info("Upload failed: {}".format(error))
				sd_upload_failed(filename, remote_name, 10)
				self._serial_obj.makeexclusive(False)
				raise flashforge.FlashForgeError(error)

			self._serial_obj.makeexclusive(False)
			# NB M23 select will also trigger a print on FlashForge
			self._comm.selectFile("0:/user/%s\r\n" % remote_name, True)
			# TODO: need to set the correct file size for the progress indicator

		bgcode = b""
		file_size = 0
		# unfortunately we cannot get the list of files on the SD card from FlashForge so we just name the remote file
		# the same as the source and hope for the best
		remote_name = filename

		file = open(path, "rb")
		bgcode = file.read()
		file_size = len(bgcode)
		file.close()

		self._logger.info("Starting SDCard upload from {} to {}".format(filename, remote_name))
		sd_upload_started(filename, remote_name)

		thread = threading.Thread(target=process_upload, name="FlashForge.SD_Uploader")
		thread.daemon = True
		thread.start()

		return remote_name



# If you want your plugin to be registered within OctoPrint under a different name than what you defined in setup.py
# ("OctoPrint-PluginSkeleton"), you may define that here. Same goes for the other metadata derived from setup.py that
# can be overwritten via __plugin_xyz__ control properties. See the documentation for that.
__plugin_name__ = "FlashForge Plugin"
__plugin_pythoncompat__ = ">=2.7,<4"


def __plugin_load__():
	global __plugin_implementation__
	__plugin_implementation__ = FlashForgePlugin()

	global __plugin_hooks__
	__plugin_hooks__ = {
		"octoprint.plugin.softwareupdate.check_config": __plugin_implementation__.get_update_information,
		"octoprint.comm.transport.serial.factory": __plugin_implementation__.printer_factory,
		"octoprint.comm.transport.serial.additional_port_names": __plugin_implementation__.get_additional_port_names,
		"octoprint.filemanager.extension_tree": __plugin_implementation__.get_extension_tree,
		"octoprint.comm.protocol.gcode.queuing": __plugin_implementation__.rewrite_gcode,
		"octoprint.printer.sdcardupload": __plugin_implementation__.upload_to_sd
	}

