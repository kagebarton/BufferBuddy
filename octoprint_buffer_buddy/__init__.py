# coding=utf-8
from __future__ import absolute_import

import octoprint.plugin
from octoprint.util import monotonic_time
import time
import re
import flask
from octoprint.events import eventManager, Events
import math

ADVANCED_OK = re.compile(r"ok (N(?P<line>\d+) )?P(?P<planner_buffer_avail>\d+) B(?P<command_buffer_avail>\d+)")
REPORT_INTERVAL = 1 # seconds
POST_RESEND_WAIT = 0 # seconds
INFLIGHT_TARGET_MAX = 255 # Octoprint has a hard limit of 50 entries in the buffer for resends so it must be less than that, with a buffer

class BufferBuddyPlugin(octoprint.plugin.SettingsPlugin,
						octoprint.plugin.AssetPlugin,
						octoprint.plugin.TemplatePlugin,
						octoprint.plugin.SimpleApiPlugin,
						octoprint.plugin.StartupPlugin
						):

	def __init__(self):
		# Set variables that we may use before we can pull the settings etc
		self.last_cts = 0
		self.last_report = 0
		
		self.enabled = False
		self.originalenabled = False

		self.state = 'initialising'

		self.advanced_ok_detected = False

		self.min_cts_interval = 1.0 
		self.inflight_target = 0
		self.planner_buffer_size = 0
		self.command_buffer_size = 0
		

		eventManager().subscribe(Events.CONNECTING, self.on_connecting)
		eventManager().subscribe(Events.DISCONNECTED, self.on_disconnected)
		eventManager().subscribe(Events.TRANSFER_STARTED, self.on_transfer_started)
		eventManager().subscribe(Events.TRANSFER_DONE, self.on_print_finish)
		eventManager().subscribe(Events.TRANSFER_FAILED, self.on_print_finish)
		eventManager().subscribe(Events.PRINT_STARTED, self.on_print_started)
		eventManager().subscribe(Events.PRINT_DONE, self.on_print_finish)
		eventManager().subscribe(Events.PRINT_FAILED, self.on_print_finish)

		self.reset_statistics()
	
	def on_connecting(self, event, payload):
		self.command_buffer_size = 0
		self.planner_buffer_size = 0
		self.state = 'detecting'

	def on_disconnected(self, event, payload):
		self.command_buffer_size = 0
		self.planner_buffer_size = 0
		self.state = 'disconnected'
		self.set_status('Disconnected')
		self.send_plugin_state()

	def on_transfer_started(self, event, payload):
		self.reset_statistics()
		self.state = 'transferring'
		self.send_plugin_state()

	def on_print_started(self, event, payload):
		self.reset_statistics()
		self.state = 'waiting_to_start' # original printing
		self.send_plugin_state()

	def on_print_finish(self, event, payload):
		self.set_status('Ready')
		self.state = 'ready'
		self.send_plugin_state()

	def reset_statistics(self):
		self.command_underruns_detected = 0
		self.planner_underruns_detected = 0
		self.resends_detected = 0
		self.clear_to_sends_triggered = 0
		self.did_resend = False
		self.enabled = self.originalenabled

	def set_buffer_sizes(self, planner_buffer_size, command_buffer_size):
		self.planner_buffer_size = planner_buffer_size
		self.command_buffer_size = command_buffer_size
		self.inflight_target = min(command_buffer_size - 1, INFLIGHT_TARGET_MAX)
		self.state = 'detected'
		self.advanced_ok_detected = True
		self._logger.info("Detected planner buffer size as {}, command buffer size as {}, setting inflight_target to {}".format(planner_buffer_size, command_buffer_size, self.inflight_target))
		self.send_plugin_state()

	##~~ StartupPlugin mixin

	def on_after_startup(self):
		self.apply_settings()
		self._logger.info("BufferBuddy loaded")

	##~~ SettingsPlugin mixin

	def get_settings_defaults(self):
		return dict(
			enabled=True,
			min_cts_interval=0.1,
			sd_inflight_target=4,
			stopcommand = "M31",
			startafter = 50,
			startafterZ = 0
		)

	def on_settings_save(self, data):
		octoprint.plugin.SettingsPlugin.on_settings_save(self, data)
		self.apply_settings()

	def apply_settings(self):
		self.enabled = self._settings.get_boolean(["enabled"])
		self.min_cts_interval = self._settings.get_float(["min_cts_interval"])
		self.sd_inflight_target = self._settings.get_int(["sd_inflight_target"])
		self.stopcommand = self._settings.get(["stopcommand"])
		self.startafter = self._settings.get_int(["startafter"])
		self.startafterZ = self._settings.get_float(["startafterZ"])
		self.originalenabled = self.enabled

	##~~ Frontend stuff
	def send_message(self, type, message):
		self._plugin_manager.send_plugin_message(self._identifier, {"type": type, "message": message})

	def set_status(self, message):
		self.send_message("status", message)

	def send_plugin_state(self):
		self.send_message("state", self.plugin_state())

	def plugin_state(self):
		return {
			"planner_buffer_size": self.planner_buffer_size,
			"command_buffer_size": self.command_buffer_size,
			"inflight_target": self.inflight_target,
			"state": self.state,
			"enabled": self.enabled,
			"advanced_ok_detected": self.advanced_ok_detected,
		}

	def on_api_get(self, request):
		return flask.jsonify(state=self.plugin_state())

	def get_api_commands(self):
		return dict(clear=[])

	def on_api_command(self, command, data):
		# No commands yet
		return None

	##~~ Core logic

	# Assumptions: This is never called concurrently, and we are free to access anything in comm
	# FIXME: Octoprint considers the job finished when the last line is sent, even when there are lines inflight
	def gcode_received(self, comm, line, *args, **kwargs):				
		# Try to figure out buffer sizes for underrun detection by looking at the N0 M110 N0 response
		# Important: This runs before on_after_startup
		if self.planner_buffer_size == 0 and "ok N0 " in line:
			matches = ADVANCED_OK.search(line)
			if matches:
				# ok output always returns BLOCK_BUFFER_SIZE - 1 due to 
				#     FORCE_INLINE static uint8_t moves_free() { return BLOCK_BUFFER_SIZE - 1 - movesplanned(); }
				# for whatever reason
				planner_buffer_size = int(matches.group('planner_buffer_avail')) + 1
				# We add +1 here as ok will always return BUFSIZE-1 as we've just sent it a command
				command_buffer_size = int(matches.group('command_buffer_avail')) + 1
				self.set_buffer_sizes(planner_buffer_size, command_buffer_size)
				self.set_status('Buffer sizes detected')

		if self.did_resend and not comm._resendActive:
			self.did_resend = False
			self.set_status('Resend over, resuming...')

		if "ok " in line:
			matches = ADVANCED_OK.search(line)

			if matches is None or matches.group('line') is None:
				return line


			current_line_number = comm._current_line

			ok_line_number = int(matches.group('line'))
			command_buffer_avail = int(matches.group('command_buffer_avail'))
			planner_buffer_avail = int(matches.group('planner_buffer_avail'))
			queue_size = comm._send_queue._qsize()
			inflight_target = self.sd_inflight_target if comm.isStreaming() else self.inflight_target
			inflight = current_line_number - ok_line_number
			inflight += comm._clear_to_send._counter # If there's a clear_to_send pending, we need to count it as inflight cause it will be soon

			if(self.state == "waiting_to_start"):
				if((ok_line_number < self.startafter) or (comm._currentZ < self.startafterZ)):
					self.enabled = False
				else:
					self.state = 'printing'
					self.enabled = self.originalenabled

			if(self.state == 'stopping'):
				self._logger.debug("stopping - current_line_number - 1 = " + str(current_line_number -1) + " ok_line_number = " + str(ok_line_number))
				if((current_line_number -1) != ok_line_number):
					return "echo:busy: processing"
				else:
					self._logger.debug("Stopped")
					self.state = 'stopped'
					return line

			if(self.state == 'sync'):
				self._logger.debug("Sync - current_line_number - 1 = " + str(current_line_number -1) + " ok_line_number = " + str(ok_line_number))
				if((current_line_number -1) != ok_line_number):
					return "echo:busy: processing"
				else:
					self._logger.debug("Sync done")
					self.state = 'printing'
					self.enabled = self.originalenabled
					return line

			should_report = False
			should_send = False

			# If we're in a resend state, try to lower inflight commands by consuming ok's
			if comm._resendActive and self.enabled:
				if not self.did_resend:
					self.resends_detected += 1
					self.did_resend = True
					self.set_status('Resend detected, backing off')
				self.last_cts = monotonic_time() + POST_RESEND_WAIT # Hack to delay before resuming CTS after resend event to give printer some time to breathe
				if inflight > (inflight_target / 2):
					self._logger.warn("using a clear to decrease inflight, inflight: {}, line: {}".format(inflight, line))
					comm._ok_timeout = monotonic_time() + 0.05 # Reduce the timeout in case we eat too many OKs
					return None

			# detect underruns if printing
			if not comm.isStreaming():
				if command_buffer_avail == self.command_buffer_size - 1:
					self.command_underruns_detected += 1

				if planner_buffer_avail == self.planner_buffer_size - 1:
					self.planner_underruns_detected += 1

			if (monotonic_time() - self.last_report) > REPORT_INTERVAL:
				should_report = True

			if command_buffer_avail > 2: # As we are going to send, and _monitor thread of Octoprint will also send due to OK received, we need to have at leat 2 spots.
				if inflight < inflight_target and (monotonic_time() - self.last_cts) > self.min_cts_interval:
					should_send = True

			if should_send and self.enabled:
				# user must change _clear_to_send._max to at least 2 on Octoprint interface -- firmware protocol "ok buffer size"
				# On Octoprint code _continue_sending() is limiting the command queue size to 1. The line below needs to be changed on comm.py or the plugin will not work.
				# while self._active and not self._send_queue.qsize():
				# -- Change to
				# while self._active and (self._send_queue.qsize() < self._ack_max):
				# This will set the command queue to the same size as _clear_to_send._max, so it will work as default for all users and only affect those who change "ok buffer size" on the interface
				comm._clear_to_send.set() # allways need to call if we wish to send additional command.
				comm._continue_sending() # always call this, as Octoprint limits the addition of commands to queue anyway.
				self._logger.debug("Detected available command buffer, triggering a send")
				# this enables the send loop to send if it's waiting
				self.clear_to_sends_triggered += 1
				self.last_cts = monotonic_time()
				#should_report = True # no need to update every send. keep updating based only on time to reduce load.

			if should_report:
				self.send_message("update", {
					"current_line_number": current_line_number,
					"acked_line_number": ok_line_number,
					"inflight": inflight,
					"planner_buffer_avail": planner_buffer_avail,
					"command_buffer_avail": command_buffer_avail,
					"resends_detected": self.resends_detected,
					"planner_underruns_detected": self.planner_underruns_detected,
					"command_underruns_detected": self.command_underruns_detected,
					"cts_triggered": self.clear_to_sends_triggered,
					"send_queue_size": queue_size,
				})
				self._logger.debug("State: {} current line: {} ok line: {} buffer avail: {} inflight: {} cts: {} cts_max: {} queue: {}".format(self.state, current_line_number, ok_line_number, command_buffer_avail, inflight, comm._clear_to_send._counter, comm._clear_to_send._max, queue_size))
				self.last_report = monotonic_time()
				if self.enabled:
					self.set_status('Active')
				else:
					self.set_status('Monitoring')

		return line

	##~~ AssetPlugin mixin

	def get_assets(self):
		# Define your plugin's asset files to automatically include in the
		# core UI here.
		return dict(
			js=["js/buffer-buddy.js"],
			css=["css/buffer-buddy.css"],
			less=["less/buffer-buddy.less"]
		)

	##~~ Softwareupdate hook

	def get_update_information(self):
		# Define the configuration for your plugin to use with the Software Update
		# Plugin here. See https://docs.octoprint.org/en/master/bundledplugins/softwareupdate.html
		# for details.
		return dict(
			buffer_buddy=dict(
				displayName="BufferBuddy Plugin",
				displayVersion=self._plugin_version,

				# version check: github repository
				type="github_release",
				user="fflosi",
				repo="BufferBuddy",
				current=self._plugin_version,

				# update method: pip
				pip="https://github.com/fflosi/BufferBuddy/archive/{target_version}.zip"
			)
		)

	##~~ AssetPlugin
	def get_assets(self):
		return dict(
			js=["js/buffer-buddy.js"]
		)

	##~~ TemplatePlugin
	def get_template_configs(self):
		return [
				dict(type="sidebar", custom_bindings=False),
				dict(type="settings", custom_bindings=False)
		]

	def gcode_sent(self, comm_instance, phase, cmd, cmd_type, gcode, *args, **kwargs):
		if gcode and cmd == self.stopcommand:
			self._logger.debug("State changed to stopping buffer - stop command = " + cmd)
			self.state = 'stopping'
			self.enabled = False
		return None

	def gcode_queuing(self, comm_instance, phase, cmd, cmd_type, gcode, *args, **kwargs):
		# Check if M command is going to be sent to sync queue.
		if gcode and cmd.startswith('M'):
			self._logger.debug("State changed to sync - sync command = " + cmd)
			self.state = 'sync'
			self.enabled = False
		return None

