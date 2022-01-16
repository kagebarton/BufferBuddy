# BufferBuddy

BufferBuddy aims to prevent print quality issues when printing over USB with Octoprint. Designed for Marlin with `ADVANCED_OK` support, but may work for other firmwares that also support `ADVANCED_OK` output.

This is a fork from https://github.com/chendo/BufferBuddy for personal use. I'm sharing is case anyone wishes to use it.

**WARNING:** See details on original page https://github.com/chendo/BufferBuddy**

This plugin requires `ADVANCED_OK` to function.

## Main changes from original

- No longer check if there is command on queue before calling "comm._continue_sending()". Octoprint limits the addition of commands to queue anyway.
- Changed self.inflight_target to be half of planner_buffer_size. It means we have full planner + 50% on buffer.
- will check if command_buffer_avail is > 2 as we are going to send an additional command but _monitor thread of Octoprint will also send due to OK received, so we need to have at least 2 spots
- no longer change _clear_to_send._max. User must change _clear_to_send._max to at least 2 on Octoprint interface -- firmware protocol "ok buffer size"
- On Octoprint code _continue_sending() is limiting the command queue size to 1. 
    The line below needs to be changed on comm.py or the plugin will not work.
    
    while self._active and not self._send_queue.qsize():
    -- Change to
    while self._active and (self._send_queue.qsize() < self._ack_max):
    
    This will set the command queue to the same size as _clear_to_send._max, so it will work as default for all users and only affect those who change "ok buffer size" on the interface. Eventually i will ask for an update on octoprint, but right now you need to change that manually.



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
https://github.com/fflosi/BufferBuddy/releases/download/0.1.1/v0.1.1.zip

