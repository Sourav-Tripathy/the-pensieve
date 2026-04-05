#!/usr/bin/env python3
"""
thinker.py — The Pensieve Reasoning Loop

A task-agnostic autonomous loop that feeds a TASK CONFIG to DeepSeek-R1 (1.5b
via Ollama), streams its reasoning token-by-token, runs any tool calls it
emits, writes its reasoning to a markdown journal, and commits to GitHub when
a task-defined stopping condition is met.

HOW TO SWITCH TASKS
───────────────────
Each task lives in a TaskConfig dataclass instance defined at the bottom of
this file (TASK_REGISTRY).  Change ACTIVE_TASK to switch tasks.

TOOL CALL SYNTAX (model-facing)
────────────────────────────────
The model can call a tool by emitting:

    ---TOOL: <tool_name> {"arg1": val1}---

thinker.py will execute the tool and inject the result as a system message
before the next model turn.

BREAK / COMMIT SYNTAX (model-facing)
──────────────────────────────────────
    ---BREAK---   →  commit reasoning.md + push, then continue
    ---DONE---    →  task is complete; run final validation, commit, push, stop

Usage:
    python thinker.py
"""

import json
import os
import re
import sys
import time
import textwrap
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import requests

# ─────────────────────────────────────────────
# GLOBAL CONFIG
# ─────────────────────────────────────────────
OLLAMA_BASE_URL    = "http://localhost:11434"
MODEL              = "deepseek-r1:1.5b"
REPO_ROOT          = Path(__file__).parent
REASONING_FILE     = REPO_ROOT / "reasoning.md"
LOGS_DIR           = REPO_ROOT / "logs"
LOGS_FILE          = LOGS_DIR / "session.log"

MAX_CONTEXT_TOKENS = 2800   # stay well inside 4096-token window
MAX_TOKENS_PER_TURN = 700
INTER_TURN_SLEEP   = 2      # seconds between turns
REPEAT_THRESHOLD   = 3      # repeated sentences before abort

BREAK_MARKER = "---BREAK---"
DONE_MARKER  = "---DONE---"

# ─────────────────────────────────────────────
# IMPORTS FROM TOOLS
# ─────────────────────────────────────────────
from tools import (
    run_tool_calls,
    log_event,
    validate_sudoku,
    git_commit as tool_git_commit,
    git_push   as tool_git_push,
    write_reasoning,
    format_sudoku,
    LOGS_DIR,
)

LOGS_DIR.mkdir(exist_ok=True)


# ─────────────────────────────────────────────
# TASK CONFIG DATACLASS
# ─────────────────────────────────────────────
@dataclass
class TaskConfig:
    name: str                    # short task name (used in logs/commit messages)
    system_prompt: str           # sets the model's role and rules
    first_prompt: str            # opening message to kick off the task
    continue_prompt: str         # message sent each subsequent turn
    done_prompt: str             # injected when model emits ---DONE---
    max_turns: int = 60          # hard limit on turns
    # Optional hook: called with the final reasoning text, returns (passed: bool, detail: str)
    final_check: Callable[[str], tuple[bool, str]] | None = None
    # Files that must be staged on commit (relative to REPO_ROOT)
    commit_files: list[str] = field(default_factory=lambda: ["reasoning.md", "logs/session.log"])


# ─────────────────────────────────────────────
# THE SUDOKU PUZZLE
# ─────────────────────────────────────────────
# A classic medium-difficulty puzzle.  0 = empty cell.
SUDOKU_PUZZLE = [
    [5,3,0,0,7,0,0,0,0],
    [6,0,0,1,9,5,0,0,0],
    [0,9,8,0,0,0,0,6,0],
    [8,0,0,0,6,0,0,0,3],
    [4,0,0,8,0,3,0,0,1],
    [7,0,0,0,2,0,0,0,6],
    [0,6,0,0,0,0,2,8,0],
    [0,0,0,4,1,9,0,0,5],
    [0,0,0,0,8,0,0,7,9],
]

_PUZZLE_STR = format_sudoku(SUDOKU_PUZZLE)


def _sudoku_final_check(reasoning_text: str) -> tuple[bool, str]:
    """
    Extract the model's claimed solution from reasoning.md and validate it.
    The model is asked to emit its final grid as JSON inside:
        ---SOLUTION: [[...],[...],...] ---
    """
    pattern = re.compile(
        r"---SOLUTION:\s*(\[\[.*?\]\])\s*---",
        re.DOTALL
    )
    m = pattern.search(reasoning_text)
    if not m:
        return False, "No ---SOLUTION: [[...]] --- block found in reasoning.md"
    try:
        grid = json.loads(m.group(1))
    except json.JSONDecodeError as e:
        return False, f"Could not parse solution JSON: {e}"

    res = validate_sudoku(grid)
    return res["ok"], res["message"]


