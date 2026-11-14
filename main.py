"""CLI entrypoint: describe a bug in plain English and let the agent try to fix it."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from google import genai

import tools
from agent import AgentError, run_agent


def main() -> int:
    load_dotenv(Path(__file__).resolve().parent / ".env")

    if not os.getenv("GEMINI_API_KEY"):
        print("GEMINI_API_KEY is not set. Copy .env.example to .env and add your key.")
        return 1
    if not tools.WORKSPACE.is_dir():
        print(f"Workspace folder not found: {tools.WORKSPACE}")
        return 1

    try:
        bug = input("Describe the bug in simple English: ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\nNo bug description given. Exiting.")
        return 1
    if not bug:
        print("No bug description given. Exiting.")
        return 1

    client = genai.Client()  # reads GEMINI_API_KEY from the environment
    try:
        return 0 if run_agent(client, bug).success else 2
    except AgentError as exc:
        print(f"\nAgent stopped: {exc}\nOriginal files have been restored.")
        return 3
    except KeyboardInterrupt:
        print("\nInterrupted. Original files have been restored.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
