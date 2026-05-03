# Divoom MiniToo Bluetooth Photo Protocol

Notes from reversing the Divoom Android app + Android Bluetooth snoop captures, then validating sends from macOS.

## Device / transport

- Device: `Divoom MiniToo-Audio`
- Bluetooth address used from macOS: `B1:21:81:B1:F0:84`
- Transport: Bluetooth Classic SPP over RFCOMM
- Working app protocol channel from macOS: RFCOMM channel `1`, opened directly via `IOBluetooth`.
- The macOS serial device `/dev/cu.DivoomMiniToo-Audio` exists, but was unreliable for this protocol. Direct RFCOMM worked.

SDP advertised services observed from macOS:

```text
JL_SPP  channel 10
JL_SPP  channel 1
SMS/MMS channel 17
JL_HFP  channel 4
```

Direct `IOBluetooth` findings:

- Channel `1` works for the Divoom app protocol when opened directly.
- Channel `10` was advertised but failed to open from macOS during testing.
- Channel `17` opened but is not the app protocol; it produced unrelated SMS/MMS-ish data.

## macOS connection learning

If the Divoom is connected as an audio device in macOS, opening the app RFCOMM path can fail or interact with the wrong profile.

Validated reliable flow:

```bash
blueutil --disconnect B1:21:81:B1:F0:84 || true
tools/divoom-rfcomm-send B1:21:81:B1:F0:84 1 <packets-lenpref.bin> 0.012
```

This avoids the noisy/visible audio profile and opens the RFCOMM app channel directly. The disconnect causes an audible reconnect/noise if audio was active, so the next goal is a daemon that opens RFCOMM once and keeps it open for multiple sends.

Preferred future UX:

- Start daemon once.
- Daemon opens RFCOMM channel `1` directly.
- Keep the channel open.
- Send multiple image jobs through the daemon without repeated disconnect/reconnect.

## Generic frame format

From decompiled Android method `com.divoom.Divoom.bluetooth.s.k()`:

```text
01 <declared_len_le16> <cmd> <body...> <checksum_le16> 02
```

Fields:

- `0x01`: start byte
- `declared_len`: `total_frame_len - 4`, little-endian uint16
  - Equivalent: `len(body) + 3`
- `cmd`: one-byte command
- `body`: command-specific bytes
- `checksum`: little-endian uint16
  - `sum(frame[1 : len(frame)-3]) & 0xffff`
- `0x02`: end byte

Python implementation:

```python
def frame(cmd: int, body: bytes = b"") -> bytes:
    out = bytearray(7 + len(body))
    out[0] = 0x01
    declared = len(out) - 4
    out[1:3] = declared.to_bytes(2, "little")
    out[3] = cmd & 0xFF
    out[4:4 + len(body)] = body
    checksum = sum(out[1:len(out) - 3]) & 0xFFFF
    out[-3:-1] = checksum.to_bytes(2, "little")
    out[-1] = 0x02
    return bytes(out)
```

## Photo / animation command

Command:

```text
0x8b = SPP_APP_NEW_GIF_CMD2020
```

From decompiled enum:

```java
SPP_APP_NEW_GIF_CMD2020(139)
```

### Start packet

Before chunks, send a start packet:

```text
cmd  = 0x8b
body = 00 <total_payload_len_le32>
```

Example from captured payload length `11937` (`0x2ea1`):

```hex
01 08 00 8b 00 a1 2e 00 00 ae 01 02
```

### Device request

The device asks for animation data with:

```hex
01 07 00 04 8b 55 00 01 ec 00 02
```

Interpretation from app logs/decompiled code:

- Outer command: `0x04`
- Extended/app command type: `0x8b`
- `bArr[6] == 0`: “device requests animation send”

The app then sends all chunks.

### Chunk packet

Each chunk uses command `0x8b`:

```text
cmd  = 0x8b
body = 01 <total_payload_len_le32> <seq_le16> <chunk_payload...>
```

Observed chunking:

- Full chunk payload: `256` bytes
- Full framed chunk length: `270` bytes
- Sequence starts at `0`
- Last chunk is shorter

Example first chunk prefix:

```hex
01 0a 01 8b 01 a1 2e 00 00 00 00 ...payload... checksum checksum 02
```

Decoded:

