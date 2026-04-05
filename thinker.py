#!/usr/bin/env python3
import copy
import json
import platform
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

# Probed at startup — see probe_max_ctx()
NUM_CTX             = 32768   # will be overwritten
MAX_CONTEXT_TOKENS  = 32768   # will be overwritten
MAX_TOKENS_PER_TURN = -1
INTER_TURN_SLEEP    = 2
REPEAT_THRESHOLD    = 4

_CTX_CANDIDATES = [65536, 57344, 49152, 40960, 32768, 24576, 16384]


def probe_max_ctx() -> int:
    """Try context sizes largest-first; return the biggest one Ollama accepts."""
    global NUM_CTX, MAX_CONTEXT_TOKENS
    probe_payload = {
        "model": MODEL,
        "messages": [{"role": "user", "content": "hi"}],
        "stream": False,
        "options": {"num_predict": 1},
    }
    for ctx in _CTX_CANDIDATES:
        probe_payload["options"]["num_ctx"] = ctx
        try:
            r = requests.post(f"{OLLAMA_BASE_URL}/api/chat",
                              json=probe_payload, timeout=30)
            if r.status_code == 200:
                NUM_CTX = ctx
                MAX_CONTEXT_TOKENS = ctx
                print(f"[CTX PROBE] num_ctx={ctx} tokens ✓")
                return ctx
            err = r.json().get("error", "?")
            print(f"[CTX PROBE] ctx={ctx} → {err}", file=sys.stderr)
        except Exception as e:
            print(f"[CTX PROBE] ctx={ctx} → exception: {e}", file=sys.stderr)
    # absolute fallback
    NUM_CTX = 8192
    MAX_CONTEXT_TOKENS = 8192
    print("[CTX PROBE] fallback to 8192", file=sys.stderr)
    return 8192

BREAK_MARKER    = "---BREAK---"
DONE_MARKER     = "---DONE---"
# Model emits this to report the grid state it has reached so far.
# Orchestrator reads it to keep working_grid in sync across turns.
GRID_MARKER_RE  = re.compile(r"---GRID:\s*(\[\[.*?\]\])\s*---", re.DOTALL)

LOGS_DIR.mkdir(exist_ok=True)


@dataclass
class TaskConfig:
    name: str
    system_prompt: str
    first_prompt: str
    continue_prompt: str          # fallback static prompt (unused if build_continue_prompt is set)
    max_turns: int = 60
    final_check: Callable | None = None
    extra_commit_files: list[str] = field(default_factory=list)
    # If set, called each turn with task_state to produce the dynamic continue prompt.
    build_continue_prompt: Callable | None = None
    # Mutable per-run state (e.g. current working grid). Reset in main() before the loop.
    task_state: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Sudoku task
# ---------------------------------------------------------------------------

