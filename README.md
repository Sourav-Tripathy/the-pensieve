# The Pensieve

> *"The mind is not a vessel to be filled, but a fire to be kindled."*  
> — Plutarch

A framework for watching **DeepSeek-R1 (1.5b)** tackle structured reasoning tasks — completely autonomously, using pure deduction and a small set of callable tools.  Every step is logged, every insight is committed to git.

---

## The Idea

Rather than feeding the model open-ended philosophical prompts, we give it **concrete, verifiable tasks** that require step-by-step reasoning.  The model works in a loop, calling tools when it needs them, writing its reasoning to a journal, and committing progress to this repo at every natural checkpoint.

The current task: **solve a 9×9 Sudoku puzzle using pure logical deduction — no programs, no guessing**.

---

## Current Task — Sudoku Solver

The model is given a classic medium-difficulty Sudoku:

```
+-------+-------+-------+
| 5 3 _ | _ 7 _ | _ _ _ |
| 6 _ _ | 1 9 5 | _ _ _ |
| _ 9 8 | _ _ _ | _ 6 _ |
+-------+-------+-------+
| 8 _ _ | _ 6 _ | _ _ 3 |
| 4 _ _ | 8 _ 3 | _ _ 1 |
| 7 _ _ | _ 2 _ | _ _ 6 |
+-------+-------+-------+
| _ 6 _ | _ _ _ | 2 8 _ |
| _ _ _ | 4 1 9 | _ _ 5 |
| _ _ _ | _ 8 _ | _ 7 9 |
+-------+-------+-------+
```

**Rules the model must follow:**
- Reason step-by-step from the given clues only
- Use candidate elimination (naked singles, hidden singles)
- No trial-and-error / backtracking guessing
- Call `validate_sudoku` tool when it believes the grid is complete
- Emit `---SOLUTION: [[...]] ---` then `---DONE---` when finished

---

## Architecture

```
thinker.py          ← task-agnostic reasoning loop
    │
    ├─ TASK_REGISTRY  ← plug in new tasks here (change ACTIVE_TASK)
    │
    ├─ streams tokens from DeepSeek-R1:1.5b via Ollama
    │
    ├─ detects ---TOOL: <name> {args}--- calls → dispatches via tools.py
    │
    ├─ writes each reasoning step to reasoning.md
    │
    ├─ commits + pushes on every ---BREAK--- marker
    │
    └─ runs final_check() on ---DONE---, commits if verified ✅

tools.py            ← tool library (callable by the model)
    ├─ validate_sudoku  → checks if a 9×9 grid is correctly solved
    ├─ write_reasoning  → append a block to reasoning.md
    ├─ read_reasoning   → returns current reasoning.md contents
    ├─ git_commit       → stage + commit files
    ├─ git_push         → push to origin
    └─ log_event        → write a structured entry to logs/session.log

reasoning.md        ← the model's step-by-step journal (auto-generated)
logs/session.log    ← structured log of all loop events
```

---

## Adding a New Task

1. Define a `TaskConfig` instance in `thinker.py` with:
   - `system_prompt` — role & rules
   - `first_prompt` / `continue_prompt` / `done_prompt`
   - `final_check` — optional validation function
   - `commit_files` — which files to stage on commit
2. Add it to `TASK_REGISTRY`
3. Set `ACTIVE_TASK = "your_task_name"`
4. Run `python thinker.py`

---

## Setup

```bash
# 1. Make sure Ollama is running
ollama serve

# 2. Pull the model (if you haven't already)
ollama pull deepseek-r1:1.5b

# 3. Install Python dependency
pip install requests

# 4. Run a task
python thinker.py
```

Reasoning accumulates in [`reasoning.md`](./reasoning.md).  
Session events are logged to [`logs/session.log`](./logs/session.log).

---

## Files

| File | Purpose |
|------|---------|
| `thinker.py` | Task-agnostic reasoning loop + task registry |
| `tools.py` | Tool library callable by the model |
| `reasoning.md` | Auto-generated reasoning journal for the current task |
| `logs/session.log` | Structured event log for the running session |
| `README.md` | This file |

---

## Task History

| # | Task | Status | Commits |
|---|------|--------|---------|
| 1 | Sudoku Solver (medium) | 🔄 In progress | — |

---

## Why?

Because watching a 1.5-billion-parameter model work through a logic puzzle — cell by cell, elimination by elimination, with every doubt and correction committed to git — is a surprisingly compelling window into how structured reasoning actually works (or fails) at small scale.

The commits are the breadcrumbs.  The reasoning.md is the proof of work.