# ─────────────────────────────────────────────
# SUDOKU TASK CONFIG
# ─────────────────────────────────────────────
SUDOKU_TASK = TaskConfig(
    name="sudoku-solver",
    system_prompt=textwrap.dedent(f"""
        You are a careful, methodical puzzle solver.

        Your task is to solve a 9×9 Sudoku puzzle using pure logical reasoning —
        NO programs, NO code, NO guessing.  Only deduction.

        RULES OF SUDOKU (reminder):
        - Every row must contain the digits 1–9 with no repetition.
        - Every column must contain the digits 1–9 with no repetition.
        - Every 3×3 box must contain the digits 1–9 with no repetition.
        - A 0 in the grid means the cell is empty — you must fill it in.

        YOUR PUZZLE (0 = empty):
        {_PUZZLE_STR}

        HOW TO REASON:
        1. For each empty cell, list which digits 1-9 are NOT already in its
           row, column, and 3×3 box — these are its "candidates".
        2. If a cell has only ONE candidate, place it.
        3. If within a row/column/box, a digit can only go in ONE remaining
           cell, place it there (hidden single).
        4. Work systematically — row by row, column by column, box by box.
        5. Show EVERY elimination step explicitly. Do not skip.

        TOOL CALLS — you may call tools using this exact syntax on its own line:
            ---TOOL: validate_sudoku {{"grid": [[r1...],[r2...],...]}}---
        Call validate_sudoku only when you believe the grid is complete.

        FORMATTING RULES:
        - Write your reasoning step by step.
        - When you finish working through a meaningful chunk (e.g. placed 2-3 cells),
          write ---BREAK--- on its own line. This triggers a commit of your progress.
        - When you are certain the puzzle is fully solved, emit the solution like:
            ---SOLUTION: [[row1],[row2],...,[row9]] ---
          Then write ---DONE--- on its own line.
        - Do NOT emit ---DONE--- until every cell is filled and you've verified it.
    """).strip(),

    first_prompt=textwrap.dedent(f"""
        Here is your Sudoku puzzle:

        {_PUZZLE_STR}

        Start solving. Begin with a systematic candidates analysis:
        for each empty cell in Row 1, list which digits are already used in
        its row, column, and box — then identify which digit(s) can go there.
        Show every step.  Write ---BREAK--- after each meaningful chunk of progress.
    """).strip(),

    continue_prompt=textwrap.dedent("""
        Good work. Continue solving from where you left off.
        Pick the next empty cell (or group of cells) and work through the
        candidate elimination methodically.
        Show every elimination. Write ---BREAK--- after placing a set of cells.
        When the grid is completely filled and verified, emit ---SOLUTION: [[...]] ---
        followed by ---DONE--- on its own line.
    """).strip(),

    done_prompt=textwrap.dedent("""
        You have emitted ---DONE---. The puzzle will now be validated.
        If the solution is correct, the session will end with a final commit.
        If incorrect, you will receive error feedback and must correct your work.
    """).strip(),

    max_turns=60,
    final_check=_sudoku_final_check,
    commit_files=["reasoning.md", "logs/session.log"],
)


# ─────────────────────────────────────────────
# TASK REGISTRY  ←  add new tasks here
# ─────────────────────────────────────────────
TASK_REGISTRY: dict[str, TaskConfig] = {
    "sudoku": SUDOKU_TASK,
    # "next_task": NEXT_TASK,
}

# ── CHANGE THIS to switch tasks ──────────────
ACTIVE_TASK = "sudoku"


# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────

def rough_token_count(text: str) -> int:
    return len(text) // 4


def trim_history(history: list[dict], max_tokens: int) -> list[dict]:
    """Drop oldest exchange pairs (but never the system prompt) to stay within budget."""
    while True:
        total = sum(rough_token_count(m["content"]) for m in history)
        if total <= max_tokens or len(history) <= 2:
            break
        history.pop(1)
    return history


