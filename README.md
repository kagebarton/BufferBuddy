# BufferBuddy

BufferBuddy aims to prevent print quality issues when printing over USB with Octoprint. Designed for Marlin with `ADVANCED_OK` support, but may work for other firmwares that also support `ADVANCED_OK` output.

This is a fork of https://github.com/fflosi/BufferBuddy, which is itself a fork of the original https://github.com/chendo/BufferBuddy.

**WARNING:** See details on original page https://github.com/chendo/BufferBuddy**

This plugin requires `ADVANCED_OK` to function.

## No setup needed

Install it and it works on a stock OctoPrint. There is no `comm.py` to patch and no "ok buffer size" to change:

- When it first sends an extra line on a connection, BufferBuddy raises that connection's "ok buffer size" to its inflight target plus 2. OctoPrint throws away every `ok` over that size that arrives before its send loop catches up, and each one it throws away is a line lost from flight for good. Your saved setting is never changed, and disabling the plugin restores the original value.
- OctoPrint only refills its send queue when it's empty, so when several `ok`s arrive at once it sends one line where it should send several. BufferBuddy keeps a line queued for every pending clear to send, using the same steps as OctoPrint's `_continue_sending()`. OctoPrint's own code is never patched.
- Lines waiting in that queue still go out after a pause or cancel (OctoPrint doesn't clear it), so expect a few more short moves than stock OctoPrint, on top of what's already in the printer's buffer.
- It only sends an extra line if everything the printer hasn't moved into its command queue yet still fits in the printer's serial RX buffer (setting, default 128 bytes = Marlin's `RX_BUFFER_SIZE` default). ADVANCED_OK doesn't report that buffer, and overflowing it drops bytes and causes resends.
- During a resend it adds no lines and never swallows an `ok`. (The original swallowed them, which can hang a print on Marlin: OctoPrint then repeats a line Marlin has already processed, and Marlin silently ignores it.)
- On every connection it checks that the OctoPrint internals it relies on still exist. If any are missing, the sidebar shows "Unsupported OctoPrint version" and the plugin stays inactive.

## Changes from fflosi's version

- Inflight target is chendo's `BUFSIZE - 1` again, capped 5 lines below OctoPrint's resend history (`serial.lastLineBufferSize`, default 50) so every line in flight can still be resent.
- Still requires more than 2 free slots in the command buffer before sending, because OctoPrint also sends a line for the same `ok`.
- The manual `comm.py` patch and the "ok buffer size" instructions are gone (see above).
- Update checks point at this fork.

## Recomendations

- Check your buffer size (BUFSIZE) on Marlin. It should be at least half of planner buffer size (BLOCK_BUFFER_SIZE) + 2 for full usage.
    most of the times, increrasing BLOCK_BUFFER_SIZE on Marlin is already suficient to reduce buffer problems without using the plugin.
- TX_BUFFER_SIZE needs to be at least 32 for advanced ok. See marlin documentation.
- RX_BUFFER_SIZE - I'm not sure the parameter here, but I believe its better to have at least 2 time the MAX_CMD_SIZE for two commands on buffer.
    So at least 192. To be sure use 256 or 512 if you can.

## Tested with

This plugin has been tested with Marlin bugfix-2.0.x whti the bellow configurations on BTT SRK 2 and Octoprint 1.7.2 with changes decribed above - _continue_sending().
Marlin config:
#define BLOCK_BUFFER_SIZE 64
#define MAX_CMD_SIZE 96
#define BUFSIZE 64
#define TX_BUFFER_SIZE 128
#define RX_BUFFER_SIZE 512
#define ADVANCED_OK
#define BAUDRATE 500000

** Important ** Tested using USART connection instead of USB.

** No test was done on resend procedure.

## Setup

This fork is not on the plugin repository
Install from plugin Manager using the link bellow:
https://github.com/kagebarton/BufferBuddy/archive/0.2.0.zip