SUDOKU_PUZZLE = [
    [0,0,0, 2,6,0, 7,0,1],
    [6,8,0, 0,7,0, 0,9,0],
    [1,9,0, 0,0,4, 5,0,0],
    [8,2,0, 1,0,0, 0,4,0],
    [0,0,4, 6,0,2, 9,0,0],
    [0,5,0, 0,0,3, 0,2,8],
    [0,0,9, 3,0,0, 0,7,4],
    [0,4,0, 0,5,0, 0,3,6],
    [7,0,3, 0,1,8, 0,0,0],
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


def _build_sudoku_continue_prompt(state: dict) -> str:
    """Build a fully-grounded continue prompt that shows the model exactly
    what has been filled so far and what it still needs to solve."""
    wg = state["working_grid"]
    filled = sum(1 for r in range(9) for c in range(9) if wg[r][c] != 0)
    empty_cells = [
        f"R{r+1}C{c+1}"
        for r in range(9) for c in range(9)
        if wg[r][c] == 0
    ]
    grid_str = format_sudoku(wg)

    # Show at most 30 empty cells in the list to keep the prompt short
    empty_summary = ", ".join(empty_cells[:30])
    if len(empty_cells) > 30:
        empty_summary += f" … (+{len(empty_cells)-30} more)"

    return textwrap.dedent(f"""
        ── CURRENT STATE ({filled}/81 cells filled, {len(empty_cells)} remaining) ──

        {grid_str}

        Remaining empty cells: {empty_summary}

        ── YOUR TASK ──
        Pick the next empty cell from the list above.
        For that cell, list every digit already present in its row, its column,
        and its 3×3 box — then state the single remaining candidate and place it.
        Show every elimination step.

        After placing one or more cells, emit the full 9×9 grid with your updates
        (use 0 for cells still empty):
            ---GRID: [[r1c1,r1c2,...,r1c9],[r2c1,...],…,[r9c1,...,r9c9]] ---
        Then write ---BREAK--- on its own line.

        When every cell is filled emit:
            ---SOLUTION: [[row1],…,[row9]] ---
        followed by:
            ---DONE---
    """).strip()


SUDOKU_TASK = TaskConfig(
    name="sudoku-solver",
    system_prompt=textwrap.dedent(f"""
        You are a careful, methodical puzzle solver.

        Solve this 9×9 Sudoku using pure logical reasoning — no code, no guessing.

        RULES:
        - Every row, column, and 3×3 box must contain digits 1–9 with no repetition.
        - 0 = empty cell.

        ORIGINAL PUZZLE (for reference — do not use these as already-solved):
        {_PUZZLE_STR}

        HOW TO REASON (follow this for every empty cell):
        1. List every digit already present in the cell's row.
        2. List every digit already present in the cell's column.
        3. List every digit already present in the cell's 3×3 box.
        4. The candidate set = {{1..9}} minus the union of the above.
        5. If exactly one candidate remains → place it.
        6. If multiple candidates remain → note them and move to a cell where
           elimination works, or use hidden-single logic across a row/column/box.

        GRID REPORTING (critical — the orchestrator relies on this):
        After placing one or more cells in a turn, you MUST emit the full 9×9
        grid showing everything you have placed so far, using 0 for cells
        still unresolved:
            ---GRID: [[r1c1,…,r1c9],[r2c1,…],…,[r9c1,…,r9c9]] ---
        Then write ---BREAK--- on its own line.

        COMPLETION:
        When every cell is filled emit exactly:
            ---SOLUTION: [[row1],…,[row9]] ---
            ---DONE---

        TOOL CALLS:
            ---TOOL: validate_sudoku {{"grid": [[r1...],[r2...],...]}}---
        Only call validate_sudoku when you believe the grid is complete.
    """).strip(),

    first_prompt=textwrap.dedent(f"""
        The puzzle is shown in the system prompt. Start with Row 1.

        For each empty cell (0) in Row 1, work through steps 1–5 from the
        HOW TO REASON section. Show every elimination explicitly.

        After you have placed all cells you can determine in this turn, emit:
            ---GRID: [[r1c1,…,r1c9],[r2c1,…],…,[r9c1,…,r9c9]] ---
        (use 0 for cells still empty)
        Then write ---BREAK--- on its own line.
    """).strip(),

    # Static fallback — not used because build_continue_prompt is set.
    continue_prompt="Continue from where you left off.",

    max_turns=60,
    final_check=_sudoku_final_check,
    extra_commit_files=["results.md"],
    build_continue_prompt=_build_sudoku_continue_prompt,
    # task_state is reset to a fresh copy of the puzzle at the start of main()
    task_state={},
)

TASK_REGISTRY = {"sudoku": SUDOKU_TASK}
ACTIVE_TASK   = "sudoku"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_reasoning_path(task_name: str) -> Path:
    ts   = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    safe = task_name.replace("-", "_").replace(" ", "_")
    return REPO_ROOT / f"reasoning_{safe}_{ts}.md"


def rough_token_count(text: str) -> int:
    return len(text) // 4


def trim_history(history: list[dict], max_tokens: int) -> list[dict]:
    """Remove oldest non-system messages until we fit within max_tokens.
    Always keeps the system message (index 0) and the most recent exchange."""
    while True:
        if sum(rough_token_count(m["content"]) for m in history) <= max_tokens:
            break
        if len(history) <= 2:
            break
        history.pop(1)
    return history


def extract_grid_update(response: str, original_puzzle: list[list[int]]) -> list[list[int]] | None:
    """Parse ---GRID: [[...]] --- from the model response and return a merged
    grid where original clues are never overwritten.
    Also falls back to ---SOLUTION: [[...]] --- if present.
    Returns None if no valid 9×9 grid is found."""
    # Try ---GRID: first, then ---SOLUTION:
    for pattern in (
        r"---GRID:\s*(\[\[.*?\]\])\s*---",
        r"---SOLUTION:\s*(\[\[.*?\]\])\s*---",
    ):
        m = re.search(pattern, response, re.DOTALL)
        if not m:
            continue
        try:
            grid = json.loads(m.group(1))
        except json.JSONDecodeError:
            continue
        if len(grid) != 9 or any(len(row) != 9 for row in grid):
            continue
        # Merge: keep original clues, accept new non-zero values for blank cells
        merged = [row[:] for row in original_puzzle]
        for r in range(9):
            for c in range(9):
                if original_puzzle[r][c] == 0 and isinstance(grid[r][c], int) and 1 <= grid[r][c] <= 9:
                    merged[r][c] = grid[r][c]
        return merged
    return None


def stream_generate(prompt: str, history: list[dict]) -> tuple[str, float]:
    """Returns (response_text, tokens_per_second)."""
    payload = {
        "model": MODEL,
        "messages": history + [{"role": "user", "content": prompt}],
        "stream": True,
        "options": {
            "num_ctx": NUM_CTX,
            "num_predict": MAX_TOKENS_PER_TURN,
            "temperature": 0.7,
            "top_p": 0.9,
            "repeat_penalty": 1.15,
            "repeat_last_n": 256,
        },
    }
    full_response = ""
    _counts: dict[str, int] = {}
    _aborted = False
    tps = 0.0

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
                    eval_count    = chunk.get("eval_count", 0)
                    eval_duration = chunk.get("eval_duration", 0)
                    if eval_duration > 0:
                        tps = eval_count / (eval_duration / 1e9)
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

    return full_response, tps


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
    """Write one turn's output to the reasoning file.
    Think-tag content is preserved in a collapsible <details> block so the
    model's internal scratchpad is visible for post-run analysis."""
    think_re    = re.compile(r"<think>(.*?)</think>", re.DOTALL)
    think_match = think_re.search(response)
    # Strip think tags and protocol markers from the visible answer
    clean = think_re.sub("", response).strip()
    clean = (clean
             .replace(BREAK_MARKER, "")
             .replace(DONE_MARKER, "")
             .replace("---GRID:", "\n*[grid update]*\n\n---GRID:")  # keep grid lines readable
             .strip())

    block = f"### Step {turn}\n\n"
    # Always write the internal scratchpad if it exists — key for debugging
    if think_match and (inner := think_match.group(1).strip()):
        block += f"<details>\n<summary>Internal scratchpad (think)</summary>\n\n{inner}\n\n</details>\n\n"
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


def log_machine_info() -> None:
    try:
        mem_kb = int(next(
            l.split()[1] for l in Path("/proc/meminfo").read_text().splitlines()
            if l.startswith("MemTotal")
        ))
        mem_gb = round(mem_kb / 1024 / 1024, 1)
    except Exception:
        mem_gb = "?"

    info = (
        f"MACHINE host={platform.node()} "
        f"os={platform.system()} {platform.release()} "
        f"arch={platform.machine()} "
        f"cpu={platform.processor() or platform.machine()} "
        f"cores={platform.os.cpu_count() if hasattr(platform, 'os') else __import__('os').cpu_count()} "
        f"ram={mem_gb}GB"
    )
    raw_log(info)


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def main() -> None:
    task           = TASK_REGISTRY[ACTIVE_TASK]
    reasoning_file = make_reasoning_path(task.name)

    # Reset mutable task state to a fresh deep copy of the original puzzle
    # so re-running the script never carries over a previous session's grid.
    task.task_state = {"working_grid": copy.deepcopy(SUDOKU_PUZZLE)}

    print(f"  Task : {task.name}")
    print(f"  Model: {MODEL}")
    print(f"  File : {reasoning_file.name}\n")

    try:
        requests.get(f"{OLLAMA_BASE_URL}/api/tags", timeout=5).raise_for_status()
        print("[OK] Ollama running\n")
    except Exception:
        print(f"[ERROR] Cannot reach Ollama at {OLLAMA_BASE_URL}")
        sys.exit(1)

    print("[PROBING] Finding max context that fits in current RAM...")
    probe_max_ctx()
    print(f"  → num_ctx={NUM_CTX}  history_budget={MAX_CONTEXT_TOKENS} tokens\n")

    init_reasoning_file(task, reasoning_file)
    log_machine_info()

    history         = [{"role": "system", "content": task.system_prompt}]
    turn            = 1
    done            = False
    current_prompt  = task.first_prompt
    pending_results : list[str] = []

    while turn <= task.max_turns and not done:
        print(f"\n── TURN {turn}/{task.max_turns} ──\n")

        # Show the orchestrator's current view of the grid before each turn
        wg = task.task_state["working_grid"]
        filled = sum(1 for r in range(9) for c in range(9) if wg[r][c] != 0)
        print(f"[STATE] {filled}/81 cells filled\n")

        raw_log(f"TURN_START turn={turn} filled={filled}/81")
        log_event("TURN_START", f"turn={turn}")

        if pending_results:
            current_prompt = f"[TOOL RESULTS]\n{chr(10).join(pending_results)}\n\n{current_prompt}"
            pending_results.clear()

        history = trim_history(history, MAX_CONTEXT_TOKENS)

        try:
            response, tps = stream_generate(current_prompt, history)
        except Exception as e:
            raw_log(f"TURN_ERROR {e}")
            break

        history.append({"role": "user",      "content": current_prompt})
        history.append({"role": "assistant", "content": response})
        append_reasoning_turn(response, turn, reasoning_file)
        raw_log(f"TURN_RESPONSE chars={len(response)} tps={tps:.1f}")
        print(f"  [{tps:.1f} tok/s]")

        # ------------------------------------------------------------------
        # Update working grid from the model's ---GRID: [[...]] --- output
        # ------------------------------------------------------------------
        grid_update = extract_grid_update(response, SUDOKU_PUZZLE)
        if grid_update:
            task.task_state["working_grid"] = grid_update
            new_filled = sum(1 for r in range(9) for c in range(9) if grid_update[r][c] != 0)
            print(f"[GRID UPDATE] now {new_filled}/81 cells filled")
            raw_log(f"GRID_UPDATE filled={new_filled}/81")
        else:
            print("[GRID] No valid ---GRID: marker found this turn — grid state unchanged.")

        for tr in run_tool_calls(response):
            line = f"Tool `{tr['tool']}`: {'OK' if tr['ok'] else 'FAIL'} — {tr['message']}"
            pending_results.append(line)
            print(f"\n[TOOL] {line}")
            raw_log(f"TOOL_RESULT {line}")

        if BREAK_MARKER in response:
            print(f"\n[BREAK] progress noted at turn {turn}")
            raw_log(f"BREAK turn={turn}")

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
                    # Build a fresh grounded prompt with the current grid
                    current_prompt = (
                        task.build_continue_prompt(task.task_state)
                        if task.build_continue_prompt
                        else task.continue_prompt
                    )
                    turn += 1
                    time.sleep(INTER_TURN_SLEEP)
                    continue
            else:
                do_commit_and_push(task, turn, reasoning_file, label="DONE")
                done = True
            break

        # Build dynamic continue prompt with the latest grid state
        current_prompt = (
            task.build_continue_prompt(task.task_state)
            if task.build_continue_prompt
            else task.continue_prompt
        )
        turn += 1
        time.sleep(INTER_TURN_SLEEP)

    status = "SOLVED" if done else "STOPPED"
    print(f"\n{status} | turns={turn-1} file={reasoning_file.name}")
    raw_log(f"SESSION_END turns={turn-1} done={done}")

    if not done:
        do_commit_and_push(task, turn - 1, reasoning_file, label="session-end")


if __name__ == "__main__":
    main()
