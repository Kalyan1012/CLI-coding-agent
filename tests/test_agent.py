"""Tests the agent loop with a scripted fake Gemini client (no network, no API key)."""

import pytest
from google.genai import types

import agent
import tools

BUGGY = "def add(a, b):\n    return a - b\n"
FIXED = "def add(a, b):\n    return a + b\n"


def call(name, **args):
    part = types.Part.from_function_call(name=name, args=args)
    return types.GenerateContentResponse(
        candidates=[types.Candidate(content=types.Content(role="model", parts=[part]))]
    )


def text(message):
    part = types.Part.from_text(text=message)
    return types.GenerateContentResponse(
        candidates=[types.Candidate(content=types.Content(role="model", parts=[part]))]
    )


class FakeModels:
    def __init__(self, script):
        self.script = list(script)
        self.last_contents = None

    def generate_content(self, model, contents, config):
        assert config.automatic_function_calling.disable is True  # manual loop is enforced
        self.last_contents = contents
        return self.script.pop(0)


class FakeClient:
    def __init__(self, script):
        self.models = FakeModels(script)


@pytest.fixture
def ws(tmp_path, monkeypatch):
    root = tmp_path / "python_project"
    root.mkdir()
    (root / "main.py").write_text(BUGGY)
    monkeypatch.setattr(tools, "WORKSPACE", root)
    return root


def test_parse_marker_finds_value_and_strips_the_line():
    value, rest = agent.parse_marker("STATUS: FIXED\nChanged line 2.", "STATUS")
    assert value == "FIXED"
    assert rest == "Changed line 2."


def test_parse_marker_tolerates_markdown_decoration():
    value, _ = agent.parse_marker("**STATUS: CANNOT_FIX**\nreason", "STATUS")
    assert value == "CANNOT_FIX"


def test_parse_marker_returns_none_when_missing():
    assert agent.parse_marker("no marker here", "STATUS")[0] is None


def test_successful_fix_on_first_attempt(ws):
    fix_call = call("replace_line", file_path="main.py", line_number=2, new_line="return a + b")
    client = FakeClient([fix_call, text("STATUS: FIXED\nChanged line 2."), text("VERIFIED: YES\nLooks right.")])
    result = agent.run_agent(client, "add subtracts")
    assert result.success and result.attempts == 1
    assert (ws / "main.py").read_text() == FIXED
    # the model turn is appended verbatim (keeps thought signatures attached to function calls)
    assert any(c is fix_call.candidates[0].content for c in client.models.last_contents)


def test_failed_verification_and_giving_up_roll_back_everything(ws):
    client = FakeClient([
        call("replace_line", file_path="main.py", line_number=2, new_line="return 0"),
        text("STATUS: FIXED\nchanged"),
        text("VERIFIED: NO\nthat is wrong"),
        text("STATUS: CANNOT_FIX\nnothing found"),
        text("STATUS: CANNOT_FIX\nnothing found"),
    ])
    result = agent.run_agent(client, "something is off")
    assert not result.success and result.attempts == agent.MAX_ATTEMPTS
    assert (ws / "main.py").read_text() == BUGGY  # byte-for-byte original


def test_tool_call_cap_ends_the_attempt(ws):
    endless = [call("read_file", file_path="main.py")] * (agent.MAX_FIX_TOOL_CALLS + 1)
    client = FakeClient(endless + [text("STATUS: CANNOT_FIX\nx"), text("STATUS: CANNOT_FIX\nx")])
    result = agent.run_agent(client, "loops forever")
    assert not result.success
    assert (ws / "main.py").read_text() == BUGGY


def test_retry_delay_is_read_from_the_api_error():
    from google.genai import errors

    exc = errors.ClientError(429, {"error": {"message": "quota hit. Please retry in 55.8s.", "status": "RESOURCE_EXHAUSTED"}})
    assert agent._retry_delay(exc) == 55.8
    assert agent._retry_delay(errors.ClientError(400, {"error": {"message": "bad request"}})) is None


def test_rate_limit_is_retried_using_the_servers_delay(ws, monkeypatch):
    from google.genai import errors

    sleeps = []
    monkeypatch.setattr(agent.time, "sleep", sleeps.append)
    limit = errors.ClientError(429, {"error": {"message": "Please retry in 20s.", "status": "RESOURCE_EXHAUSTED"}})
    client = FakeClient([text("STATUS: CANNOT_FIX\nx")] * 3)
    original = client.models.generate_content
    state = {"failed": False}

    def flaky(model, contents, config):
        if not state["failed"]:
            state["failed"] = True
            raise limit
        return original(model, contents, config)

    client.models.generate_content = flaky
    agent.run_agent(client, "anything")
    assert sleeps == [21]  # waited for the server's 20s (+1s margin), not a fixed 2s


def test_persistent_api_failure_raises_agent_error_and_restores_files(ws, monkeypatch):
    from google.genai import errors

    monkeypatch.setattr(agent.time, "sleep", lambda _: None)

    class AlwaysBroken:
        class models:
            @staticmethod
            def generate_content(model, contents, config):
                raise errors.ServerError(503, {"error": {"message": "overloaded"}})

    before = (ws / "main.py").read_text()
    with pytest.raises(agent.AgentError):
        agent.run_agent(AlwaysBroken(), "anything")
    assert (ws / "main.py").read_text() == before
