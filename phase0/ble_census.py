#!/usr/bin/env python3
"""Read-only BLE survey of the LifeSpan TX6 Glow-Up base.

Safe to run any time: it only scans, enumerates GATT, subscribes to notify
characteristics, and sends the known LifeSpan *read* opcodes (A1 xx). It never
sends control or init opcodes (that's ble_control_probe.py).

Usage:
  python ble_census.py --scan              # find the treadmill (plug it in, wake it)
  python ble_census.py --address <ADDR>    # full GATT dump + protocol probes

What we're trying to learn (see PLAN.md):
  1. Does the base expose the native LifeSpan service FFF0 (like consoles do)?
  2. Does it expose standard FTMS (0x1826)? Both?
  3. Which characteristic accepts the A1 reads: FFF2 (treadspan/Omni) or FFF1 (pcorliss)?
"""

import argparse
import asyncio
import sys

from bleak import BleakClient, BleakScanner

LIFESPAN_SERVICE = "0000fff0-0000-1000-8000-00805f9b34fb"
CHAR_FFF1 = "0000fff1-0000-1000-8000-00805f9b34fb"  # notify (Omni); write+notify (pcorliss)
CHAR_FFF2 = "0000fff2-0000-1000-8000-00805f9b34fb"  # write (Omni)
FTMS_SERVICE = "00001826-0000-1000-8000-00805f9b34fb"
FTMS_TREADMILL_DATA = "00002acd-0000-1000-8000-00805f9b34fb"

# LifeSpan read opcodes (from treadspan protocol-analysis + daeken gist).
A1_READS = {
    0x81: "unknown-constant-1",
    0x82: "speed (u16 BE; mph ~= 0.00435*raw - 0.009)",
    0x85: "distance (u16 BE)",
    0x86: "unknown-constant-2",
    0x87: "calories",
    0x88: "steps",
    0x89: "elapsed time [hh mm ss]",
    0x91: "state (01 standby, 03 running, 04 summary, 05 paused)",
}


async def scan() -> None:
    print("Scanning 15s — wake the treadmill with the remote so it advertises...")
    devices = await BleakScanner.discover(timeout=15.0, return_adv=True)
    for device, adv in sorted(devices.values(), key=lambda p: p[1].rssi or -999, reverse=True):
        print(f"{device.address}  rssi={adv.rssi:>4}  name={device.name or '?':<24} "
              f"services={[u[4:8] for u in adv.service_uuids] or '-'}")
    print("\nThe TX6 base may advertise with no name (a bare MAC) and no service UUIDs.")
    print("Candidates: strong RSSI near the treadmill; try --address on each.")


def on_notify(label: str):
    def handler(_char, data: bytearray) -> None:
        print(f"  [notify {label}] {data.hex(' ')}")
    return handler


async def census(address: str) -> None:
    print(f"Connecting to {address} ...")
    async with BleakClient(address, timeout=30.0) as client:
        print("Connected. GATT dump:\n")
        has_lifespan = has_ftms = False
        write_props = {}
        for service in client.services:
            print(f"service {service.uuid}  ({service.description})")
            if service.uuid.lower() == LIFESPAN_SERVICE:
                has_lifespan = True
            if service.uuid.lower() == FTMS_SERVICE:
                has_ftms = True
            is_dis = service.uuid.lower().startswith("0000180a")
            for char in service.characteristics:
                line = f"  char {char.uuid}  props={'|'.join(char.properties)}"
                if is_dis and "read" in char.properties:
                    try:
                        raw = await client.read_gatt_char(char.uuid)
                        line += f"  value={bytes(raw).decode('utf-8', 'replace')!r}"
                    except Exception as exc:
                        line += f"  (read failed: {exc})"
                write_props[char.uuid.lower()] = char.properties
                print(line)
        print()

        if has_ftms:
            print("FTMS present — subscribing to Treadmill Data for 10s (walk on it if safe):")
            try:
                await client.start_notify(FTMS_TREADMILL_DATA, on_notify("2ACD"))
                await asyncio.sleep(10)
                await client.stop_notify(FTMS_TREADMILL_DATA)
            except Exception as exc:
                print(f"  FTMS subscribe failed: {exc}")

        if not has_lifespan:
            print("No FFF0 service — native LifeSpan protocol not exposed on this target.")
            return

        print("FFF0 present — probing A1 reads.")
        await client.start_notify(CHAR_FFF1, on_notify("FFF1"))
        for write_char, label in ((CHAR_FFF2, "FFF2"), (CHAR_FFF1, "FFF1")):
            props = write_props.get(write_char, [])
            if not any("write" in p for p in props):
                print(f"\n--- {label} not writable ({'|'.join(props) or 'absent'}); skipping ---")
                continue
            with_response = "write" in props  # else write-without-response only
            print(f"\n--- writing reads to {label} (response={with_response}) ---")
            for opcode, meaning in A1_READS.items():
                cmd = bytes([0xA1, opcode, 0, 0, 0])
                try:
                    await client.write_gatt_char(write_char, cmd, response=with_response)
                    print(f"  sent A1 {opcode:02X} ({meaning})")
                except Exception as exc:
                    print(f"  write A1 {opcode:02X} to {label} failed: {exc}")
                    break  # this write target doesn't work; try the other
                await asyncio.sleep(0.5)
        await asyncio.sleep(2)
        await client.stop_notify(CHAR_FFF1)
        print("\nDone. A response of 'a1 ff ...' = unknown opcode; 'a1 aa ...' = success.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--scan", action="store_true")
    parser.add_argument("--address")
    args = parser.parse_args()
    if args.scan:
        asyncio.run(scan())
    elif args.address:
        asyncio.run(census(args.address))
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
