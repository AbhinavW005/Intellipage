"""Reproducible real Windows workload for IntelliPage process monitoring."""

from __future__ import annotations

import argparse
import time
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("file", type=Path)
    parser.add_argument("--seconds", type=float, default=10.0)
    parser.add_argument("--memory-mb", type=int, default=16)
    args = parser.parse_args()
    if args.seconds <= 0 or args.memory_mb < 1:
        parser.error("seconds and memory-mb must be positive")

    payload = bytearray(args.memory_mb * 1024 * 1024)
    deadline = time.perf_counter() + args.seconds
    passes = 0
    checksum = 0
    while time.perf_counter() < deadline:
        with args.file.open("rb", buffering=0) as handle:
            while block := handle.read(1024 * 1024):
                checksum = (checksum + block[0] + block[-1]) & 0xFFFFFFFF
        offset = (passes * 4096) % len(payload)
        payload[offset] = (payload[offset] + 1) & 0xFF
        passes += 1

    print(
        f"Completed {passes} real file passes with {len(payload):,} resident bytes; "
        f"checksum={checksum}."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
