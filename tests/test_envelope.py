"""Parser tests for the JSON-envelope waterfall.

Run with: ./venv/bin/python -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import unittest

from core.envelope import parse_tool_calls
from core.openai_adapter import format_messages_to_prompt

BASH_TOOL = {
    "type": "function",
    "function": {
        "name": "bash",
        "description": "run shell",
        "parameters": {
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
        },
    },
}

ECHO_TOOL = {
    "type": "function",
    "function": {
        "name": "echo",
        "description": "repeat text",
        "parameters": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    },
}

TOOLS = [BASH_TOOL, ECHO_TOOL]


def args_of(call):
    return json.loads(call["function"]["arguments"])


class CleanEnvelopeTests(unittest.TestCase):
    def test_tool_call(self):
        text = (
            '{"content": null, "tool_calls": '
            '[{"name": "bash", "arguments": {"command": "ls -la"}}]}'
        )
        content, calls, attempted = parse_tool_calls(text, TOOLS)
        self.assertEqual(content, "")
        self.assertFalse(attempted)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["function"]["name"], "bash")
        self.assertIsInstance(calls[0]["function"]["arguments"], str)
        self.assertEqual(args_of(calls[0]), {"command": "ls -la"})
        self.assertTrue(calls[0]["id"].startswith("call_"))
        self.assertEqual(calls[0]["type"], "function")

    def test_talk_only_is_not_an_attempt(self):
        content, calls, attempted = parse_tool_calls(
            '{"content": "4", "tool_calls": []}', TOOLS
        )
        self.assertEqual(content, "4")
        self.assertEqual(calls, [])
        self.assertFalse(attempted, "valid talk-only envelope must not trigger a retry")

    def test_talk_and_call(self):
        text = (
            '{"content": "listing now", "tool_calls": '
            '[{"name": "bash", "arguments": {"command": "ls"}}]}'
        )
        content, calls, _ = parse_tool_calls(text, TOOLS)
        self.assertEqual(content, "listing now")
        self.assertEqual(len(calls), 1)

    def test_multiple_calls(self):
        text = (
            '{"content": null, "tool_calls": ['
            '{"name": "bash", "arguments": {"command": "ls"}},'
            '{"name": "echo", "arguments": {"text": "hi"}}]}'
        )
        _, calls, _ = parse_tool_calls(text, TOOLS)
        self.assertEqual([c["function"]["name"] for c in calls], ["bash", "echo"])
        self.assertNotEqual(calls[0]["id"], calls[1]["id"])


class WeirdContentTests(unittest.TestCase):
    def test_braces_quotes_newlines_unicode_and_tag_lookalikes(self):
        command = (
            "echo '}'; echo \"</tool_call>\"; printf 'π→\\n'; "
            "echo \"a{b}\"; printf '%s' '␣'"
        )
        envelope = {
            "content": None,
            "tool_calls": [{"name": "bash", "arguments": {"command": command}}],
        }
        content, calls, attempted = parse_tool_calls(
            json.dumps(envelope, ensure_ascii=False), TOOLS
        )
        self.assertFalse(attempted)
        self.assertEqual(len(calls), 1)
        self.assertEqual(args_of(calls[0])["command"], command)

    def test_trailing_commas(self):
        text = (
            '{"content": null, "tool_calls": '
            '[{"name": "bash", "arguments": {"command": "ls",},},],}'
        )
        _, calls, _ = parse_tool_calls(text, TOOLS)
        self.assertEqual(args_of(calls[0]), {"command": "ls"})

    def test_single_quotes_and_unquoted_keys(self):
        text = "{'content': None, tool_calls: [{name: 'bash', arguments: {command: 'ls'}}]}"
        _, calls, _ = parse_tool_calls(text, TOOLS)
        self.assertEqual(args_of(calls[0]), {"command": "ls"})

    def test_truncated_json(self):
        text = '{"content": null, "tool_calls": [{"name": "bash", "arguments": {"command": "ls -la"'
        _, calls, _ = parse_tool_calls(text, TOOLS)
        self.assertEqual(len(calls), 1)
        self.assertEqual(args_of(calls[0]), {"command": "ls -la"})

    def test_arguments_encoded_as_json_string(self):
        text = (
            '{"content": null, "tool_calls": '
            '[{"name": "bash", "arguments": "{\\"command\\": \\"ls\\"}"}]}'
        )
        _, calls, _ = parse_tool_calls(text, TOOLS)
        self.assertEqual(args_of(calls[0]), {"command": "ls"})

    def test_fenced_with_surrounding_prose(self):
        text = (
            "Sure, running that now.\n\n```json\n"
            '{"content": null, "tool_calls": [{"name": "bash", "arguments": {"command": "ls"}}]}\n'
            "```\n\nDone."
        )
        _, calls, _ = parse_tool_calls(text, TOOLS)
        self.assertEqual(args_of(calls[0]), {"command": "ls"})

    def test_fenced_broken_json(self):
        text = (
            "```json\n{'content': None, 'tool_calls': [{'name': 'bash', "
            "'arguments': {'command': 'ls'}}]}\n```"
        )
        _, calls, _ = parse_tool_calls(text, TOOLS)
        self.assertEqual(args_of(calls[0]), {"command": "ls"})


class RejectionTests(unittest.TestCase):
    def test_unknown_tool_is_an_attempt(self):
        content, calls, attempted = parse_tool_calls(
            '{"content": null, "tool_calls": '
            '[{"name": "rm_rf", "arguments": {"path": "/"}}]}',
            TOOLS,
        )
        self.assertEqual(calls, [])
        self.assertTrue(attempted)

    def test_missing_required_key_is_an_attempt(self):
        _, calls, attempted = parse_tool_calls(
            '{"content": null, "tool_calls": [{"name": "bash", "arguments": {}}]}',
            TOOLS,
        )
        self.assertEqual(calls, [])
        self.assertTrue(attempted)

    def test_non_object_arguments_is_an_attempt(self):
        _, calls, attempted = parse_tool_calls(
            '{"content": null, "tool_calls": '
            '[{"name": "bash", "arguments": ["ls"]}]}',
            TOOLS,
        )
        self.assertEqual(calls, [])
        self.assertTrue(attempted)

    def test_bare_call_object_is_accepted(self):
        # The model dropped the wrapper but kept a valid call shape.
        _, calls, attempted = parse_tool_calls(
            '{"name": "bash", "arguments": {"command": "ls"}}', TOOLS
        )
        self.assertFalse(attempted)
        self.assertEqual(args_of(calls[0]), {"command": "ls"})

    def test_dsml_garbage_is_an_attempt(self):
        text = (
            "<\uff5c\uff5cDSML\uff5c\uff5c calls>\n"
            "<\uff5c\uff5cDSML\uff5c\uff5c invoke name=\"bash\" string=\"true\">\n"
            "<\uff5c\uff5cDSML\uff5c\uff5c parameter name=\"command\" string=\"true\">ls\n"
            "<\uff5c\uff5cDSML\uff5c\uff5c invoke>\n"
            "<\uff5c\uff5cDSML\uff5c\uff5c calls>"
        )
        _, calls, attempted = parse_tool_calls(text, TOOLS)
        self.assertEqual(calls, [])
        self.assertTrue(attempted)

    def test_plain_prose_is_not_an_attempt(self):
        content, calls, attempted = parse_tool_calls("2 + 2 = 4.", TOOLS)
        self.assertEqual(content, "2 + 2 = 4.")
        self.assertEqual(calls, [])
        self.assertFalse(attempted)

    def test_prose_that_mentions_tool_calls_is_not_an_attempt(self):
        content, calls, attempted = parse_tool_calls(
            "I want to run bash but tool_calls are unavailable.", TOOLS
        )
        self.assertIn("tool_calls", content)
        self.assertEqual(calls, [])
        self.assertFalse(attempted)

    def test_unquoted_tool_call_syntax_is_an_attempt(self):
        _, calls, attempted = parse_tool_calls(
            "tool_calls: [{name: bash, arguments: {command: ls}}]", TOOLS
        )
        # json5 can still recover this one; it must not be treated as prose.
        self.assertTrue(calls or attempted)
        if calls:
            self.assertEqual(args_of(calls[0]), {"command": "ls"})

    def test_no_tools_offered_drops_unreachable_call(self):
        content, calls, attempted = parse_tool_calls(
            '{"content": "no tools here", "tool_calls": [{"name": "bash", "arguments": {"command": "ls"}}]}',
            None,
        )
        self.assertEqual(content, "no tools here")
        self.assertEqual(calls, [])
        self.assertFalse(attempted)

    def test_none_and_empty_input(self):
        for value in (None, ""):
            content, calls, attempted = parse_tool_calls(value, TOOLS)
            self.assertEqual(content, "")
            self.assertEqual(calls, [])
            self.assertFalse(attempted)


class ContentOnlyEnvelopeTests(unittest.TestCase):
    """A talk-only envelope may omit the empty "tool_calls" array."""

    def test_content_only(self):
        content, calls, attempted = parse_tool_calls(
            '{"content": "Yes, the function allocates for you."}', TOOLS
        )
        self.assertEqual(content, "Yes, the function allocates for you.")
        self.assertEqual(calls, [])
        self.assertFalse(attempted)

    def test_content_only_single_quotes(self):
        content, _, attempted = parse_tool_calls(
            "{'content': 'malloc returns the buffer to the caller.'}", TOOLS
        )
        self.assertEqual(content, "malloc returns the buffer to the caller.")
        self.assertFalse(attempted)

    def test_content_only_with_trailing_echo(self):
        content, _, attempted = parse_tool_calls(
            '{"content": "The +1 is for the terminator."}\n\n'
            "Reminder: answer with exactly one JSON envelope.",
            TOOLS,
        )
        self.assertEqual(content, "The +1 is for the terminator.")
        self.assertFalse(attempted)

    def test_prose_containing_json_example_is_not_unwrapped(self):
        text = 'The envelope looks like {"content": "hi", "tool_calls": []} in the prompt.'
        content, calls, attempted = parse_tool_calls(text, TOOLS)
        self.assertEqual(content, text)
        self.assertEqual(calls, [])
        self.assertFalse(attempted)


class SalvageTests(unittest.TestCase):
    """Broken envelopes must never be shown to the client as raw JSON."""

    CODE = (
        "```c\n"
        "char *out = malloc((size_t)bestLen + 1);\n"
        "if (!out) { return NULL; }\n"
        'printf("%s\\n", out);\n'
        "free(out);\n"
        "```"
    )

    def test_truncated_mid_string(self):
        text = '{"content": "Yes — the Malloc variant does the allocating.\n\n' + self.CODE
        content, calls, attempted = parse_tool_calls(text, TOOLS)
        self.assertEqual(calls, [])
        self.assertNotIn('{"content"', content)
        self.assertIn("Malloc variant", content)
        self.assertIn('printf("%s', content)
        self.assertIn(", out);", content)
        self.assertIn("free(out);", content)
        self.assertFalse(attempted)

    def test_unescaped_quotes_inside_code(self):
        text = (
            '{"content": "Use printf("hi") then free(out), it is fine."}'
        )
        content, calls, attempted = parse_tool_calls(text, TOOLS)
        self.assertEqual(calls, [])
        self.assertNotIn('{"content"', content)
        self.assertIn('printf("hi")', content)
        self.assertIn("it is fine.", content)
        self.assertFalse(attempted)

    def test_truncated_after_dangling_tool_calls(self):
        text = (
            '{"content": "Calling bash now.", "tool_calls": [{"name": "bash", '
            '"arguments": {"command": "ls"'
        )
        content, calls, attempted = parse_tool_calls(text, TOOLS)
        # The call may be unrecoverable, but the text is readable.
        self.assertIn("Calling bash now.", content)
        self.assertNotIn('{"content"', content)

    def test_content_only_missing_closing_quote(self):
        content, calls, attempted = parse_tool_calls(
            '{"content": "yes, that is the contract', TOOLS
        )
        self.assertEqual(content, "yes, that is the contract")
        self.assertEqual(calls, [])
        self.assertFalse(attempted)

    def test_fenced_envelope_with_inner_code_fence(self):
        inner = "\n\n```c\nout[bestLen] = '\\0';\nreturn out;\n```\n\nDone."
        text = (
            '```json\n{"content": "Yes — the Malloc variant does the allocating.'
            + inner
            + '", "tool_calls": []}\n```'
        )
        content, calls, attempted = parse_tool_calls(text, TOOLS)
        self.assertEqual(calls, [])
        self.assertFalse(attempted)
        self.assertIn("out[bestLen] = '\\0';", content)
        self.assertIn("Done.", content)

    def test_fenced_example_inside_prose_is_not_unwrapped(self):
        text = (
            "The format is:\n\n```json\n"
            '{"content": "hi", "tool_calls": []}\n```\n\nUse it.'
        )
        content, calls, _ = parse_tool_calls(text, TOOLS)
        self.assertIn("The format is:", content)
        self.assertIn("Use it.", content)
        self.assertEqual(calls, [])

    def test_unreadable_envelope_fails_loudly(self):
        content, calls, attempted = parse_tool_calls(
            '{"content": null, "tool_calls": [', TOOLS
        )
        self.assertEqual(content, "")
        self.assertEqual(calls, [])
        self.assertTrue(attempted, "must trigger retry/502, not show JSON")

    def test_escaped_backslash_zero_survives(self):
        content, _, _ = parse_tool_calls(
            '{"content": "out[len] = \'\\\\0\';"}', TOOLS
        )
        self.assertIn("'\\0'", content)

    def test_invalid_c_escape_does_not_truncate(self):
        # '\0' is not a legal JSON escape -- repair passes used to cut the
        # answer there, losing everything after the memcpy line.
        text = (
            '{"content": "char *out = malloc((size_t)bestLen + 1);\n'
            "memcpy(out, tab + bestStart, bestLen);\n"
            "out[bestLen] = '\\0';\n"
            "return out;\n"
            '```", "tool_calls": []}'
        )
        content, calls, attempted = parse_tool_calls(text, TOOLS)
        self.assertEqual(calls, [])
        self.assertFalse(attempted)
        self.assertIn("out[bestLen] = '\\0';", content)
        self.assertIn("return out;", content)

    def test_envelope_instructions_are_prepended(self):
        prompt = format_messages_to_prompt(
            [{"role": "user", "content": "list files"}], tools=TOOLS
        )
        self.assertIn("Output EXACTLY ONE JSON object", prompt)
        self.assertIn('"tool_calls"', prompt)
        self.assertIn("required", prompt.lower())
        self.assertIn("Tool: bash", prompt)
        # Rules live in the tool block, never after the last user turn -- a
        # trailing instruction reads as dialogue and gets echoed back.
        self.assertLess(
            prompt.index("Reminder: answer with exactly one JSON envelope"),
            prompt.index("[User]"),
        )
        self.assertTrue(prompt.rstrip().endswith("[User]\nlist files"))

    def test_no_tools_means_no_envelope_instructions(self):
        prompt = format_messages_to_prompt([{"role": "user", "content": "hi"}])
        self.assertNotIn("Output EXACTLY ONE JSON object", prompt)
        self.assertEqual(prompt, "[User]\nhi")

    def test_assistant_tool_calls_render_as_json_envelope(self):
        prompt = format_messages_to_prompt(
            [
                {"role": "user", "content": "list files"},
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {
                                "name": "bash",
                                "arguments": '{"command": "ls -la"}',
                            },
                        }
                    ],
                },
                {"role": "tool", "name": "bash", "content": "/tmp/x"},
            ],
            tools=TOOLS,
        )
        self.assertNotIn("DSML", prompt)
        self.assertNotIn("<|", prompt)
        self.assertIn('"tool_calls": [', prompt)
        self.assertIn('"arguments": {', prompt)
        self.assertIn("[Tool Result (bash)]\n/tmp/x", prompt)


if __name__ == "__main__":
    unittest.main()
