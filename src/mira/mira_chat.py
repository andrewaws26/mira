"""LLM-backed natural language interface to Mira's tools.

Lets the user type 'show me Saturn' instead of 'goto target_name=Saturn'.
Routes through the Anthropic Messages API with Mira's TOOLS exposed as
tool definitions. Claude picks the right tools, the server executes them
locally with the live ToolContext, results loop back into the conversation,
final text is returned to the UI.

Requires ANTHROPIC_API_KEY in env or ~/mira/.env. Without it, the chat
endpoint returns a clear error directing the user to set the key.

No SDK dep -- raw urllib to the messages endpoint. Conversation state
lives in the browser (the UI sends prior turns back with each new
message) so server has no per-session memory.
"""
from __future__ import annotations

import inspect
import json
import logging
import os
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
MODEL = "claude-sonnet-4-6"
MAX_TOKENS = 4096
MAX_TOOL_LOOPS = 8

SYSTEM_PROMPT = """\
You are Mira, a quiet observing companion for a stargazer at a Celestron NexStar 130SLT.

The user is interacting with you through Mira's web UI. They will say
things like "show me Saturn" or "what kind of object is M51?" -- pick the
right tool from the catalog and call it. Don't over-explain; the user can
see the live preview themselves.

Voice and behavior rules:
  - One or two sentences per reply. The UI shows tool results directly,
    so don't repeat them verbatim.
  - Spanish-accented English (Spanglish) is welcome on conversational
    interjections (bueno, mira, listo, ahi, despacito) but never on
    technical nouns (RA, Dec, ISO, shutter, plate solve, stack).
  - Never narrate every step. Pick the tool, call it, summarize result.
  - If a tool fails, surface the actual error briefly; suggest one fix.

Available tools are passed in the tool definitions. When the user asks
to point at a target, use `goto`. When they ask what kind of object
something is, use `classify_target`. When they ask to capture without
slewing, use `smart_capture`. When they ask status, use `get_mount_position`
or `get_observer_location`.
"""


def get_api_key() -> Optional[str]:
    """Look for ANTHROPIC_API_KEY in env first, then ~/mira/.env."""
    if key := os.environ.get("ANTHROPIC_API_KEY"):
        return key
    env_file = Path("~/mira/.env").expanduser()
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line.startswith("ANTHROPIC_API_KEY="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return None


def build_tool_definitions(tool_layer) -> list[dict]:
    """Inspect tools.TOOLS and produce Anthropic-format tool definitions."""
    defs = []
    for fn in tool_layer.TOOLS:
        sig = inspect.signature(fn)
        params = [p for p in sig.parameters.values() if p.name != "ctx"]

        # Build a JSON schema. We don't have full type info for nested
        # params; treat ints/floats/bools/strs natively, fall back to string.
        properties: dict[str, dict] = {}
        required: list[str] = []
        for p in params:
            ptype = _python_type_to_json(p.annotation)
            prop: dict[str, Any] = {"type": ptype}
            # Anthropic likes description on each param; pull from docstring if possible
            properties[p.name] = prop
            if p.default is inspect.Parameter.empty:
                required.append(p.name)

        desc = (fn.__doc__ or "").strip().split("\n", 1)[0]
        defs.append({
            "name": fn.__name__,
            "description": desc,
            "input_schema": {
                "type": "object",
                "properties": properties,
                "required": required,
            },
        })
    return defs


def _python_type_to_json(ann) -> str:
    """Crude annotation -> JSON Schema type."""
    if ann in (int,): return "integer"
    if ann in (float,): return "number"
    if ann in (bool,): return "boolean"
    if ann in (str,): return "string"
    # Optional[T], Union, complex types -> string by default
    return "string"


def run_chat(
    message: str,
    history: list[dict],
    tool_layer,
    ctx,
) -> dict:
    """Single chat turn. Returns {response, tool_calls, error?}.

    history: prior messages as Anthropic-format dicts
             [{"role": "user", "content": [...]}, {"role":"assistant",...}]
    """
    api_key = get_api_key()
    if not api_key:
        return {
            "error": (
                "ANTHROPIC_API_KEY not set. Add it to ~/mira/.env or export it. "
                "The Tool mode (direct tool calls like 'classify_target Saturn') "
                "still works without an API key."
            ),
        }

    tools = build_tool_definitions(tool_layer)
    name_to_fn = {fn.__name__: fn for fn in tool_layer.TOOLS}

    messages = list(history) + [{"role": "user", "content": message}]
    trace: list[dict] = []

    for loop in range(MAX_TOOL_LOOPS):
        body = {
            "model": MODEL,
            "max_tokens": MAX_TOKENS,
            "system": SYSTEM_PROMPT,
            "tools": tools,
            "messages": messages,
        }
        try:
            resp = _post_anthropic(body, api_key)
        except urllib.error.HTTPError as e:
            return {"error": f"Anthropic API error: HTTP {e.code} {e.read().decode(errors='replace')[:200]}"}
        except (urllib.error.URLError, TimeoutError) as e:
            return {"error": f"Anthropic API unreachable: {e}"}

        stop_reason = resp.get("stop_reason")
        content = resp.get("content", [])

        # Did the model request tools?
        tool_uses = [c for c in content if c.get("type") == "tool_use"]
        text_blocks = [c for c in content if c.get("type") == "text"]

        if tool_uses:
            # Add assistant message (raw content blocks) to history
            messages.append({"role": "assistant", "content": content})
            # Execute each tool, build tool_result content
            tool_results = []
            for tu in tool_uses:
                tname = tu["name"]
                targs = tu.get("input", {}) or {}
                trace.append({"tool": tname, "args": targs})
                fn = name_to_fn.get(tname)
                if fn is None:
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": tu["id"],
                        "content": f"unknown tool: {tname}",
                        "is_error": True,
                    })
                    continue
                try:
                    out = fn(**targs, ctx=ctx)
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": tu["id"],
                        "content": json.dumps(_jsonable(out)),
                    })
                except Exception as e:
                    logger.exception("chat: tool %s failed", tname)
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": tu["id"],
                        "content": f"{type(e).__name__}: {e}",
                        "is_error": True,
                    })
            messages.append({"role": "user", "content": tool_results})
            continue

        # No tool calls: this is the final assistant response
        text = "".join(b.get("text", "") for b in text_blocks)
        # Also append the assistant turn to history for the client to send back
        messages.append({"role": "assistant", "content": content})
        return {
            "response": text,
            "tool_calls": trace,
            "history": messages,
            "stop_reason": stop_reason,
        }

    return {
        "error": f"chat exceeded {MAX_TOOL_LOOPS} tool-use loops without converging",
        "tool_calls": trace,
    }


def _post_anthropic(body: dict, api_key: str) -> dict:
    req = urllib.request.Request(
        ANTHROPIC_URL,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "x-api-key": api_key,
            "anthropic-version": ANTHROPIC_VERSION,
            "content-type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


def _jsonable(v: Any) -> Any:
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    if isinstance(v, dict):
        return {k: _jsonable(val) for k, val in v.items()}
    if isinstance(v, Path):
        return str(v)
    if isinstance(v, datetime):
        return v.isoformat()
    try:
        return str(v)
    except Exception:
        return repr(v)
