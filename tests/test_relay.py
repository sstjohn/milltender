"""Browser Web Bluetooth relay: command frames route to whichever transport owns
the treadmill, and the relay takes precedence over the local BLE link."""

import milltender


class FakeWS:
    def __init__(self):
        self.closed = False
        self.sent = []

    async def send_bytes(self, b):
        self.sent.append(bytes(b))


class FakeClient:
    is_connected = True

    def __init__(self):
        self.writes = []

    async def write_gatt_char(self, char, data, response=False):
        self.writes.append(bytes(data))


async def test_send_frame_prefers_relay_over_local_ble(daemon):
    ws, client = FakeWS(), FakeClient()
    daemon.relay_ws, daemon.client = ws, client
    await daemon._send_frame(b"\x02\x51\x03")
    assert ws.sent == [b"\x02\x51\x03"] and client.writes == []


async def test_send_frame_falls_back_to_local_ble(daemon):
    client = FakeClient()
    daemon.relay_ws, daemon.client = None, client
    await daemon._send_frame(b"\x02\x51\x03")
    assert client.writes == [b"\x02\x51\x03"]


async def test_send_cmd_frames_and_routes_to_relay(daemon):
    ws = FakeWS()
    daemon.relay_ws = ws
    # conftest replaces the bound send_cmd with a recorder; call the real one
    await milltender.Daemon.send_cmd(daemon, bytes([0x53, 0x02, 15, 0x00]))
    assert ws.sent == [milltender.frame(bytes([0x53, 0x02, 15, 0x00]))]


async def test_no_transport_raises(daemon):
    daemon.relay_ws, daemon.client = None, None
    import pytest
    from aiohttp import web
    with pytest.raises(web.HTTPConflict):
        await daemon._send_frame(b"\x02\x51\x03")


def test_relay_ingest_routes_treadmill_and_hr_channels(daemon, monkeypatch):
    tm, hr = [], []
    monkeypatch.setattr(daemon, "on_treadmill_frame", lambda c, d: tm.append(bytes(d)))
    monkeypatch.setattr(daemon, "on_hr", lambda c, d: hr.append(bytes(d)))
    daemon._relay_ingest(b"\x00\x02\x51\x03")  # channel 0: treadmill notification
    daemon._relay_ingest(b"\x01\x10\x48")      # channel 1: HR measurement
    daemon._relay_ingest(b"")                   # empty frame is ignored
    assert tm == [b"\x02\x51\x03"] and hr == [b"\x10\x48"]


def test_truncated_hr_frame_does_not_propagate(daemon):
    # a bare channel tag with no HR payload must be swallowed, not raise out of the
    # socket loop and strand the bridge
    daemon._relay_ingest(b"\x01")  # channel 1, empty payload -> on_hr(None, b"")
    assert daemon.latest_hr is None


def test_hr_measurement_from_relay_updates_bpm_and_rr(daemon):
    # standard 0x2A37: flags=0x10 (RR present, 8-bit bpm), bpm=72, one RR = 1024/1024 s
    daemon.on_hr(None, bytes([0x10, 72, 0x00, 0x04]))
    assert daemon.latest_hr == 72
    assert daemon.rr_events and abs(daemon.rr_events[-1][1] - 1.0) < 0.01
