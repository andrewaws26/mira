"""Tool layer: standalone Python functions exposed to the CLI and to MCP.

Each function in this module is one capability. The CLI maps subcommands to
these functions. The MCP server exposes these same functions as MCP tools so
Claude can call them directly. Tests can pass a fake ToolContext to swap in
mocks for the mount, camera, solver, and ephemeris.

Docstrings here become MCP tool descriptions. Write them so a model picking
between tools can decide what to call from the description alone.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Union

from pathlib import Path

from .camera import Camera, CameraError
from .config import CameraConfig, Config, load_config, setup_logging
from .ephemeris import Ephemeris, NameNotFoundError, get_ephemeris
from .iphone_camera import IphoneCamera, IphoneCameraConfig
from .mount import CelestronMount, MountError, ObserverInfo
from .narration import CompositionError, CompositionResult, compose
from .sfx import SfxError, SfxResult, generate as generate_sfx_audio
from .solver import SolveFailed, Solver, SolverError
from .speech import SpeechError, Speaker
from .state import StateDB

logger = logging.getLogger(__name__)


def _local_utc_offset_hours() -> float:
    """Local timezone offset from UTC, in hours. Positive east of UTC."""
    import time as _time
    if _time.daylight and _time.localtime().tm_isdst:
        return -_time.altzone / 3600.0
    return -_time.timezone / 3600.0


@dataclass
class ToolContext:
    """Shared dependencies for the tool layer. Construct once, reuse across calls.

    The mount is the only stateful piece: it owns a TCP connection to indiserver.
    Call `.connect_mount()` before any mount-touching tool runs, and
    `.shutdown()` on exit.
    """

    config: Config
    state: StateDB
    mount: CelestronMount
    # Camera backend: imagesnap-based Camera, or HTTP-based IphoneCamera
    # talking to the MiraCam iOS app. Selected by config.camera.source.
    camera: Union[Camera, IphoneCamera]
    solver: Solver
    ephemeris: Ephemeris
    speaker: Optional[Speaker] = None
    session_id: Optional[int] = None
    _mount_connected: bool = field(default=False, init=False, repr=False)

    @classmethod
    def from_config(cls, config_path: Path | str | None = None) -> "ToolContext":
        cfg = load_config(config_path)
        setup_logging(cfg)
        state = StateDB(cfg.storage.state_db)
        state.init()
        mount = CelestronMount(
            host=cfg.mount.indi_host,
            port=cfg.mount.indi_port,
            serial_port=cfg.mount.port or None,
            observer=ObserverInfo(
                latitude_deg=cfg.observer.latitude,
                longitude_deg=cfg.observer.longitude,
                elevation_m=cfg.observer.elevation_m,
                utc_offset_hours=_local_utc_offset_hours(),
            ),
        )
        camera = _build_camera(cfg.camera)
        # Solver does not validate the ASTAP binary at construction time, so
        # subcommands that do not need plate solving (resolve, where, status)
        # still work even when ASTAP is not yet installed.
        solver = Solver(
            astap_path=cfg.solver.astap_path,
            estimated_fov_deg=cfg.solver.estimated_fov_deg,
            timeout_seconds=cfg.solver.timeout_seconds,
            star_db=cfg.solver.star_db,
        )
        ephemeris = get_ephemeris(
            observer_lat_deg=cfg.observer.latitude,
            observer_lon_deg=cfg.observer.longitude,
            elevation_m=cfg.observer.elevation_m,
        )
        speaker = (
            Speaker(
                voice_id=cfg.speech.voice_id,
                model_id=cfg.speech.model_id,
                voice_settings={
                    "stability": cfg.speech.stability,
                    "similarity_boost": cfg.speech.similarity_boost,
                    "style": cfg.speech.style,
                    "use_speaker_boost": cfg.speech.use_speaker_boost,
                },
            )
            if cfg.speech.enabled
            else None
        )
        return cls(
            config=cfg,
            state=state,
            mount=mount,
            camera=camera,
            solver=solver,
            ephemeris=ephemeris,
            speaker=speaker,
        )

    def connect_mount(self, timeout: float = 10.0) -> None:
        if not self._mount_connected:
            self.mount.connect(timeout=timeout)
            self._mount_connected = True

    def disconnect_mount(self) -> None:
        if self._mount_connected:
            try:
                self.mount.disconnect()
            finally:
                self._mount_connected = False

    def shutdown(self) -> None:
        self.disconnect_mount()
        if self.session_id is not None:
            try:
                self.state.end_session(self.session_id)
            except Exception:  # noqa: BLE001
                logger.exception("failed to end session %d cleanly", self.session_id)


def _build_camera(cfg: CameraConfig) -> Union[Camera, IphoneCamera]:
    """Pick the camera backend based on config.camera.source."""
    if cfg.source == "iphone_bridge":
        return IphoneCamera(
            IphoneCameraConfig(
                base_url=cfg.iphone_url,
                discovery_timeout_s=cfg.iphone_discovery_timeout_s,
            )
        )
    # Default / "imagesnap": legacy Continuity-Camera-via-imagesnap path.
    return Camera(
        device_name=cfg.device_name,
        capture_dir=cfg.capture_dir,
        warmup_seconds=cfg.warmup_seconds,
        flip_180=cfg.flip_180,
    )


_default_ctx: Optional[ToolContext] = None


def set_default_context(ctx: ToolContext | None) -> None:
    """Install (or clear) the module-level context that tools fall back to."""
    global _default_ctx
    _default_ctx = ctx


def get_default_context() -> ToolContext:
    """Return the module-level context, building one from config on first use."""
    global _default_ctx
    if _default_ctx is None:
        _default_ctx = ToolContext.from_config()
    return _default_ctx


def _ctx(ctx: ToolContext | None) -> ToolContext:
    return ctx if ctx is not None else get_default_context()


def _speak(ctx: ToolContext, text: str) -> None:
    """Best-effort speech: silently swallow errors so a TTS hiccup never
    blocks an observation. Logged so debugging is possible.
    """
    if ctx.speaker is None or not ctx.speaker.is_configured():
        return
    try:
        ctx.speaker.speak(text, blocking=ctx.config.speech.blocking)
    except SpeechError as e:
        logger.warning("speech failed: %s", e)


def say(text: str, *, ctx: ToolContext | None = None) -> bool:
    """Speak text out loud through the configured TTS voice.

    Use this to give the user audible confirmations, target names,
    orientation hints, or short narrations while their eye stays at the
    eyepiece. Aim for 2 to 3 sentences per call (roughly 20 to 50 words);
    the eleven_v3 TTS destabilizes on one-word outputs and produces
    warbled phonemes. For a multi-fact narration, send the full passage
    in a single call rather than chunking; back-to-back small calls
    leave audible gaps because each new playback waits for the prior
    subprocess to drain. Do not read out coordinates, image paths, or
    stack traces.

    Args:
        text: utterance to synthesize and play.

    Returns:
        True if speech was attempted, False if speech is disabled or no
        API key is configured.
    """
    c = _ctx(ctx)
    if c.speaker is None or not c.speaker.is_configured():
        return False
    try:
        c.speaker.speak(text, blocking=c.config.speech.blocking)
        return True
    except SpeechError as e:
        logger.warning("speech failed: %s", e)
        return False


def compose_narration(
    story_text: str,
    music_prompt: str,
    *,
    voice_id: Optional[str] = None,
    voice_settings: Optional[dict] = None,
    music_volume: float = 0.35,
    intro_sfx_prompt: Optional[str] = None,
    outro_sfx_prompt: Optional[str] = None,
    intro_sfx_duration_s: Optional[float] = 6.0,
    outro_sfx_duration_s: Optional[float] = 8.0,
    output_path: Optional[Path | str] = None,
    ctx: ToolContext | None = None,
) -> dict:
    """Create a narrated audio piece (voice over a music bed) and save it.

    This is a creation tool. It synthesizes a narration via ElevenLabs TTS,
    generates a matched-length music bed via the ElevenLabs Music API, mixes
    them into a single mp3, and writes the file to disk. Nothing is played.
    The caller chooses what to do with the file.

    Use when the user wants a longer-form audio piece: a story with a
    cinematic bed, a guided tour, an atmospheric narration. Distinct from
    `say`, which is for short spoken responses while the user is at the
    eyepiece in Mira's own voice.

    The narration voice is independent of Mira's persona voice. The default
    is George (warm captivating storyteller, British). Override with
    `voice_id` to switch to a different ElevenLabs voice. See the voice
    catalog at the bottom of this docstring for picks by piece, and `mira
    voices` to browse the live account list.

    ## Voice selection decision tree

    Distilled from extensive iteration in May 2026. The single most
    valuable decision is which tool to reach for first.

    For a NEW character, ask in order:
      1. Does a premade voice already fit? (George, Joseph, Bill, Lily,
         Adam, Nathaniel, etc.) — Premade voices have guaranteed studio
         recording quality. Reach here FIRST for frame narrators and any
         role where vocal character doesn't need to be specific. Premade
         voices are v2-trained but synthesize on v3; emotional ceiling
         is lower than v3-designed voices.
      2. Does a library PVC fit, found by filter (accent + age + gender
         + use_case)? — Library Professional Voice Clones are clean,
         distinctive, and zero-design-effort. BUT: as of May 2026, none
         are formally v3-verified (`verified_languages` lacks eleven_v3).
         They synthesize on v3 with v2-flavored emotion: clean but
         emotionally limited. Use for warm-narrative characters who do
         not need to BREAK emotionally (Liina/Jorge/Tom registers).
         Search via `/v1/shared-voices` with filters; the web UI's
         curated collections ("Best for v3," "Announcers and Radio
         Hosts," "Epic Voices," per-language top-picks) are NOT exposed
         via API.
      3. Does the character NEED real emotion (panic, joy cracking,
         grief breaking)? — Design v3-native via Voice Design. v3 is
         the only model with realistic emotional range. Library PVCs
         and premades will sound zonked on emotional peaks; v3-designed
         voices can actually crack. Trade-off: variable recording
         quality, periodically boxy timbre. Follow the canonical recipe
         (see below).

    For a STRUGGLING character:
      - Voice quality wrong (boxy, muffled), character right: try
        Voice Remix on the voice with explicit anti-boxy descriptors.
        BE EXPLICIT ABOUT GENDER in remix prompts; remix can drift
        gender if unspecified.
      - Character wrong (flat reactions, wrong age, wrong register):
        re-design with stronger prompts, or Voice Remix toward the new
        register.
      - Recording quality right (clean) but character wrong: Voice
        Changer (speech-to-speech) — synthesize lines in a clean source
        voice, transform to target voice_id. Inherits source recording
        quality, target character.

    For SHARPNESS / EMOTIONAL RANGE (the most-fought issue in May 2026):
      - Lower stability to 0.20 (Creative-mode floor) for character
        voices. Higher (0.32) only for clarity-frame narrator.
      - Push style to 0.88-0.92.
      - Design prompts must EXPLICITLY authorize emotional dynamism.
        Static descriptors like "panicked" or "calm" produce zonked
        flatline. Use dynamic descriptors: "voice cracks when caught
        off guard," "shifts register inside a single sentence," "quick
        to react," "rapid emotional shifts within a single utterance."
        This unlocks the v3 ceiling.
      - Write fragmenting dialogue: em-dash cutoffs, mid-sentence
        pivots, ALL CAPS on key words, real interjections ("oh shit,"
        "wait, no, look," "holy crap"), repetition that captures
        frozen-loop emotion ("oh god oh god oh god").
      - For multi-character scenes, route through the Text to Dialogue
        API (shared emotional context across speakers, native em-dash
        interruptions). Solo-stitched dialogue cannot match dialogue-API
        coordination for fast back-and-forth.

    ## Voice slot management

    Plans have a hard cap on designed voices (30 on the Max plan as of
    May 2026). Voice Design + Voice Remix both consume slots. Library
    voices saved to My Voices do NOT consume slots — only generated
    ones do. When hitting the cap, DELETE superseded voices first:
    failed redesign attempts, voices from pivoted pieces that never
    shipped, early experiments. Use the v2 listing endpoint
    (`/v2/voices?page_size=100`) and filter to `category == "generated"`
    to see what you've created.

    ## When to consult the live ElevenLabs documentation

    This docstring captures what was true in May 2026, distilled from
    multi-session piece-building experience. The underlying APIs evolve.
    BEFORE building a new piece in a new register (a comedy bit, a
    horror radio play, a multi-vocalist musical), spend 2-3 minutes
    fetching the official docs to confirm capabilities haven't changed
    and to scout for tags or features added since this docstring was
    written. The relevant pages:

      v3 best practices (audio tags, stability, voice pairing):
        https://elevenlabs.io/docs/overview/capabilities/text-to-speech/best-practices
      v3 audio tag reference (1450+ tags across 11 categories):
        https://audio-generation-plugin.com/elevenlabs-v3/
        https://elevenlabs.io/blog/v3-audiotags
      Music API best practices (prompt registers, exclusions, BPM/key):
        https://elevenlabs.io/docs/overview/capabilities/music/best-practices
      Music composition plans (multi-section structure):
        https://elevenlabs.io/docs/eleven-api/guides/how-to/music/composition-plans
      Sound Effects API (duration, prompt_influence, looping):
        https://elevenlabs.io/docs/overview/capabilities/sound-effects
      Help-center SFX prompt guide:
        https://help.elevenlabs.io/hc/en-us/articles/25735604945041
      Text to Dialogue API (multi-speaker in one call, <2000 chars):
        https://elevenlabs.io/docs/overview/capabilities/text-to-dialogue
        https://elevenlabs.io/docs/api-reference/text-to-dialogue/convert
      Audiobook narrator voice collection:
        https://elevenlabs.io/voice-library/audiobook-narrator
      Voice Design (custom voice from prompt, two-step API flow):
        https://elevenlabs.io/docs/eleven-creative/voices/voice-design
        https://elevenlabs.io/docs/api-reference/text-to-voice/design
      ElevenLabs Showcase (community projects, mostly agents/music,
      few narrative examples to learn from as of May 2026):
        https://showcase.elevenlabs.io/projects

    Skip the doc-fetch when re-rendering the same piece type (another
    myth narration, another sky meditation): the patterns in this
    docstring are dialed in for that. Fetch when:
      - The user asks for a new register (musical, comedic, horror,
        documentary, ASMR, multi-vocalist)
      - A planned piece needs a feature this docstring doesn't cover
        (composition plans, looping SFX beds longer than 30s, MIDI-style
        timing)
      - The user reports "this isn't responding to tags / sounds flat"
        and the auto-tune defaults haven't shifted
      - A render fails with an unfamiliar error code

    When the docs disagree with this docstring, treat the live docs as
    authoritative and update this docstring with what you learn so the
    correction sticks.

    ## Audio tags (eleven_v3)

    Place tags inline to color delivery. Per official docs (verified May
    2026), tag combinations ARE permitted: `[whispers][shaken]` on the
    same phrase is supported and often gets you closer to a performance
    than a single tag. Test combinations though; some collide.

    The auto-tune (`narration.tune_voice_settings`) classifies a subset
    of tags into three buckets and steers stability+style by their ratio:

      ADVENTUROUS  [excited] [shouts] [confidently] [resolutely]
                   [determined] [angrily] [happily] [laughs] [triumphant]
      INTIMATE     [softly] [whispers] [warmly] [sadly] [calmly]
                   [pensively] [thoughtfully] [tender] [reverent] [gently]
                   [intimately]
      EXPLORATORY  [curious] [puzzled] [questioning] [hopeful] [wondering]

    OFFICIAL ELEVENLABS-CURATED 40 TAGS (highest reliability, June 2025):

    These are the tags ElevenLabs themselves vouch for in their public
    one-pager. When in doubt, use these; they are tested across voices.

      Voice-related (19): [laughs] [laughs harder] [starts laughing]
                          [wheezing] [whispers] [sighs] [exhales]
                          [sarcastic] [curious] [excited] [crying]
                          [snorts] [mischievously] [gasp] [giggles]
                          [panicked] [tired] [shouting] [trembling]
                          [serious] [robotically] [amazed]
      Sound effects (10): [gunshot] [applause] [clapping] [explosion]
                          [swallows] [gulps] [door slams] [rainfall]
                          [distant echo] [heartbeat] [thunder]
      Unique/special (7): [strong X accent] [sings] [woo] [fart]
                          [asmr mode] [underwater] [echoes]

    Beyond the official 40, ElevenLabs's broader unofficial library
    catalogs 1450+ tags across 11 categories (mood, environment, body
    states, narrative, genre, dialogue, effects, accents, humor,
    introspective). These work, but reliability varies per voice and
    per prompt. The high-yield subset below combines the official 40
    with a curated extension that has held up across pieces:

      Emotional       [crying] [in tears] [overcome] [breaking voice]
                      [sarcastic] [mischievously] [dramatic] [in awe]
                      [amazed] [shaken] [tired] [sorrowful] [angry]
                      [panicked] [trembling] [serious] [curious]
                      [excited]

      Vocal effects   [laughs] [laughs harder] [starts laughing]
                      [laughs softly] [chuckles] [giggles] [snorts]
                      [wheezing] [sighs] [sighs heavily] [exhales]
                      [gasp] [gasps] [pants] [breathing heavily]
                      [clears throat] [stammered apology] [robotically]

      Pacing/Direction [whispers] [shouts] [rushed] [drawn out]
                       [breathlessly] [hesitates] [pause] [pregnant pause]

      Dialogue        [interrupts] [overlapping] [talks over]
                      [responds quickly] [building tension]
                      [emotional outburst]

      Narrative style [poetic reading] [documentary style] [noir narration]
                      [unreliable narrator] [omniscient narrator]
                      [first-person narrative]

      Genre framing   [gothic horror] [film noir] [western] [cyberpunk]
                      [space opera] [dark fantasy] [comedy]

      Inline SFX      [gunshot] [applause] [clapping] [explosion]
                      [swallows] [gulps] [door slams] [rainfall]
                      [thunder] [thunderstorm] [campfire crackle]
                      [ocean waves] [forest morning] [heartbeat]

      Audio effects   [echo] [echoes] [distant echo] [reverb]
                      [underwater] [asmr mode] [telephone filter]
                      [vinyl crackle] [glitch] [pitch shift down]

      Accents         [strong X accent] where X is region/nationality.
                      Confirmed working: [strong french accent],
                      [strong texas accent], [strong british accent],
                      [strong southern us accent]. Effective for
                      cultural injection on a clean voice (e.g.
                      [strong chinese accent] on Joseph for an
                      Asian-cultural folktale without resorting to a
                      bedroom-mic shared-library clone).

      Humor           [deadpan delivery] [dry humor] [sarcastic remark]

    Tag stacking IS supported and often essential for performance: e.g.
    `[angry][laughing]` for an angry laugh, `[sad][whispers]` for
    weeping intimacy, `[shaken][breaking voice]` for a grief beat.
    Experiment; some combinations collide. Tags do NOT get read aloud,
    even if the model fumbles a delivery. Tag effectiveness varies per
    voice and per script (v3 is in public alpha, May 2026); audition
    new tag combos before committing them to a long render.

    Descriptive bracket tags also work, beyond the canonical list. Try
    `[trying to sound confident but nervous]`, `[suppressed emotion]`,
    `[gathering courage]`, `[forced calm]`, `[through gritted teeth]`.
    The model interprets natural-language stage directions in brackets
    as guidance, not lookup keys. This is how to express layered states
    where a single canonical tag does not capture the texture.

    To extend auto-tune coverage, add new tags to the sets in
    `narration.py` (`_ADVENTUROUS_TAGS`, `_INTIMATE_TAGS`, `_EXPLORATORY_TAGS`).

    ## Punctuation and capitalization shape delivery

    v3 takes punctuation seriously. ELLIPSES (`...`) create pauses with
    weight; commas vs. periods change phrasing pace; ALL CAPS on a single
    word increases emphasis without changing meaning. Use these alongside
    tags for fine control. v3 does NOT support SSML `<break/>` tags; use
    punctuation and `[pause]`-style tags instead.

    Hyphens / em dashes are interpreted as interruption cues in dialogue
    contexts (Text to Dialogue API): `"Maybe we should-"` cut off by
    the next speaker reads as a clean interrupt rather than a hard cut.
    For solo narration outside dialogue mode, hyphens just read as
    normal punctuation.

    ## Script length and consistency

    Short prompts (<250 chars) are less consistent on v3. The model is
    trained on longer-context speech and underperforms on a single
    fragment. For per-line synthesis (cinematic build pattern), this
    means very short lines like "Yes." or "I see it." can come out
    weirdly emotive or dropped-flat depending on auto-tune. Two
    mitigations:

      - Pad short lines with a contextual tag inside brackets that
        carries length: `[softly][thoughtfully] I see it.` reads more
        consistently than just `I see it.`
      - Group lines in a single synthesis call when context matters.
        Two short rapid-fire lines from the same character can be
        synthesized as a single text with a short pause built in via
        ellipsis: `"Yes... I see it."` instead of two clips.

    ## Crafting effective scripts (per ElevenLabs guidance)

    Beyond tags and punctuation, four principles separate flat
    scripts from performative ones:

      1. CONTEXT BEFORE CONTENT. Provide situational + emotional setup
         before the dialogue itself, in narration prose, not just tags.
         Weak:    `[angry] I cannot believe you did that.`
         Better:  `She found the empty jar and understood. Her voice
                  fell to a near-whisper. [angry] I cannot believe you
                  did that.`
         Even when the narrator section is short, the transition cues
         the voice into the right starting state.

      2. EMOTIONAL JOURNEY MAPPING. Plan the arc within a character's
         block of lines. Don't stay flat-emotional through a paragraph.
         A grief monologue can move from `[breaking voice]` -> `[crying]`
         -> `[overcome]` -> `[whispers]` across 4-6 lines. The arc gives
         the listener something to ride.

      3. CHARACTER VOICE CONSISTENCY. For each character, hold a brief
         profile: base emotion, stress response, humor style,
         vulnerability. Apply consistently across their lines so the
         character feels coherent. Example for a detective:
           base = [serious][analytical]
           stress = [intense][focused]
           humor = [dry][sarcastic]
           vulnerability = [quiet][reflective]

      4. LAYERED EMOTIONAL COMPLEXITY. Stack tags for nuanced internal
         states; the standalone canonical tags are usually too clean
         for a real moment. `[trying to sound confident but nervous]`,
         `[forced laugh][quieter][worried]`, `[controlled anger][through
         gritted teeth]` all synthesize as the descriptive prompt asks.

    ## Voice settings auto-tune

    On by default. Reads v3 audio tags from the script and picks stability
    and style aimed at performative delivery (never documentary-flat).
    Concrete ranges it lands in:

      - Heavy adventurous script (mostly [excited] / [confidently]):
            stability ~0.20-0.30, style ~0.80-0.85
      - Mixed-tag script (the typical case):
            stability ~0.35-0.45, style ~0.65-0.75
      - Heavy intimate script ([softly] / [warmly] / [whispers] dominant):
            stability ~0.45-0.55, style ~0.55-0.65

    Override only when the auto-tuned values do not fit. Common reasons:
      - The script is uniformly intimate but the chosen voice (e.g. Lily,
        velvety British) reads even smoother than auto-tune expects.
        Drop stability ~0.10 below auto.
      - The voice is naturally dynamic (Adam, Liam) and you want a calmer
        delivery: raise stability ~0.10 above auto.
      - Voice acting (multiple characters, theatrical performance):
        push stability to 0.20-0.30 with style 0.80-0.90 to unlock more
        swing. Auto-tune defaults are calibrated for solo narration and
        leave headroom on purpose.
    Pass `voice_settings={"stability": X}` to pin only that knob; style
    stays auto-tuned. Per-voice stability/style notes are listed in the
    voice catalog below.

    ElevenLabs's UI exposes three preset modes (Creative, Natural, Robust)
    that map roughly to:
      Creative  ~stability 0.20-0.35   most expressive, prone to glitches
      Natural   ~stability 0.45-0.55   closest to source, balanced
      Robust    ~stability 0.65-0.85   stable, ignores tag direction
    Stay in Creative or Natural for tagged scripts. Robust kills tag
    responsiveness and is only useful for unstyled documentary reads.

    Mapping by content type (per ElevenLabs guidance):
      Audiobook narration       Natural,  high tag responsiveness
      Character dialogue        Creative, very high tag responsiveness
      Corporate/news/journalism Robust,   medium tag responsiveness
      Educational material      Natural,  high
      Entertainment/comedy      Creative (low stability), exceptional

    ## Bookend SFX

    Pass `intro_sfx_prompt` to generate a cinematic open (a conch call,
    distant thunder, the rustle of leaves) that crossfades into the
    music; `outro_sfx_prompt` does the same for the close. Durations
    default to 6s and 8s respectively. Both go through the ElevenLabs
    Sound Effects API. SFX prompts are forgiving; describe the moment,
    not the genre ("dawn wind through pine, no music" beats "ambient
    intro track").

    ## Music API: prompting

    Per official docs (May 2026), prompt length does NOT correlate with
    quality. High-level intent prompts ("eerie meditation bed for night
    sky narration") often work as well as long technical specs. Two
    registers both work and can mix:

      Abstract mood:    eerie, foreboding, peaceful, raw, vast, lonely
      Detailed musical: dissonant violin screeches, pulsing sub-bass,
                        low cellos in C minor, glacial harmonic shifts

    High-leverage levers:
      - "instrumental only" (no vocals; cinematic narration beds)
      - "solo X" prefix for stem clarity ("solo cello", "solo french horn")
      - Tempo as range: "around 50-60 BPM"
      - Key signature: "in A minor", "in C dorian"
      - Negative space: "designed as a quiet bed under voice"
      - Exclusions: "no drums, no percussion of any kind, no vocals,
        no synthesizers" (combine multiple in one phrase)
      - Internal arc: "builds to one restrained climax around two thirds
        in then a long slow descent to near silence"

    For multi-section structured pieces (intro/verse/chorus, or scenic
    arcs with discrete movements), use the Music API's "composition
    plans" feature instead of a single prompt. Out of scope for
    `compose_narration`; see ElevenLabs docs.

    Rejections: the API rejects prompts that name copyrighted works
    (song titles, character names, named composers, named films like
    "Interstellar"). On rejection the API returns a clean
    `prompt_suggestion`; Mira surfaces it in the error message; rerun
    with the sanitized version. Length clamped to [10s, 300s]; pieces
    longer than 5 minutes have a silent music tail for the overflow,
    which can serve cosmic/contemplative pieces (silence is a feature,
    not a bug, when the script lands the close in a whisper).

    Cinematic-bed recipe (verified working for night sky narration):
      "cosmic ambient orchestral drone, scored as a quiet bed under
       spoken narration: extremely slow glacial pace, low sustained
       string drones in C minor with very gradual harmonic shifts every
       30 to 40 seconds, distant solo cello holding long notes,
       sub-bass drone in low fifths underneath, sparse high register
       chimes once every 20 seconds, builds to one restrained swelling
       climax around two thirds in then a long slow descent back to near
       silence, vast and lonely and patient, no drums, no percussion of
       any kind, no melody, no vocals, just slow harmonic breathing,
       very slow tempo around 50 to 60 BPM, designed to leave space
       around the spoken voice"

    ## Sound Effects API: prompting

    Real limits per official docs (May 2026):
      - Duration range: 0.1 to 30 seconds (Mira's `synthesize_sfx`
        constants are conservative at 0.5-22; raise if you need longer)
      - Default duration: auto-guessed from the prompt if None
      - prompt_influence (0-1, default 0.3): higher = literal adherence,
        lower = more creative variation. Bump to 0.6-0.8 for SFX that
        must hit a specific scripted moment (a bowstring at 'step back',
        a knife unsheathed). Leave at default for atmosphere.
      - Looping mode exists for ambient beds longer than 30s with
        seamless start/end (out of scope for `synthesize_sfx`'s thin
        wrapper today; access via the API directly if needed for a
        7-minute night-sky bed).

    Vocabulary the model recognizes (use these terms in prompts):
      Impact      collisions, hits
      Whoosh      directional movement, arrows, sword swings
      Ambience    environmental atmosphere
      Drone       textured sustained atmosphere
      Braam       cinematic hit (the trailer-music low BWAAAH)
      Glitch      malfunction effects
      One-shot    single non-looping event
      Loop        seamless repeat

    Composition tips:
      - Single events synthesize cleaner than complex sequences. For
        "bowstring twang into arrow whistle into thud" you'll get a
        better result splitting into two or three SFX clips and
        layering with timed offsets in ffmpeg, vs. one prompt.
      - For atmospheric beds, name the ENVIRONMENT first ("close ocean
        waves on wet sand at night") then add what's NOT there ("no
        music, no voices, no birds"). The negative list keeps the
        model from injecting unwanted content.
      - For trigger-anchored events (the bow at 'step back'), include
        a tail of silence in the prompt: "ends with one single distant
        thud, then total silence" so the SFX has its own breathing room
        in the mix.

    Quality failures and how to mitigate them (May 2026 user feedback):

    The SFX model handles BIG sounds well (ocean, wind, rain, thunder,
    fire, applause) but hallucinates niche specific foley into adjacent
    textures the way a sloppy auto-correct does. Verified failures:
      - "pen scratching on paper" -> metal screeching
      - "crickets at night" -> firecrackers
      - "low-frequency hum from life support" -> buzzing static
      - "door creaks" can produce wood scrape or gear grind
      - "footsteps on stairs" can produce rhythmic thuds resembling
        machinery

    Four-step mitigation, in order of impact:

      1. AMBIENCE FRAMING beats specific foley. Instead of asking for
         "pen scratching on paper at a desk," ask for "very quiet
         Victorian observatory at dawn, faint distant rooster, soft
         creaking wooden floor under weight." The model fills in the
         scene-appropriate quiet sounds itself, and gets them right
         because the framing is broad enough to ride on training data.

      2. EXPLICIT NEGATIVE CUES. Tell the model what NOT to do. Add
         phrases like "no buzzing, no static, no electronic noise, no
         hum, no machinery" to atmospheric prompts. The Titan piece
         had a buzzing-instead-of-hum failure that disappeared the
         moment negative cues were added. This is the single most
         reliable fix when a specific hallucination keeps recurring.

      3. RAISE prompt_influence to 0.5-0.7 (vs default 0.3). Forces
         literal adherence at the cost of creative variety. Particularly
         helpful for atmospheric beds where the model would otherwise
         drift toward common alternatives.

      4. KNOWN-GOOD VOCABULARY. Use the model's documented terminology
         (Ambience, Drone, Whoosh, Impact, Braam, Loop, distant). Avoid
         niche regional or domain terms (Polynesian instrument names,
         specific bird species, foreign-language words for objects);
         the model has no training to reach for them and will fall
         back to the wrong texture.

    If a generated SFX is bad, the cheapest fix is regeneration with
    the same prompt (the SFX API is non-deterministic). The next
    cheapest is rephrasing toward ambience + explicit negative cues.
    Audition 2-3 generations of any critical SFX before committing it
    to a long render where it cannot be cheaply re-mixed.

    Sound Effect Library and Music Marketplace (verified May 2026):
    Both are Studio-UI features only. NOT API-accessible. You cannot
    pull pre-curated SFX clips or browse-and-license music tracks
    programmatically. Stay on the synthesis APIs for cinematic builds.

    ## Voice Design: spinning up custom voices on demand

    For characters where neither the account voices nor the public
    library nail the register, Voice Design generates a custom voice
    from a text description. Two-step API flow:

      POST /v1/text-to-voice/design
        body: {
          "voice_description": "...",   # see prompt framework below
          "model_id": "eleven_ttv_v3",
          "text": "preview text 250+ chars ideally",
          "guidance_scale": 30.0,        # 20-40 recommended
          "loudness": 0.5
        }
        returns: { previews: [3 entries with generated_voice_id + audio_base_64] }

      POST /v1/text-to-voice/create-voice-from-preview
        body: {
          "voice_name": "...",
          "voice_description": "...",
          "generated_voice_id": "<from step 1>"
        }
        returns: { voice_id: "..." }    # use this like any voice_id

    Saved designed voices occupy a slot on the account and are reusable
    across pieces. See `~/mira-experiments/design_telegraph_voice.py`
    as a worked example.

    Canonical prompt framework (USE LITERALLY, per official docs):
      Native <Language>. <Gender>, <Age range>. <Quality level>.
      Persona: <2-5 words>. Emotion: <2-3 adjectives>.
      <1-2 sentences about timbre, pacing, delivery>

    Working example (the 1859 telegraph operator in Carrington Event):
      "Native English. Male, in his 50s. Good quality.
       Persona: 19th century American telegraph operator.
       Emotion: alarmed, urgent, weathered.
       A gravelly working-class voice with a slight Mid-Atlantic accent.
       Delivery is rapid and breathless, like reporting an emergency."

    Critical framework rules (session-tested):
      - Pick ONE quality tier: Ok / Good / Very good / Excellent /
        Studio / Broadcast. NEVER stack. The docs explicitly warn:
        "Including these types of phrases can sometimes reduce the
        accuracy of the prompt in general if the voice is very specific
        or niche." Niche persona + "studio quality" stacking was the
        verified cause of the boxy-Mom failure across four iterations.
      - guidance_scale default 25-30. Use 35-40 ONLY when accent or
        timbre specificity is the point. Higher guidance trades audio
        quality for prompt adherence; on niche prompts the trade
        produces boxy/muffled output. Docs canonical guidance values:
          20%   Movie Trailer, Squeaky Mouse (broad register)
          25%   Drill Sergeant
          30%   Pirate, Evil Ogre, Spanish Support Agent
          35%   Southern Woman, Arabic Customer Service
          38%   Mad Scientist (accent + character)
          40%   New Yorker, British Entrepreneur (accent-specific)
      - Specify pitch/timbre, not "feminine/masculine" alone (e.g.
        "lower-pitched, husky female voice")
      - Use "thick" or "slight" modifiers with named regional accents;
        avoid vague "foreign accent" phrasing
      - DO NOT include FX terms (reverb, echo, phone, tape) in the
        voice description; these belong in post-processing
      - Use "emphasis" or "delivery" instead of "accent" for intonation
        patterns that aren't regional
      - Match preview text to voice intent; a calm preview text on a
        gravelly-rough description produces unstable results
      - Be explicit about gender in EVERY prompt; Voice Design and
        especially Voice Remix can drift gender if unspecified

    EMOTIONAL DYNAMISM in design prompts (the most-recent breakthrough):
    Static-emotion descriptors ("panicked," "calm," "tired") produce
    voices stuck at one emotional register. They sound clean but
    "zonked on too high a dose of an antidepressant" (verbatim user
    feedback May 2026). The voice cannot break, cannot react, cannot
    flicker.

    Fix: explicitly authorize emotional dynamism in the prompt itself.
    Add phrases like:
      - "voice cracks when caught off guard"
      - "shifts register inside a single sentence"
      - "quick to react"
      - "rapid emotional shifts within a single utterance"
      - "audibly breathing"
      - "softens with awe, sharpens with annoyance"
      - "never composed for its own sake, alive and responsive"

    Voices designed with these unlock the v3 expressive ceiling. The
    Backyard UFO cast (Tom skeptic, Brittany believer, Linda mom)
    were all designed with this approach and produced the sharpness
    that earlier pieces lacked.

    Variable recording quality is real. Voice Design's three previews
    per generation can each have different timbre. If preview 0 sounds
    boxy/muffled, audition preview 1 and 2 (saved to build dir as
    `preview_<role>_<i>.mp3`) and pick a different one. If all three
    are bad, lower guidance_scale (try 22-25) and re-design.

    Safety guardrails (May 2026, ElevenLabs side):
      - Cannot design child or teen voices (blocked at prompt level
        with HTTP 403 blocked_generation). Use young-adult library
        voices for youth roles (Jessica `cgSgspJ2msm6clMCkdW9`, Liam
        `TX3LPaxmHKxFdv7VOQHJ`, Ana Rita `wJqPPQ618aTW29mptyoc`).
      - Cannot synthesize preview text describing minors in distress
        (e.g. "child swallowed quarter, not breathing"). Rewrite to
        adult scenarios (parent who collapsed, etc.).

    PVCs (professional voice clones beyond the public library) are
    documented as not yet fully optimized for v3. Use Instant Voice
    Clones (IVCs) or designed voices for v3 work. Premade library
    voices remain the safest pick for production-critical narration.

    ## Voice Remix (patch a designed voice in place)

    Endpoint: `POST /v1/text-to-voice/{voice_id}/remix` (voice_id in
    PATH, not body). Body: { voice_description, text, guidance_scale,
    loudness, prompt_strength }. Returns 3 previews; save chosen via
    create-voice-from-preview like Voice Design.

    voice_description here describes the CHANGE you want, not the full
    voice. Example: "Same warm tired mid-40s mom character, but voice
    should crack with surprise, soften with awe, sharpen with
    annoyance, all within single sentences. Female mid-40s American
    accent. Keep the warmth, add the sharpness."

    Gender drift footgun: Voice Remix can flip gender if the remix
    description doesn't restate it. The Mom v3 remix went male because
    the prompt said "her" but didn't explicitly say "female." Always
    restate gender in remix prompts.

    prompt_strength (0-1, default 0.8): balance prompt vs reference
    audio. 0.7 leaves more of the source voice intact; 0.9 leans hard
    into the new description. Use 0.7 for surgical patches (timbre
    only), 0.9 for character-level shifts.

    Works on previously designed voices, IVCs, PVCs, and library voices
    with infinite notice periods. Does NOT work on premade library
    voices (George, Sarah, Adam, etc.).

    ## Voice Changer / Speech-to-Speech (rare but powerful)

    Endpoint: `POST /v1/speech-to-speech/{voice_id}` with audio file.
    Takes existing audio and re-renders it in the target voice. Source
    recording quality is inherited; target voice character is applied.

    Use when:
      - A designed voice has perfect character but boxy timbre — synth
        lines in a clean premade voice (Sarah, George), then transform
        to the designed voice_id as target
      - A library voice has perfect quality but you want different
        character — synth in the library voice, transform to a designed
        target

    Out of scope for `compose_narration` today. Access the endpoint
    directly via urllib for one-off salvage operations.

    ## Mixing best practices (single ffmpeg pass)

    The cinematic build pattern (`build_alien_visit.py`,
    `build_orion_v2.py`) mixes everything in one `filter_complex`:

      [0:a]volume=BED_VOL[bed]            # music bed at low volume
      [N:a]adelay=MS|MS[vN]               # voice line N delayed to start time
      [M:a]loudnorm=I=-12,volume=V,
           adelay=MS|MS[sM]               # SFX normalized then volumed/delayed
      [bed][v0]...[s0]...amix=
           inputs=N:duration=longest:
           normalize=0,alimiter=limit=0.95[out]

    Volume calibration that holds up across pieces:
      - Music bed:     0.18-0.25  (lower for cosmic/intimate; higher for
                                   lively narrative)
      - Voice:         1.0 (no scaling; voices vary 0.7-1.0 naturally)
      - SFX dramatic:  1.0-1.10  (after loudnorm; bow, thunder, impact)
      - SFX ambient:   0.45-0.65 (after loudnorm; wind, ocean, room tone)

    `loudnorm=I=-12:LRA=6:TP=-1.5` on SFX before volume/delay normalizes
    inconsistent ElevenLabs SFX levels so a single volume coefficient
    behaves predictably. Without it, identical volume= values produce
    wildly different perceived loudness across SFX clips.

    `alimiter=limit=0.95` at the end prevents amix overshoot from
    clipping when voices, bed, and SFX all peak together.

    `normalize=0` on amix is critical: amix's default normalize=1 will
    auto-divide everything by the input count, killing your bed level
    when you have 80+ voice streams. Always pass `normalize=0`.

    Voice tail "blip" fix: each ElevenLabs mp3 voice clip can have a
    tiny artifact at its tail (decoder boundary, frame padding) that
    becomes audible as a click when adjacent to the silence introduced
    by `adelay`. Apply a 50ms `afade=t=out` on each voice clip BEFORE
    the adelay in the filter graph:

        [{idx}:a]afade=t=out:st={dur-0.05}:d=0.05,
                 adelay={delay}|{delay}[{lbl}]

    `dur` is the clip's probed duration in seconds. The fade is short
    enough to be imperceptible but kills the boundary click. Verified
    on the Carrington render (May 2026). Apply per voice clip; SFX
    clips already get loudnorm normalization that masks the same issue.

    Voice synthesis: parallelize with ThreadPoolExecutor(max_workers=4).
    More than 4 risks ElevenLabs rate-limiting; less is unnecessarily
    slow on long pieces.

    Cache intermediates by stable filename (`line_NNN_speaker.mp3`,
    `sfx_NAME.mp3`, `music.mp3`) so re-runs only re-synth what changed.
    To bust the cache, delete the file or directory.

    ## Voice catalog

    All voices below are on the account today and synthesize fine on
    eleven_v3 even though none are formally `verified_languages` v3 (this
    has been validated repeatedly: the alien-visit cinematic used Joseph
    and Grace on v3 with no issues). When picking, weight first by
    use_case = "narrative_story", then by accent and emotional fit.

    *** STORY / NARRATOR (top picks for long-form pieces) ***

      George   (JBFqnCBsd6RMkjVDRZzb)  male British, warm
        Default. Captivating storyteller cadence; safe pick for any
        myth/folktale/cosmology piece. Auto-tune lands well, no override
        needed for typical mixed-tag scripts.

      Joseph   (oFoV1sxkPgfSMBqWZKyp)  male US-Southern, deep baritone
        Resonant, authoritative, gravitas-heavy. Picked for the alien-
        visit cinematic; reads four-thousand-year-old folktales
        beautifully. Expect a slight Southern flavor regardless of
        material; lean into it for grounded/ancient feel, avoid for
        urban/sci-fi.

      Bill     (pqHfZKP75CvOlQylNhV4)  male American, "old wise"
        Grandfather-by-the-fire. The high-stability exception: Bill
        wants stability ~0.10 ABOVE auto for his measured cadence to
        land. Never push him into low-stability territory; he shatters.
        Try `voice_settings={"stability": 0.55}` for intimate scripts.

      Lily     (pFZP5JQG7iQjIQuC4Bku)  female British, velvety
        Intimate, actressy, breathy. Reads even smoother than auto-tune
        expects: drop stability ~0.10 BELOW auto for emotional swing,
        e.g. `{"stability": 0.30}` on a [softly]-heavy piece. Strong
        pick for feminine-protagonist stories.

      Brian    (nPczCjzI2devNBz1zQrb)  male American, deep/comforting
        Smoother and less Southern than Joseph; documentary-narrator
        flavor. Reach for him when Joseph feels too heavy.

      Grace    (wxlw6ulIrrWVbeIh6Jut)  female US-Southern, honey drawl
        Approachable, unhurried, neighbor-by-the-porch. Pairs well with
        Joseph for a male/female cinematic with regional flavor. Used in
        the alien-visit piece.

    *** EXPRESSIVE / DYNAMIC RANGE (handle low stability well) ***

      Adam     (pNInz6obpgDQGcFmaJgB)  male American, dominant/firm
        Brash bright tenor; can take low stability without pitch breaking.
        Good for assertive characters or high-energy beats.

      Liam     (TX3LPaxmHKxFdv7VOQHJ)  male American, energetic young
        Tolerates low stability. Social-media tone by default; useful for
        modern/punchy pieces, less for ancient myth.

      Harry    (SOYHLrjzK2X1ezoPC6cr)  male American, "fierce warrior"
        Character voice. Battle scenes, hero monologues. Steps on quiet
        material.

      Callum   (N2lVS1w4EtoT3dr4eOWO)  male American, husky trickster
        Gravelly, slightly menacing. Antagonists, mystery beats.

      Rick     (2O7bZzDHV7ipIs6A0YLs)  raspy mad scientist
        Pure character voice. Cynical, slurred, scientific cadence.
        Comedic only.

    *** EDUCATIONAL / CLEAN DELIVERY ***

      Alice    (Xb7hH8MSUJpSbSDYk0k2)  female British, educator
      Matilda  (XrExE9yKIg1WjnnlVkGX)  female American, professional
      Daniel   (onwK4e9ZLuTAKqWW03F9)  male British, broadcaster
      Bella    (hpp4J3VqNfWAUOO0d1Us)  female American, warm-pro
      Sarah    (EXAVITQu4vr4xnSDxMaL)  female American, mature
        These are competent for explainers and tours. They tend toward
        documentary-flat and need explicit audio tags + low stability to
        feel like an experience.

    *** SPANISH-ACCENTED ENGLISH (Mira's register) ***

      Vega     (pTX8uGyVgHCWLj6IkcbC)  female, calm narrator
        Mira's persona voice on the Starter plan. Free-tier accounts
        cannot synthesize her; fall back to Sarah. Slow-paced, intimate,
        wellness-leaning. The default voice in `~/mira/config.yaml`.
      Veronica (86U1Fs5aPKKkDKi9PRxz)  female Puerto Rican, narrative
      Esperanza (6sefJctHkzCgLShKcnrI) female Colombian, serene
      Laura-Bilingual (FGLJyeekUzxl8M3CTG9M) female Latina, English+ES
      Fernanda (ARmPWZKt7WpXh6QDHA6x)  female Mexican, articulate
      Lorena   (dvIBbCEt41yUyHBRbI5A)  female Mexican, vibrant

    *** CONVERSATIONAL (rarely the right pick for narration) ***

      River, Will, Jessica, Eric, Chris, Roger, Charlie, Laura-Quirky.
      Reach past these for any mythic/cinematic piece.

    ## Asian-accent shared-library voices (use voice_id directly)

    Not on the account but synthesizable by passing the voice_id; the
    shared-library voice gets added implicitly on first use. Validated
    by usage_1y > 500K (popularity proxy for quality) and use_case =
    narrative_story unless noted. Audition first for any high-stakes
    piece; accents that look fine in metadata can sound stiff in
    narration. Andrew's standing rule (May 2026): accent for cultural
    authenticity is good, but only when the voice is genuinely
    expressive; otherwise default to George/Joseph/Bill.

      Deep Bass - Malaysian Soul (NIkIuJZ8oQMuKZqwKtnm)
        Male, low bass, Malaysian-Chinese accent. Commanding, warm,
        natural vibrato. Strongest accent-authentic candidate for
        Chinese folktales (Chang'e, Hou Yi, Mid-Autumn). 1.34M usage_1y.

      Bilingual Sakura (HBr48ROZd1B2dv74C8bN)
        Female, Japanese, clear and warm bilingual narrator. Travel-
        guide / educational feel. Good for Japanese folktales; risks
        cultural mismatch on Chinese material.

      Yusuke (Lci8YeL6PAFHJjNKvwXq)
        Male, Japanese, articulate. Educational-narration feel
        (3blue1brown style). Clean but on the calm side; tag heavily
        to push him out of documentary-flat.

      Kei (qjx83Y0UcERgVPICvVpl)
        Male, Japanese, calm raspy youthful. Good for younger or more
        intimate Asian-cultural narration.

      Louis - Singaporean (rOVKXrU0YcQMzmTNwaDq)
        Male, Singaporean, calm conversational. Cosmopolitan East-Asian
        feel; less culturally specific than the Mainland-Chinese voices.

      Jet Du - ASMR Chinese (mEHuKdn0uRQSMynXjRNO)
        Male, Chinese, soft whisper. ASMR/meditative; perfect for the
        intimate beats of a piece but cannot carry full narration.

      Mila - Soft Meditation (zNk6QuA4ZKSf5GTyAPuF)
        Female, gentle Asian-Australian accent. Meditation/affirmation
        register. Calm, slow.

    Hard skip: any voice with a strong non-English accent on
    `eleven_multilingual_v2`. Koro (Maori, NZ) was unintelligible on v2
    in May 2026 testing. Stay on v3 for accent-strong voices, full stop.

    Recording-quality note: `premade` voices were captured in ElevenLabs
    studios and sound clean. `professional` voices passed review but are
    still cloned from user-uploaded audio; the cloner's room/mic comes
    through at v3 fidelity (bedroom reverb, USB-mic resonance). Shared-
    library voices (not on the account, used by voice_id directly) are
    the most variable: studio-quality cloners exist alongside laptop-mic
    ones. For polished pieces, prefer `premade` first and audition any
    cloned voice before committing.

    Cultural-accent trick: if you want, e.g., a Chinese-accented English
    delivery for a Chinese folktale but the cloned-voice options sound
    bedroom-recorded, try `[strong Chinese accent]` on a clean premade
    voice (George, Joseph, Bill). The result is variable per voice and
    per script; audition first. PVCs (professional voice clones beyond
    the public library) are documented as not fully optimized for v3
    yet, so the trick works best on premade voices.

    ## When to escalate from compose_narration to a custom cinematic

    `compose_narration` is single-voice. For multi-character pieces
    there are TWO escalation paths, picked by piece length:

    Path A: Text to Dialogue API (POST /v1/text-to-dialogue) for SHORT
    scenes under ~2000 characters total. One API call returns coordinated
    multi-speaker audio with v3 tags honored across speakers AND with
    shared emotional context (characters react to each other). This is
    the right tool for fast back-and-forth, interruptions, comedic
    timing, and any scene where the model can leverage shared prosody.
    Solo-stitched dialogue cannot match it for natural pacing. Limits:

      - 2000 character total cap across all inputs[].text
      - Up to 10 unique voices per request
      - eleven_v3 only
      - Every input.text must have non-empty text AFTER stripping audio
        tags and emojis. `("speaker", "[long pause]")` alone will fail
        with HTTP 400 input_text_empty. Merge with adjacent utterance:
        `("speaker", "[long pause][deflated] Oh.")` works.
      - Inline audio tags supported per turn ([giggling], [curious], etc)
      - Punctuation does interruption work: hyphens/em-dashes for
        cutoffs, ellipses for trailing speech
      - Optional `seed` (0..4294967295) for deterministic re-renders

    Request shape:
      {
        "inputs": [
          {"text": "[curious] Who's there?", "voice_id": "..."},
          {"text": "[giggling] It's me.",    "voice_id": "..."}
        ],
        "model_id": "eleven_v3",
        "settings": {"stability": 0.20, "style": 0.92, "use_speaker_boost": true}
      }

    CRITICAL FOOTGUN: per-voice VOICE_SETTINGS are IGNORED in dialogue
    mode. The dialogue endpoint uses a single settings dict in the
    request body that applies to all speakers. If you set per-voice
    stability=0.20 elsewhere in your build script, the dialogue scenes
    will STILL synthesize at whatever stability you pass here (default
    0.50). For emotional swing, you MUST pass `{"stability": 0.20,
    "style": 0.92}` in the request body. The Signal piece bridge scenes
    were under-emoted for two iterations because this was missed.

    Mira does not yet wrap this endpoint. When a piece needs it, either
    add `synthesize_dialogue()` to `narration.py` (mirrors
    `synthesize_voice` but POSTs to /v1/text-to-dialogue) or call the
    endpoint directly via urllib in a one-off experiment. See
    `~/mira-experiments/build_backyard.py::synthesize_dialogue_scene`
    for a working pattern.

    Path B: Custom build script for LONG pieces (>2000 chars), pieces
    needing woven SFX at specific timestamps, or pieces needing a music
    bed that arcs across scenes. Models: `build_alien_visit.py`,
    `build_orion_v2.py`. The pattern:

      - Import `synthesize_voice`, `synthesize_sfx`, `synthesize_music`
        directly from `mira.narration` and `mira.sfx`
      - SCRIPT is a list of (speaker, text, gap_after_seconds) tuples
      - Per-character voice_id and voice_settings dicts at top of file
      - SFX_EVENTS list with name/prompt/duration/trigger_event/lead_in_s
      - One MUSIC_PROMPT for the whole piece; arc described in prompt
      - LEAD_IN_S constant for opening silent atmosphere
      - Single ffmpeg filter_complex mixing voice + SFX + music with
        adelay per stream, loudnorm before SFX volume, alimiter ceiling
      - ThreadPoolExecutor(max_workers=4) for parallel voice synthesis
      - Cache by stable filename; bust by deleting build dir

    The two paths compose: in a long cinematic build script, you can use
    Path A inside a single SCRIPT entry to get a tight back-and-forth
    exchange with shared timing context, then layer the resulting clip
    into the larger ffmpeg mix. Use Path A whenever two characters
    interrupt or talk over each other, since native dialogue handling
    keeps overlaps natural in ways adelay-stitched solo synths cannot.

    ## Audiobook-specialized voices (in the public library)

    Per the ElevenLabs Audiobook Narrator collection (May 2026), five
    voices are tuned for long-form storytelling. Not on the account by
    default; pass voice_id directly. Audition before committing.

      David       BNgbHR0DNeZixGQVzloa   British male, deep storyteller
      Nathaniel   7S3KNdLDL7aRgBVRQb1z   British male, deep/rich/mature,
                                         literary works across genres
      Ana Rita    wJqPPQ618aTW29mptyoc   British female (young), smooth
                                         expressive, emotional narration
      Amelia      nBoLwpO4PAjQaQwVKPI1   Australian female (young),
                                         versatile, character-friendly
      Grandma Rachel 0rEo3eAjssGDUCXHYENf US Southern senior female,
                                         warm, tall-tale-telling
                                         grandmother voice

    These complement the on-account picks (George/Joseph/Bill/Lily) when
    a piece needs a specifically literary or grandmotherly register.

    Requires ffmpeg and ffprobe on PATH (`brew install ffmpeg`). Requires
    ELEVENLABS_API_KEY in the environment or in ~/mira/.env.

    Args:
        story_text: the narration text. Audio tags allowed inline.
        music_prompt: free-text description of the music bed (instruments,
            tempo, mood). Avoid named copyrighted works.
        voice_id: ElevenLabs voice id. Defaults to George.
        voice_settings: optional override of the narration voice settings.
            Recognized keys: stability, similarity_boost, style,
            use_speaker_boost. Lower stability gives more emotional swing.
        music_volume: how loud the music bed sits under the voice, in
            [0.0, 1.0]. Default 0.35.
        output_path: where to save the mp3. Defaults to a timestamped name
            under ~/mira/captures/narrations/.

    Returns:
        Dict with the saved path and metadata about the piece:
          output_path (str), voice_duration_s, music_duration_s,
          voice_id, story_text, music_prompt.
    """
    # ctx accepted for symmetry with the rest of the tool layer; this tool
    # only touches ElevenLabs and ffmpeg, so it does not need the mount
    # or camera dependencies.
    _ = ctx
    out: Optional[Path] = None
    if output_path is not None:
        out = Path(output_path).expanduser() if isinstance(output_path, str) else output_path
    try:
        result: CompositionResult = compose(
            story_text=story_text,
            music_prompt=music_prompt,
            voice_id=voice_id,
            voice_settings=voice_settings,
            music_volume=music_volume,
            intro_sfx_prompt=intro_sfx_prompt,
            outro_sfx_prompt=outro_sfx_prompt,
            intro_sfx_duration_s=intro_sfx_duration_s,
            outro_sfx_duration_s=outro_sfx_duration_s,
            output_path=out,
        )
    except CompositionError as e:
        logger.error("compose_narration failed: %s", e)
        raise
    return {
        "output_path": str(result.output_path),
        "voice_duration_s": round(result.voice_duration_s, 3),
        "music_duration_s": round(result.music_duration_s, 3),
        "voice_id": result.voice_id,
        "story_text": result.story_text,
        "music_prompt": result.music_prompt,
        "intro_sfx_prompt": intro_sfx_prompt,
        "outro_sfx_prompt": outro_sfx_prompt,
        "intro_sfx_duration_s": result.intro_sfx_duration_s,
        "outro_sfx_duration_s": result.outro_sfx_duration_s,
    }


def generate_sfx(
    prompt: str,
    *,
    duration_seconds: Optional[float] = None,
    prompt_influence: float = 0.3,
    output_path: Optional[Path | str] = None,
    ctx: ToolContext | None = None,
) -> dict:
    """Generate a sound effect from a text prompt and save it as mp3.

    Use for stingers, atmospheric beds, or one-shot sounds: a conch shell
    call, distant thunder, an owl hoot, the rustle of canoe ropes, the
    whir of a telescope motor. Pure creation tool. The file is saved
    under ~/mira/captures/sfx/ and the path is returned. Nothing is
    played. The caller decides what to do with the file.

    Args:
        prompt: free-text description of the sound. The more sensory the
            better ("a single deep conch shell call across open water,
            with reverb tail" beats "conch").
        duration_seconds: optional length in [0.5, 22]. When omitted, the
            model picks a length appropriate to the prompt.
        prompt_influence: in [0, 1]. Higher follows the prompt more
            literally; lower lets the model exercise creative freedom.
            Default 0.3.
        output_path: optional output path. Defaults to a timestamped
            name under ~/mira/captures/sfx/.

    Returns:
        Dict with output_path, prompt, duration_seconds, prompt_influence.
    """
    _ = ctx
    out: Optional[Path] = None
    if output_path is not None:
        out = Path(output_path).expanduser() if isinstance(output_path, str) else output_path
    try:
        result: SfxResult = generate_sfx_audio(
            prompt=prompt,
            duration_seconds=duration_seconds,
            prompt_influence=prompt_influence,
            output_path=out,
        )
    except SfxError as e:
        logger.error("generate_sfx failed: %s", e)
        raise
    return {
        "output_path": str(result.output_path),
        "prompt": result.prompt,
        "duration_seconds": result.duration_seconds,
        "prompt_influence": result.prompt_influence,
    }


def get_target_coordinates(name: str, *, ctx: ToolContext | None = None) -> tuple[float, float]:
    """Resolve a target name to apparent equatorial coordinates.

    Use this when the user asks to point at a named object (planet, Messier
    object, named star, common DSO alias). Returns coordinates valid right
    now at the configured observer location, accounting for precession,
    nutation, and aberration. Pass the result to `slew_to`.

    Args:
        name: Target name. Examples: "Jupiter", "Mars", "M31", "Andromeda",
              "Vega", "Pleiades", "Orion Nebula", "Polaris".

    Returns:
        Tuple of (ra_degrees, dec_degrees) where RA is in [0, 360) and Dec
        is in [-90, 90].

    Raises:
        NameNotFoundError: if the name is not in the catalog.
    """
    coords = _ctx(ctx).ephemeris.resolve(name)
    return coords.ra_deg, coords.dec_deg


def capture_frame(*, ctx: ToolContext | None = None) -> Path:
    """Capture a single frame from the iPhone via Continuity Camera.

    Saves a JPEG under the configured capture directory. Use this before
    `plate_solve` to get a starfield image of the current pointing.

    Returns:
        Path to the saved JPEG.

    Raises:
        CameraError: if imagesnap is missing, the camera is not visible,
            or capture fails.
    """
    return _ctx(ctx).camera.capture()


def plate_solve(
    image_path: Path | str,
    *,
    ra_hint_deg: Optional[float] = None,
    dec_hint_deg: Optional[float] = None,
    ctx: ToolContext | None = None,
) -> Optional[tuple[float, float]]:
    """Plate-solve an image to find what the telescope is pointed at.

    Runs ASTAP against the image. Optional RA/Dec hints (typically the
    mount's reported position) speed up the solve substantially.

    Args:
        image_path: path to a JPEG, PNG, or FITS image.
        ra_hint_deg: optional approximate RA in degrees.
        dec_hint_deg: optional approximate Dec in degrees.

    Returns:
        Tuple of (ra_degrees, dec_degrees) on success, or None if ASTAP
        could not find a solution.
    """
    c = _ctx(ctx)
    try:
        result = c.solver.solve(
            image_path,
            ra_hint_deg=ra_hint_deg,
            dec_hint_deg=dec_hint_deg,
        )
    except SolveFailed as e:
        logger.info("plate solve failed: %s", e)
        return None
    except SolverError as e:
        logger.error("plate solve error: %s", e)
        raise
    return result.ra_deg, result.dec_deg


def sync_mount(ra_deg: float, dec_deg: float, *, ctx: ToolContext | None = None) -> bool:
    """Tell the mount its current pointing is at the given RA/Dec.

    Call this after a successful `plate_solve` so the mount knows where it
    actually is. This is what replaces traditional star alignment: the
    plate solution overrides whatever fake alignment was used at startup.

    Args:
        ra_deg: apparent RA in degrees [0, 360).
        dec_deg: apparent Dec in degrees [-90, 90].

    Returns:
        True if the mount accepted the sync.
    """
    c = _ctx(ctx)
    c.connect_mount()
    success = c.mount.sync(ra_deg=ra_deg, dec_deg=dec_deg)
    if success:
        c.state.record_sync(
            ra_deg=ra_deg,
            dec_deg=dec_deg,
            session_id=c.session_id,
        )
    return success


def slew_to(ra_deg: float, dec_deg: float, *, ctx: ToolContext | None = None) -> bool:
    """Command the mount to slew to the given apparent RA/Dec.

    This issues the slew and blocks until the mount reports completion or
    times out. Sync the mount first with `sync_mount` if it has not already
    been synced this session.

    Args:
        ra_deg: target RA in degrees [0, 360).
        dec_deg: target Dec in degrees [-90, 90].

    Returns:
        True if the slew completed successfully.
    """
    c = _ctx(ctx)
    c.connect_mount()
    slew_id = c.state.record_slew(
        target_name=None,
        target_ra_deg=ra_deg,
        target_dec_deg=dec_deg,
        session_id=c.session_id,
    )
    success = c.mount.slew_to(ra_deg=ra_deg, dec_deg=dec_deg)
    achieved_ra, achieved_dec = c.mount.get_position()
    c.state.update_slew_result(
        slew_id=slew_id,
        achieved_ra_deg=achieved_ra,
        achieved_dec_deg=achieved_dec,
        success=success,
    )
    return success


def get_mount_position(*, ctx: ToolContext | None = None) -> tuple[float, float]:
    """Query the mount for its current pointing.

    Returns the mount's own belief about where it is pointing, which is
    only as good as its last sync. Use `plate_solve` if you need ground truth.

    Returns:
        Tuple of (ra_degrees, dec_degrees).
    """
    c = _ctx(ctx)
    c.connect_mount()
    return c.mount.get_position()


def wait_for_slew_complete(timeout: int = 60, *, ctx: ToolContext | None = None) -> bool:
    """Block until the mount finishes its current slew.

    Useful when slew was issued asynchronously, or to wait out tracking
    settling. `slew_to` already blocks internally; this is for cases where
    a slew was issued by other means.

    Args:
        timeout: maximum seconds to wait.

    Returns:
        True if the mount became idle within the timeout, False if it timed out.
    """
    c = _ctx(ctx)
    c.connect_mount()
    return c.mount.wait_slew_complete(timeout=float(timeout))


def get_observer_location(*, ctx: ToolContext | None = None) -> tuple[float, float]:
    """Return the configured observer latitude and longitude in degrees.

    Used by ephemeris computations and as a sanity check for the operator.
    Configure via `observer.latitude` and `observer.longitude` in config.yaml.

    Returns:
        Tuple of (latitude_degrees, longitude_degrees). Negative latitude is
        southern hemisphere; negative longitude is west of Greenwich.
    """
    obs = _ctx(ctx).config.observer
    return obs.latitude, obs.longitude


INDISERVER_PIDFILE = Path("~/mira/indiserver.pid").expanduser()
INDISERVER_LOGFILE = Path("~/mira/indiserver.log").expanduser()


def _indiserver_listening(host: str = "localhost", port: int = 7624) -> bool:
    import socket as _socket

    try:
        with _socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        return False


def wake_up(*, ctx: ToolContext | None = None) -> dict:
    """Bring Mira online: start indiserver if needed, connect to the mount,
    and report status.

    Use this when the user says "turn on Mira", "wake up", "start a
    session", "open Mira", or anything else that means "make the
    telescope reachable from here." Idempotent: safe to call when
    indiserver is already running and the mount is already connected.

    Pre-requisites the user must handle manually (Mira cannot do these):
      - Mount is powered on
      - Hand controller is past its alignment screens (any "fake"
        alignment will do)
      - FTDI cable connects the hand controller to the Mac

    Returns a dict with:
      indiserver_started: bool (True if we started a fresh process)
      indiserver_pid: int or None
      mount_connected: bool
      ra_deg, dec_deg: float (current pointing if connected)
      message: short human-readable status

    On any failure the dict still returns; check `mount_connected`.
    """
    import shutil as _shutil
    import subprocess as _subprocess
    import time as _time

    c = _ctx(ctx)
    result = {
        "indiserver_started": False,
        "indiserver_pid": None,
        "mount_connected": False,
        "ra_deg": None,
        "dec_deg": None,
        "message": "",
    }

    indi_bin = _shutil.which("indiserver")
    if indi_bin is None:
        result["message"] = "indiserver not on PATH; build INDI first"
        _speak(c, "I cannot find indiserver. Build INDI first.")
        return result

    if _indiserver_listening(c.config.mount.indi_host, c.config.mount.indi_port):
        result["message"] = "indiserver already up"
    else:
        INDISERVER_LOGFILE.parent.mkdir(parents=True, exist_ok=True)
        log_fh = open(INDISERVER_LOGFILE, "ab")
        proc = _subprocess.Popen(
            [indi_bin, "-v", "indi_celestron_gps"],
            stdin=_subprocess.DEVNULL,
            stdout=log_fh,
            stderr=log_fh,
            start_new_session=True,
        )
        INDISERVER_PIDFILE.write_text(str(proc.pid))
        result["indiserver_started"] = True
        result["indiserver_pid"] = proc.pid
        for _ in range(80):
            _time.sleep(0.1)
            if _indiserver_listening(c.config.mount.indi_host, c.config.mount.indi_port):
                break
        else:
            result["message"] = "indiserver started but never listened on 7624"
            _speak(c, "Started indiserver but it never listened. Check the log.")
            return result
        result["message"] = f"indiserver started, pid {proc.pid}"

    try:
        c.connect_mount(timeout=10.0)
        ra, dec = c.mount.get_position(timeout=3.0)
        result["mount_connected"] = True
        result["ra_deg"] = ra
        result["dec_deg"] = dec
        result["message"] = f"mount connected at RA={ra:.4f}, Dec={dec:.4f}"
        _speak(c, "[excited] Despierta! Mira is on. The mount is connected. The sky is yours.")
    except MountError as e:
        result["message"] = (
            f"indiserver up, mount NOT connected: {e}. "
            "Did you finish the fake alignment on the hand controller?"
        )
        _speak(c, "Mira is partly up. Mount did not respond. Finish the alignment on the hand controller, then try again.")
    return result


def shut_down(*, ctx: ToolContext | None = None) -> dict:
    """Gracefully end the Mira session: disconnect the mount and stop the
    indiserver process Mira started with `wake_up`.

    Use this when the user says "shut down Mira", "good night", "we're
    done", or similar. Idempotent.

    Returns:
      indiserver_killed: bool (True if we sent SIGTERM)
      message: short human-readable status
    """
    import os as _os
    import signal as _signal
    import subprocess as _subprocess

    c = _ctx(ctx)
    result = {"indiserver_killed": False, "message": ""}

    try:
        c.disconnect_mount()
    except MountError:
        pass

    if INDISERVER_PIDFILE.exists():
        try:
            pid = int(INDISERVER_PIDFILE.read_text().strip())
            try:
                _os.kill(pid, _signal.SIGTERM)
                result["indiserver_killed"] = True
                result["message"] = f"sent SIGTERM to indiserver (pid {pid})"
            except ProcessLookupError:
                result["message"] = f"indiserver pid {pid} already gone"
        except (ValueError, OSError):
            pass
        INDISERVER_PIDFILE.unlink(missing_ok=True)
    else:
        proc = _subprocess.run(
            ["pkill", "-f", "indiserver.*indi_celestron_gps"],
            capture_output=True,
            text=True,
        )
        if proc.returncode == 0:
            result["indiserver_killed"] = True
            result["message"] = "killed stray indiserver"
        elif _indiserver_listening():
            result["message"] = "indiserver still listening (Mira did not start it; leaving alone)"
        else:
            result["message"] = "indiserver already down"

    _speak(c, "[warmly] Buenas noches. Mira is down. Power off the mount when you are ready.")
    return result


def orient(*, ctx: ToolContext | None = None, drive_seconds: float = 12.0) -> bool:
    """Coarse mount orientation: drive the scope upward and northward until
    it is pointing roughly at Polaris.

    For users in the Northern Hemisphere, Polaris sits at altitude equal
    to your latitude (38 degrees from Louisville, KY) and stays fixed.
    Driving the mount toward it gives a known reference point even when
    the alignment is fake and coordinate-based slews keep getting
    refused by the firmware horizon guard.

    Mechanism: this fires the TELESCOPE_MOTION_NS=NORTH switch for
    `drive_seconds`, then stops. The motion switches drive the motors
    directly and bypass the coordinate-based goto, so the firmware lock
    that blocks `slew_to` calls does not apply.

    After the drive, the user typically uses `mira jog` to fine-center
    Polaris in the eyepiece, then `mira sync` to lock in a real
    coordinate frame.

    Args:
        drive_seconds: how long to drive north. Default 12s, which moves
            the scope through roughly half its travel at slew rate 5.

    Returns:
        True if the motion switch was successfully sent.
    """
    import time as _time

    c = _ctx(ctx)
    c.connect_mount()
    _speak(c, "[excited] Orienting north toward Polaris. Drive incoming.")
    try:
        c.mount.client.set_switch(
            "TELESCOPE_MOTION_NS",
            {"MOTION_NORTH": True, "MOTION_SOUTH": False},
        )
        _time.sleep(drive_seconds)
        c.mount.client.set_switch(
            "TELESCOPE_MOTION_NS",
            {"MOTION_NORTH": False, "MOTION_SOUTH": False},
        )
    except MountError as e:
        logger.error("orient: motion switch failed: %s", e)
        _speak(c, "Orient failed. Mount did not accept the motion switch.")
        return False
    _time.sleep(0.5)
    try:
        ra, dec = c.mount.get_position(timeout=3.0)
        logger.info("orient: drove %ss north; now at RA=%.4f Dec=%.4f", drive_seconds, ra, dec)
    except MountError:
        pass
    _speak(c, "[warmly] Pointing roughly north. Center Polaris with jog, then sync.")
    return True


def goto(target_name: str, *, ctx: ToolContext | None = None) -> bool:
    """Plate-solve current pointing, sync the mount, and slew to a named target.

    This is the primary headline operation. The flow is:
      1. Resolve target name to apparent RA/Dec.
      2. Capture a frame of the current sky.
      3. Plate-solve to learn true current pointing.
      4. Sync the mount to that solved position.
      5. Slew to the target.

    No traditional star alignment is required. The user does a deliberately
    bad fake alignment via the hand controller; this routine overwrites it.

    Args:
        target_name: anything `get_target_coordinates` accepts, e.g.
            "Jupiter", "M31", "Vega".

    Returns:
        True if the mount reached the target. False if any step failed.
    """
    c = _ctx(ctx)
    c.connect_mount()

    # 1. Target.
    try:
        target_ra, target_dec = get_target_coordinates(target_name, ctx=c)
    except NameNotFoundError as e:
        logger.error("goto: %s", e)
        _speak(c, f"I do not know {target_name}.")
        return False
    logger.info("goto: target %s at RA=%.4f Dec=%.4f", target_name, target_ra, target_dec)
    _speak(c, f"Slewing to {target_name}.")

    # 2. Capture for solving the current pointing.
    try:
        cur_ra_hint, cur_dec_hint = c.mount.get_position()
    except MountError:
        cur_ra_hint, cur_dec_hint = None, None
    try:
        image = capture_frame(ctx=c)
    except CameraError as e:
        logger.error("goto: capture failed: %s", e)
        return False

    # 3. Solve.
    solved = plate_solve(
        image,
        ra_hint_deg=cur_ra_hint,
        dec_hint_deg=cur_dec_hint,
        ctx=c,
    )
    if solved is None:
        logger.error("goto: plate solve failed for %s", image)
        return False
    solved_ra, solved_dec = solved
    c.state.record_sync(
        ra_deg=solved_ra,
        dec_deg=solved_dec,
        image_path=str(image),
        session_id=c.session_id,
    )

    # 4. Sync mount.
    sync_ok = c.mount.sync(ra_deg=solved_ra, dec_deg=solved_dec)
    if not sync_ok:
        logger.error("goto: mount did not accept sync")
        return False
    # Give the mount a beat to commit the sync before issuing a slew.
    time.sleep(0.5)

    # 5. Slew to target.
    slew_id = c.state.record_slew(
        target_name=target_name,
        target_ra_deg=target_ra,
        target_dec_deg=target_dec,
        session_id=c.session_id,
    )
    success = c.mount.slew_to(ra_deg=target_ra, dec_deg=target_dec)
    achieved_ra, achieved_dec = c.mount.get_position()
    c.state.update_slew_result(
        slew_id=slew_id,
        achieved_ra_deg=achieved_ra,
        achieved_dec_deg=achieved_dec,
        success=success,
    )
    if success:
        logger.info(
            "goto %s: arrived at RA=%.4f Dec=%.4f", target_name, achieved_ra, achieved_dec
        )
        _speak(c, f"{target_name} acquired.")
    else:
        logger.warning("goto %s: slew did not finish in time", target_name)
        _speak(c, f"{target_name} slew did not complete.")
    return success


# Public list of tool functions. Used by the MCP server to enumerate.
TOOLS = (
    get_target_coordinates,
    capture_frame,
    plate_solve,
    sync_mount,
    slew_to,
    get_mount_position,
    wait_for_slew_complete,
    get_observer_location,
    goto,
    orient,
    say,
    compose_narration,
    generate_sfx,
    wake_up,
    shut_down,
)
