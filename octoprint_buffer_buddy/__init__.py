# coding=utf-8
from __future__ import absolute_import

import octoprint.plugin
from octoprint.util import monotonic_time
import time
import re
import flask
from octoprint.events import eventManager, Events

ADVANCED_OK = re.compile(r"ok (N(?P<line>\d+) )?P(?P<planner_buffer_avail>\d+) B(?P<command_buffer_avail>\d+)")
REPORT_INTERVAL = 1 # seconds
POST_RESEND_WAIT = 0 # seconds
RESEND_HISTORY_MARGIN = 5 # lines. Octoprint can only resend lines still in its history (serial.lastLineBufferSize, default 50), so keep inflight this far below it
MIN_CLEAR_TO_SEND_MAX = 2 # "ok buffer size" (serial.ackMax) defaults to 1, which caps away the clear to send we add on top of Octoprint's own
DEFAULT_RX_BUFFER_SIZE = 128 # bytes, Marlin's default RX_BUFFER_SIZE
ASSUMED_LINE_BYTES = 48 # bytes per line on the wire, "N1234 " and "*123" included, to turn the RX buffer size into lines

# Octoprint internals BufferBuddy relies on. Checked on every new connection, if any are missing BufferBuddy stays inactive
REQUIRED_COMM_INTERNALS = (
	"_current_line", "_clear_to_send", "_send_queue", "_resendActive", "_state", "_currentFile",
	"STATE_STARTING", "STATE_PRINTING", "job_on_hold", "isStreaming", "isSdPrinting",
	"_send_from_command_queue", "_send_from_job_queue", "_send_from_job",
)
REQUIRED_CLEAR_TO_SEND_INTERNALS = ("set", "max", "counter")

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

		self.state = 'initialising'

		self.advanced_ok_detected = False

		self.min_cts_interval = 1.0
		self.rx_buffer_lines = DEFAULT_RX_BUFFER_SIZE // ASSUMED_LINE_BYTES
		self.inflight_target = 0
		self.planner_buffer_size = 0
		self.command_buffer_size = 0

		self.compatible = True
		self.checked_comm = None # comm object the compatibility check last ran on, Octoprint makes a new one per connection
		self.raised_comm = None # comm object whose clear to send max we raised
		self.original_clear_to_send_max = None

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
		# The next connection gets a new comm object, built from the saved settings
		self.checked_comm = None
		self.raised_comm = None
		self.state = 'disconnected'
		self.set_status('Disconnected')
		self.send_plugin_state()

	def on_transfer_started(self, event, payload):
		self.reset_statistics()
		self.state = 'transferring'
		self.send_plugin_state()

	def on_print_started(self, event, payload):
		self.reset_statistics()
		self.state = 'printing'
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

	def set_buffer_sizes(self, planner_buffer_size, command_buffer_size):
		self.planner_buffer_size = planner_buffer_size
		self.command_buffer_size = command_buffer_size
		resend_history = self._settings.global_get_int(["serial", "lastLineBufferSize"])
		self.inflight_target = min(command_buffer_size - 1, resend_history - RESEND_HISTORY_MARGIN)
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
			rx_buffer_size=DEFAULT_RX_BUFFER_SIZE,
		)

	def on_settings_save(self, data):
		octoprint.plugin.SettingsPlugin.on_settings_save(self, data)
		self.apply_settings()

	def apply_settings(self):
		self.enabled = self._settings.get_boolean(["enabled"])
		self.min_cts_interval = self._settings.get_float(["min_cts_interval"])
		self.sd_inflight_target = self._settings.get_int(["sd_inflight_target"])
		self.rx_buffer_lines = max(1, self._settings.get_int(["rx_buffer_size"]) // ASSUMED_LINE_BYTES)
		if not self.enabled:
			self.restore_clear_to_send_max()

	##~~ Frontend stuff
	def send_message(self, type, message):
		self._plugin_manager.send_plugin_message(self._identifier, {"type": type, "message": message})

	def set_status(self, message):
		if not self.compatible:
			message = 'Unsupported Octoprint version, inactive'
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
			"compatible": self.compatible,
		}

	def on_api_get(self, request):
		return flask.jsonify(state=self.plugin_state())

	def get_api_commands(self):
		return dict(clear=[])

	def on_api_command(self, command, data):
		# No commands yet
		return None

	##~~ Octoprint internals

	def check_compatibility(self, comm):
		self.checked_comm = comm
		missing = [name for name in REQUIRED_COMM_INTERNALS if not hasattr(comm, name)]
		if not missing:
			missing = ["_clear_to_send." + name for name in REQUIRED_CLEAR_TO_SEND_INTERNALS if not hasattr(comm._clear_to_send, name)]
		self.compatible = not missing
		if missing:
			self._logger.warning("This Octoprint version lacks internals BufferBuddy needs ({}), staying inactive".format(", ".join(missing)))
			self.set_status('Unsupported Octoprint version')
			self.send_plugin_state()

	def raise_clear_to_send_max(self, comm):
		# Only on this connection: the saved "ok buffer size" is untouched and the next connection starts from it again
		clear_to_send = comm._clear_to_send
		if self.raised_comm is comm or clear_to_send.max is None or clear_to_send.max >= MIN_CLEAR_TO_SEND_MAX:
			return
		self.original_clear_to_send_max = clear_to_send.max
		clear_to_send.max = MIN_CLEAR_TO_SEND_MAX
		self.raised_comm = comm
		self._logger.info("Raised this connection's ok buffer size from {} to {}".format(self.original_clear_to_send_max, MIN_CLEAR_TO_SEND_MAX))

	def restore_clear_to_send_max(self):
		comm = self.raised_comm
		if comm is None:
			return
		comm._clear_to_send.max = self.original_clear_to_send_max
		self.raised_comm = None
		self._logger.info("Restored this connection's ok buffer size to {}".format(self.original_clear_to_send_max))

	def queue_next_line(self, comm):
		# One pass of Octoprint's _continue_sending(), minus its "only refill an empty send queue" guard
		if comm._send_from_command_queue():
			return True
		if comm.job_on_hold:
			return False
		if comm._send_from_job_queue():
			return True
		job_active = comm._state in (comm.STATE_STARTING, comm.STATE_PRINTING) and not (
			comm._currentFile is None or comm._currentFile.done or comm.isSdPrinting()
		)
		return job_active and comm._send_from_job()

	def fill_send_queue(self, comm, clear_to_sends):
		# Octoprint only refills an empty send queue. When oks arrive faster than its send loop runs, the second
		# ok finds the line queued for the first one still there, adds nothing, and a line in flight is lost.
		# So keep a line queued for every clear to send.
		while comm._send_queue.qsize() < clear_to_sends and self.queue_next_line(comm):
			pass
		return comm._send_queue.qsize() >= clear_to_sends

	def send_one_more(self, comm):
		# This hook runs before Octoprint handles the same ok, so cover the clear to sends already pending,
		# the one Octoprint adds for this ok, and ours
		if not self.fill_send_queue(comm, comm._clear_to_send.counter + 2):
			return False # nothing left to send
		comm._clear_to_send.set()
		return True

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

		if comm is not self.checked_comm:
			self.check_compatibility(comm)
		if not self.compatible:
			return line

		if self.did_resend and not comm._resendActive:
			self.did_resend = False
			self.set_status('Resend over, resuming...')

		if "ok " in line:
			matches = ADVANCED_OK.search(line)

			if matches is None or matches.group('line') is None:
				return line
				
			ok_line_number = int(matches.group('line'))
			current_line_number = comm._current_line
			command_buffer_avail = int(matches.group('command_buffer_avail'))
			planner_buffer_avail = int(matches.group('planner_buffer_avail'))
			queue_size = comm._send_queue.qsize()
			inflight_target = self.sd_inflight_target if comm.isStreaming() else self.inflight_target
			inflight = current_line_number - ok_line_number
			inflight += comm._clear_to_send.counter # If there's a clear_to_send pending, we need to count it as inflight cause it will be soon

			should_report = False
			should_send = False

			# During a resend only stop adding lines (below). Never swallow an ok: Octoprint needs each one to step
			# through the lines it resends, and if it times out instead, the line it repeats gets no reply at all,
			# because Marlin silently drops a line number it has already processed. The print then hangs.
			if comm._resendActive:
				if not self.did_resend:
					self.resends_detected += 1
					self.did_resend = True
					self.set_status('Resend detected, backing off' if self.enabled else 'Resend detected')
				self.last_cts = monotonic_time() + POST_RESEND_WAIT # Hack to delay before resuming CTS after resend event to give printer some time to breathe

			# detect underruns if printing
			if not comm.isStreaming():
				if command_buffer_avail == self.command_buffer_size - 1:
					self.command_underruns_detected += 1

				if planner_buffer_avail == self.planner_buffer_size - 1:
					self.planner_underruns_detected += 1

			if (monotonic_time() - self.last_report) > REPORT_INTERVAL:
				should_report = True

			# The command queue holds BUFSIZE - B lines, the one this ok is for included. Lines sent after those are
			# in transit or in the printer's serial RX buffer, which ADVANCED_OK doesn't report and which overflows
			# (dropping bytes) if a burst arrives while the printer is busy. So after Octoprint's line for this ok
			# and ours, everything not yet in the command queue must still fit in the RX buffer.
			unacked = current_line_number - 1 - ok_line_number + comm._clear_to_send.counter
			queued = self.command_buffer_size - command_buffer_avail - 1
			fits_rx_buffer = unacked + 2 - queued <= self.rx_buffer_lines

			if command_buffer_avail > 2 and fits_rx_buffer: # As we are going to send, and _monitor thread of Octoprint will also send due to OK received, we need to have at leat 2 spots.
				if inflight < inflight_target and (monotonic_time() - self.last_cts) > self.min_cts_interval:
					should_send = True

			if self.enabled and not comm._resendActive:
				# A line for each clear to send pending, plus the one Octoprint adds for this ok
				self.fill_send_queue(comm, comm._clear_to_send.counter + 1)

			if should_send and self.enabled and not comm._resendActive:
				self.raise_clear_to_send_max(comm)
				if self.send_one_more(comm):
					self._logger.debug("Detected available command buffer, triggering a send")
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
				self._logger.debug("current line: {} ok line: {} buffer avail: {} inflight: {} cts: {} cts_max: {} queue: {}".format(current_line_number, ok_line_number, command_buffer_avail, inflight, comm._clear_to_send.counter, comm._clear_to_send.max, queue_size))
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
				user="kagebarton",
				repo="BufferBuddy",
				current=self._plugin_version,

				# update method: pip
				pip="https://github.com/kagebarton/BufferBuddy/archive/{target_version}.zip"
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
	}

