#!/usr/bin/env python3
import json
import re
import sys
import time
import textwrap
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import requests

from tools import (
    run_tool_calls, log_event, validate_sudoku,
    save_result, git_commit as tool_git_commit,
    git_push as tool_git_push, write_reasoning,
    format_sudoku, LOGS_DIR,
)

OLLAMA_BASE_URL     = "http://localhost:11434"
MODEL               = "deepseek-r1:1.5b"
REPO_ROOT           = Path(__file__).parent
LOGS_DIR            = REPO_ROOT / "logs"
LOGS_FILE           = LOGS_DIR / "session.log"

MAX_CONTEXT_TOKENS  = 2800
MAX_TOKENS_PER_TURN = 700
INTER_TURN_SLEEP    = 2
REPEAT_THRESHOLD    = 3

BREAK_MARKER = "---BREAK---"
DONE_MARKER  = "---DONE---"

LOGS_DIR.mkdir(exist_ok=True)


@dataclass
class TaskConfig:
    name: str
    system_prompt: str
    first_prompt: str
    continue_prompt: str
    max_turns: int = 60
    final_check: Callable | None = None
    extra_commit_files: list[str] = field(default_factory=list)


# --- Sudoku task ---

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


def _sudoku_final_check(reasoning_text: str) -> tuple[bool, str, str]:
    m = re.search(r"---SOLUTION:\s*(\[\[.*?\]\])\s*---", reasoning_text, re.DOTALL)
    if not m:
        return False, "No ---SOLUTION: [[...]] --- block found", ""
    try:
        grid = json.loads(m.group(1))
    except json.JSONDecodeError as e:
        return False, f"Could not parse solution JSON: {e}", ""
    res = validate_sudoku(grid)
    return res["ok"], res["message"], format_sudoku(grid) if res["ok"] else ""


SUDOKU_TASK = TaskConfig(
    name="sudoku-solver",
    system_prompt=textwrap.dedent(f"""
        You are a careful, methodical puzzle solver.

        Solve this 9x9 Sudoku using pure logical reasoning. No code. No guessing.

        RULES:
        - Every row, column, and 3x3 box must contain digits 1-9 with no repetition.
        - 0 = empty cell.

        YOUR PUZZLE:
        {_PUZZLE_STR}

        HOW TO REASON:
        1. For each empty cell, list candidates (digits not in its row, column, or box).
        2. If only one candidate: place it.
        3. If a digit can only go in one cell within a row/column/box: place it.
        4. Work row by row, column by column, box by box. Show every step.

        TOOL CALLS (use exactly this syntax on its own line):
            ---TOOL: validate_sudoku {{"grid": [[r1...],[r2...],...]}}---
        Only call validate_sudoku when you think the grid is complete.

        MARKERS:
        - Write ---BREAK--- after each meaningful chunk of progress (triggers a commit).
        - When solved, emit: ---SOLUTION: [[row1],...,[row9]] ---
          Then on the next line: ---DONE---
    """).strip(),

    first_prompt=textwrap.dedent(f"""
        Here is your Sudoku:

        {_PUZZLE_STR}

        Start with Row 1: for each empty cell list what digits are already used
        in its row, column, and box, then state the only remaining candidate.
        Show every step. Write ---BREAK--- after each chunk of progress.
    """).strip(),

    continue_prompt=textwrap.dedent("""
        Continue from where you left off. Pick the next empty cell or group,
        eliminate candidates methodically, and place the digit.
        Write ---BREAK--- after each chunk.
        When fully solved: emit ---SOLUTION: [[...]] --- then ---DONE---.
    """).strip(),

    max_turns=60,
    final_check=_sudoku_final_check,
    extra_commit_files=["results.md"],
)

TASK_REGISTRY = {"sudoku": SUDOKU_TASK}
ACTIVE_TASK   = "sudoku"


# --- Helpers ---

def make_reasoning_path(task_name: str) -> Path:
    ts   = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    safe = task_name.replace("-", "_").replace(" ", "_")
    return REPO_ROOT / f"reasoning_{safe}_{ts}.md"


def rough_token_count(text: str) -> int:
    return len(text) // 4