def stream_generate(prompt: str, history: list[dict]) -> str:
    """Send a chat request to Ollama with streaming; return the full response."""
    messages = history + [{"role": "user", "content": prompt}]
    payload = {
        "model": MODEL,
        "messages": messages,
        "stream": True,
        "options": {
            "num_ctx": 4096,
            "num_predict": MAX_TOKENS_PER_TURN,
            "temperature": 0.3,     # low temp for deterministic deduction
            "top_p": 0.9,
            "repeat_penalty": 1.3,
            "repeat_last_n": 128,
        },
    }

    full_response = ""
    _sentence_counts: dict[str, int] = {}
    _aborted = False

    def _check_repetition(text: str) -> bool:
        sentences = re.split(r'(?<=[.!\?\n])\s+', text.strip())
        for s in sentences:
            s = s.strip().lower()
            if len(s) < 20:
                continue
            _sentence_counts[s] = _sentence_counts.get(s, 0) + 1
            if _sentence_counts[s] >= REPEAT_THRESHOLD:
                return True
        return False

    try:
        with requests.post(
            f"{OLLAMA_BASE_URL}/api/chat",
            json=payload, stream=True, timeout=300
        ) as resp:
            resp.raise_for_status()
            for line in resp.iter_lines():
                if not line:
                    continue
                chunk = json.loads(line)
                token = chunk.get("message", {}).get("content", "")
                if token:
                    print(token, end="", flush=True)
                    full_response += token
                    if len(full_response) % 200 < len(token):
                        if _check_repetition(full_response):
                            print("\n[REPEAT GUARD] Loop detected — aborting turn.", file=sys.stderr)
                            _aborted = True
                            break
                if chunk.get("done"):
                    break
    except requests.exceptions.RequestException as e:
        print(f"\n[ERROR] Ollama request failed: {e}", file=sys.stderr)
        raise

    print()  # newline after streaming

    if _aborted:
        lines = full_response.split("\n")
        seen: set[str] = set()
        clean: list[str] = []
        for ln in lines:
            key = ln.strip().lower()
            if key and key in seen and len(key) > 20:
                break
            clean.append(ln)
            if key:
                seen.add(key)
        full_response = "\n".join(clean)
        full_response += f"\n\n{BREAK_MARKER}"

    return full_response


def do_commit_and_push(task: TaskConfig, turn: int, label: str = "progress") -> None:
    """Stage task files, commit, and push."""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    msg = f"pensieve({task.name}): {label} — turn {turn} [{ts}]"
    tool_git_commit(msg, task.commit_files)
    tool_git_push()
    log_event("COMMIT_PUSH", msg)


def init_reasoning_file(task: TaskConfig) -> None:
    """Write the reasoning.md header for this task session."""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    header = (
        f"# The Pensieve — {task.name}\n\n"
        f"> *Session started: {ts}*  \n"
        f"> *Model: {MODEL}*\n\n"
        f"---\n\n"
        f"## Puzzle\n\n"
        f"```\n{_PUZZLE_STR}\n```\n\n"
        f"---\n\n"
        f"## Reasoning Log\n\n"
    ) if task.name.startswith("sudoku") else (
        f"# The Pensieve — {task.name}\n\n"
        f"> *Session started: {ts}*  \n"
        f"> *Model: {MODEL}*\n\n"
        f"---\n\n"
        f"## Reasoning Log\n\n"
    )
    write_reasoning(header, turn=0, append=False)
    log_event("SESSION_START", f"task={task.name} model={MODEL}")


def append_reasoning_turn(response: str, turn: int) -> None:
    """Strip think tags and write a turn block to reasoning.md."""
    think_re = re.compile(r"<think>(.*?)</think>", re.DOTALL)
    think_match = think_re.search(response)

    clean = think_re.sub("", response).strip()
    clean = clean.replace(BREAK_MARKER, "").replace(DONE_MARKER, "").strip()

    block = f"### Step {turn}\n\n"
    if think_match:
        inner = think_match.group(1).strip()
        if inner:
            block += f"<details>\n<summary>🧠 Internal scratchpad</summary>\n\n{inner}\n\n</details>\n\n"
    if clean:
        block += f"{clean}\n\n"
    block += "---\n"

    with open(REASONING_FILE, "a", encoding="utf-8") as f:
        f.write(block)


# ─────────────────────────────────────────────
# LOG FILE APPEND (raw session log)
# ─────────────────────────────────────────────

