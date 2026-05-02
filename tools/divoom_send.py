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
    parser = argparse.ArgumentParser(description="Convert image and submit it to the Divoom RFCOMM daemon")
    parser.add_argument("image", type=Path)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=40583)
    parser.add_argument("--delay", type=float, default=0.012)
    parser.add_argument("--speed", type=int, default=1000)
    parser.add_argument("--zstd-level", type=int, default=17)
    parser.add_argument("--out-dir", type=Path, default=Path("captures/mac-send"))
    parser.add_argument("--daemon-dry-run", action="store_true", help="ask daemon to parse but not send")
    parser.add_argument("--build-only", action="store_true", help="only build packet files; do not contact daemon")
    args = parser.parse_args()

    args.out_dir.mkdir(parents=True, exist_ok=True)
    payload, preview = send_divoom_image.build_payload(args.image, speed=args.speed, level=args.zstd_level)
    packets = send_divoom_image.build_packets(payload)

    stem = args.image.stem
    preview.save(args.out_dir / f"{stem}-preview-128.png")
    preview.resize((512, 512), send_divoom_image.Image.Resampling.NEAREST).save(args.out_dir / f"{stem}-preview-4x.png")
    payload_path = args.out_dir / f"{stem}-payload.bin"
    packet_path = args.out_dir / f"{stem}-packets-lenpref.bin"
    payload_path.write_bytes(payload)
    out = bytearray()
    for p in packets:
        out += len(p).to_bytes(2, "little") + p
    packet_path.write_bytes(out)

    print(f"image={args.image}")
    print(f"payload={payload_path} len={len(payload)} zstd_len={int.from_bytes(payload[6:10], 'big')}")
    print(f"packets={packet_path} count={len(packets)} bytes={sum(map(len, packets))}")
    print(f"preview={args.out_dir / f'{stem}-preview-4x.png'}")

    if args.build_only:
        return 0

    resp = submit(args.host, args.port, packet_path, args.delay, args.daemon_dry_run)
    print("daemon:", json.dumps(resp, ensure_ascii=False))
    return 0 if resp.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