```text
01          start
0a 01       declared len = 266
8b          cmd
01          chunk marker
a1 2e 00 00 total payload len = 11937
00 00       seq = 0
...         256-byte chunk payload
```

### Final ACK

After a successful transfer, device replied:

```hex
01 09 00 04 bd 55 13 01 05 00 38 01 02
```

This matched Android capture and direct macOS sends.

## Encoded image payload

For the captured Android photo send, the reassembled payload began:

```hex
25 01 03 e8 08 08 00 00 2e 97 28 b5 2f fd ...
```

Layout from decompiled `W2.c.f()`:

```text
25                  format/type marker, decimal 37
01                  frame count / valid count
03 e8               speed, big-endian uint16; 1000 here
08                  row count
08                  column count
00 00 2e 97         compressed zstd length, big-endian uint32; 11927 here
28 b5 2f fd ...     Zstandard frame
```

Zstd details from the captured payload:

- Compressed length: `11927`
- Decompressed length: `49152`
- Raw pixel format: RGB888
- Dimensions: `128x128`
- `128 * 128 * 3 = 49152`

## Image conversion

Current sender behavior for arbitrary images:

1. EXIF transpose.
2. Convert to RGB.
3. Center-crop square.
4. Resize to `128x128`.
5. Use raw RGB888 bytes.
6. Zstd-compress with level `17`.
7. Prefix encoded payload header:

```text
25 01 <speed_be16> 08 08 <zstd_len_be32> <zstd_frame>
```

Then wrap into `0x8b` start + chunks.

## GIF / video conversion

Android GIF/video sends use the same `0x8b` start/request/chunk transport. The encoded payload generalizes the still-image header:

```text
25 <frame_count_u8> <speed_ms_be16> <row_blocks_u8> <col_blocks_u8> <zstd_len_be32> <zstd_frame>
```

Where:

- `row_blocks` / `col_blocks` are 16-pixel blocks.
- Pixel dimensions are `col_blocks * 16` by `row_blocks * 16`.
- Decompressed bytes are concatenated RGB888 frames.
- Expected decompressed length is `frame_count * width * height * 3`.
- `frame_count` is one byte, so current tooling limits video/GIF sends to `<=255` frames.

MP4/video support in `tools/divoom_send.py` shells out to `ffmpeg` to sample frames, center-crop square, resize, concatenate RGB888 frames, zstd-compress, and send through the same daemon path.

Example practical send:

```bash
.venv/bin/python tools/divoom_send.py input.mp4
```

Early MP4 tests falsely suggested a small raw-size limit because Python zstd's default window was too large. The key fix was matching Android's zstd window.

Critical zstd finding:

- Android's working high-frame payloads use a `128 KiB` zstd window (`window_log=17`).
- Python zstd's default can choose a larger window for multi-frame `128x128` video.
- With a larger window, the device may ACK the transfer but display black/glitched output.
- Current tooling defaults to `--zstd-window-log 17` to match Android captures.

Validated Android-style and macOS-generated sends:

- Resent captured Android payload `128x128`, `16` frames, `speed=75`, payload `5124` bytes: works/quality good.
- MP4-derived `128x128`, `4` frames, `speed=100`, `posterize-bits=4`, zstd window `17`: works/loops.
- MP4-derived `128x128`, `10` frames, `speed=100`, `posterize-bits=4`, zstd window `17`: works/looks good.
- MP4-derived `128x128`, `16` frames, `speed=75`, `posterize-bits=4`, zstd window `17`: works/looks best in early testing.
- MP4-derived `128x128`, `16` frames, `speed=75`, `posterize-bits=5`: works with better color but transfer is larger/slower.
- MP4-derived `128x128`, `26` frames, `speed=100`, `posterize-bits=4`: works/loops well.
- MP4-derived `128x128`, `26` frames, `speed=100`, `posterize-bits=5`: works but transfer feels too large/slow.
- MP4-derived `128x128`, `32` frames, `speed=100`, `posterize-bits=4`: works, but transfer time starts to feel less ideal.
- `48x48` / `3x3` blocks glitched in testing; stick to observed stable block sizes: `1x1`, `2x2`, `4x4`, `8x8` (`16`, `32`, `64`, `128` px).

Practical rules:

