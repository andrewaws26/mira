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
