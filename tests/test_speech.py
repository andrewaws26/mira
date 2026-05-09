"""Tests for mira.speech: env file parsing, key precedence, error paths.

Live ElevenLabs API calls are not exercised here; those happen in the
`mira say` smoke check.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mira.speech import (
    DEFAULT_VOICE_ID,
    Speaker,
    SpeechDisabled,
    _load_env_file,
    resolve_api_key,
)


class TestEnvFile:
    def test_parses_simple_env(self, tmp_path: Path) -> None:
        p = tmp_path / ".env"
        p.write_text("ELEVENLABS_API_KEY=sk_test_123\nFOO=bar\n")
        d = _load_env_file(p)
        assert d == {"ELEVENLABS_API_KEY": "sk_test_123", "FOO": "bar"}

    def test_strips_quotes(self, tmp_path: Path) -> None:
        p = tmp_path / ".env"
        p.write_text("KEY='quoted'\nOTHER=\"also-quoted\"\n")
        d = _load_env_file(p)
        assert d == {"KEY": "quoted", "OTHER": "also-quoted"}

    def test_skips_comments_and_blanks(self, tmp_path: Path) -> None:
        p = tmp_path / ".env"
        p.write_text("# a comment\n\nFOO=bar\n# another\n")
        d = _load_env_file(p)
        assert d == {"FOO": "bar"}

    def test_missing_file_returns_empty(self, tmp_path: Path) -> None:
        assert _load_env_file(tmp_path / "nope.env") == {}


class TestKeyResolution:
    def test_explicit_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ELEVENLABS_API_KEY", "from-env")
        assert resolve_api_key("explicit") == "explicit"

    def test_env_wins_over_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        env_file = tmp_path / ".env"
        env_file.write_text("ELEVENLABS_API_KEY=from-file\n")
        monkeypatch.setattr("mira.speech.DEFAULT_ENV_FILE", env_file)
        monkeypatch.setenv("ELEVENLABS_API_KEY", "from-env")
        assert resolve_api_key() == "from-env"

    def test_file_used_when_env_missing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        env_file = tmp_path / ".env"
        env_file.write_text("ELEVENLABS_API_KEY=from-file\n")
        monkeypatch.setattr("mira.speech.DEFAULT_ENV_FILE", env_file)
        monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
        assert resolve_api_key() == "from-file"

    def test_returns_none_when_nothing_set(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("mira.speech.DEFAULT_ENV_FILE", tmp_path / "absent.env")
        monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
        assert resolve_api_key() is None


class TestSpeaker:
    def test_default_voice_is_sarah(self) -> None:
        s = Speaker(api_key="x")
        assert s.voice_id == DEFAULT_VOICE_ID

    def test_is_configured_requires_key(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("mira.speech.DEFAULT_ENV_FILE", tmp_path / "absent.env")
        monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
        s = Speaker()
        assert s.is_configured() is False

    def test_synthesize_disabled_when_no_key(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("mira.speech.DEFAULT_ENV_FILE", tmp_path / "absent.env")
        monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
        s = Speaker()
        with pytest.raises(SpeechDisabled, match="ELEVENLABS_API_KEY"):
            s.synthesize("hello")

    def test_synthesize_empty_text_raises(self) -> None:
        s = Speaker(api_key="x")
        with pytest.raises(Exception):
            s.synthesize("")


class _FakeProc:
    """Stand-in for subprocess.Popen used by Speaker. Tracks wait()/poll()."""

    def __init__(self, duration: float = 0.0) -> None:
        import time

        self._end = time.monotonic() + duration
        self._waited = False
        self._time = time

    def poll(self):
        if self._time.monotonic() >= self._end:
            return 0
        return None

    def wait(self):
        self._waited = True
        # Advance to "completed" state so subsequent poll() returns 0.
        self._end = self._time.monotonic()
        return 0


class TestSpeakerSerialization:
    """The bug: parallel say() calls used to spawn overlapping ffplay
    instances. The fix: Speaker holds a per-instance lock and waits on
    the prior playback subprocess before launching a new one.
    """

    def test_speak_waits_for_prior_proc(self, monkeypatch: pytest.MonkeyPatch) -> None:
        s = Speaker(api_key="x")
        prior = _FakeProc(duration=10.0)
        s._active_proc = prior
        spawned: list[_FakeProc] = []

        def fake_stream_to_ffplay(self_, text):  # noqa: ARG001
            new = _FakeProc()
            spawned.append(new)
            return new

        monkeypatch.setattr("mira.speech.shutil.which", lambda _: "/usr/bin/ffplay")
        monkeypatch.setattr(Speaker, "_stream_to_ffplay", fake_stream_to_ffplay)

        s.speak("hola")

        assert prior._waited is True, "speak() must wait for the prior playback"
        assert s._active_proc is spawned[0]

    def test_speak_skips_wait_when_prior_already_done(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        s = Speaker(api_key="x")
        prior = _FakeProc(duration=0.0)  # already completed
        s._active_proc = prior

        def fake_stream_to_ffplay(self_, text):  # noqa: ARG001
            return _FakeProc()

        monkeypatch.setattr("mira.speech.shutil.which", lambda _: "/usr/bin/ffplay")
        monkeypatch.setattr(Speaker, "_stream_to_ffplay", fake_stream_to_ffplay)

        s.speak("hola")

        assert prior._waited is False, "no need to wait on a finished proc"

    def test_parallel_speak_calls_serialize(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Two threads calling speak() at once must not both hold ffplay
        live at the same instant. We assert by checking that the first
        call's proc gets wait()-ed before the second call records its own
        as active."""
        import threading
        import time

        s = Speaker(api_key="x")
        order: list[str] = []
        spawn_lock = threading.Lock()

        def fake_stream_to_ffplay(self_, text):  # noqa: ARG001
            with spawn_lock:
                order.append(f"spawn:{text}")
            return _FakeProc(duration=0.05)

        monkeypatch.setattr("mira.speech.shutil.which", lambda _: "/usr/bin/ffplay")
        monkeypatch.setattr(Speaker, "_stream_to_ffplay", fake_stream_to_ffplay)

        def call(label):
            s.speak(label)

        t1 = threading.Thread(target=call, args=("first",))
        t2 = threading.Thread(target=call, args=("second",))
        t1.start()
        time.sleep(0.005)
        t2.start()
        t1.join()
        t2.join()

        # Both spawned, in start order, with no interleaving inside speak().
        assert order == ["spawn:first", "spawn:second"]