# If you want your plugin to be registered within OctoPrint under a different name than what you defined in setup.py
# ("OctoPrint-PluginSkeleton"), you may define that here. Same goes for the other metadata derived from setup.py that
# can be overwritten via __plugin_xyz__ control properties. See the documentation for that.
__plugin_name__ = "BufferBuddy"

# Starting with OctoPrint 1.4.0 OctoPrint will also support to run under Python 3 in addition to the deprecated
# Python 2. New plugins should make sure to run under both versions for now. Uncomment one of the following
# compatibility flags according to what Python versions your plugin supports!
#__plugin_pythoncompat__ = ">=2.7,<3" # only python 2
#__plugin_pythoncompat__ = ">=3,<4" # only python 3
__plugin_pythoncompat__ = ">=2.7,<4" # python 2 and 3

def __plugin_load__():
	global __plugin_implementation__
	__plugin_implementation__ = BufferBuddyPlugin()

	global __plugin_hooks__
	__plugin_hooks__ = {
		"octoprint.plugin.softwareupdate.check_config": __plugin_implementation__.get_update_information,
		"octoprint.comm.protocol.gcode.received": __plugin_implementation__.gcode_received,
		"octoprint.comm.protocol.gcode.sent": __plugin_implementation__.gcode_sent,
		"octoprint.comm.protocol.gcode.queuing": __plugin_implementation__.gcode_queuing,
	}

