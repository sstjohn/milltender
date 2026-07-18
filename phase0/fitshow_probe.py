#!/usr/bin/env python3
"""FitShow protocol probe for the TX6 Glow-Up base (FITSHOW FS-BT-D2 module).

Protocol from QDomyos-Zwift fitshowtreadmill driver:
  frame = 0x02 <payload...> <XOR of payload> 0x03, write to FFF2 (no response),
  replies arrive framed the same way as notifications on FFF1.
  SYS_INFO=0x50 (sub: 0 model, 1 date, 2 speed-range, 3 incline, 4 totals, 5 ?)
  SYS_STATUS=0x51 (single byte; reply carries state + live sports data incl. steps)
  SYS_DATA=0x52 sub 0 (sport data: elapsed, distance, kcal, steps)
  SYS_CONTROL=0x53 (sub: 0 user, 1 ready/start, 2 set-target, 3 stop, 6 pause)

Usage:
  python fitshow_probe.py status [--seconds 20]   # SAFE: read-only poll loop
  python fitshow_probe.py info                    # SAFE: model/date/ranges/totals
  python fitshow_probe.py start-test [--kmh 1.0]  # MOVES THE BELT (guarded)
"""

import argparse
import asyncio

from bleak import BleakClient

ADDRESS = "9C7EC52B-C7C8-4765-8DC5-242A1D94B663"  # FS-3D6CD7 (macOS CoreBluetooth UUID)
WRITE_CHAR = "0000fff2-0000-1000-8000-00805f9b34fb"
NOTIFY_CHAR = "0000fff1-0000-1000-8000-00805f9b34fb"

STATUS_NAMES = {0: "NORMAL", 1: "END", 2: "START", 3: "RUNNING", 4: "STOP",
                5: "ERROR", 6: "SAFETY", 7: "STUDY", 10: "PAUSED"}


def frame(payload: bytes) -> bytes:
    xor = 0
    for b in payload:
        xor ^= b
    return bytes([0x02, *payload, xor, 0x03])


def decode(data: bytes) -> None:
    print(f"  [raw] {data.hex(' ')}")
    if len(data) < 4 or data[0] != 0x02 or data[-1] != 0x03:
        print("        (not a complete FitShow frame)")
        return
    payload = data[1:-2]
    xor = 0
    for b in payload:
        xor ^= b
    if xor != data[-2]:
        print(f"        (checksum mismatch: got {data[-2]:02x}, want {xor:02x})")
    cmd = payload[0]
    if cmd == 0x51 and len(payload) >= 2:
        state = payload[1]
        print(f"        SYS_STATUS state={state} ({STATUS_NAMES.get(state, '?')})")
        # QZ running-data layout, offsets relative to its buffer; try both alignments
        for label, a in (("payload[1:]", payload[1:]), ("payload", payload)):
            if len(a) >= 12:
                speed = a[1] / 10.0
                elapsed = a[3] | (a[4] << 8)
                dist = a[5] | (a[6] << 8)
                kcal = a[7] | (a[8] << 8)
                steps = a[9] | (a[10] << 8)
                print(f"        as {label}: speed={speed:.1f}km/h elapsed={elapsed}s "
                      f"dist={dist * 0.1:.1f}km? kcal={kcal} steps={steps}")
    elif cmd == 0x50 and len(payload) >= 2:
        sub = payload[1]
        body = payload[2:]
        print(f"        SYS_INFO sub={sub} body={body.hex(' ')} ascii={body.decode('ascii', 'replace')!r}")
    elif cmd == 0x52 and len(payload) >= 2:
        a = payload[2:]
        if len(a) >= 8:
            print(f"        SYS_DATA: elapsed={a[0] | a[1] << 8}s dist={a[2] | a[3] << 8} "
                  f"kcal={a[4] | a[5] << 8} steps={a[6] | a[7] << 8}")
    elif cmd == 0x53:
        print(f"        SYS_CONTROL ack: {payload.hex(' ')}")


async def session(commands, seconds: float) -> None:
    """Connect, subscribe, send framed payloads at 1s intervals, keep polling status."""
    async with BleakClient(ADDRESS, timeout=30.0) as client:
        await client.start_notify(NOTIFY_CHAR, lambda _c, d: decode(bytes(d)))

        async def send(payload: bytes) -> None:
            pkt = frame(payload)
            print(f"-> {pkt.hex(' ')}")
            await client.write_gatt_char(WRITE_CHAR, pkt, response=False)
            await asyncio.sleep(1.0)

        for payload in commands:
            await send(payload)
        end = asyncio.get_event_loop().time() + seconds
        while asyncio.get_event_loop().time() < end:
            await send(bytes([0x51]))


async def run_status(seconds: int) -> None:
    await session([], seconds)


async def run_info() -> None:
    await session([bytes([0x50, sub]) for sub in (0, 1, 2, 3, 4, 5)], seconds=3)


async def run_start_test(kmh: float) -> None:
    speed = round(kmh * 10)
    async with BleakClient(ADDRESS, timeout=30.0) as client:
        await client.start_notify(NOTIFY_CHAR, lambda _c, d: decode(bytes(d)))

        async def send(payload: bytes, settle: float = 1.5) -> None:
            pkt = frame(payload)
            print(f"-> {pkt.hex(' ')}")
            await client.write_gatt_char(WRITE_CHAR, pkt, response=False)
            await asyncio.sleep(settle)

        try:
            # user data (id=1, gender/age/weight/height placeholders), per QZ init
            await send(bytes([0x53, 0x00, 0x01, 0x00, 0x6E, 0x28, 0x50, 0xAA]))
            await send(bytes([0x53, 0x01, 0, 0, 0, 0, 0x00, 0x00, 0x00, 0x00]))  # ready/start, sport id 0, mode normal
            for _ in range(3):
                await send(bytes([0x51]))  # watch it count down / start
            await send(bytes([0x53, 0x02, speed, 0x00]))  # set target speed, incline 0
            for _ in range(8):
                await send(bytes([0x51]))
        finally:
            print("-> STOP")
            await send(bytes([0x53, 0x03]), settle=3.0)
            await send(bytes([0x51]))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_status = sub.add_parser("status")
    p_status.add_argument("--seconds", type=int, default=20)
    sub.add_parser("info")
    p_start = sub.add_parser("start-test")
    p_start.add_argument("--kmh", type=float, default=1.0)
    args = parser.parse_args()

    if args.cmd == "status":
        asyncio.run(run_status(args.seconds))
    elif args.cmd == "info":
        asyncio.run(run_info())
    elif args.cmd == "start-test":
        assert 1.0 <= args.kmh <= 2.0, "keep the probe slow"
        confirm = input("This MOVES THE BELT. Belt clear, remote in hand? Type 'yes': ")
        if confirm.strip().lower() == "yes":
            asyncio.run(run_start_test(args.kmh))


if __name__ == "__main__":
    main()
