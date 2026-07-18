"""FitShow frame construction and the daemon's frame acceptance."""

import milltender
from conftest import feed, status_frame


def test_frame_wraps_payload_with_xor_checksum():
    pkt = milltender.frame(bytes([0x51]))
    assert pkt == bytes([0x02, 0x51, 0x51, 0x03])


def test_frame_checksum_matches_multibyte_payload():
    payload = bytes([0x53, 0x02, 0x0A, 0x00])
    pkt = milltender.frame(payload)
    xor = 0
    for b in payload:
        xor ^= b
    assert pkt[-2] == xor


def test_corrupt_checksum_is_dropped(daemon):
    good = bytearray(status_frame(state=3, speed_raw=10, elapsed=5, steps=8))
    good[-2] ^= 0xFF
    feed(daemon, bytes(good))
    assert daemon.latest == {}


def test_short_and_unframed_data_ignored(daemon):
    feed(daemon, b"\x02\x03")
    feed(daemon, b"garbage")
    assert daemon.latest == {}


def test_status_frame_updates_state(daemon):
    feed(daemon, status_frame(state=0))
    assert daemon.latest["state"] == 0
    assert not daemon.in_session


def test_clamp_mph_respects_global_limit(monkeypatch):
    monkeypatch.setattr(milltender, "MAX_MPH", 2.5)
    assert milltender.clamp_mph(4.0) == 2.5
    assert milltender.clamp_mph(0.1) == 0.4
    assert milltender.clamp_mph(1.7) == 1.7
