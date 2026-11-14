"""Validates the benchmark's own data (no API calls)."""

import pytest

import benchmark

SOURCE = benchmark.SOURCE.read_text()


def test_clean_source_passes_every_check(tmp_path):
    (tmp_path / "main.py").write_text(SOURCE)
    assert benchmark._passes(tmp_path, benchmark.FULL_CHECK), (
        "python_project/main.py is not in its original bug-free state"
    )


@pytest.mark.parametrize("case", benchmark.CASES, ids=lambda c: c["id"])
def test_each_bug_is_unique_and_detected(tmp_path, case):
    assert SOURCE.count(case["old"]) == 1
    (tmp_path / "main.py").write_text(SOURCE.replace(case["old"], case["new"], 1))
    assert not benchmark._passes(tmp_path, benchmark._script([case["check"]]))


def test_api_errors_are_reported_as_errored_not_as_agent_failures():
    import agent

    class Down:
        class models:
            @staticmethod
            def generate_content(model, contents, config):
                from google.genai import errors

                raise errors.ClientError(400, {"error": {"message": "nope"}})

    outcome = benchmark.run_case(Down(), benchmark.CASES[0], SOURCE, verbose=False)
    assert outcome["errored"] and not outcome["solved"] and outcome["attempts"] == 0
    assert benchmark.tools.WORKSPACE == benchmark.REAL_WORKSPACE