- Prefer `128x128` now that zstd `window_log=17` is fixed.
- Keep zstd window at `17`.
- Do not activate a custom face after upload unless you intentionally want to switch away from the uploaded animation; doing so can make the animation appear only briefly.
- Posterizing/noise reduction helps MP4-derived video compress more like phone GIFs. `posterize-bits=4` is fast/small; `posterize-bits=5` has better color but larger transfer.
- More frames increase both duration and transfer time. A good balance is around `20` frames at `100ms` (`~2s`).

Balanced recommended profile:

```bash
.venv/bin/python tools/divoom_send.py input.mp4 \
  --size 128 \
  --max-frames 20 \
  --fps 10 \
  --speed 100 \
  --posterize-bits 4
```

Higher-color shorter profile:

```bash
.venv/bin/python tools/divoom_send.py input.mp4 \
  --size 128 \
  --max-frames 16 \
  --fps 13.333 \
  --speed 75 \
  --posterize-bits 5
```

Longer-duration profile:

```bash
.venv/bin/python tools/divoom_send.py input.mp4 \
  --size 128 \
  --max-frames 26 \
  --fps 10 \
  --speed 100 \
  --posterize-bits 4
```

Build-only preview/packet generation:

```bash
.venv/bin/python tools/divoom_send.py input.mp4 --build-only
```

Full `128x128` video can generate large transfers; reduce `--max-frames`, use `--posterize-bits 4`, or lower `--fps` if Bluetooth transfer time is too high. Avoid reducing below `128x128` unless transfer time matters more than sharpness.

## Artifacts / tools

Captured/reconstructed artifacts:

- `captures/resend-btlogs/hci_snoop_2026_05_03_00_27_39.cfa`
- `captures/reconstructed/photo-transfer-reassembled.bin`
- `captures/reconstructed/photo-transfer.zst`
- `captures/reconstructed/photo-transfer-decompressed.bin`
- `captures/reconstructed/photo-transfer-preview.png`
- `captures/reconstructed/protocol-notes.md`

Scripts:

- `tools/parse_divoom_spp.py`
  - Parses tshark-exported SPP payloads.
  - Reassembles photo payload.
  - Strips per-frame checksums correctly.
  - Decompresses Zstd.
- `tools/send_divoom_image.py`
  - Converts an image or video/GIF-like frame sequence to Divoom payload/packet files.
  - Uses zstd `window_log=17` by default to match Android GIF sends.
  - Its pyserial send path is not preferred.
- `tools/DivoomRFCOMMSend.swift`
  - Direct IOBluetooth RFCOMM sender.
- `tools/divoom-rfcomm-send`
  - Compiled Swift sender binary.
- `tools/divoom-menubar`
  - macOS menu-bar controller for daemon lifecycle and image sending.

Working direct send example:

```bash
.venv/bin/python tools/send_divoom_image.py fixer.png --dry-run
blueutil --disconnect B1:21:81:B1:F0:84 || true
tools/divoom-rfcomm-send B1:21:81:B1:F0:84 1 captures/mac-send/fixer-packets-lenpref.bin 0.012
```

Known-good result:

- `fixer.png` was converted and sent successfully from macOS.
- Device returned final ACK:

```hex
01 09 00 04 bd 55 13 01 05 00 38 01 02
```

## Next improvement: daemon

Goal: avoid disconnect/reconnect noise for every image.

Implemented tools:

- `tools/DivoomDaemon.swift`
- `tools/divoom-daemon`
- `tools/divoom_send.py`
  - Preferred CLI for image/video sends through the daemon.
  - Supports MP4/video tuning: `--start`, `--duration`, `--fps`, `--speed`, `--max-frames`, `--size`, `--brightness`, `--contrast`, `--saturation`, `--posterize-bits`, `--sharpen`, `--zstd-window-log`.

Daemon behavior:

- Build a small daemon that opens direct RFCOMM channel `1` once.
- Keep the channel alive.
- Listen on localhost TCP `127.0.0.1:40583`.
- Accept JSON jobs containing a length-prefixed packet file path.
- For each job:
  - convert image
  - build packets
  - send start packet
  - wait for request if available
  - send chunks
  - wait for final ACK
- This should avoid repeated Bluetooth audio reconnect sounds.

Current usage:

```bash
# One-time start. If this fails with ret=0x-1ffffd44, disconnect the audio
# profile once, then start the daemon again.
blueutil --disconnect B1:21:81:B1:F0:84 || true
tools/divoom-daemon B1:21:81:B1:F0:84 1 40583
```

