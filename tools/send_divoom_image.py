#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import serial
import zstandard as zstd
from PIL import Image, ImageOps


CMD_APP_NEW_GIF_2020 = 0x8B


def u16le(n: int) -> bytes:
    return n.to_bytes(2, "little")


def u32le(n: int) -> bytes:
    return n.to_bytes(4, "little")


def u16be(n: int) -> bytes:
    return n.to_bytes(2, "big")


def u32be(n: int) -> bytes:
    return n.to_bytes(4, "big")


def frame(cmd: int, body: bytes = b"") -> bytes:
    # Divoom new-mode SPP frame, from com.divoom.Divoom.bluetooth.s.k().
    out = bytearray(7 + len(body))
    out[0] = 0x01
    declared = len(out) - 4
    out[1:3] = u16le(declared)
    out[3] = cmd & 0xFF
    out[4 : 4 + len(body)] = body
    checksum = sum(out[1 : len(out) - 3]) & 0xFFFF
    out[-3:-1] = u16le(checksum)
    out[-1] = 0x02
    return bytes(out)


def build_payload(image_path: Path, speed: int = 1000, level: int = 17) -> tuple[bytes, Image.Image]:
    src = Image.open(image_path)
    src = ImageOps.exif_transpose(src).convert("RGB")
    # Match app UX: center-crop square, then 128x128 RGB888.
    side = min(src.size)
    left = (src.width - side) // 2
    top = (src.height - side) // 2
    img = src.crop((left, top, left + side, top + side)).resize((128, 128), Image.Resampling.LANCZOS)

    raw = img.tobytes("raw", "RGB")
    zbytes = zstd.ZstdCompressor(level=level, write_content_size=True).compress(raw)

    # From W2.c.f(): marker/frame/speed/rows/cols + big-endian compressed length + zstd frame.
    header = bytes([0x25, 0x01]) + u16be(speed) + bytes([0x08, 0x08]) + u32be(len(zbytes))
    return header + zbytes, img


def build_packets(payload: bytes) -> list[bytes]:
    packets: list[bytes] = []
    # Start command body from CmdManager.n(): 00 + total payload length u32le.
    packets.append(frame(CMD_APP_NEW_GIF_2020, b"\x00" + u32le(len(payload))))

    chunk_size = 256
    for seq, off in enumerate(range(0, len(payload), chunk_size)):
        chunk = payload[off : off + chunk_size]
        # From e3.h.f(): prefix 01 + total_len u32le + seq u16le + payload chunk.
        body = b"\x01" + u32le(len(payload)) + u16le(seq) + chunk
        packets.append(frame(CMD_APP_NEW_GIF_2020, body))
    return packets


def hexdump(b: bytes, max_len: int = 64) -> str:
    shown = b[:max_len].hex(" ")
    return shown + (" ..." if len(b) > max_len else "")


def read_available(ser: serial.Serial, wait: float = 0.25) -> bytes:
    end = time.time() + wait
    buf = bytearray()
    while time.time() < end:
        n = ser.in_waiting
        if n:
            buf.extend(ser.read(n))
            end = time.time() + wait
        else:
            time.sleep(0.02)
    return bytes(buf)


def send_packets(port: str, packets: list[bytes], delay: float, wait_request: bool) -> None:
    print(f"opening {port}...")
    with serial.Serial(port, baudrate=115200, timeout=0.2, write_timeout=3) as ser:
        time.sleep(1.0)
        stale = read_available(ser, 0.2)
        if stale:
            print(f"stale_rx {len(stale)}: {hexdump(stale)}")

        print(f"tx start {len(packets[0])}: {packets[0].hex()}")
        ser.write(packets[0])
        ser.flush()

        if wait_request:
            print("waiting for device request 0x8b...")
            deadline = time.time() + 8
            got = bytearray()
            while time.time() < deadline:
                got.extend(read_available(ser, 0.15))
                if bytes.fromhex("010700048b550001ec0002") in got or b"\x8b\x55\x00" in got:
                    print(f"rx request {len(got)}: {hexdump(bytes(got))}")
                    break
            else:
                print(f"warning: no explicit request seen; rx={hexdump(bytes(got))}")

        for i, pkt in enumerate(packets[1:]):
            ser.write(pkt)
            ser.flush()
            if i == 0 or i == len(packets) - 2 or (i + 1) % 10 == 0:
                print(f"tx chunk {i}/{len(packets)-2} len={len(pkt)}")
            time.sleep(delay)

        tail = read_available(ser, 2.0)
        if tail:
            print(f"rx tail {len(tail)}: {hexdump(tail, 160)}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Send a 128x128 RGB photo to Divoom MiniToo over macOS Bluetooth SPP")
    parser.add_argument("image", type=Path)
    parser.add_argument("--port", default="/dev/cu.DivoomMiniToo-Audio")
    parser.add_argument("--delay", type=float, default=0.006, help="seconds between chunk writes")
    parser.add_argument("--speed", type=int, default=1000)
    parser.add_argument("--zstd-level", type=int, default=17)
    parser.add_argument("--no-wait-request", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--out-dir", type=Path, default=Path("captures/mac-send"))
    args = parser.parse_args()

    args.out_dir.mkdir(parents=True, exist_ok=True)
    payload, preview = build_payload(args.image, speed=args.speed, level=args.zstd_level)
    packets = build_packets(payload)

    stem = args.image.stem
    preview.save(args.out_dir / f"{stem}-preview-128.png")
    preview.resize((512, 512), Image.Resampling.NEAREST).save(args.out_dir / f"{stem}-preview-4x.png")
    (args.out_dir / f"{stem}-payload.bin").write_bytes(payload)
    (args.out_dir / f"{stem}-packets.bin").write_bytes(b"".join(packets))

    print(f"payload_len={len(payload)} zstd_len={int.from_bytes(payload[6:10], 'big')} packets={len(packets)}")
    print(f"start={packets[0].hex()}")
    print(f"first_chunk={hexdump(packets[1])}")
    print(f"last_chunk_len={len(packets[-1])}")

    if args.dry_run:
        return 0

    send_packets(args.port, packets, args.delay, wait_request=not args.no_wait_request)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
