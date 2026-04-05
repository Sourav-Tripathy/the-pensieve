# The Pensieve

A DeepSeek-R1 1.5b model is given a task. It tries to complete it using pure reasoning.

---

## Current Task — Sudoku Solver

Solve a 9×9 Sudoku puzzle using only logical deduction. No code. No guessing.

---

## Process

1. The model receives the puzzle and reasons step by step — candidate elimination, row/column/box checks.
2. When it places a set of cells it writes `---BREAK---` → triggers a git commit of its progress.
3. When it believes the puzzle is solved it emits `---SOLUTION: [[...]] ---` then `---DONE---`.
4. The solution is validated automatically. If wrong, the errors are fed back and it tries again.
5. If correct, a final commit is pushed and the session ends.

The model can also call tools mid-reasoning using `---TOOL: tool_name {args}---`.

---

## Files

| File | What it stores |
|------|----------------|
| `thinker.py` | The reasoning loop. Streams model output, handles markers, runs tool calls, manages commits. |
| `tools.py` | Tool library — `validate_sudoku`, `git_commit`, `git_push`, `write_reasoning`, `read_reasoning`, `log_event`. |
| `reasoning.md` | The model's step-by-step reasoning journal. Written turn by turn during the session. |
| `logs/session.log` | Structured log of every event — turns, tool calls, commits, validation results. |