def raw_log(line: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with open(LOGS_FILE, "a", encoding="utf-8") as f:
        f.write(f"{ts} | {line}\n")


# ─────────────────────────────────────────────
# MAIN LOOP
# ─────────────────────────────────────────────

def main() -> None:
    task = TASK_REGISTRY[ACTIVE_TASK]

    print("=" * 60)
    print(f"  THE PENSIEVE")
    print(f"  Task  : {task.name}")
    print(f"  Model : {MODEL}")
    print(f"  Output: {REASONING_FILE}")
    print(f"  Logs  : {LOGS_FILE}")
    print("=" * 60)
    print()

    # Check Ollama
    try:
        r = requests.get(f"{OLLAMA_BASE_URL}/api/tags", timeout=5)
        r.raise_for_status()
        print("[OK] Ollama is running.\n")
    except Exception:
        print(f"[ERROR] Cannot reach Ollama at {OLLAMA_BASE_URL}")
        print("        Make sure `ollama serve` is running.")
        sys.exit(1)

    init_reasoning_file(task)

    history: list[dict] = [
        {"role": "system", "content": task.system_prompt}
    ]

    turn = 1
    commits_made = 0
    done = False
    current_prompt = task.first_prompt
    pending_tool_results: list[str] = []

    while turn <= task.max_turns and not done:
        print(f"\n{'─' * 60}")
        print(f"  TURN {turn} / {task.max_turns}")
        print(f"{'─' * 60}\n")
        raw_log(f"TURN_START turn={turn}")
        log_event("TURN_START", f"turn={turn}")

        # Inject any pending tool results into prompt
        if pending_tool_results:
            tool_summary = "\n".join(pending_tool_results)
            current_prompt = (
                f"[TOOL RESULTS]\n{tool_summary}\n\n{current_prompt}"
            )
            pending_tool_results.clear()

        # Trim context
        history = trim_history(history, MAX_CONTEXT_TOKENS)

        # Stream model response
        try:
            response = stream_generate(current_prompt, history)
        except Exception as e:
            raw_log(f"TURN_ERROR {e}")
            print(f"\n[FATAL] Could not get response: {e}")
            break

        # Update history
        history.append({"role": "user", "content": current_prompt})
        history.append({"role": "assistant", "content": response})

        # Write reasoning to file
        append_reasoning_turn(response, turn)
        raw_log(f"TURN_RESPONSE chars={len(response)}")

        # ── Handle tool calls ───────────────────────────────────
        tool_results = run_tool_calls(response)
        for tr in tool_results:
            result_line = (
                f"Tool `{tr['tool']}`: {'✅' if tr['ok'] else '❌'} {tr['message']}"
            )
            pending_tool_results.append(result_line)
            print(f"\n[TOOL] {result_line}")
            raw_log(f"TOOL_RESULT {result_line}")

        # ── Handle ---BREAK--- ──────────────────────────────────
        if BREAK_MARKER in response:
            print(f"\n[BREAK] Committing progress at turn {turn}…")
            do_commit_and_push(task, turn, label="progress")
            commits_made += 1
            raw_log(f"BREAK_COMMIT commits={commits_made}")
            time.sleep(INTER_TURN_SLEEP)

        # ── Handle ---DONE--- ───────────────────────────────────
        if DONE_MARKER in response:
            print(f"\n[DONE] Model signalled completion at turn {turn}.")
            raw_log("DONE_SIGNAL")

            # Run final check
            if task.final_check:
                reasoning_text = REASONING_FILE.read_text(encoding="utf-8")
                passed, detail = task.final_check(reasoning_text)
                check_line = f"Final check: {'PASSED ✅' if passed else 'FAILED ❌'} — {detail}"
                print(f"\n[CHECK] {check_line}")
                raw_log(f"FINAL_CHECK {check_line}")

                with open(REASONING_FILE, "a", encoding="utf-8") as f:
                    f.write(f"\n---\n\n## Final Validation\n\n{check_line}\n")

                if passed:
                    do_commit_and_push(task, turn, label="SOLVED")
                    done = True
                else:
                    # Feed failure back so model can correct itself
                    pending_tool_results.append(
                        f"⚠️ Validation failed: {detail}\n"
                        f"Please find and correct the error. Re-emit ---SOLUTION: [[...]] --- "
                        f"and ---DONE--- when fixed."
                    )
                    current_prompt = task.continue_prompt
                    turn += 1
                    time.sleep(INTER_TURN_SLEEP)
                    continue
            else:
                do_commit_and_push(task, turn, label="DONE")
                done = True

            break

        current_prompt = task.continue_prompt
        turn += 1
        time.sleep(INTER_TURN_SLEEP)

    # ── Session summary ─────────────────────────────────────────
    print(f"\n{'=' * 60}")
    status = "SOLVED ✅" if done else f"STOPPED (turn limit or error)"
    print(f"  {status}")
    print(f"  Turns: {turn - 1} / {task.max_turns}")
    print(f"  Commits: {commits_made}")
    print(f"  Reasoning: {REASONING_FILE}")
    print(f"{'=' * 60}")

    raw_log(f"SESSION_END turns={turn-1} commits={commits_made} done={done}")

    if not done:
        # Final safety commit of whatever was written
        do_commit_and_push(task, turn - 1, label="session-end")


if __name__ == "__main__":
    main()
