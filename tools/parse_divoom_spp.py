#!/usr/bin/env python3
from __future__ import annotations

import csv
import struct
from pathlib import Path

import zstandard as zstd


ROOT = Path(__file__).resolve().parents[1]
TSV = ROOT / "captures" / "resend-btspp.tsv"
OUT = ROOT / "captures" / "reconstructed"


def parse_rows():
    rows = []
    for line in TSV.read_text().splitlines():
        if not line.strip():
            continue
        # The tshark command used a literal backslash separator because this
        # macOS shell invocation preserved '\t'. Keep parser tolerant.
        parts = line.split("\t") if "\t" in line else line.split("\\")
        frame, t, direction, src, dst, rf_len, hexdata = parts
        rows.append(
            {
                "frame": int(frame),
                "time": float(t),
                "dir": int(direction),
                "src": src,
                "dst": dst,
                "rf_len": int(rf_len),
                "data": bytes.fromhex(hexdata),
            }
        )
    return rows


def describe_packet(data: bytes) -> str:
    if len(data) < 4 or data[0] != 0x01:
        return f"raw len={len(data)}"
    declared = int.from_bytes(data[1:3], "little")
    cmd = data[3]
    extra = ""
    if len(data) >= 6 and data[4] in (0x8B, 0xBD):
        # inbound extended command shape: 01 <len16> 04 <cmd_type> 55 ... 02
        extra = f" ext_type=0x{data[4]:02x} magic=0x{data[5]:02x}"
    return f"len={len(data)} declared={declared} cmd=0x{cmd:02x}{extra}"


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    rows = parse_rows()

    summary = []
    for r in rows:
        d = r["data"]
        summary.append(
            f"frame={r['frame']:>4} t={r['time']:>9.6f} dir={r['dir']} "
            f"{r['src']}->{r['dst']} {describe_packet(d)} data={d[:24].hex()}"
        )
    (OUT / "spp-summary.txt").write_text("\n".join(summary) + "\n")

    # Outbound photo chunks: cmd 0x8b, 0x01 + le16 length framing.
    chunks = []
    for r in rows:
        d = r["data"]
        if r["dir"] == 0 and len(d) >= 10 and d[0] == 1 and d[3] == 0x8B:
            declared = int.from_bytes(d[1:3], "little")
            if declared == len(d) - 4:
                chunks.append((r, d[4:]))

    # Focus on the transfer whose chunks carry subheader: 01 a1 2e 00 00 <seq16> 00 ...
    transfer_chunks = []
    for r, body in chunks:
        if len(body) >= 8 and body[0] == 0x01 and body[1] == 0xA1:
            total = int.from_bytes(body[1:3], "little")
            seq = int.from_bytes(body[5:7], "little")
            transfer_chunks.append((seq, total, r, body))

    transfer_chunks.sort(key=lambda x: x[0])
    meta_lines = []
    payload_parts = []
    for seq, total, r, body in transfer_chunks:
        # s.c() wraps every command as:
        #   01 <declared_len le16> <cmd> <body> <checksum16 le> 02
        # For this 0x8b transfer, body is:
        #   01 <total_payload_len le16> 00 00 <seq le16> <chunk_payload>
        # Since `body` here starts at packet byte 4, chunk_payload is body[7:-3].
        part = body[7:-3]
        payload_parts.append(part)
        meta_lines.append(
            f"seq={seq:04d} frame={r['frame']} spp_len={len(r['data'])} "
            f"declared={int.from_bytes(r['data'][1:3], 'little')} total_field={total} part={len(part)}"
        )

    reassembled = b"".join(payload_parts)
    (OUT / "photo-transfer-chunks.txt").write_text("\n".join(meta_lines) + "\n")
    (OUT / "photo-transfer-reassembled.bin").write_bytes(reassembled)

    zpos = reassembled.find(bytes.fromhex("28b52ffd"))
    report = [
        f"spp_rows={len(rows)}",
        f"outbound_8b_chunks={len(chunks)}",
        f"photo_transfer_chunks={len(transfer_chunks)}",
        f"reassembled_len={len(reassembled)}",
        f"zstd_magic_offset={zpos}",
        f"first_64={reassembled[:64].hex(' ')}",
    ]

    if zpos >= 0:
        zbytes = reassembled[zpos:]
        (OUT / "photo-transfer.zst").write_bytes(zbytes)
        try:
            raw = zstd.ZstdDecompressor().decompress(zbytes)
        except Exception as e:
            report.append(f"zstd_decompress_error={e!r}")
        else:
            (OUT / "photo-transfer-decompressed.bin").write_bytes(raw)
            report.extend(
                [
                    f"zstd_len={len(zbytes)}",
                    f"decompressed_len={len(raw)}",
                    f"decompressed_first_64={raw[:64].hex(' ')}",
                ]
            )
            # Guess common pixel sizes.
            for bpp in (1, 2, 3, 4):
                if len(raw) % bpp == 0:
                    pixels = len(raw) // bpp
                    report.append(f"if_{bpp}_bytes_per_pixel_pixels={pixels}")

    (OUT / "analysis-report.txt").write_text("\n".join(report) + "\n")
    print("\n".join(report))


if __name__ == "__main__":
    main()
