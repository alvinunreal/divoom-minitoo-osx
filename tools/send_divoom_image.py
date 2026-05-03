#!/usr/bin/env python3
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import time
from pathlib import Path

import serial
import zstandard as zstd
from PIL import Image, ImageEnhance, ImageOps


CMD_APP_NEW_GIF_2020 = 0x8B
VIDEO_SUFFIXES = {".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi"}


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


def _normalize_size(size: int) -> int:
    if size <= 0 or size % 16 != 0 or size > 128:
        raise ValueError("size must be a positive multiple of 16 up to 128")
    return size


def _center_crop_resize(src: Image.Image, size: int) -> Image.Image:
    src = ImageOps.exif_transpose(src).convert("RGB")
    # Match app UX: center-crop square, then resize to the Divoom block grid.
    side = min(src.size)
    left = (src.width - side) // 2
    top = (src.height - side) // 2
    return src.crop((left, top, left + side, top + side)).resize((size, size), Image.Resampling.LANCZOS)


def _animation_payload(
    raw_frames: list[bytes], *, size: int, speed: int, level: int, window_log: int | None = 17
) -> bytes:
    if not raw_frames:
        raise ValueError("at least one frame is required")
    if len(raw_frames) > 255:
        raise ValueError("Divoom animation frame count is one byte; use <= 255 frames")
    size = _normalize_size(size)
    frame_len = size * size * 3
    for i, raw in enumerate(raw_frames):
        if len(raw) != frame_len:
            raise ValueError(f"frame {i} has {len(raw)} bytes, expected {frame_len}")
    if not 0 <= speed <= 0xFFFF:
        raise ValueError("speed must fit uint16 milliseconds")

    raw = b"".join(raw_frames)
    if window_log is None:
        compressor = zstd.ZstdCompressor(level=level, write_content_size=True)
    else:
        compressor = zstd.ZstdCompressor(
            compression_params=zstd.ZstdCompressionParameters.from_level(
                level, window_log=window_log, write_content_size=True
            )
        )
    zbytes = compressor.compress(raw)

    blocks = size // 16
    # From W2.c.f(): marker/frame/speed/rows/cols + big-endian compressed length + zstd frame.
    header = bytes([0x25, len(raw_frames)]) + u16be(speed) + bytes([blocks, blocks]) + u32be(len(zbytes))
    return header + zbytes


def build_payload(
    image_path: Path, speed: int = 1000, level: int = 17, size: int = 128, window_log: int | None = 17
) -> tuple[bytes, Image.Image]:
    size = _normalize_size(size)
    img = _center_crop_resize(Image.open(image_path), size)
    payload = _animation_payload([img.tobytes("raw", "RGB")], size=size, speed=speed, level=level, window_log=window_log)
    return payload, img