def trim_history(history: list[dict], max_tokens: int) -> list[dict]:
    while True:
        if sum(rough_token_count(m["content"]) for m in history) <= max_tokens:
            break
        if len(history) <= 2:
            break
        history.pop(1)
    return history


def stream_generate(prompt: str, history: list[dict]) -> str:
    payload = {
        "model": MODEL,
        "messages": history + [{"role": "user", "content": prompt}],
        "stream": True,
        "options": {
            "num_ctx": 4096,
            "num_predict": MAX_TOKENS_PER_TURN,
            "temperature": 0.3,
            "top_p": 0.9,
            "repeat_penalty": 1.3,
            "repeat_last_n": 128,
        },
    }
    full_response = ""
    _counts: dict[str, int] = {}
    _aborted = False

    def _repeating(text: str) -> bool:
        for s in re.split(r'(?<=[.!?\n])\s+', text.strip()):
            s = s.strip().lower()
            if len(s) < 20:
                continue
            _counts[s] = _counts.get(s, 0) + 1
            if _counts[s] >= REPEAT_THRESHOLD:
                return True
        return False

    try:
        with requests.post(f"{OLLAMA_BASE_URL}/api/chat",
                           json=payload, stream=True, timeout=300) as resp:
            resp.raise_for_status()
            for line in resp.iter_lines():
                if not line:
                    continue
                chunk = json.loads(line)
                token = chunk.get("message", {}).get("content", "")
                if token:
                    print(token, end="", flush=True)
                    full_response += token
                    if len(full_response) % 200 < len(token) and _repeating(full_response):
                        print("\n[REPEAT GUARD] aborting turn.", file=sys.stderr)
                        _aborted = True
                        break
                if chunk.get("done"):
                    break
    except requests.exceptions.RequestException as e:
        print(f"\n[ERROR] {e}", file=sys.stderr)
        raise

    print()

    if _aborted:
        seen: set[str] = set()
        clean: list[str] = []
        for ln in full_response.split("\n"):
            key = ln.strip().lower()
            if key and key in seen and len(key) > 20:
                break
            clean.append(ln)
            if key:
                seen.add(key)
        full_response = "\n".join(clean) + f"\n\n{BREAK_MARKER}"

    return full_response


def do_commit_and_push(task: TaskConfig, turn: int, reasoning_file: Path, label: str = "progress") -> None:
    ts  = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    msg = f"pensieve({task.name}): {label} — turn {turn} [{ts}]"
    files = [str(reasoning_file.relative_to(REPO_ROOT)), "logs/session.log"] + task.extra_commit_files
    tool_git_commit(msg, files)
    tool_git_push()
    log_event("COMMIT_PUSH", msg)


def init_reasoning_file(task: TaskConfig, reasoning_file: Path) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    if task.name.startswith("sudoku"):
        header = (
            f"# The Pensieve — {task.name}\n\n"
            f"> *{ts}* | *{MODEL}*\n\n---\n\n"
            f"## Puzzle\n\n```\n{_PUZZLE_STR}\n```\n\n---\n\n## Reasoning\n\n"
        )
    else:
        header = f"# The Pensieve — {task.name}\n\n> *{ts}* | *{MODEL}*\n\n---\n\n## Reasoning\n\n"
    write_reasoning(header, turn=0, append=False, reasoning_file=reasoning_file)
    log_event("SESSION_START", f"task={task.name} file={reasoning_file.name}")


def append_reasoning_turn(response: str, turn: int, reasoning_file: Path) -> None:
    think_re    = re.compile(r"<think>(.*?)</think>", re.DOTALL)
    think_match = think_re.search(response)
    clean       = think_re.sub("", response).strip()
    clean       = clean.replace(BREAK_MARKER, "").replace(DONE_MARKER, "").strip()

    block = f"### Step {turn}\n\n"
    if think_match and (inner := think_match.group(1).strip()):
        block += f"<details>\n<summary>Internal scratchpad</summary>\n\n{inner}\n\n</details>\n\n"
    if clean:
        block += f"{clean}\n\n"
    block += "---\n"

    with open(reasoning_file, "a", encoding="utf-8") as f:
        f.write(block)


