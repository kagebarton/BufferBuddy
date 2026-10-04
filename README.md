# BufferBuddy

BufferBuddy aims to prevent print quality issues when printing over USB with Octoprint. Designed for Marlin with `ADVANCED_OK` support, but may work for other firmwares that also support `ADVANCED_OK` output.

This is a fork of https://github.com/fflosi/BufferBuddy, which is itself a fork of the original https://github.com/chendo/BufferBuddy.

**WARNING:** See details on original page https://github.com/chendo/BufferBuddy**

This plugin requires `ADVANCED_OK` to function.

## No setup needed

Install it and it works on a stock OctoPrint. There is no `comm.py` to patch and no "ok buffer size" to change:

- When it first sends an extra line in a print, BufferBuddy raises that connection's "ok buffer size" to its inflight target plus 2. OctoPrint throws away every `ok` over that size that arrives before its send loop catches up, and each one it throws away is a line lost from flight for good. Outside a print (before it starts, after it ends, while paused, or when disabled) it puts the original value back, so `ok`s for the last lines in flight can't pile up and later release a burst of lines while the printer heats up. Your saved setting is never changed.
- OctoPrint only refills its send queue when it's empty, so when several `ok`s arrive at once it sends one line where it should send several. BufferBuddy keeps a line queued for every pending clear to send, using the same steps as OctoPrint's `_continue_sending()`. OctoPrint's own code is never patched.
- Lines waiting in that queue still go out after a pause or cancel (OctoPrint doesn't clear it), so expect a few more short moves than stock OctoPrint, on top of what's already in the printer's buffer.
- It only sends an extra line if everything the printer hasn't moved into its command queue yet still fits in the printer's serial RX buffer (setting, default 128 bytes = Marlin's `RX_BUFFER_SIZE` default). It counts the actual bytes of those lines, line numbers and checksums included. ADVANCED_OK doesn't report that buffer, and overflowing it drops bytes and causes resends.
- It adds at most one line per `ok`, so inflight climbs to its limit over that many `ok`s at the start of a print and after a resend.
- During a resend it adds no lines and never swallows an `ok`. (The original swallowed them, which can hang a print on Marlin: OctoPrint then repeats a line Marlin has already processed, and Marlin silently ignores it.)
- On every connection it checks that the OctoPrint internals it relies on still exist. If any are missing, the sidebar shows "Unsupported OctoPrint version" and the plugin stays inactive. If it hits an error while running, it logs it once, hands the connection back to stock OctoPrint and says so in the sidebar until the printer reconnects.

## Sidebar

- **Status**: Ready, Printing, Paused, Uploading to SD, Resend detected and so on. "(monitoring only)" means BufferBuddy is disabled in its settings and only watches.
- **Throughput**: lines the printer acknowledged per second, over the last second. Shown during a job, along with **In flight**: lines sent but not yet acknowledged. When enabled, it also says what's holding inflight where it is: the RX buffer, the command buffer, or the target (`BUFSIZE - 1`). **In flight average** is over the whole job, and is what the plugin achieves: stock OctoPrint keeps 1.
- **Planner**: moves waiting in the printer's planner now, and on average over the time of the print. This is the buffer that keeps the printer moving. When it runs low, Marlin's `SLOWDOWN` slows the print down, and when it empties the printer stops. Higher is better.
- **Resends**: resend episodes during the print. One lost byte makes the printer reject every line already sent behind it, and each of those asks for its own resend, so resends less than a second apart count as one episode.

There are no underrun counters any more. "Command underruns" counted every `ok` where the printer's command queue held nothing behind that line. That happens on nearly every line unless the planner is full, and on every line with stock OctoPrint. "Planner underruns" needed an `ok` reporting an empty planner, which never happens for a move, because Marlin adds the move to the planner before it sends the `ok`.

## Changes from fflosi's version

- Inflight target is chendo's `BUFSIZE - 1` again, capped 5 lines below OctoPrint's resend history (`serial.lastLineBufferSize`, default 50) so every line in flight can still be resent.
- Still requires more than 2 free slots in the command buffer before sending, because OctoPrint also sends a line for the same `ok`.
- The manual `comm.py` patch and the "ok buffer size" instructions are gone (see above).
- Update checks point at this fork.

## Firmware settings

- **`BUFSIZE` is the main lever.** It sets the inflight target (`BUFSIZE - 1`, at most 45), and lines in flight wait safely in Marlin's command queue. Marlin's default of 4 leaves the plugin almost nothing to work with.
- **`RX_BUFFER_SIZE`** holds the lines that haven't reached the command queue yet, and BufferBuddy only keeps as many there as fit. Once the planner is full, the command queue holds nearly every line in flight, so a small RX buffer costs little: Marlin's default of 128 bytes did almost as well as 1024 below. Set the plugin's "Printer RX buffer size" to your firmware's value: too high and the printer drops bytes and asks for resends, too low and the plugin holds back.
- **`BLOCK_BUFFER_SIZE`** is how many moves the planner holds. It's what keeps the printer moving when lines arrive unevenly.

Measured on the Aquila below with a stress test of 18,000 0.3 mm moves at 100 mm/s, which needs 333 lines/s (the moves alone take about 54 s):

| Firmware | Stock OctoPrint | BufferBuddy 0.2.0 | BufferBuddy 0.3.0 |
|---|---|---|---|
| RX 128, BUFSIZE 8 | 238 s | 125 s | |
| RX 1024, BUFSIZE 32 | 238 s | 83 s | 78 s |
| RX 1024, BUFSIZE 32, plugin's RX setting at 128 | | | 77 s |

Homing at the start varies by a few seconds between runs.

## Known limitations

- OctoPrint considers a print finished when it sends the last line, so "print done" (and anything that runs on it) comes up to `BUFSIZE - 1` lines early, while the printer is still working through them.
- `ok`s for earlier lines in flight arrive while the printer is busy with a long command like `M190` or `M109`. OctoPrint takes them as the end of that command, so it can log "Communication timeout" around heat-up, and its heat-up time accounting is off.

## Tested with

Voxelab Aquila (STM32F103, Marlin 2.1 ProUI fork with `ADVANCED_OK`, `RX_BUFFER_SIZE 1024`, `BUFSIZE 32`, `BLOCK_BUFFER_SIZE 128`, 250000 baud over its USB serial adapter), OctoPrint 1.11.8 on octo4a (Android). A 215,000-line print finished with the planner full on 84% of `ok`s and every resend recovered. Logging the serial traffic (OctoPrint's `serial.log`) costs octo4a enough CPU to cut the stress test from about 330 to 258 lines/s, which starves the planner, so leave it off unless you're diagnosing something.

## Setup

This fork is not on the plugin repository
Install from plugin Manager using the link bellow:
https://github.com/kagebarton/BufferBuddy/archive/0.3.0.zip

