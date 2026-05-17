"""Natural language interface to Mira's tools via the local `claude` CLI.

The UI's terminal chat mode talks to this. We shell out to Claude Code's
non-interactive mode (`claude --print --output-format json`) so the
conversation runs against Andrew's Claude Max subscription instead of
the metered Anthropic API. Mira's MCP server (registered as `mira` in
Claude settings) gives claude direct access to all 17 tools, so it can
goto / classify_target / smart_capture / etc. without us re-wrapping
each tool definition by hand.

Conversation continuity: claude returns a session_id; we pass it back
to the UI which sends it on the next turn. We then invoke claude with
`--resume <session_id>` to keep the conversation context.

Without the claude CLI installed (it ships with Claude Code), the chat
endpoint returns a clear error directing the user to install it.
"""
from __future__ import annotations

import json
import logging
import shutil
import subprocess
from typing import Optional

logger = logging.getLogger(__name__)


SYSTEM_PROMPT = """\
You are Mira, the voice of the telescope control system the user is talking
to through a web UI. The Mira MCP server gives you direct access to the
telescope tools: goto / smart_capture / classify_target / capture_frame /
plate_solve / slew_to / get_mount_position / orient / etc.

When the user says "show me Saturn", call goto. When they ask "what kind
of object is M51?", call classify_target. When they want to capture
without slewing, call smart_capture. Pick the right tool, call it,
summarize the result in one or two sentences.

Voice rules:
  - One or two sentences per reply. The UI shows tool results directly,
    so don't repeat them verbatim.
  - Spanish-accented English (Spanglish) welcome on conversational
    interjections (bueno, mira, listo, ahi, despacito); never on
    technical nouns (RA, Dec, ISO, shutter, plate solve, stack).
  - Don't narrate every step. Pick the tool, call it, brief summary.
  - On tool failure, surface the error briefly and suggest one fix.
"""


def have_claude_cli() -> bool:
    return shutil.which("claude") is not None


def run_chat(
    message: str,
    session_id: Optional[str] = None,
    *,
    timeout_s: float = 180.0,
) -> dict:
    """Single chat turn via the local claude CLI.

    message:    user's latest message
    session_id: returned from the previous turn; pass back to continue the
                same conversation. None starts a fresh session.

    Returns: {response, session_id, tool_calls?, error?}
    """
    if not have_claude_cli():
        return {
            "error": (
                "claude CLI not found on PATH. Install Claude Code to use chat mode "
                "(https://claude.ai/code). Tool mode (direct tool calls) still works."
            ),
        }

    cmd = [
        "claude",
        "--print",
        "--output-format", "json",
        "--append-system-prompt", SYSTEM_PROMPT,
    ]
    if session_id:
        cmd += ["--resume", session_id]
    cmd.append(message)

    logger.info("chat: invoking claude CLI (resume=%s, msg=%d chars)",
                bool(session_id), len(message))

    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout_s,
        )
    except subprocess.TimeoutExpired:
        return {"error": f"claude CLI timed out after {timeout_s}s"}
    except Exception as e:
        return {"error": f"claude CLI failed to launch: {e}"}

    if proc.returncode != 0:
        return {
            "error": f"claude CLI exited {proc.returncode}",
            "stderr": proc.stderr.strip()[:500],
        }

    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {
            "error": "claude CLI returned non-JSON output",
            "raw": proc.stdout[:500],
        }

    if data.get("is_error"):
        return {
            "error": data.get("result") or "claude reported an error",
            "subtype": data.get("subtype"),
        }

    return {
        "response": data.get("result", ""),
        "session_id": data.get("session_id"),
        "duration_ms": data.get("duration_ms"),
        "num_turns": data.get("num_turns"),
    }
