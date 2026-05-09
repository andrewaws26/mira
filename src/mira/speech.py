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
import shutil
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
# Eleven v3 is the most expressive model and supports inline audio tags like
# [excited], [curious], [warmly], [whispers]. Costs more credits per character
# than v2, but Mira's utterances are short.
DEFAULT_MODEL_ID = "eleven_v3"
DEFAULT_ENV_FILE = Path("~/mira/.env").expanduser()

# Voice settings tuned for an excited-teacher delivery: warm, inflected,
# leans into the audio tags and exclamation marks the persona uses.
#   stability       lower = more inflection, higher = more even/monotone
#   style           higher = more emotional exaggeration
#   similarity_boost  how closely to match the source voice character
DEFAULT_VOICE_SETTINGS = {
    "stability": 0.45,
    "similarity_boost": 0.75,
    "style": 0.70,
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


# Fallback model when v3 fails (livekit#3235-style alpha-endpoint hiccups).
FALLBACK_MODEL_ID = "eleven_turbo_v2_5"

# Streaming output format. PCM avoids the MP3 encode step on the ElevenLabs
# side, which independent benchmarks (vexyl.ai, 2026) show drops time-to-
# first-audio from ~500ms to ~478ms for short utterances.
STREAM_OUTPUT_FORMAT = "pcm_22050"
STREAM_SAMPLE_RATE = 22050  # must match STREAM_OUTPUT_FORMAT


@dataclass
class Speaker:
    """Synthesize and play audio. Construct once, reuse across calls.

    Streaming path (preferred when ffplay is on PATH):
      ElevenLabs /stream endpoint -> raw PCM 22.05kHz mono ->
      ffplay -f s16le -ar 22050 -ac 1 -nodisp -autoexit
    avoids the MP3 encode/decode round-trip and starts playback as the
    first chunk arrives. Falls back to MP3 -> afplay when ffplay is missing.
    """

    voice_id: str = DEFAULT_VOICE_ID
    model_id: str = DEFAULT_MODEL_ID
    api_key: Optional[str] = None
    timeout: float = 30.0
    voice_settings: Optional[dict] = None
    optimize_streaming_latency: int = 3  # 0..4; 3 saves ~50-75ms with minor quality cost
    fallback_model_id: str = FALLBACK_MODEL_ID

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
        """Synthesize and play `text`.

        Prefers a streaming-PCM path through ffplay (lower latency, no temp
        file). Falls back to MP3 + afplay if ffplay is not on PATH. Returns
        the temp-file path used by the fallback path, or None for streaming.
        """
        if not text or not text.strip():
            raise SpeechError("nothing to speak")
        if not self.is_configured():
            raise SpeechDisabled(
                "ELEVENLABS_API_KEY not set. Add it to ~/mira/.env or export it."
            )
        if shutil.which("ffplay") is not None:
            self._stream_to_ffplay(text, blocking=blocking)
            return None
        return self._mp3_via_afplay(text, blocking=blocking)

    def _stream_to_ffplay(self, text: str, blocking: bool) -> None:
        """Stream PCM from ElevenLabs straight into ffplay's stdin.

        Tries the configured model first; on HTTP failure (typical: 403 from
        the v3 alpha endpoint hitting a transient rate gate) automatically
        retries with `fallback_model_id`. The fallback only triggers if the
        primary model is v3 and the fallback differs from it.
        """
        ffplay = subprocess.Popen(
            [
                "ffplay",
                "-f", "s16le",
                "-ar", str(STREAM_SAMPLE_RATE),
                "-ac", "1",
                "-nodisp",
                "-autoexit",
                "-loglevel", "quiet",
                "-i", "-",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if ffplay.stdin is None:
            raise SpeechError("ffplay stdin not available")

        models_to_try = [self.model_id]
        if self.model_id == "eleven_v3" and self.fallback_model_id and self.fallback_model_id != self.model_id:
            models_to_try.append(self.fallback_model_id)

        last_err: Optional[Exception] = None
        try:
            for attempt, model in enumerate(models_to_try):
                try:
                    self._stream_one(text, model, ffplay.stdin)
                    last_err = None
                    break
                except SpeechError as e:
                    last_err = e
                    if attempt < len(models_to_try) - 1:
                        logger.warning("model %s failed (%s); falling back to %s", model, e, models_to_try[attempt + 1])
                        continue
                    raise
        finally:
            try:
                ffplay.stdin.close()
            except (OSError, BrokenPipeError):
                pass
        if blocking:
            ffplay.wait()
        if last_err is not None:
            raise last_err

    def _stream_one(self, text: str, model_id: str, sink) -> None:
        # v3 (alpha) rejects optimize_streaming_latency with HTTP 400
        # "unsupported_model". Only attach it for v2-family models where it's
        # a documented latency optimization.
        params = [f"output_format={STREAM_OUTPUT_FORMAT}"]
        if model_id != "eleven_v3":
            params.append(f"optimize_streaming_latency={self.optimize_streaming_latency}")
        url = f"{ELEVENLABS_BASE}/text-to-speech/{self.voice_id}/stream?" + "&".join(params)
        body = json.dumps(
            {
                "text": text,
                "model_id": model_id,
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
                "accept": "audio/pcm",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                while True:
                    chunk = resp.read(4096)
                    if not chunk:
                        break
                    try:
                        sink.write(chunk)
                    except (BrokenPipeError, OSError):
                        return
        except urllib.error.HTTPError as e:
            try:
                detail = e.read().decode("utf-8", errors="replace")[:300]
            except Exception:  # noqa: BLE001
                detail = ""
            raise SpeechError(f"ElevenLabs HTTP {e.code}: {detail}") from e
        except urllib.error.URLError as e:
            raise SpeechError(f"ElevenLabs unreachable: {e.reason}") from e

    def _mp3_via_afplay(self, text: str, blocking: bool) -> Optional[Path]:
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
                "Neither ffplay nor afplay found. Install ffmpeg or run on macOS."
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
