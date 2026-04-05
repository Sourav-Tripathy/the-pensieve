#!/usr/bin/env python3
"""
tools.py — The Pensieve Tool Library

A collection of callable tools that the reasoning loop (thinker.py) can invoke.
Each tool is a plain function that returns a dict:
    {"ok": bool, "result": <any>, "message": str}

Tools are registered in the TOOL_REGISTRY dict at the bottom of this file.
The model can request a tool call by emitting a line like:
    ---TOOL: <tool_name> <json_args>---

thinker.py will parse that line, call the tool, and inject the result back
into the model's next prompt.
"""

import json
import re
import subprocess
import logging
from datetime import datetime, timezone
from pathlib import Path

# ─────────────────────────────────────────────
# PATHS  (relative to this file's directory)
# ─────────────────────────────────────────────
REPO_ROOT      = Path(__file__).parent
REASONING_FILE = REPO_ROOT / "reasoning.md"
LOGS_DIR       = REPO_ROOT / "logs"
LOGS_FILE      = LOGS_DIR / "session.log"

# ─────────────────────────────────────────────
# LOGGER SETUP
# ─────────────────────────────────────────────
LOGS_DIR.mkdir(exist_ok=True)
logging.basicConfig(
    filename=str(LOGS_FILE),
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)
logger = logging.getLogger("pensieve")


def _ok(result, message: str = "") -> dict:
    return {"ok": True, "result": result, "message": message}


def _err(message: str) -> dict:
    return {"ok": False, "result": None, "message": message}


# ─────────────────────────────────────────────
# TOOL: log_event
# ─────────────────────────────────────────────

def log_event(event: str, detail: str = "") -> dict:
    """
    Write a structured entry to logs/session.log.

    Args:
        event  : short event name, e.g. "TURN_START", "TOOL_CALL", "COMMIT"
        detail : free-form detail string
    """
    msg = f"[{event}] {detail}" if detail else f"[{event}]"
    logger.info(msg)
    return _ok(None, msg)


# ─────────────────────────────────────────────
# TOOL: write_reasoning
# ─────────────────────────────────────────────

def write_reasoning(text: str, turn: int = 0, append: bool = True) -> dict:
    """
    Write or append a reasoning block to reasoning.md.

    Args:
        text   : the text to write
        turn   : turn number (0 = header write)
        append : if False, overwrite the file completely
    """
    try:
        mode = "a" if append else "w"
        with open(REASONING_FILE, mode, encoding="utf-8") as f:
            if turn:
                f.write(f"\n### Step {turn}\n\n")
            f.write(text.strip())
            f.write("\n\n")
        log_event("WRITE_REASONING", f"turn={turn} chars={len(text)}")
        return _ok(None, "reasoning.md updated")
    except OSError as e:
        return _err(f"write_reasoning failed: {e}")


# ─────────────────────────────────────────────
# TOOL: read_reasoning
# ─────────────────────────────────────────────

def read_reasoning() -> dict:
    """
    Return the current contents of reasoning.md.
    Useful for the model to re-read its own prior steps.
    """
    try:
        if not REASONING_FILE.exists():
            return _ok("", "reasoning.md does not exist yet")
        content = REASONING_FILE.read_text(encoding="utf-8")
        return _ok(content, f"read {len(content)} chars from reasoning.md")
    except OSError as e:
        return _err(f"read_reasoning failed: {e}")


# ─────────────────────────────────────────────
# TOOL: validate_sudoku
# ─────────────────────────────────────────────

def validate_sudoku(grid: list[list[int]]) -> dict:
    """
    Check whether a 9×9 Sudoku grid is correctly solved.

    Args:
        grid : 9×9 list of lists with ints 1-9 (0 = empty cell)

    Returns:
        ok      : True if perfectly solved, False otherwise
        result  : dict with "solved", "errors" keys
        message : human-readable verdict
    """
    if not isinstance(grid, list) or len(grid) != 9:
        return _err("grid must be a 9×9 list-of-lists")
    for row in grid:
        if not isinstance(row, list) or len(row) != 9:
            return _err("each row must have exactly 9 integers")

    errors = []
    digits = set(range(1, 10))

    # Check rows
    for r, row in enumerate(grid):
        if set(row) != digits:
            errors.append(f"Row {r+1} invalid: {row}")

    # Check columns
    for c in range(9):
        col = [grid[r][c] for r in range(9)]
        if set(col) != digits:
            errors.append(f"Col {c+1} invalid: {col}")

    # Check 3×3 boxes
    for box_r in range(3):
        for box_c in range(3):
            box = [
                grid[box_r * 3 + dr][box_c * 3 + dc]
                for dr in range(3)
                for dc in range(3)
            ]
            if set(box) != digits:
                errors.append(
                    f"Box ({box_r+1},{box_c+1}) invalid: {box}"
                )

    has_empty = any(cell == 0 for row in grid for cell in row)
    if has_empty:
        errors.append("Grid contains empty cells (0s)")

    solved = len(errors) == 0
    verdict = "✅ Sudoku solved correctly!" if solved else f"❌ Not solved — {len(errors)} error(s)"
    log_event("VALIDATE_SUDOKU", f"solved={solved} errors={len(errors)}")
    return {"ok": solved, "result": {"solved": solved, "errors": errors}, "message": verdict}


