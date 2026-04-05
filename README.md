# The Pensieve

A DeepSeek-R1 1.5b model is given a task. It tries to complete it using pure reasoning.

---

## Current Task — Sudoku Solver

Solve a 9×9 Sudoku puzzle using only logical deduction. No code. No guessing.

---

## Process

1. The model receives the puzzle and reasons step by step — candidate elimination, row/column/box checks.
2. After placing cells in a turn it emits `---GRID: [[...]] ---` so the orchestrator can record progress.
3. The orchestrator injects the current filled grid + list of remaining empty cells into **every** continue prompt — the model always knows exactly what has been placed and what is left.
4. When it believes the puzzle is solved it emits `---SOLUTION: [[...]] ---` then `---DONE---`.
5. The solution is validated automatically. If wrong, the errors are fed back and it tries again.
6. If correct, a final commit is pushed and the session ends.

The model can also call tools mid-reasoning using `---TOOL: tool_name {args}---`.

> **Context sizing:** At startup, `thinker.py` runs a probing function (`probe_max_ctx`) that
> tries context sizes from largest to smallest — `[65536, 57344, 49152, 40960, 32768, 24576, 16384]` —
> and picks the biggest one Ollama accepts without error. This was added because requesting a fixed
> large context repeatedly caused out-of-memory crashes: llama.cpp (which Ollama uses underneath)
> allocates the KV-cache as a single contiguous block, and on machines with limited RAM that block
> can exceed available free memory. The probe finds the safe ceiling dynamically at runtime.

---

## Files

| File | What it stores |
|------|----------------|
| `thinker.py` | The reasoning loop. Streams model output, handles markers, tracks grid state, manages commits. |
| `tools.py` | Tool library — `validate_sudoku`, `git_commit`, `git_push`, `write_reasoning`, `read_reasoning`, `log_event`. |
| `reasoning_*.md` | The model's step-by-step reasoning journal, including internal `<think>` scratchpad in collapsible blocks. |
| `logs/session.log` | Structured log of every event — turns, grid updates, tool calls, commits, validation results. |


during run 1

[PROBING] Finding max context that fits in current RAM...
[CTX PROBE] num_ctx=65536 tokens ✓
  → num_ctx=65536  history_budget=65536 tokens

### Run 1 Summary

*   **Status**: Failed. The session timed out after 60 turns without determining a solution (`STOPPED`).
*   **Grid Updates Failed**: The model never emitted a valid `---GRID: [[...]] ---` marker. The orchestrator's state therefore remained permanently frozen at `36/81` cells filled, feeding the original unmodified puzzle state to the model every single turn.
*   **Truncation and Repetition**: The model continually spiraled into looping output, hitting the `[REPEAT GUARD]` heavily in nearly every turn and having its generation aborted.
*   **Missing Closing Tags**: The model generated copious amount of text inside the `<think>` tag, but was truncated or aborted before outputting the closing `</think>` tag. Consequently, the parser's regex (`<think>(.*?)</think>`) failed to match, so the internal scratchpad tokens "leaked" into the visible response file instead of being neatly captured in the `<details>` blocks, and the structured markers (like `---GRID: ...`) were completely missed or never reached.

### TODO
*   **Parser Failure**: The current regex-based parser fails when thoughts are truncated. Need to rewrite the file-writing logic to simultaneously stream output into the reasoning file as it happens, ensuring that even if the turn triggers the repeat guard or gets truncated, the thoughts are still successfully added to the file.
*   **Grid Output Issue**: Investigate why the model fails to emit `---GRID` tags properly and refine the system prompt to explicitly force it to structure the visible output correctly.