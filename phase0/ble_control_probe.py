#!/usr/bin/env python3
"""Cautious BLE *control* probe for the TX6 Glow-Up base.

SAFETY: This starts the belt. Nobody on the treadmill, nothing touching the belt,
remote within reach as a manual stop. The sequence is: init, start, set 0.4 mph,
read back speed for a few seconds, stop. Ctrl-C at any point sends stop.

Byte sequences come verbatim from pcorliss/treadmill (verified working on a
TR1000-GlowUp base): init [02.., C2.., E9 FF.., E4 00 F4..], start E1, stop E0,
speed D0 <units> <hundredths>. That code writes everything to FFF1; Omni-console
captures use FFF2 for writes — we take --write-char to try either.

Usage:
  python ble_control_probe.py --address <ADDR> [--write-char fff1|fff2] [--mph 0.4]
"""

import argparse
import asyncio

from bleak import BleakClient

CHAR = "0000{}-0000-1000-8000-00805f9b34fb"

INIT_SEQUENCE = [
    bytes.fromhex("02 00 00 00 00"),
    bytes.fromhex("c2 00 00 00 00"),
    bytes.fromhex("e9 ff 00 00 00"),
    bytes.fromhex("e4 00 f4 00 00"),
]
START = bytes.fromhex("e100000000")
STOP = bytes.fromhex("e000000000")
READ_SPEED = bytes.fromhex("a18200000000")[:5]


def speed_cmd(mph: float) -> bytes:
    units, hundredths = int(mph), round(mph % 1 * 100)
    return bytes([0xD0, units, hundredths, 0x00, 0x00])


async def probe(address: str, write_char: str, mph: float) -> None:
    notify_char = CHAR.format("fff1")
    wchar = CHAR.format(write_char)

    def on_notify(_c, data: bytearray) -> None:
        print(f"  [notify] {data.hex(' ')}")

    async with BleakClient(address, timeout=30.0) as client:
        await client.start_notify(notify_char, on_notify)

        async def send(cmd: bytes, label: str, settle: float = 1.0) -> None:
            print(f"-> {label}: {cmd.hex(' ')} (write to {write_char})")
            await client.write_gatt_char(wchar, cmd, response=True)
            await asyncio.sleep(settle)

        try:
            for i, cmd in enumerate(INIT_SEQUENCE):
                await send(cmd, f"init[{i}]")
            await send(START, "START (belt may move!)", settle=3.0)
            await send(speed_cmd(mph), f"set speed {mph} mph", settle=2.0)
            for _ in range(5):
                await send(READ_SPEED, "read speed", settle=1.0)
        finally:
            print("-> STOP")
            try:
                await client.write_gatt_char(wchar, STOP, response=True)
            except Exception as exc:
                print(f"  STOP write failed ({exc}) — USE THE REMOTE.")
            await asyncio.sleep(2)
            await client.stop_notify(notify_char)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--address", required=True)
    parser.add_argument("--write-char", choices=["fff1", "fff2"], default="fff1")
    parser.add_argument("--mph", type=float, default=0.4)
    args = parser.parse_args()
    assert 0.4 <= args.mph <= 1.0, "keep the probe slow"
    confirm = input("Belt clear, nobody on it, remote in hand? Type 'yes': ")
    if confirm.strip().lower() != "yes":
        return
    asyncio.run(probe(args.address, args.write_char, args.mph))


if __name__ == "__main__":
    main()
