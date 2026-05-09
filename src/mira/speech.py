"""Text-to-speech via the ElevenLabs API.

The user is at an eyepiece under a dark sky. Reading a screen breaks the
spell and the dark adaptation. Speech output lets Mira hand back results
without forcing the user to look at the laptop.

ElevenLabs handles the bilingual voicing nicely with the multilingual
model: the same voice speaks English and the occasional Spanish phrase
without sounding spliced.

The API key is read in this order:
  1. The `api_key` parameter passed to `Speaker(...)`.
  2. The `ELEVENLABS_API_KEY` environment variable.
  3. The line `ELEVENLABS_API_KEY=...` in `~/mira/.env`.

Audio is synthesized to MP3, written to a temp file, and played via the
macOS `afplay` binary. afplay is part of Core Audio so no install needed.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

ELEVENLABS_BASE = "https://api.elevenlabs.io/v1"
DEFAULT_VOICE_ID = "EXAVITQu4vr4xnSDxMaL"  # Sarah: mature, reassuring, confident
DEFAULT_MODEL_ID = "eleven_multilingual_v2"
DEFAULT_ENV_FILE = Path("~/mira/.env").expanduser()

# Voice settings tuned for a calm observing companion. Higher stability gives a
# more even tone; moderate similarity preserves character.
DEFAULT_VOICE_SETTINGS = {
    "stability": 0.55,
    "similarity_boost": 0.75,
    "style": 0.15,
    "use_speaker_boost": True,
}


class SpeechError(RuntimeError):
    """Raised when synthesis or playback fails."""


class SpeechDisabled(SpeechError):
    """Raised when speech is disabled in config or no API key is available."""


@dataclass
class Voice:
    voice_id: str
    name: str
    description: str = ""


def _load_env_file(path: Optional[Path] = None) -> dict[str, str]:
    """Parse a .env-style file into a dict. Returns empty if the file is missing.

    The default path resolves at call time so tests can monkeypatch
    `DEFAULT_ENV_FILE` and have it take effect.
    """
    out: dict[str, str] = {}
    p = path if path is not None else DEFAULT_ENV_FILE
    if not p.exists():
        return out
    try:
        for raw in p.read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            k, _, v = line.partition("=")
            v = v.strip().strip("'").strip('"')
            out[k.strip()] = v
    except OSError:
        pass
    return out


def resolve_api_key(explicit: Optional[str] = None) -> Optional[str]:
    """Find the ElevenLabs key in the documented precedence."""
    if explicit:
        return explicit
    env = os.environ.get("ELEVENLABS_API_KEY")
    if env:
        return env
    return _load_env_file().get("ELEVENLABS_API_KEY")


@dataclass
class Speaker:
    """Synthesize and play audio. Construct once, reuse across calls."""

    voice_id: str = DEFAULT_VOICE_ID
    model_id: str = DEFAULT_MODEL_ID
    api_key: Optional[str] = None
    timeout: float = 30.0
    voice_settings: Optional[dict] = None

    def __post_init__(self) -> None:
        self.api_key = resolve_api_key(self.api_key)
        if self.voice_settings is None:
            self.voice_settings = dict(DEFAULT_VOICE_SETTINGS)

    def is_configured(self) -> bool:
        return bool(self.api_key)

    def synthesize(self, text: str) -> bytes:
        """Synthesize text to MP3 audio. Returns raw bytes."""
        if not text or not text.strip():
            raise SpeechError("nothing to speak")
        if not self.is_configured():
            raise SpeechDisabled(
                "ELEVENLABS_API_KEY not set. Add it to ~/mira/.env or export it."
            )
        url = f"{ELEVENLABS_BASE}/text-to-speech/{self.voice_id}"
        body = json.dumps(
            {
                "text": text,
                "model_id": self.model_id,
                "voice_settings": self.voice_settings,
            }
        ).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={
                "xi-api-key": self.api_key or "",
                "Content-Type": "application/json",
                "accept": "audio/mpeg",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")[:300] if hasattr(e, "read") else ""
            raise SpeechError(f"ElevenLabs HTTP {e.code}: {detail}") from e
        except urllib.error.URLError as e:
            raise SpeechError(f"ElevenLabs unreachable: {e.reason}") from e

    def speak(self, text: str, blocking: bool = False) -> Optional[Path]:
        """Synthesize and play `text`. Returns the temp-file path used.

        Args:
            text: text to speak.
            blocking: if True, wait for playback to finish before returning.

        The temp file is best-effort cleaned up. On error, no playback occurs
        and the exception propagates.
        """
        audio = self.synthesize(text)
        fd, name = tempfile.mkstemp(suffix=".mp3", prefix="mira-speech-")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(audio)
        except Exception:
            os.close(fd)
            raise
        path = Path(name)
        try:
            if blocking:
                subprocess.run(["afplay", str(path)], check=False)
                _safe_unlink(path)
                return None
            else:
                # detached playback; we leak the temp file, OS reaps it on reboot
                subprocess.Popen(
                    ["afplay", str(path)],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                return path
        except FileNotFoundError as e:
            _safe_unlink(path)
            raise SpeechError(
                "afplay not found. macOS only feature for now."
            ) from e


def _safe_unlink(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def list_voices(api_key: Optional[str] = None, timeout: float = 15.0) -> list[Voice]:
    """List voices available on the user's ElevenLabs account."""
    key = resolve_api_key(api_key)
    if not key:
        raise SpeechDisabled("ELEVENLABS_API_KEY not set")
    req = urllib.request.Request(
        f"{ELEVENLABS_BASE}/voices",
        headers={"xi-api-key": key, "accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raise SpeechError(f"ElevenLabs HTTP {e.code}") from e
    out: list[Voice] = []
    for v in data.get("voices", []):
        labels = v.get("labels") or {}
        desc_parts = [
            labels.get("gender", ""),
            labels.get("accent", ""),
            labels.get("age", ""),
            labels.get("description") or labels.get("descriptive") or "",
        ]
        desc = " ".join(p for p in desc_parts if p).strip()
        out.append(
            Voice(
                voice_id=v.get("voice_id", ""),
                name=v.get("name", ""),
                description=desc,
            )
        )
    return out