def raw_log(line: str) -> None:
    LOGS_DIR.mkdir(exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with open(LOGS_FILE, "a", encoding="utf-8") as f:
        f.write(f"{ts} | {line}\n")


# --- Main loop ---

def main() -> None:
    task           = TASK_REGISTRY[ACTIVE_TASK]
    reasoning_file = make_reasoning_path(task.name)

    print(f"  Task : {task.name}")
    print(f"  Model: {MODEL}")
    print(f"  File : {reasoning_file.name}\n")

    try:
        requests.get(f"{OLLAMA_BASE_URL}/api/tags", timeout=5).raise_for_status()
        print("[OK] Ollama running\n")
    except Exception:
        print(f"[ERROR] Cannot reach Ollama at {OLLAMA_BASE_URL}")
        sys.exit(1)

    init_reasoning_file(task, reasoning_file)

    history         = [{"role": "system", "content": task.system_prompt}]
    turn            = 1
    commits_made    = 0
    done            = False
    current_prompt  = task.first_prompt
    pending_results : list[str] = []

    while turn <= task.max_turns and not done:
        print(f"\n── TURN {turn}/{task.max_turns} ──\n")
        raw_log(f"TURN_START turn={turn}")
        log_event("TURN_START", f"turn={turn}")

        if pending_results:
            current_prompt = f"[TOOL RESULTS]\n{chr(10).join(pending_results)}\n\n{current_prompt}"
            pending_results.clear()

        history = trim_history(history, MAX_CONTEXT_TOKENS)

        try:
            response = stream_generate(current_prompt, history)
        except Exception as e:
            raw_log(f"TURN_ERROR {e}")
            break

        history.append({"role": "user",      "content": current_prompt})
        history.append({"role": "assistant", "content": response})
        append_reasoning_turn(response, turn, reasoning_file)
        raw_log(f"TURN_RESPONSE chars={len(response)}")

        for tr in run_tool_calls(response):
            line = f"Tool `{tr['tool']}`: {'OK' if tr['ok'] else 'FAIL'} — {tr['message']}"
            pending_results.append(line)
            print(f"\n[TOOL] {line}")
            raw_log(f"TOOL_RESULT {line}")

        if BREAK_MARKER in response:
            print(f"\n[BREAK] committing turn {turn}…")
            do_commit_and_push(task, turn, reasoning_file, label="progress")
            commits_made += 1
            raw_log(f"BREAK_COMMIT commits={commits_made}")
            time.sleep(INTER_TURN_SLEEP)

        if DONE_MARKER in response:
            print(f"\n[DONE] turn {turn}")
            raw_log("DONE_SIGNAL")

            if task.final_check:
                res     = task.final_check(reasoning_file.read_text(encoding="utf-8"))
                passed, detail, sol_str = res[0], res[1], res[2] if len(res) > 2 else ""
                verdict = f"{'PASSED' if passed else 'FAILED'} — {detail}"
                print(f"\n[CHECK] {verdict}")
                raw_log(f"FINAL_CHECK {verdict}")

                with open(reasoning_file, "a", encoding="utf-8") as f:
                    f.write(f"\n---\n\n## Validation\n\n{verdict}\n")

                if passed:
                    save_result(task.name, _PUZZLE_STR, sol_str, reasoning_file.name, verdict)
                    do_commit_and_push(task, turn, reasoning_file, label="SOLVED")
                    done = True
                else:
                    pending_results.append(
                        f"Validation failed: {detail}\n"
                        "Correct the error, re-emit ---SOLUTION: [[...]] --- and ---DONE---."
                    )
                    current_prompt = task.continue_prompt
                    turn += 1
                    time.sleep(INTER_TURN_SLEEP)
                    continue
            else:
                do_commit_and_push(task, turn, reasoning_file, label="DONE")
                done = True
            break

        current_prompt = task.continue_prompt
        turn += 1
        time.sleep(INTER_TURN_SLEEP)

    status = "SOLVED" if done else "STOPPED"
    print(f"\n{status} | turns={turn-1} commits={commits_made} file={reasoning_file.name}")
    raw_log(f"SESSION_END turns={turn-1} commits={commits_made} done={done}")

    if not done:
        do_commit_and_push(task, turn - 1, reasoning_file, label="session-end")


if __name__ == "__main__":
    main()
