"""Tests for mira.preview: ffmpeg device parsing and resolution."""

from __future__ import annotations

import pytest

from mira.preview import (
    PreviewError,
    list_avfoundation_devices,
    resolve_device_index,
)


# Captured real ffmpeg output from this Mac.
SAMPLE_FFMPEG_STDERR = """\
[AVFoundation indev @ 0x881058140] AVFoundation video devices:
[AVFoundation indev @ 0x881058140] [0] MacBook Air Camera
[AVFoundation indev @ 0x881058140] [1] Andrew Camera
[AVFoundation indev @ 0x881058140] [2] MacBook Air Desk View Camera
[AVFoundation indev @ 0x881058140] [3] Andrew Desk View Camera
[AVFoundation indev @ 0x881058140] [4] Capture screen 0
[AVFoundation indev @ 0x881058140] AVFoundation audio devices:
[AVFoundation indev @ 0x881058140] [0] Andrew Microphone
[AVFoundation indev @ 0x881058140] [1] BlackHole 2ch
[AVFoundation indev @ 0x881058140] [2] MacBook Air Microphone
"""


class _FakeProc:
    def __init__(self, stderr: str) -> None:
        self.stderr = stderr
        self.stdout = ""
        self.returncode = 1


@pytest.fixture
def fake_ffmpeg(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "mira.preview._ffmpeg_binary", lambda: "/fake/ffmpeg"
    )

    def fake_run(*_args, **_kwargs):
        return _FakeProc(SAMPLE_FFMPEG_STDERR)

    monkeypatch.setattr("mira.preview.subprocess.run", fake_run)


class TestListDevices:
    def test_parses_video_only(self, fake_ffmpeg: None) -> None:
        devices = list_avfoundation_devices()
        # Must include all five video devices, no audio devices.
        names = [n for _, n in devices]
        assert "MacBook Air Camera" in names
        assert "Andrew Camera" in names
        assert "MacBook Air Desk View Camera" in names
        assert "Capture screen 0" in names
        # audio devices excluded
        assert "Andrew Microphone" not in names
        assert "BlackHole 2ch" not in names

    def test_indices_correct(self, fake_ffmpeg: None) -> None:
        devices = dict((name, idx) for idx, name in list_avfoundation_devices())
        assert devices["MacBook Air Camera"] == 0
        assert devices["Andrew Camera"] == 1
        assert devices["Capture screen 0"] == 4

    def test_no_devices_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("mira.preview._ffmpeg_binary", lambda: "/fake/ffmpeg")

        def empty_run(*_args, **_kwargs):
            return _FakeProc("AVFoundation video devices:\nAVFoundation audio devices:\n")

        monkeypatch.setattr("mira.preview.subprocess.run", empty_run)
        with pytest.raises(PreviewError, match="could not enumerate"):
            resolve_device_index("anything")


class TestResolveDeviceIndex:
    def test_exact_match(self, fake_ffmpeg: None) -> None:
        assert resolve_device_index("Andrew Camera") == 1

    def test_case_insensitive(self, fake_ffmpeg: None) -> None:
        assert resolve_device_index("andrew camera") == 1
        assert resolve_device_index("ANDREW CAMERA") == 1

    def test_substring_match(self, fake_ffmpeg: None) -> None:
        assert resolve_device_index("Andrew") == 1  # matches "Andrew Camera" first
        assert resolve_device_index("MacBook Air") == 0  # MacBook Air Camera

    def test_no_match_lists_available(self, fake_ffmpeg: None) -> None:
        with pytest.raises(PreviewError, match="not visible"):
            resolve_device_index("iPhone 17 Pro")

    def test_iphone_legacy_name_does_not_match(self, fake_ffmpeg: None) -> None:
        # If the user's config still says "iPhone" but the actual device is
        # named after the user, this surfaces as a clear PreviewError.
        with pytest.raises(PreviewError, match="not visible"):
            resolve_device_index("iPhone")


class TestMissingFfmpeg:
    def test_ffmpeg_not_on_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("shutil.which", lambda _: None)
        from mira.preview import _ffmpeg_binary

        with pytest.raises(PreviewError, match="brew install ffmpeg"):
            _ffmpeg_binary()

    def test_ffplay_not_on_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("shutil.which", lambda _: None)
        from mira.preview import _ffplay_binary

        with pytest.raises(PreviewError, match="brew install ffmpeg"):
            _ffplay_binary()