In another shell:

```bash
.venv/bin/python tools/divoom_send.py fixer.png
```

Dry-run through daemon, parsing the packet file but not sending to device:

```bash
.venv/bin/python tools/divoom_send.py fixer.png --daemon-dry-run
```

Build packet files only:

```bash
.venv/bin/python tools/divoom_send.py fixer.png --build-only
```

Validated smoke test:

```text
daemon: {"ok": true, "packets": 177, "message": "dry run", "bytes": 47384}
```

Expected successful real-send daemon response:

```json
{"ok":true,"message":"sent","packets":177,"bytes":47384,"sawRequest":true,"sawAck":true}
```

Important operational note:

- The daemon may need a one-time audio-profile disconnect before it can open RFCOMM channel `1`.
- Once the daemon holds the RFCOMM channel open, subsequent image sends should not require repeated disconnect/reconnect.
- In testing, daemon jobs sometimes reported `sent but final ACK not observed` while the device still updated correctly. Treat this as a daemon callback/ACK-observation issue, not necessarily a send failure.

## Menu-bar controller

Build:

```bash
swiftc tools/DivoomMenuBar.swift -framework AppKit -o tools/divoom-menubar
```

Run from the repo root:

```bash
tools/divoom-menubar
```

Menu-bar title:

- `◇ Divoom` = daemon stopped
- `◆ Divoom` = daemon running

Menu actions:

- `Send Image…`
  - Choose a PNG/JPEG/image file.
  - Uses `.venv/bin/python tools/divoom_send.py <image>`.
  - Disabled while daemon is stopped.
- `Activate Custom Face 1`
  - Sends captured `Channel/SetClockSelectId` for `ClockId=984`.
  - Uses `.venv/bin/python tools/divoom_clock.py custom1`.
  - Disabled while daemon is stopped.
- `Activate Custom Face 2`
  - Sends captured `Channel/SetClockSelectId` for `ClockId=986`.
  - Uses `.venv/bin/python tools/divoom_clock.py custom2`.
  - Disabled while daemon is stopped.
- `Start Daemon`
  - Starts `tools/divoom-daemon` without disconnecting audio first.
  - Disabled while daemon is already running.
- `Disconnect Audio + Start Daemon`
  - Runs `blueutil --disconnect B1:21:81:B1:F0:84`, then starts daemon.
  - Use this if normal start fails because macOS audio owns the RFCOMM channel.
  - Disabled while daemon is already running.
- `Stop Daemon`
  - Terminates running `tools/divoom-daemon` processes.
  - Disabled while daemon is stopped.
- `Restart Daemon`
  - Stops daemon, disconnects audio, then starts daemon.
- `Disconnect Divoom Audio`
  - Lets phone/app connect again or frees RFCOMM.
  - Disabled when audio profile is already disconnected.
- `Reconnect Divoom Audio`
  - Requests normal macOS Bluetooth reconnect.
  - Disabled when audio profile is already connected.
- `Open Captures Folder`
  - Opens `captures/mac-send`.
- `Open Protocol Notes`
  - Opens this file.

Current UX notes:

- The menu refreshes when opened, so daemon/audio status should reflect current state.
- The app avoids modal success/error popups; status appears as the `Last:` line in the menu.
- Current implementation is a lean menu-bar binary, not a packaged `.app` yet. If desired, wrap it in a proper LaunchAgent or `.app` bundle later.

## Custom face selection

Captured Android actions:

```text
custom face 1 -> ClockId 984
custom face 2 -> ClockId 986
```

The app sends JSON command `Channel/SetClockSelectId` as generic command `0x01`.

Captured body for custom face 1:

```json
{"ClockId":984,"Command":"Channel/SetClockSelectId","DeviceId":600111083,"DevicePassword":1777733348,"Language":"en","LcdIndependence":0,"LcdIndex":0,"PageIndex":0,"ParentClockId":0,"ParentItemId":"","Token":1777741943,"UserId":404779143}
```

Captured body for custom face 2 is the same except `ClockId=986`.

CLI:

```bash
.venv/bin/python tools/divoom_clock.py custom1
.venv/bin/python tools/divoom_clock.py custom2
# or any explicit clock id:
.venv/bin/python tools/divoom_clock.py 984
```
