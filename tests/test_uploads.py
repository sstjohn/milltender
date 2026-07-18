"""Strava token handling: rotation persistence and fallback."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import uploads  # noqa: E402


class FakeResponse:
    def __init__(self, body):
        self._body = body

    def json(self):
        return self._body

    def raise_for_status(self):
        pass


def _run_refresh(monkeypatch, tmp_path, file_text, response_body):
    token_file = tmp_path / ".strava_refresh_token"
    if file_text is not None:
        token_file.write_text(file_text)
    monkeypatch.setattr(uploads, "STRAVA_TOKEN_FILE", token_file)
    monkeypatch.setenv("STRAVA_CLIENT_ID", "1")
    monkeypatch.setenv("STRAVA_CLIENT_SECRET", "s")
    monkeypatch.setenv("STRAVA_REFRESH_TOKEN", "env-token")
    sent = {}

    def fake_post(url, data=None, timeout=None, **kw):
        sent.update(data)
        return FakeResponse(response_body)

    monkeypatch.setattr(uploads.requests, "post", fake_post)

    # run only the refresh half: upload will die on the missing file, and that's fine
    try:
        uploads.upload_strava(tmp_path / "nope.fit")
    except FileNotFoundError:
        pass
    return sent, token_file


def test_env_token_used_when_file_absent(monkeypatch, tmp_path):
    sent, token_file = _run_refresh(monkeypatch, tmp_path, None,
                                    {"access_token": "a"})
    assert sent["refresh_token"] == "env-token"
    assert not token_file.exists()


def test_empty_file_falls_back_to_env(monkeypatch, tmp_path):
    sent, _ = _run_refresh(monkeypatch, tmp_path, "",
                           {"access_token": "a"})
    assert sent["refresh_token"] == "env-token"


def test_rotated_token_is_persisted(monkeypatch, tmp_path):
    sent, token_file = _run_refresh(monkeypatch, tmp_path, "old-token",
                                    {"access_token": "a", "refresh_token": "new-token"})
    assert sent["refresh_token"] == "old-token"
    assert token_file.read_text() == "new-token"
    assert not token_file.with_suffix(".tmp").exists()
