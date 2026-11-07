"""
Envelope tool-call parser.

The model is instructed to answer with exactly one JSON object::

    {"content": "<message or null>",
     "tool_calls": [{"name": "<tool>", "arguments": {...}}]}

Models drift from that contract. They fence the object, add prose around it,
emit trailing commas, single quotes, unquoted keys, truncated JSON, or encode
``arguments`` as a JSON *string*. This module recovers the envelope with a
waterfall:

    0. the leading object itself -- a talk-only envelope is still valid when
       the model omits the empty ``"tool_calls"`` array.
    1. ``json.JSONDecoder.raw_decode`` -- string-boundary safe, so braces,
       quotes, newlines, unicode and ``</tool_call>`` inside argument values
       are never mistaken for structure.
    2. ``json_repair`` -- trailing commas, missing quotes, truncation.
    3. ``json5`` -- single quotes, unquoted keys, comments.
    4. ```` ``` ```` fenced blocks.
    5. a tolerant ``content`` salvage for a broken/truncated envelope, so the
       raw JSON wrapper never reaches the client as if it were the answer.

Nothing is accepted until :func:`_validate` confirms the shape and the tool
name/required-argument contract of the request. The parser never lets a
partially recovered tool call leak into ``content``: callers get either
validated tool calls or ``attempted=True`` and can retry / fail loudly.
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Any, Dict, Iterator, List, Optional, Tuple

import json5
from json_repair import repair_json

_DEC = json.JSONDecoder()

# Fenced block. The body runs to the *end* of the reply rather than to the
# next ``` : answers about code contain their own ```c fences, and a
# non-greedy match would cut the envelope at the first one. The balanced
# scanner then decides where the real object ends.
_FENCE_RE = re.compile(r"```[ \t]*(?:json)?[ \t]*\r?\n?(.*)", re.DOTALL)

# Structural markers that say "the model was using a tool-call format". Quoted
# key names are deliberately absent: ordinary prose can quote them (asking
# "what does \"tool_calls\" mean?"), and that must not trigger a retry.
_ATTEMPT_MARKERS = (
    "```json",
    "dsml",
    "dmsl",
    "invoke",
    "<|",
)

# Unquoted keys, e.g. ``tool_calls: [...]`` -- only when used as a key, so
# ordinary prose ("tool_calls are unavailable") does not trigger a retry.
_UNQUOTED_ATTEMPT_RE = re.compile(r"\b(?:tool_calls|arguments)\s*[:=]")

# Bounded work when the fast path fails: only the first N opening braces and
# only blobs up to this size get the expensive repair passes.
_MAX_BLOBS = 24
_MAX_BLOB_CHARS = 200_000


# ----------------------------------------------------------------------
# Loaders
# ----------------------------------------------------------------------
def _loads_strict(blob: str) -> Any:
    try:
        return json.loads(blob)
    except Exception:
        return None


def _loads_repair(blob: str) -> Any:
    try:
        return json.loads(repair_json(blob))
    except Exception:
        return None


def _loads_json5(blob: str) -> Any:
    try:
        return json5.loads(blob)
    except Exception:
        return None


_LOADERS = (_loads_strict, _loads_repair, _loads_json5)


def _balanced_slice(text: str, start: int) -> Optional[str]:
    """Return ``text[start:end]`` for the object opening at ``start``.

    String literals are skipped so braces inside argument values do not end
    the slice early. Returns ``None`` when the object is unterminated.
    """
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return None


def _iter_blobs(text: str) -> Iterator[str]:
    """Yield candidate JSON objects found anywhere in ``text``.

    Each ``{`` produces the balanced object (when it terminates) and the
    remainder of the text (covers truncated output). Deduplicated so the
    repair passes never look at the same substring twice.
    """
    if not text:
        return
    text = _normalize_stray_escapes(text)
    seen: set[str] = set()
    starts = 0
    for index, char in enumerate(text):
        if char != "{":
            continue
        starts += 1
        if starts > _MAX_BLOBS:
            break
        for blob in (_balanced_slice(text, index), text[index:]):
            if not blob or len(blob) < 2 or len(blob) > _MAX_BLOB_CHARS:
                continue
            if blob in seen:
                continue
            seen.add(blob)
            yield blob


def _extract_envelopes(text: str) -> Iterator[Any]:
    """Boundary-safe scan for top-level JSON values, using raw_decode."""
    text = _normalize_stray_escapes(text or "")
    index = 0
    length = len(text)
    while index < length:
        index = text.find("{", index)
        if index == -1:
            return
        try:
            obj, end = _DEC.raw_decode(text, index)
        except json.JSONDecodeError:
            index += 1
            continue
        yield obj
        index = max(end, index + 1)


# ----------------------------------------------------------------------
# Validation
# ----------------------------------------------------------------------
def _coerce_arguments(value: Any) -> Optional[Dict[str, Any]]:
    """Accept a JSON object, or a JSON *string* that decodes to one.

    Models routinely copy OpenAI's own wire format and send
    ``"arguments": "{\\"command\\": \\"ls\\"}"``. That is recoverable, so do not
    spend the retry on it.
    """
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        for loader in _LOADERS:
            decoded = loader(value)
            if isinstance(decoded, dict):
                return decoded
    return None


def _validate(
    obj: Any, tools_by_name: Dict[str, Dict[str, Any]], bare_ok: bool = False
) -> Optional[Tuple[str, List[Dict[str, Any]]]]:
    if not isinstance(obj, dict):
        return None

    raw_calls = obj.get("tool_calls")
    if raw_calls is None and "name" in obj and "arguments" in obj:
        # Model dropped the wrapper and emitted a bare call object.
        raw_calls = [obj]
    if raw_calls is None:
        # A leading object shaped ``{"content": "..."}`` is a talk-only
        # envelope whose author omitted the empty ``tool_calls`` array. Only
        # trusted for the leading object of the reply, never for something
        # buried in prose.
        if bare_ok:
            content_value = obj.get("content")
            if isinstance(content_value, str):
                return (content_value.strip(), [])
        return None
    if isinstance(raw_calls, dict):
        raw_calls = [raw_calls]
    if not isinstance(raw_calls, list):
        return None
    if not raw_calls:
        # ``[]`` is a legitimate "talk only" envelope.
        content = obj.get("content")
        return ((content if isinstance(content, str) else "").strip(), [])

    content_value = obj.get("content")
    content = content_value if isinstance(content_value, str) else None

    calls: List[Dict[str, Any]] = []
    for call in raw_calls:
        if not isinstance(call, dict):
            return None
        name = call.get("name")
        if not isinstance(name, str) or not name:
            return None
        spec = tools_by_name.get(name)
        if spec is None:
            if not tools_by_name:
                # No tools were declared, so there is nothing to call: keep the
                # text instead of failing the whole reply.
                continue
            return None
        args = _coerce_arguments(call.get("arguments"))
        if args is None:
            return None
        required = spec.get("required") or []
        if any(key not in args for key in required):
            return None
        calls.append(
            {
                "id": f"call_{uuid.uuid4().hex[:16]}",
                "type": "function",
                "function": {
                    "name": name,
                    "arguments": json.dumps(args, ensure_ascii=False),
                },
            }
        )

    return (content or "").strip(), calls


def _looks_like_attempt(text: str) -> bool:
    lowered = (text or "").lower()
    if any(marker in lowered for marker in _ATTEMPT_MARKERS):
        return True
    return bool(_UNQUOTED_ATTEMPT_RE.search(text or ""))


# ----------------------------------------------------------------------
# Lenient envelope recovery
# ----------------------------------------------------------------------
_ENVELOPE_START_RE = re.compile(r"""^\s*\{\s*["']?content["']?\s*:""", re.IGNORECASE)
_CONTENT_KEY_RE = re.compile(r"""^\s*\{\s*["']?content["']?\s*:\s*""", re.IGNORECASE)
# ``, "tool_calls": ...`` left dangling at the end of a truncated reply.
_TOOL_CALLS_TAIL_RE = re.compile(r"""\s*,\s*["']?tool_calls["']?\s*:.*\Z""", re.DOTALL)
# A non-empty call array means a tool call was probably lost in the breakage.
_LOST_CALL_RE = re.compile(r"""["']?tool_calls["']?\s*:\s*\[\s*\{""")
_STRUCT_TAIL_RE = re.compile(r"""^\s*,\s*["']?(?:tool_calls|content)["']?\s*:""")
_HEX = set("0123456789abcdefABCDEF")
_JSON_ESCAPE_CHARS = set('"\\/bfnrt')


def _normalize_stray_escapes(text: str) -> str:
    """Escape backslashes that are not valid JSON escapes.

    C code is the usual source: ``'\\0'``, ``'\\d'`` or a Windows path are
    invalid inside a JSON string, and repair passes react by truncating the
    value there. Turning them into ``\\\\0`` / ``\\\\d`` preserves the author's
    intent (a literal backslash) instead of losing the rest of the answer.
    """
    if "\\" not in text:
        return text
    out: List[str] = []
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if char != "\\" or index + 1 >= length:
            out.append(char)
            index += 1
            continue
        nxt = text[index + 1]
        if nxt == "u" and index + 5 < length and all(
            digit in _HEX for digit in text[index + 2 : index + 6]
        ):
            out.append(text[index : index + 6])
            index += 6
            continue
        if nxt in _JSON_ESCAPE_CHARS:
            out.append(text[index : index + 2])
            index += 2
            continue
        out.append("\\\\")
        index += 1
    return "".join(out)

_ESCAPES = {
    "n": "\n",
    "t": "\t",
    "r": "\r",
    "b": "\b",
    "f": "\f",
    '"': '"',
    "'": "'",
    "\\": "\\",
    "/": "/",
}


def _unescape_json_string(raw: str) -> str:
    """Decode JSON escapes without failing on stray backslashes or quotes."""
    out: List[str] = []
    index = 0
    length = len(raw)
    while index < length:
        char = raw[index]
        if char != "\\" or index + 1 >= length:
            out.append(char)
            index += 1
            continue
        nxt = raw[index + 1]
        if nxt in _ESCAPES:
            out.append(_ESCAPES[nxt])
            index += 2
            continue
        if nxt == "u" and index + 5 < length:
            digits = raw[index + 2 : index + 6]
            try:
                out.append(chr(int(digits, 16)))
                index += 6
                continue
            except ValueError:
                pass
        # Unknown escape (e.g. ``\0``): keep the backslash literally.
        out.append(char)
        index += 1
    return "".join(out)


def _parse_leading_envelope(
    text: str, tools_by_name: Dict[str, Dict[str, Any]]
) -> Optional[Tuple[str, List[Dict[str, Any]]]]:
    """Validate the object that opens the reply, tolerating a missing key.

    A talk-only envelope is legal JSON, but models often drop the empty
    ``"tool_calls"`` array. Restricting ``bare_ok`` to the *leading* object
    keeps prose that merely contains a JSON example from being unwrapped.
    """
    stripped = (text or "").lstrip()
    stripped = _normalize_stray_escapes(stripped)
    if not stripped.startswith("{"):
        return None

    balanced = _balanced_slice(stripped, 0)
    candidates: List[str] = []
    if balanced:
        candidates.append(balanced)
    if stripped not in candidates:
        candidates.append(stripped)

    for blob in candidates:
        for loader in _LOADERS:
            obj = loader(blob)
            if obj is None:
                continue
            result = _validate(obj, tools_by_name, bare_ok=True)
            if result is None:
                continue
            if balanced is None and not result[0] and not result[1]:
                # Unterminated reply and the repair invented an empty object.
                continue
            return result
    return None


def _salvage_envelope(text: str) -> Optional[Tuple[str, bool]]:
    """Recover ``content`` from a broken/truncated envelope.

    Returns ``(content, lost_call)`` or ``None`` when the reply is not an
    envelope. Used only after the waterfall fails -- an unparseable envelope
    must never reach the client as raw JSON.
    """
    stripped = (text or "").lstrip()
    match = _CONTENT_KEY_RE.match(stripped)
    if not match:
        return None

    rest = stripped[match.end() :]
    if not rest:
        return None
    quote = rest[0]
    if quote not in "\"'":
        # content: null / 123 -- nothing human-readable to salvage.
        return None
    body = rest[1:]

    # Find the closing quote. A candidate only counts when what follows looks
    # structural (end of text, ``}``, or ``, "tool_calls":``), so unescaped
    # quotes inside code -- printf("%s\n", out) -- are not mistaken for the end.
    end: Optional[int] = None
    escaped = False
    for index, char in enumerate(body):
        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if char != quote:
            continue
        remainder = body[index + 1 :]
        if not remainder.strip() or remainder.lstrip().startswith("}"):
            end = index
        elif _STRUCT_TAIL_RE.match(remainder):
            end = index

    raw = body if end is None else body[:end]
    raw = _TOOL_CALLS_TAIL_RE.sub("", raw)
    content = _unescape_json_string(raw).strip()
    if not content:
        return None
    return content, bool(_LOST_CALL_RE.search(stripped))


# ----------------------------------------------------------------------
# Public API
# ----------------------------------------------------------------------
def parse_tool_calls(
    text: str, tools: Optional[List[Dict[str, Any]]] = None
) -> Tuple[str, List[Dict[str, Any]], bool]:
    """Parse an assistant reply into ``(content, tool_calls, attempted)``.

    ``content`` is the human-facing text (empty when the model only called a
    tool). ``attempted`` is True when the reply looked like a tool call but no
    envelope validated -- the caller should retry with self-correction and
    fail loudly if that also misses.
    """
    text = text or ""
    tools_by_name: Dict[str, Dict[str, Any]] = {}
    for tool in tools or []:
        function = (tool or {}).get("function") or {}
        name = function.get("name")
        if isinstance(name, str) and name:
            tools_by_name[name] = function.get("parameters") or {}

    # 0. Envelope-shaped but unterminated: read it as text instead of letting
    #    json_repair guess where the string stopped.
    stripped = text.strip()
    if _ENVELOPE_START_RE.match(stripped) and _balanced_slice(stripped, 0) is None:
        salvaged = _salvage_envelope(stripped)
        if salvaged is not None:
            content, lost_call = salvaged
            return content, [], bool(lost_call)

    # 1. Leading object -- the normal case, including a talk-only envelope
    #    that omitted the empty "tool_calls" array.
    leading = _parse_leading_envelope(text, tools_by_name)
    if leading is not None:
        return leading[0], leading[1], False

    # 2. raw_decode -- boundary safe, handles every character inside strings.
    for obj in _extract_envelopes(text):
        result = _validate(obj, tools_by_name)
        if result is not None and result[1]:
            return result[0], result[1], False

    # 3. json_repair, then 4. json5 -- over every candidate object. Only call
    #    envelopes qualify here: a talk-only object buried in prose is an
    #    example being discussed, not the answer.
    for blob in _iter_blobs(text):
        for loader in (_loads_repair, _loads_json5):
            obj = loader(blob)
            if obj is None:
                continue
            result = _validate(obj, tools_by_name)
            if result is not None and result[1]:
                return result[0], result[1], False

    # 5. fenced blocks -- run the full blob waterfall inside each fence.
    for match in _FENCE_RE.finditer(text):
        body = match.group(1)
        prefix_blank = not text[: match.start()].strip()
        fenced = _parse_leading_envelope(body, tools_by_name)
        if fenced is not None and (fenced[1] or prefix_blank):
            # Talk-only is trusted only when the fence *is* the whole reply;
            # otherwise the object is an example being quoted in prose.
            return fenced[0], fenced[1], False
        for obj in _extract_envelopes(body):
            result = _validate(obj, tools_by_name)
            if result is not None and result[1]:
                return result[0], result[1], False
        for blob in _iter_blobs(body):
            for loader in _LOADERS:
                obj = loader(blob)
                if obj is None:
                    continue
                result = _validate(obj, tools_by_name)
                if result is not None and result[1]:
                    return result[0], result[1], False

    # 6. Last resort: a broken or truncated envelope still carries readable
    #    text. Never hand the raw JSON wrapper to the client.
    salvaged = _salvage_envelope(stripped)
    if salvaged is not None:
        content, lost_call = salvaged
        return content, [], bool(lost_call)

    if _ENVELOPE_START_RE.match(stripped):
        # Envelope-shaped but unreadable: fail loudly (retry / 502) instead of
        # displaying JSON as the assistant's answer.
        return "", [], True

    return stripped, [], _looks_like_attempt(text)