def build_video_payload(
    video_path: Path,
    *,
    speed: int,
    level: int = 17,
    size: int = 128,
    fps: float | None = None,
    max_frames: int = 60,
    window_log: int | None = 17,
    start: float | None = None,
    duration: float | None = None,
    brightness: float = 0.0,
    contrast: float = 1.0,
    saturation: float = 1.0,
    posterize_bits: int | None = None,
    sharpen: float = 1.0,
) -> tuple[bytes, Image.Image, dict[str, int | float]]:
    size = _normalize_size(size)
    if max_frames <= 0 or max_frames > 255:
        raise ValueError("max_frames must be in the range 1..255")

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg is required for video input; install it or send a still image")

    if start is not None and start < 0:
        raise ValueError("start must be non-negative")
    if duration is not None and duration <= 0:
        raise ValueError("duration must be positive")
    if contrast <= 0 or saturation < 0:
        raise ValueError("contrast must be positive and saturation must be non-negative")
    if posterize_bits is not None and not 1 <= posterize_bits <= 8:
        raise ValueError("posterize_bits must be in the range 1..8")
    if sharpen <= 0:
        raise ValueError("sharpen must be positive")

    vf_parts: list[str] = []
    if fps is not None:
        if fps <= 0:
            raise ValueError("fps must be positive")
        vf_parts.append(f"fps={fps}")
    if brightness != 0.0 or contrast != 1.0 or saturation != 1.0:
        vf_parts.append(f"eq=brightness={brightness}:contrast={contrast}:saturation={saturation}")
    vf_parts.extend(
        [
            f"scale={size}:{size}:force_original_aspect_ratio=increase",
            f"crop={size}:{size}",
        ]
    )
    cmd = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
    ]
    if start is not None:
        cmd += ["-ss", str(start)]
    cmd += [
        "-i",
        str(video_path),
    ]
    if duration is not None:
        cmd += ["-t", str(duration)]
    cmd += [
        "-vf",
        ",".join(vf_parts),
        "-frames:v",
        str(max_frames),
        "-an",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "rgb24",
        "-",
    ]
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    if proc.returncode != 0:
        err = proc.stderr.decode(errors="replace").strip()
        raise RuntimeError(f"ffmpeg failed: {err}")

    frame_len = size * size * 3
    if len(proc.stdout) < frame_len:
        raise RuntimeError("ffmpeg produced no complete video frames")
    frame_count = len(proc.stdout) // frame_len
    raw = proc.stdout[: frame_count * frame_len]
    raw_frames: list[bytes] = []
    preview: Image.Image | None = None
    for i in range(frame_count):
        img = Image.frombytes("RGB", (size, size), raw[i * frame_len : (i + 1) * frame_len])
        if posterize_bits is not None and posterize_bits < 8:
            img = ImageOps.posterize(img, posterize_bits)
        if sharpen != 1.0:
            img = ImageEnhance.Sharpness(img).enhance(sharpen)
        if preview is None:
            preview = img.copy()
        raw_frames.append(img.tobytes("raw", "RGB"))
    assert preview is not None
    payload = _animation_payload(raw_frames, size=size, speed=speed, level=level, window_log=window_log)
    meta: dict[str, int | float] = {
        "frames": frame_count,
        "size": size,
        "speed": speed,
        "zstd_len": int.from_bytes(payload[6:10], "big"),
    }
    if fps is not None:
        meta["fps"] = fps
    if start is not None:
        meta["start"] = start
    if duration is not None:
        meta["duration"] = duration
    if brightness != 0.0:
        meta["brightness"] = brightness
    if contrast != 1.0:
        meta["contrast"] = contrast
    if saturation != 1.0:
        meta["saturation"] = saturation
    if posterize_bits is not None:
        meta["posterize_bits"] = posterize_bits
    if sharpen != 1.0:
        meta["sharpen"] = sharpen
    return payload, preview, meta


def build_media_payload(
    path: Path,
    *,
    speed: int,
    level: int = 17,
    size: int = 128,
    fps: float | None = None,
    max_frames: int = 60,
    window_log: int | None = 17,
    start: float | None = None,
    duration: float | None = None,
    brightness: float = 0.0,
    contrast: float = 1.0,
    saturation: float = 1.0,
    posterize_bits: int | None = None,
    sharpen: float = 1.0,
) -> tuple[bytes, Image.Image, dict[str, int | float | str]]:
    if path.suffix.lower() in VIDEO_SUFFIXES:
        payload, preview, video_meta = build_video_payload(
            path,
            speed=speed,
            level=level,
            size=size,
            fps=fps,
            max_frames=max_frames,
            window_log=window_log,
            start=start,
            duration=duration,
            brightness=brightness,
            contrast=contrast,
            saturation=saturation,
            posterize_bits=posterize_bits,
            sharpen=sharpen,
        )
        meta: dict[str, int | float | str] = dict(video_meta)
        meta["kind"] = "video"
        return payload, preview, meta
    payload, preview = build_payload(path, speed=speed, level=level, size=size, window_log=window_log)
    return payload, preview, {"kind": "image", "frames": 1, "size": size, "speed": speed, "zstd_len": int.from_bytes(payload[6:10], "big")}


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


