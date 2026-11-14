"""Manual tool-calling loop for the Gemini coding agent (no agent frameworks).

Per attempt:
  1. FIX phase    - the model reads code with tools and edits lines with replace_line.
                    It ends with a text reply starting `STATUS: FIXED` or `STATUS: CANNOT_FIX`.
  2. VERIFY phase - if FIXED, the model re-reads its own edits and answers `VERIFIED: YES|NO`.
An attempt fails if the model gives up, hits the tool-call cap, or does not verify its fix.
After a failed attempt the workspace is restored to the original snapshot.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field

from google import genai
from google.genai import errors, types

import tools

MAX_ATTEMPTS = 3
MAX_FIX_TOOL_CALLS = 15      # hard cap on tool calls in the fix phase of one attempt
MAX_VERIFY_TOOL_CALLS = 5    # hard cap on tool calls in the verify phase of one attempt

DEFAULT_MODEL = "gemini-3.5-flash-lite"
RETRYABLE_CODES = {429, 500, 503}
FALLBACK_CODES = {404, 429, 500, 503}
MAX_RETRIES = 5
MAX_WAIT_SECONDS = 90  # never sleep longer than this for one retry

SYSTEM_PROMPT = """You are a careful Python debugging agent working inside a small project.
The user describes a logical bug in plain English. Find the faulty line(s) and fix them.

Rules:
- You cannot run code. Work only by reading source with your tools and reasoning about it.
- Tools: search_files, search_in_file, read_file, replace_line. All line numbers are 1-based.
- First locate the relevant code: search_files with short, specific keywords (e.g. a function name),
  then search_in_file or read_file.
- Always see the exact current line number via read_file/search_in_file before calling replace_line.
  Never guess a line number.
- Make the smallest change that fixes the bug, normally ONE line. replace_line replaces exactly one
  line and keeps its indentation. If a fix needs several lines, call replace_line once per line
  (line numbers never shift because replace_line never adds or removes lines).
- Do not touch unrelated code, formatting, or comments. Do not make speculative edits.
- When finished, reply with plain text (no tool call) whose FIRST line is exactly
  `STATUS: FIXED` or `STATUS: CANNOT_FIX`. Then give a concise summary (2-4 sentences): which
  line was changed, what it was before and after, and why that fixes the bug. If CANNOT_FIX,
  explain what you checked.
