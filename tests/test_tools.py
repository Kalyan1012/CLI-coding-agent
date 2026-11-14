import pytest

import tools

ORIGINAL = "def add(a, b):\n    return a + b\n\n\ndef sub(a, b):\n    return a - b\n"


@pytest.fixture
def ws(tmp_path, monkeypatch):
    root = tmp_path / "python_project"
    root.mkdir()
    (root / "main.py").write_text(ORIGINAL)
    (root / "notes.txt").write_text("nothing special\n")
    monkeypatch.setattr(tools, "WORKSPACE", root)
    tools.EDIT_LOG.clear()
    return root


def test_read_file_numbers_every_line(ws):
    lines = tools.read_file("main.py").splitlines()
    assert lines[0] == "1: def add(a, b):"
    assert lines[1] == "2:     return a + b"
    assert len(lines) == 6  # no phantom line after the trailing newline


def test_read_file_accepts_workspace_prefix(ws):
    assert tools.read_file("python_project/main.py") == tools.read_file("main.py")


def test_search_files_matches_name_and_content(ws):
    assert tools.search_files("notes") == ["notes.txt"]
    assert tools.search_files("def") == ["main.py"]
    assert tools.search_files("zzz-not-there") == []


def test_search_in_file_is_case_insensitive_with_context(ws):
    hits = tools.search_in_file("main.py", "RETURN")
    assert [h["line_number"] for h in hits] == [2, 6]
    context = hits[0]["context"].splitlines()
    assert context[0] == "1: def add(a, b):"
    assert context[-1] == "5: def sub(a, b):"  # 3 lines after line 2


def test_replace_line_preserves_indentation(ws):
    tools.replace_line("main.py", 6, "return a * b")
    text = (ws / "main.py").read_text()
    assert text.split("\n")[5] == "    return a * b"
    assert text.endswith("\n")
    assert text.split("\n")[:5] == ORIGINAL.split("\n")[:5]  # nothing else changed


def test_replace_line_respects_explicit_indentation(ws):
    tools.replace_line("main.py", 6, "        return 0")
    assert (ws / "main.py").read_text().split("\n")[5] == "        return 0"


def test_replace_line_records_the_edit(ws):
    tools.replace_line("main.py", 2, "return a * b")
    assert tools.EDIT_LOG == [
        {"file": "main.py", "line_number": 2, "before": "    return a + b", "after": "    return a * b"}
    ]


@pytest.mark.parametrize("bad_line", [0, -1, 7, 99])
def test_replace_line_rejects_out_of_range(ws, bad_line):
    with pytest.raises(tools.ToolError):
        tools.replace_line("main.py", bad_line, "x = 1")
    assert (ws / "main.py").read_text() == ORIGINAL


def test_replace_line_rejects_multiline_and_fractional_numbers(ws):
    with pytest.raises(tools.ToolError):
        tools.replace_line("main.py", 2, "a = 1\nb = 2")
    with pytest.raises(tools.ToolError):
        tools.replace_line("main.py", 2.5, "a = 1")
    tools.replace_line("main.py", 2.0, "return 0")  # whole-number floats from the API are fine
    assert (ws / "main.py").read_text().split("\n")[1] == "    return 0"


def test_replace_line_keeps_crlf_line_endings(ws):
    (ws / "crlf.py").write_bytes(b"x = 1\r\ny = 2\r\n")
    tools.replace_line("crlf.py", 2, "y = 3")
    assert (ws / "crlf.py").read_bytes() == b"x = 1\r\ny = 3\r\n"


def test_paths_cannot_escape_the_workspace(ws, tmp_path):
    (tmp_path / "secret.txt").write_text("top secret")
    for bad in ["../secret.txt", str(tmp_path / "secret.txt"), "../../etc/passwd"]:
        with pytest.raises(tools.ToolError):
            tools.read_file(bad)


def test_snapshot_and_restore_round_trip(ws):
    snapshot = tools.snapshot_workspace()
    tools.replace_line("main.py", 2, "return 0")
    (ws / "extra.py").write_text("junk")
    (ws / "notes.txt").unlink()
    tools.restore_workspace(snapshot)
    assert tools.snapshot_workspace() == snapshot
    assert not (ws / "extra.py").exists()
