#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import socket
from pathlib import Path

import send_divoom_image


DEFAULT_DEVICE_ID = 600111083
DEFAULT_DEVICE_PASSWORD = 1777733348
DEFAULT_TOKEN = 1777741943
DEFAULT_USER_ID = 404779143


def submit(host: str, port: int, packets_path: Path, delay: float = 0.012, dry_run: bool = False) -> dict:
    req = {"packets": str(packets_path.resolve()), "delay": delay, "dryRun": dry_run}
    with socket.create_connection((host, port), timeout=10) as s:
        s.sendall(json.dumps(req).encode() + b"\n")
        s.shutdown(socket.SHUT_WR)
        chunks = []
        while True:
            b = s.recv(4096)
            if not b:
                break
            chunks.append(b)
    return json.loads(b"".join(chunks).strip())


def build_select_clock_packet(
    clock_id: int,
    device_id: int = DEFAULT_DEVICE_ID,
    device_password: int = DEFAULT_DEVICE_PASSWORD,
    token: int = DEFAULT_TOKEN,
    user_id: int = DEFAULT_USER_ID,
) -> bytes:
    # Match Android JSON key order as captured for stable checksums/bytes.
    payload = {
        "ClockId": clock_id,
        "Command": "Channel/SetClockSelectId",
        "DeviceId": device_id,
        "DevicePassword": device_password,
        "Language": "en",
        "LcdIndependence": 0,
        "LcdIndex": 0,
        "PageIndex": 0,
        "ParentClockId": 0,
        "ParentItemId": "",
        "Token": token,
        "UserId": user_id,
    }
    body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()
    return send_divoom_image.frame(0x01, body)


def main() -> int:
    parser = argparse.ArgumentParser(description="Select a Divoom custom clock face over the RFCOMM daemon")
    parser.add_argument("clock", help="clock id or shortcut: 1/custom1=984, 2/custom2=986")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=40583)
    parser.add_argument("--out-dir", type=Path, default=Path("captures/mac-send"))
    parser.add_argument("--daemon-dry-run", action="store_true")
    parser.add_argument("--build-only", action="store_true")
    parser.add_argument("--device-id", type=int, default=DEFAULT_DEVICE_ID)
    parser.add_argument("--device-password", type=int, default=DEFAULT_DEVICE_PASSWORD)
    parser.add_argument("--token", type=int, default=DEFAULT_TOKEN)
    parser.add_argument("--user-id", type=int, default=DEFAULT_USER_ID)
    args = parser.parse_args()

    shortcuts = {"1": 984, "custom1": 984, "face1": 984, "2": 986, "custom2": 986, "face2": 986}
    clock_id = shortcuts.get(args.clock.lower(), None)
    if clock_id is None:
        clock_id = int(args.clock)

    args.out_dir.mkdir(parents=True, exist_ok=True)
    packet = build_select_clock_packet(
        clock_id,
        device_id=args.device_id,
        device_password=args.device_password,
        token=args.token,
        user_id=args.user_id,
    )
    out_path = args.out_dir / f"clock-{clock_id}-packets-lenpref.bin"
    out_path.write_bytes(len(packet).to_bytes(2, "little") + packet)

    print(f"clock_id={clock_id}")
    print(f"packet={out_path} len={len(packet)}")
    print(packet.hex())

    if args.build_only:
        return 0
    resp = submit(args.host, args.port, out_path, dry_run=args.daemon_dry_run)
    print("daemon:", json.dumps(resp, ensure_ascii=False))
    return 0 if resp.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