"""


class AgentError(Exception):
    """Unrecoverable problem (e.g. the Gemini API is failing)."""


@dataclass
class PhaseResult:
    text: str | None
    tool_calls: int
    failure: str | None = None


@dataclass
class AttemptResult:
    success: bool
    summary: str = ""
    failure: str = ""
    edits: list[dict] = field(default_factory=list)


@dataclass
class RunResult:
    success: bool
    attempts: int


# --------------------------------------------------------------------------- model access


def _retry_delay(exc: Exception) -> float | None:
    """Seconds the API asked us to wait ("Please retry in 55.8s" / retryDelay '55s'), if it said."""
    text = str(exc)
    match = re.search(r"retry in ([\d.]+)s", text) or re.search(r"retryDelay'?\"?: '?\"?([\d.]+)s", text)
    return float(match.group(1)) if match else None


class ModelRunner:
    """Calls Gemini with retry on transient errors and sticky fallback to a second model."""

    def __init__(self, client: genai.Client):
        self.client = client
        primary = os.getenv("GEMINI_MODEL") or DEFAULT_MODEL
        fallback = os.getenv("GEMINI_FALLBACK_MODEL")
        self.models = [primary] + ([fallback] if fallback and fallback != primary else [])
        self.index = 0

    @property
    def model(self) -> str:
        return self.models[self.index]

    def generate(self, contents: list[types.Content], config: types.GenerateContentConfig):
        while True:
            for retry in range(MAX_RETRIES + 1):
                try:
                    return self.client.models.generate_content(
                        model=self.model, contents=contents, config=config
                    )
                except errors.APIError as exc:
                    code = getattr(exc, "code", None)
                    if code in RETRYABLE_CODES and retry < MAX_RETRIES:
                        asked = _retry_delay(exc)
                        wait = min(MAX_WAIT_SECONDS, max(2 ** (retry + 1), (asked or 0) + 1))
                        print(f"[Model] {self.model} returned {code}; retrying in {wait:.0f}s ...")
                        time.sleep(wait)
                        continue
                    if code in FALLBACK_CODES and self.index + 1 < len(self.models):
                        failed = self.model
                        self.index += 1
                        print(f"[Model] {failed} failed ({code}); falling back to {self.model}")
                        break  # restart the retry loop with the fallback model
                    raise AgentError(f"Gemini API error ({code}) from {self.model}: {exc}") from exc


# --------------------------------------------------------------------------- helpers


def _truncate(text: str, limit: int = 160) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _format_args(args: dict) -> str:
    return ", ".join(f"{k}={json.dumps(v, ensure_ascii=False)}" for k, v in args.items())


def parse_marker(text: str, marker: str) -> tuple[str | None, str]:
    """Find a line like 'STATUS: FIXED'. Returns (UPPERCASE value or None, text without that line)."""
    value: str | None = None
    rest: list[str] = []
    for line in text.splitlines():
        cleaned = line.strip().strip("*`").strip()
        if value is None and cleaned.upper().startswith(marker.upper() + ":"):
            value = cleaned.split(":", 1)[1].strip().strip("*`").strip().upper()
        else:
            rest.append(line)
    return value, "\n".join(rest).strip()


def _execute_tool(call: types.FunctionCall) -> types.Part:
    name = call.name or ""
    args = dict(call.args or {})
    print(f"[Tool Called] {name}({_format_args(args)})")

    func = tools.TOOL_FUNCTIONS.get(name)
    if func is None:
        payload = {"error": f"Unknown tool '{name}'."}
    else:
        try:
            payload = {"result": func(**args)}
        except tools.ToolError as exc:
            payload = {"error": str(exc)}
        except TypeError as exc:
            payload = {"error": f"Bad arguments for {name}: {exc}"}
        except Exception as exc:  # keep the loop alive; let the model see the failure
            payload = {"error": f"{type(exc).__name__}: {exc}"}

    shown = payload.get("error") or payload.get("result")
    if name != "replace_line" or "error" in payload:  # replace_line already printed Before/After
        label = "error" if "error" in payload else "ok"
        print(f"    -> {label}: {_truncate(json.dumps(shown, ensure_ascii=False, default=str))}")
    return types.Part.from_function_response(name=name, response=payload)


def _build_config() -> types.GenerateContentConfig:
    return types.GenerateContentConfig(
        system_instruction=SYSTEM_PROMPT,
        tools=[types.Tool(function_declarations=tools.TOOL_DECLARATIONS)],
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
    )


def _user_message(text: str) -> types.Content:
    return types.Content(role="user", parts=[types.Part.from_text(text=text)])


def _build_prompt(bug: str, notes: list[str]) -> str:
    prompt = (
        "Bug report from the user (plain English):\n"
        f'"""\n{bug}\n"""\n\n'
        "The project workspace contains Python source files. Find the bug and fix it."
    )
    if notes:
        prompt += (
            "\n\nEarlier attempts FAILED and their edits were reverted (files are back to the "
            "original). Do not repeat the same mistake:\n" + "\n".join(notes)
        )
    return prompt


# --------------------------------------------------------------------------- one conversation phase


def _run_phase(
    runner: ModelRunner,
    contents: list[types.Content],
    config: types.GenerateContentConfig,
    budget: int,
) -> PhaseResult:
    """Loop model -> tool calls -> model ... until the model replies with text or the cap is hit."""
    used = 0
    while True:
        response = runner.generate(contents, config)
        candidate = response.candidates[0] if response.candidates else None
        content = candidate.content if candidate else None
        parts = (content.parts or []) if content else []
        if content is None or not parts:
            reason = getattr(candidate, "finish_reason", None) if candidate else "no candidates"
            return PhaseResult(None, used, f"The model returned an empty response ({reason}).")

        # Append the model turn verbatim so thought signatures stay attached to function_call parts.
        contents.append(content)

        calls = [p.function_call for p in parts if p.function_call]
        if not calls:
            text = "".join(p.text for p in parts if p.text and not p.thought).strip()
            return PhaseResult(text, used)

        response_parts: list[types.Part] = []
        for call in calls:
            if used >= budget:
                return PhaseResult(None, used, f"Tool-call limit reached ({budget} calls) without finishing.")
            used += 1
            response_parts.append(_execute_tool(call))
        contents.append(types.Content(role="user", parts=response_parts))


# --------------------------------------------------------------------------- one attempt


def _run_attempt(
    runner: ModelRunner,
    config: types.GenerateContentConfig,
    bug: str,
    notes: list[str],
) -> AttemptResult:
    tools.EDIT_LOG.clear()
    contents = [_user_message(_build_prompt(bug, notes))]

    # Phase 1: diagnose and fix
    fix = _run_phase(runner, contents, config, MAX_FIX_TOOL_CALLS)
    if fix.failure:
        return AttemptResult(False, failure=fix.failure, edits=list(tools.EDIT_LOG))

    status, summary = parse_marker(fix.text or "", "STATUS")
    if status is None or not status.startswith(("FIXED", "CANNOT")):
        return AttemptResult(
            False,
            failure="The model finished without a valid 'STATUS: FIXED' or 'STATUS: CANNOT_FIX' line.",
            edits=list(tools.EDIT_LOG),
        )
    if status.startswith("CANNOT"):
        return AttemptResult(
            False,
            failure=f"The model reported it could not fix the bug. {_truncate(summary, 300)}",
            edits=list(tools.EDIT_LOG),
        )
    if not tools.EDIT_LOG:
        return AttemptResult(False, failure="The model claimed FIXED but never edited any file.")

    # Phase 2: self-verification
    edits_before_verify = len(tools.EDIT_LOG)
    edit_lines = "\n".join(
        f"- {e['file']} line {e['line_number']}: `{e['before'].strip()}` -> `{e['after'].strip()}`"
        for e in tools.EDIT_LOG
    )
    print("[Verification] Asking the model to re-check its own edits ...")
    contents.append(
        _user_message(
            "Verification step. These are the edits you made this attempt:\n"
            f"{edit_lines}\n\n"
            "Re-read the edited lines in context (use read_file or search_in_file if needed) and check:\n"
            "1. The change really fixes the bug that was described.\n"
            "2. It does not introduce a new bug or a syntax or runtime error.\n"
            "3. No other line related to this bug still looks wrong.\n"
            "Reply with plain text whose FIRST line is exactly `VERIFIED: YES` or `VERIFIED: NO`, "
            "then 1-3 sentences of reasoning."
        )
    )
    verify = _run_phase(runner, contents, config, MAX_VERIFY_TOOL_CALLS)
    if verify.failure:
        return AttemptResult(False, failure=f"Verification failed: {verify.failure}", edits=list(tools.EDIT_LOG))

    verdict, reason = parse_marker(verify.text or "", "VERIFIED")
    print(f"[Verification] {verdict or 'NO VERDICT'}: {_truncate(reason, 200)}")
    edits = list(tools.EDIT_LOG)
    if verdict and verdict.startswith("YES"):
        if len(edits) != edits_before_verify:  # model adjusted something while verifying
            summary += f"\n\n(Adjusted during verification: {reason})"
        return AttemptResult(True, summary=summary, edits=edits)
    return AttemptResult(
        False,
        failure=f"The model did not confirm its own fix. {_truncate(reason, 300)}",
        edits=edits,
    )


# --------------------------------------------------------------------------- public entry point


def run_agent(client: genai.Client, bug_description: str) -> RunResult:
    """Run up to MAX_ATTEMPTS attempts. Returns whether a verified fix was applied and how many attempts it took."""
    snapshot = tools.snapshot_workspace()
    runner = ModelRunner(client)
    config = _build_config()
    notes: list[str] = []
    succeeded = False

    print(f"[Model] {runner.model}")
    try:
        for attempt in range(1, MAX_ATTEMPTS + 1):
            print(f"\n--- Attempt {attempt} of {MAX_ATTEMPTS} ---")
            result = _run_attempt(runner, config, bug_description, notes)

            if result.success:
                succeeded = True
                print("\n=== Bug resolved ===")
                print(result.summary)
                return RunResult(True, attempt)

            print(f"\n[Attempt {attempt} failed] {result.failure}")
            tools.restore_workspace(snapshot)
            tried = "; ".join(
                f"{e['file']}:{e['line_number']} `{e['before'].strip()}` -> `{e['after'].strip()}`"
                for e in result.edits
            ) or "no edits"
            notes.append(f"- Attempt {attempt}: edits tried: {tried}. Why it failed: {result.failure}")
            if attempt < MAX_ATTEMPTS:
                print("[Rollback] Files restored to the original snapshot for the next attempt.")

        print(
            f"\nI cannot solve this bug within the {MAX_ATTEMPTS}-attempt limit. "
            "Original files have been restored."
        )
        return RunResult(False, MAX_ATTEMPTS)
    finally:
        if not succeeded:  # also covers API errors and Ctrl+C
            tools.restore_workspace(snapshot)