# ─────────────────────────────────────────────
# TOOL: git_commit
# ─────────────────────────────────────────────

def git_commit(message: str, files: list[str] | None = None) -> dict:
    """
    Stage files and commit to the local git repo.

    Args:
        message : commit message
        files   : list of relative file paths to stage (default: stage all tracked changes)
    """
    try:
        if files:
            subprocess.run(
                ["git", "add"] + files,
                cwd=REPO_ROOT, check=True, capture_output=True
            )
        else:
            subprocess.run(
                ["git", "add", "-u"],
                cwd=REPO_ROOT, check=True, capture_output=True
            )
        result = subprocess.run(
            ["git", "commit", "-m", message],
            cwd=REPO_ROOT, check=True, capture_output=True, text=True
        )
        log_event("GIT_COMMIT", message)
        return _ok(result.stdout.strip(), f"committed: {message}")
    except subprocess.CalledProcessError as e:
        err = e.stderr.strip() if e.stderr else str(e)
        log_event("GIT_COMMIT_FAIL", err)
        return _err(f"git commit failed: {err}")


# ─────────────────────────────────────────────
# TOOL: git_push
# ─────────────────────────────────────────────

def git_push() -> dict:
    """Push committed changes to the remote origin."""
    try:
        result = subprocess.run(
            ["git", "push"],
            cwd=REPO_ROOT, check=True, capture_output=True, text=True
        )
        log_event("GIT_PUSH", "pushed to origin")
        return _ok(result.stdout.strip(), "pushed to origin")
    except subprocess.CalledProcessError as e:
        err = e.stderr.strip() if e.stderr else str(e)
        log_event("GIT_PUSH_FAIL", err)
        return _err(f"git push failed: {err}")


# ─────────────────────────────────────────────
# TOOL REGISTRY
# ─────────────────────────────────────────────

TOOL_REGISTRY: dict[str, callable] = {
    "log_event":       log_event,
    "write_reasoning": write_reasoning,
    "read_reasoning":  read_reasoning,
    "validate_sudoku": validate_sudoku,
    "git_commit":      git_commit,
    "git_push":        git_push,
}


# ─────────────────────────────────────────────
# TOOL DISPATCHER
# ─────────────────────────────────────────────

def dispatch(tool_name: str, args: dict) -> dict:
    """
    Call a registered tool by name with the given args dict.

    Returns the tool's result dict, or an error dict if the tool is unknown.
    """
    if tool_name not in TOOL_REGISTRY:
        return _err(f"Unknown tool: '{tool_name}'. Available: {list(TOOL_REGISTRY)}")
    try:
        return TOOL_REGISTRY[tool_name](**args)
    except TypeError as e:
        return _err(f"Tool '{tool_name}' argument error: {e}")


# ─────────────────────────────────────────────
# TOOL CALL PARSER
# ─────────────────────────────────────────────

# The model signals a tool call with:
#   ---TOOL: tool_name {"arg1": val1, "arg2": val2}---
TOOL_CALL_RE = re.compile(
    r"---TOOL:\s*(\w+)\s*(.*?)---",
    re.DOTALL
)


def extract_tool_calls(text: str) -> list[tuple[str, dict]]:
    """
    Parse all tool-call markers from a text block.

    Returns a list of (tool_name, args_dict) tuples in order of appearance.
    """
    calls = []
    for m in TOOL_CALL_RE.finditer(text):
        name = m.group(1).strip()
        raw_args = m.group(2).strip()
        try:
            args = json.loads(raw_args) if raw_args else {}
        except json.JSONDecodeError:
            args = {"_raw": raw_args}
        calls.append((name, args))
    return calls


def run_tool_calls(text: str) -> list[dict]:
    """
    Find all tool calls in text, execute them, and return a list of result dicts,
    each augmented with the tool name.
    """
    results = []
    for name, args in extract_tool_calls(text):
        log_event("TOOL_CALL", f"{name} args={args}")
        res = dispatch(name, args)
        res["tool"] = name
        results.append(res)
    return results


# ─────────────────────────────────────────────
# SUDOKU UTILITIES  (helpers for the puzzle)
# ─────────────────────────────────────────────

def format_sudoku(grid: list[list[int]]) -> str:
    """Return a nicely formatted string representation of a Sudoku grid."""
    lines = []
    border = "+-------+-------+-------+"
    for r, row in enumerate(grid):
        if r % 3 == 0:
            lines.append(border)
        parts = []
        for c, val in enumerate(row):
            if c % 3 == 0:
                parts.append("|")
            parts.append(" " if val == 0 else str(val))
        parts.append("|")
        lines.append(" ".join(parts))
    lines.append(border)
    return "\n".join(lines)


if __name__ == "__main__":
    # Quick self-test
    test_grid = [
        [5,3,4,6,7,8,9,1,2],
        [6,7,2,1,9,5,3,4,8],
        [1,9,8,3,4,2,5,6,7],
        [8,5,9,7,6,1,4,2,3],
        [4,2,6,8,5,3,7,9,1],
        [7,1,3,9,2,4,8,5,6],
        [9,6,1,5,3,7,2,8,4],
        [2,8,7,4,1,9,6,3,5],
        [3,4,5,2,8,6,1,7,9],
    ]
    print(format_sudoku(test_grid))
    res = validate_sudoku(test_grid)
    print(res["message"])
