# coding=utf-8
from __future__ import absolute_import

import octoprint.plugin
from octoprint.util import monotonic_time
import re
import flask
from octoprint.events import eventManager, Events

ADVANCED_OK = re.compile(r"ok (N(?P<line>\d+) )?P(?P<planner_buffer_avail>\d+) B(?P<command_buffer_avail>\d+)")
REPORT_INTERVAL = 1 # seconds
RESEND_EPISODE_GAP = 1.0 # seconds. A resend starting this soon after the last one ended is part of the same episode: every line already in flight behind a bad one draws its own resend request
RESEND_HISTORY_MARGIN = 5 # lines. Octoprint can only resend lines still in its history (serial.lastLineBufferSize, default 50), so keep inflight this far below it
CLEAR_TO_SEND_HEADROOM = 2 # clear to sends beyond the inflight target: ours plus Octoprint's own for the same ok
DEFAULT_MIN_CTS_INTERVAL = 0.1 # seconds
DEFAULT_RX_BUFFER_SIZE = 128 # bytes, Marlin's default RX_BUFFER_SIZE
ASSUMED_LINE_BYTES = 48 # bytes per line on the wire, "N1234 " and "*123" included, to turn the RX buffer size into lines

# Octoprint internals BufferBuddy relies on. Checked on every new connection, if any are missing BufferBuddy stays inactive
REQUIRED_COMM_INTERNALS = (
	"_current_line", "_clear_to_send", "_send_queue", "_resendActive", "_state", "_currentFile",
	"STATE_STARTING", "STATE_PRINTING", "job_on_hold", "isPrinting", "isStreaming", "isSdPrinting",
	"_send_from_command_queue", "_send_from_job_queue", "_send_from_job",
)
REQUIRED_CLEAR_TO_SEND_INTERNALS = ("set", "clear", "max", "counter", "acquire", "release")

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
		self.status = 'Not connected'

		self.advanced_ok_detected = False

		self.min_cts_interval = DEFAULT_MIN_CTS_INTERVAL
		self.rx_buffer_lines = DEFAULT_RX_BUFFER_SIZE // ASSUMED_LINE_BYTES
		self.inflight_target = 0
		self.planner_buffer_size = 0
		self.command_buffer_size = 0

		self.compatible = True # False stops the hook for the rest of this connection
		self.inactive_status = None # why, shown as the status
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
		self.advanced_ok_detected = False
		self.set_state('detecting', 'Detecting buffer sizes')

	def on_disconnected(self, event, payload):
		self.command_buffer_size = 0
		self.planner_buffer_size = 0
		# The next connection gets a new comm object, built from the saved settings
		self.checked_comm = None
		self.raised_comm = None
		self.set_state('disconnected', 'Disconnected')

	def on_transfer_started(self, event, payload):
		self.reset_statistics()
		self.set_state('transferring', 'Uploading to SD')

	def on_print_started(self, event, payload):
		self.reset_statistics()
		self.set_state('printing', 'Printing')

	def on_print_finish(self, event, payload):
		if self.last_update is not None:
			self.last_update.update(self.summary_stats()) # the last report can be up to REPORT_INTERVAL old
		self.set_state('ready', 'Ready')

	def reset_statistics(self):
		self.resends_detected = 0
		self.clear_to_sends_triggered = 0
		self.did_resend = False
		self.last_resend_end = None
		self.planner_queued_sum = 0
		self.planner_samples = 0
		self.oks_since_report = 0
		self.last_report = monotonic_time()
		self.last_update = None # the stats last sent to the sidebar, also served to browsers that load later

	def summary_stats(self):
		# Stats over the whole job, kept in the sidebar after it ends
		return {
			"planner_queued_avg": int(round(float(self.planner_queued_sum) / self.planner_samples)) if self.planner_samples else None,
			"resends_detected": self.resends_detected,
			"cts_triggered": self.clear_to_sends_triggered,
		}

	def set_buffer_sizes(self, planner_buffer_size, command_buffer_size):
		self.planner_buffer_size = planner_buffer_size
		self.command_buffer_size = command_buffer_size
		resend_history = self._settings.global_get_int(["serial", "lastLineBufferSize"])
		self.inflight_target = min(command_buffer_size - 1, resend_history - RESEND_HISTORY_MARGIN)
		self.advanced_ok_detected = True
		self._logger.info("Detected planner buffer size as {}, command buffer size as {}, setting inflight_target to {}".format(planner_buffer_size, command_buffer_size, self.inflight_target))
		self.set_state('detected', 'Ready')

	##~~ StartupPlugin mixin

	def on_after_startup(self):
		self.apply_settings()
		self._logger.info("BufferBuddy loaded")

	##~~ SettingsPlugin mixin

	def get_settings_defaults(self):
		return dict(
			enabled=True,
			min_cts_interval=DEFAULT_MIN_CTS_INTERVAL,
			rx_buffer_size=DEFAULT_RX_BUFFER_SIZE,
		)

	def on_settings_save(self, data):
		octoprint.plugin.SettingsPlugin.on_settings_save(self, data)
		self.apply_settings()

	def apply_settings(self):
		self.enabled = self._settings.get_boolean(["enabled"])
		# A field left blank reads as None
		min_cts_interval = self._settings.get_float(["min_cts_interval"], min=0)
		self.min_cts_interval = DEFAULT_MIN_CTS_INTERVAL if min_cts_interval is None else min_cts_interval
		rx_buffer_size = self._settings.get_int(["rx_buffer_size"], min=ASSUMED_LINE_BYTES)
		self.rx_buffer_lines = (DEFAULT_RX_BUFFER_SIZE if rx_buffer_size is None else rx_buffer_size) // ASSUMED_LINE_BYTES
		# Disabling restores the ok buffer size on the next ok, from the monitor thread, not from here: a web thread
		# could land between the hook's raise and its sends
		self.send_plugin_state()

	##~~ Frontend stuff
	def send_message(self, type, message):
		self._plugin_manager.send_plugin_message(self._identifier, {"type": type, "message": message})

	def displayed_status(self, message):
		return message if self.compatible else self.inactive_status

	def set_status(self, message):
		message = self.displayed_status(message)
		if message == self.status:
			return
		self.status = message
		self.send_message("status", message)

	def set_state(self, state, status):
		# The full state carries the status as well
		self.state = state
		self.status = self.displayed_status(status)
		self.send_plugin_state()

	def activity_status(self, comm):
		if comm.isStreaming():
			return 'Uploading to SD'
		return 'Printing' if comm.isPrinting() else 'Ready'

	def send_plugin_state(self):
		self.send_message("state", self.plugin_state())

	def plugin_state(self):
		return {
			"planner_buffer_size": self.planner_buffer_size,
			"command_buffer_size": self.command_buffer_size,
			"inflight_target": self.inflight_target,
			"state": self.state,
			"status": self.status,
			"enabled": self.enabled,
			"advanced_ok_detected": self.advanced_ok_detected,
			"stats": self.last_update,
		}

	def is_api_protected(self):
		# The sidebar only loads this for a logged in user
		return True

	def on_api_get(self, request):
		return flask.jsonify(state=self.plugin_state())

	##~~ Octoprint internals

	def check_compatibility(self, comm):
		self.checked_comm = comm
		missing = [name for name in REQUIRED_COMM_INTERNALS if not hasattr(comm, name)]
		if not missing:
			missing = ["_clear_to_send." + name for name in REQUIRED_CLEAR_TO_SEND_INTERNALS if not hasattr(comm._clear_to_send, name)]
		self.compatible = not missing
		self.inactive_status = 'Unsupported OctoPrint version, inactive'
		if missing:
			self._logger.warning("This Octoprint version lacks internals BufferBuddy needs ({}), staying inactive".format(", ".join(missing)))
			self.set_state(self.state, self.inactive_status)

	def raise_clear_to_send_max(self, comm):
		# Only on this connection: the saved "ok buffer size" is untouched and the next connection starts from it again.
		# Octoprint caps its pending clear to sends at this size and every ok adds one, so whenever its send loop falls
		# behind, the ones over the cap are thrown away and lines drop out of flight for good. Leave room for all of them.
		clear_to_send = comm._clear_to_send
		wanted = self.inflight_target + CLEAR_TO_SEND_HEADROOM
		if clear_to_send.max is None or clear_to_send.max >= wanted:
			return
		if self.raised_comm is not comm:
			self.original_clear_to_send_max = clear_to_send.max
			self.raised_comm = comm
		clear_to_send.max = wanted
		self._logger.info("Raised this connection's ok buffer size from {} to {}".format(self.original_clear_to_send_max, wanted))

	def restore_clear_to_send_max(self, comm):
		if self.raised_comm is not comm:
			return
		clear_to_send = comm._clear_to_send
		original = self.original_clear_to_send_max
		clear_to_send.acquire()
		try:
			clear_to_send.max = original
			# The counter only drops to the new max on its next set or clear, and Octoprint's send loop would use the
			# surplus first. A clear then a set clamps it now, and is a no-op when it's already within the max.
			if clear_to_send.counter > original:
				clear_to_send.clear()
				clear_to_send.set()
		finally:
			clear_to_send.release()
		self.raised_comm = None
		self._logger.info("Restored this connection's ok buffer size to {}".format(original))

	def streaming_job(self, comm):
		# A job Octoprint sends line by line. Outside one the printer may sit on a long command (heating before the
		# next print starts, a pause) while the oks for the last lines in flight bank clear to sends, which Octoprint's
		# send loop would then spend back to back on whatever it sends next.
		return comm._state in (comm.STATE_STARTING, comm.STATE_PRINTING) and not comm.isSdPrinting()

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

	##~~ Core logic

	def gcode_received(self, comm, line, *args, **kwargs):
		try:
			return self.handle_received(comm, line)
		except Exception:
			# Octoprint would log this and carry on, once per ok. Stop instead, and leave this connection to stock Octoprint.
			self._logger.exception("BufferBuddy failed handling {!r}, staying inactive until the printer reconnects".format(line))
			self.compatible = False
			self.inactive_status = 'Error, inactive until reconnect (see octoprint.log)'
			self.set_state(self.state, self.inactive_status)
			try:
				self.restore_clear_to_send_max(comm)
			except Exception:
				self._logger.exception("Couldn't restore this connection's ok buffer size")
			return line

	# Assumptions: This is never called concurrently, and we are free to access anything in comm
	# FIXME: Octoprint considers the job finished when the last line is sent, even when there are lines inflight
	def handle_received(self, comm, line):
		matches = ADVANCED_OK.search(line) if "ok " in line else None
		ok_line_number = None if matches is None or matches.group('line') is None else int(matches.group('line'))

		# Figure out the buffer sizes, for the inflight target and the planner fill, from the response to N0 M110 N0
		# Important: This runs before on_after_startup
		if self.planner_buffer_size == 0 and ok_line_number == 0:
			# ok output always returns BLOCK_BUFFER_SIZE - 1 due to 
			#     FORCE_INLINE static uint8_t moves_free() { return BLOCK_BUFFER_SIZE - 1 - movesplanned(); }
			# for whatever reason
			planner_buffer_size = int(matches.group('planner_buffer_avail')) + 1
			# We add +1 here as ok will always return BUFSIZE-1 as we've just sent it a command
			command_buffer_size = int(matches.group('command_buffer_avail')) + 1
			self.set_buffer_sizes(planner_buffer_size, command_buffer_size)

		if comm is not self.checked_comm:
			self.check_compatibility(comm)
		if not self.compatible:
			return line

		if self.did_resend and not comm._resendActive:
			self.did_resend = False
			self.last_resend_end = monotonic_time()
			self.set_status(self.activity_status(comm))

		if ok_line_number is None:
			return line

		current_line_number = comm._current_line
		command_buffer_avail = int(matches.group('command_buffer_avail'))
		planner_buffer_avail = int(matches.group('planner_buffer_avail'))
		queue_size = comm._send_queue.qsize()
		inflight = current_line_number - ok_line_number
		inflight += comm._clear_to_send.counter # If there's a clear_to_send pending, we need to count it as inflight cause it will be soon
		planner_queued = max(0, self.planner_buffer_size - 1 - planner_buffer_avail)
		job_active = comm.isPrinting() or comm.isStreaming()

		# During a resend only stop adding lines (below). Never swallow an ok: Octoprint needs each one to step
		# through the lines it resends, and if it times out instead, the line it repeats gets no reply at all,
		# because Marlin silently drops a line number it has already processed. The print then hangs.
		if comm._resendActive:
			if not self.did_resend:
				if self.last_resend_end is None or monotonic_time() - self.last_resend_end > RESEND_EPISODE_GAP:
					self.resends_detected += 1
				self.did_resend = True
				self.set_status('Resend detected, backing off' if self.enabled else 'Resend detected')
			self.last_cts = monotonic_time() # the next extra line waits min_cts_interval after the resend ends

		# No underrun counters: Marlin moves a line into the planner within milliseconds and sends its ok after,
		# so B reads BUFSIZE - 1 on nearly every ok unless the planner is full, and P can't read "empty" for a
		# move because the move is already in the planner. How full the planner stays is what shows starvation.
		if job_active:
			self.oks_since_report += 1
			if not comm.isStreaming():
				self.planner_queued_sum += planner_queued
				self.planner_samples += 1

		# The command queue holds BUFSIZE - B lines, the one this ok is for included. Lines sent after those are
		# in transit or in the printer's serial RX buffer, which ADVANCED_OK doesn't report and which overflows
		# (dropping bytes) if a burst arrives while the printer is busy. So after Octoprint's line for this ok
		# and ours, everything not yet in the command queue must still fit in the RX buffer: inflight, less the line
		# this ok is for, plus those 2, less the queued lines.
		queued = self.command_buffer_size - command_buffer_avail - 1
		fits_rx_buffer = inflight + 1 - queued <= self.rx_buffer_lines

		# Octoprint's monitor thread sends a line for this ok as well, so the command queue needs room for 2
		should_send = (command_buffer_avail > 2 and fits_rx_buffer and inflight < self.inflight_target
			and monotonic_time() - self.last_cts > self.min_cts_interval)

		active = self.enabled and self.streaming_job(comm)
		if not active:
			self.restore_clear_to_send_max(comm)
		elif not comm._resendActive:
			if should_send:
				self.raise_clear_to_send_max(comm)
			# This hook runs before Octoprint handles the same ok, so queue a line for each clear to send pending,
			# the one Octoprint adds for this ok, and ours
			if self.fill_send_queue(comm, comm._clear_to_send.counter + 1 + should_send) and should_send:
				comm._clear_to_send.set()
				self._logger.debug("Detected available command buffer, triggering a send")
				self.clear_to_sends_triggered += 1
				self.last_cts = monotonic_time()

		now = monotonic_time()
		if job_active and now - self.last_report > REPORT_INTERVAL:
			self.last_update = dict(self.summary_stats(),
				lines_per_second=int(round(self.oks_since_report / (now - self.last_report))),
				inflight=inflight,
				planner_queued=planner_queued,
			)
			self.send_message("update", self.last_update)
			self._logger.debug("current line: %s ok line: %s buffer avail: %s inflight: %s cts: %s cts_max: %s queue: %s",
				current_line_number, ok_line_number, command_buffer_avail, inflight, comm._clear_to_send.counter, comm._clear_to_send.max, queue_size)
			self.oks_since_report = 0
			self.last_report = now

		return line

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
	def is_template_autoescaped(self):
		return True

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

