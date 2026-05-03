#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import socket
from pathlib import Path

import send_divoom_image


def submit(host: str, port: int, packets_path: Path, delay: float, dry_run: bool) -> dict:
    req = {
        "packets": str(packets_path.resolve()),
        "delay": delay,
        "dryRun": dry_run,
    }
    with socket.create_connection((host, port), timeout=10) as s:
        s.sendall(json.dumps(req).encode() + b"\n")
        s.shutdown(socket.SHUT_WR)
        chunks = []
        while True:
            b = s.recv(4096)
            if not b:
                break
            chunks.append(b)
    data = b"".join(chunks).strip()
    if not data:
        raise RuntimeError("empty daemon response")
    return json.loads(data)


def main() -> int:
    parser = argparse.ArgumentParser(description="Convert image/video and submit it to the Divoom RFCOMM daemon")
    parser.add_argument("media", type=Path)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=40583)
    parser.add_argument("--delay", type=float, default=0.012)
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
    parser.add_argument("--out-dir", type=Path, default=Path("captures/mac-send"))
    parser.add_argument("--daemon-dry-run", action="store_true", help="ask daemon to parse but not send")
    parser.add_argument("--build-only", action="store_true", help="only build packet files; do not contact daemon")
    args = parser.parse_args()

    args.out_dir.mkdir(parents=True, exist_ok=True)
    is_video = args.media.suffix.lower() in send_divoom_image.VIDEO_SUFFIXES
    size = args.size if args.size is not None else (64 if is_video else 128)
    speed = send_divoom_image._speed_from_args(args.speed, args.fps if is_video else None, 1000)
    payload, preview, meta = send_divoom_image.build_media_payload(
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
    packets = send_divoom_image.build_packets(payload)

    stem = args.media.stem
    preview.save(args.out_dir / f"{stem}-preview-128.png")
    preview.resize((512, 512), send_divoom_image.Image.Resampling.NEAREST).save(args.out_dir / f"{stem}-preview-4x.png")
    payload_path = args.out_dir / f"{stem}-payload.bin"
    packet_path = args.out_dir / f"{stem}-packets-lenpref.bin"
    payload_path.write_bytes(payload)
    out = bytearray()
    for p in packets:
        out += len(p).to_bytes(2, "little") + p
    packet_path.write_bytes(out)

    print(f"media={args.media}")
    print(
        f"kind={meta['kind']} frames={meta['frames']} size={meta['size']} speed={meta['speed']} "
        f"payload={payload_path} len={len(payload)} zstd_len={meta['zstd_len']}"
    )
    print(f"packets={packet_path} count={len(packets)} bytes={sum(map(len, packets))}")
    print(f"preview={args.out_dir / f'{stem}-preview-4x.png'}")

    if args.build_only:
        return 0

    resp = submit(args.host, args.port, packet_path, args.delay, args.daemon_dry_run)
    print("daemon:", json.dumps(resp, ensure_ascii=False))
    return 0 if resp.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
