"""Session import: sidecar round-trip, FIT reconstruction, and hostile rejection."""

import json
import time

import pytest
from aiohttp import web

import milltender
from conftest import walk_samples

START = 1_784_258_637.0
NAME = time.strftime("walk-%Y%m%d-%H%M%S", time.localtime(START))


class FakePart:
    def __init__(self, filename: str, data: bytes):
        self.filename = filename
        self._data = data

    async def read(self) -> bytes:
        return self._data


class FakeUpload:
    """Stands in for req.multipart(): an async iterator over uploaded parts."""

    def __init__(self, *parts: FakePart):
        self._parts = parts

    async def multipart(self):
        return self

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        for p in self._parts:
            yield p


async def test_sidecar_import_round_trips(daemon):
    sidecar = {
        "start": START, "duration_s": 600, "moving_s": 580,
        "dist_m": 848.1, "steps": 2000, "hr_avg": 96, "hrr60": 22,
        "samples": [[i, 2.0, 96, 1, None, 110] for i in range(30)],
    }
    req = FakeUpload(FakePart("walk-whatever.json", json.dumps(sidecar).encode()))
    res = await daemon.h_import(req)
    body = json.loads(res.text)
    assert body["name"] == NAME  # from the sidecar's own start
    stored = json.loads((milltender.SESSIONS_DIR / f"{body['name']}.json").read_text())
    assert stored == sidecar  # full-fidelity path stores verbatim

    listed = await daemon.h_sessions(FakeUpload())
    row = next(s for s in json.loads(listed.text) if s["name"] == body["name"])
    assert row["steps"] == 2000 and row["hrr60"] == 22


async def test_fit_import_reconstructs_a_sidecar(daemon, tmp_path):
    samples = walk_samples(120, start=START, cadence=2, hr=90)
    fit = milltender.build_fit(samples, tmp_path / "src")

    req = FakeUpload(FakePart("export.fit", fit.read_bytes()))
    res = await daemon.h_import(req)
    body = json.loads(res.text)
    assert body["name"] == NAME and body["has_fit"]

    meta = json.loads((milltender.SESSIONS_DIR / f"{body['name']}.json").read_text())
    assert meta["steps"] == 238                       # 119 strides * 2, from the session summary
    assert meta["dist_m"] == pytest.approx(79.7, abs=0.2)
    assert meta["hr_avg"] == 90
    assert meta["hrv_baseline"] is None and meta["recovery"] is None  # unrecoverable from FIT
    assert meta["samples"][10][1] == pytest.approx(1.5, abs=0.05)     # 0.67 m/s -> mph
    assert (milltender.SESSIONS_DIR / f"{body['name']}.fit").exists()


async def test_import_rejects_missing_fields(daemon):
    req = FakeUpload(FakePart("walk.json", json.dumps({"start": START}).encode()))
    with pytest.raises(web.HTTPBadRequest):
        await daemon.h_import(req)
    assert not list(milltender.SESSIONS_DIR.glob("walk-*"))


async def test_import_rejects_hostile_start(daemon):
    """A non-numeric 'start' can't be coerced into a path — refuse, write nothing."""
    sidecar = {"start": "../../etc/passwd", "duration_s": 1, "steps": 1,
               "dist_m": 1, "samples": []}
    req = FakeUpload(FakePart("evil.json", json.dumps(sidecar).encode()))
    with pytest.raises(web.HTTPBadRequest):
        await daemon.h_import(req)
    assert not list(milltender.SESSIONS_DIR.glob("*.json"))


async def test_import_never_overwrites(daemon):
    sidecar = {"start": START, "duration_s": 1, "steps": 1, "dist_m": 1, "samples": []}
    payload = json.dumps(sidecar).encode()
    first = json.loads((await daemon.h_import(FakeUpload(FakePart("a.json", payload)))).text)
    second = json.loads((await daemon.h_import(FakeUpload(FakePart("a.json", payload)))).text)
    assert first["name"] == NAME
    assert second["name"] == f"{NAME}-2"  # collision suffixed, not clobbered