def _speed_from_args(speed: int | None, fps: float | None, default: int) -> int:
    if speed is not None:
        return speed
    if fps is not None:
        if fps <= 0:
            raise ValueError("fps must be positive")
        return max(1, round(1000 / fps))
    return default


def main() -> int:
    parser = argparse.ArgumentParser(description="Build/send a still image or MP4-style video animation to Divoom MiniToo")
    parser.add_argument("media", type=Path)
    parser.add_argument("--port", default="/dev/cu.DivoomMiniToo-Audio")
    parser.add_argument("--delay", type=float, default=0.006, help="seconds between chunk writes")
    parser.add_argument("--speed", type=int, default=None, help="frame duration in milliseconds; default 1000 for images or derived from --fps for video")
    parser.add_argument("--fps", type=float, default=6.0, help="video sampling fps; also derives speed when --speed is omitted")
    parser.add_argument("--max-frames", type=int, default=10, help="maximum video frames to send, 1..255")
    parser.add_argument("--size", type=int, default=None, help="square output size; defaults to 128 for images, 64 for video")
    parser.add_argument("--start", type=float, default=None, help="video start time in seconds")
    parser.add_argument("--duration", type=float, default=None, help="video duration limit in seconds")
    parser.add_argument("--brightness", type=float, default=0.0, help="ffmpeg eq brightness, e.g. 0.15")
    parser.add_argument("--contrast", type=float, default=1.0, help="ffmpeg eq contrast")
    parser.add_argument("--saturation", type=float, default=1.0, help="ffmpeg eq saturation")
    parser.add_argument("--posterize-bits", type=int, default=None, help="reduce color bits per channel after resize; helps 128px video compress")
    parser.add_argument("--sharpen", type=float, default=1.0, help="Pillow sharpness multiplier after resize")
    parser.add_argument("--zstd-level", type=int, default=17)
    parser.add_argument("--zstd-window-log", type=int, default=17, help="zstd window log; Android captures use 17 (128 KiB)")
    parser.add_argument("--no-wait-request", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--out-dir", type=Path, default=Path("captures/mac-send"))
    args = parser.parse_args()

    args.out_dir.mkdir(parents=True, exist_ok=True)
    is_video = args.media.suffix.lower() in VIDEO_SUFFIXES
    size = args.size if args.size is not None else (64 if is_video else 128)
    speed = _speed_from_args(args.speed, args.fps if is_video else None, 1000)
    payload, preview, meta = build_media_payload(
        args.media,
        speed=speed,
        level=args.zstd_level,
        window_log=args.zstd_window_log,
        size=size,
        fps=args.fps if is_video else None,
        max_frames=args.max_frames,
        start=args.start if is_video else None,
        duration=args.duration if is_video else None,
        brightness=args.brightness if is_video else 0.0,
        contrast=args.contrast if is_video else 1.0,
        saturation=args.saturation if is_video else 1.0,
        posterize_bits=args.posterize_bits if is_video else None,
        sharpen=args.sharpen if is_video else 1.0,
    )
    packets = build_packets(payload)

    stem = args.media.stem
    preview.save(args.out_dir / f"{stem}-preview-128.png")
    preview.resize((512, 512), Image.Resampling.NEAREST).save(args.out_dir / f"{stem}-preview-4x.png")
    (args.out_dir / f"{stem}-payload.bin").write_bytes(payload)
    (args.out_dir / f"{stem}-packets.bin").write_bytes(b"".join(packets))

    print(
        f"kind={meta['kind']} frames={meta['frames']} size={meta['size']} speed={meta['speed']} "
        f"payload_len={len(payload)} zstd_len={meta['zstd_len']} packets={len(packets)}"
    )
    print(f"start={packets[0].hex()}")
    print(f"first_chunk={hexdump(packets[1])}")
    print(f"last_chunk_len={len(packets[-1])}")

    if args.dry_run:
        return 0

    send_packets(args.port, packets, args.delay, wait_request=not args.no_wait_request)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
