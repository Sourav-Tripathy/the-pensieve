#!/usr/bin/env python3
"""
thinker.py — The Pensieve

An autonomous reasoning loop that feeds a thought experiment to DeepSeek-R1 (1.5b via Ollama),
streams its internal monologue, writes it to a markdown journal, and commits to GitHub
whenever the model reaches a natural stopping point.

Design decisions:
  - Uses Ollama's /api/generate endpoint with streaming=True so we see tokens as they arrive.
  - Context window is managed by keeping a rolling transcript capped at MAX_CONTEXT_TOKENS.
    When the transcript exceeds the cap we drop the oldest assistant turn (not the system prompt).
  - A "break" is detected when the model emits one of the BREAK_MARKERS strings, signalling
    it has finished a complete thought. We then commit the file to GitHub.
  - The model is asked to emit "---BREAK---" when it wants to pause and reflect.
  - We impose a hard MAX_TURNS guard so the script eventually stops on its own.

Usage:
    python thinker.py
"""

import os
import re
import sys
import time
import subprocess
import textwrap
from datetime import datetime, timezone
from pathlib import Path

import requests  # pip install requests

# ─────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────
OLLAMA_BASE_URL   = "http://localhost:11434"
MODEL             = "deepseek-r1:1.5b"

# The markdown file where the thought experiment is recorded
OUTPUT_FILE       = Path(__file__).parent / "thoughts.md"

# Git repo root (same dir as this script)
REPO_ROOT         = Path(__file__).parent

# How many tokens (rough estimate) to allow in rolling context
# deepseek-r1:1.5b has a 4096-token context window
MAX_CONTEXT_TOKENS = 2500   # leave headroom for the response

# Max tokens the model may generate in a single turn
MAX_TOKENS_PER_TURN = 600

# If the same ~sentence appears this many times in one turn → abort that turn
REPEAT_THRESHOLD = 3

# Max number of model turns before the script stops
MAX_TURNS         = 40

# Seconds to wait between turns so Ollama doesn't overheat
INTER_TURN_SLEEP  = 2

# The string the model emits to signal a break / commit point
BREAK_MARKER      = "---BREAK---"

# ─────────────────────────────────────────────
# THE THOUGHT EXPERIMENT PROMPT
# ─────────────────────────────────────────────
SYSTEM_PROMPT = textwrap.dedent("""
You are playing a role. Stay in character at all times.

You are a human. It is 1000 years ago.

You have never read a book. You have never heard the word "atom" or "matter" or
"molecule". You do not know what things are made of. Nobody does.

You live near a forest. You have fire. You have water. You have stone, wood,
bone, ash, mud, and your own two hands. You eat fruit and kill animals. You have
survived this way your whole life.

But you are curious. Unusually so.

Today you sit by your fire and you begin to wonder — not about gods, not about
spirits — but about the things themselves. The fire. The wood. The stone. The
water. What are they, really? What are they made of? Is there something
underneath all of it?

You have no instruments. No one to ask. Only what you can see, touch, smell,
taste, and think.

YOUR ONLY RULES:
- Reason only from what you can directly observe with your senses.
- Never use modern words or concepts you could not have invented yourself.
- Think out loud. Show every step. Show every doubt.
- When you are unsure, say so — then push through it anyway.
- Run mental experiments. Break things in your mind. Burn things. Boil things. Crush things.
- Follow every observation to its deepest possible question.
- Do not rush to a conclusion. Earn it.

IMPORTANT FORMATTING RULE:
When you feel you have completed one complete thought — one round of wondering,
observing, and reasoning — output the exact string "---BREAK---" on its own line.
This marks a natural resting point. You may then continue in the next turn, or
you may end your thinking entirely. Each segment between breaks should be rich,
detailed, and at least a paragraph long.

Begin. Sit by your fire. Look around you. Start with one thing you can see
right now — and begin to wonder about it.
""").strip()

CONTINUE_PROMPT = textwrap.dedent("""
Good. Take a breath. Continue from where you left off.
Push deeper into whatever question you were following.
Find a new angle. Find a new doubt. Chase it.
Remember: reason only from your senses, never use modern concepts.
When you feel a thought is complete, write ---BREAK--- on its own line.
""").strip()


# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────

def rough_token_count(text: str) -> int:
    """Very rough approximation: 1 token ≈ 4 characters."""
    return len(text) // 4


def build_messages(history: list[dict]) -> list[dict]:
    """
    Build the messages list for the Ollama chat endpoint.
    history is a list of {"role": "...", "content": "..."} dicts.
    """
    return history


def trim_history(history: list[dict], max_tokens: int) -> list[dict]:
    """
    Drop oldest assistant+user pairs (but never the system message) until
    the total estimated token count is below max_tokens.
    """
    if not history:
        return history

    # Always keep the system message (index 0) and the latest user message
    system_msg = history[0]  # role == "system"

    while True:
        total = sum(rough_token_count(m["content"]) for m in history)
        if total <= max_tokens:
            break
        # Find the oldest non-system message and remove it
        # We need at least system + one user message to function
        if len(history) <= 2:
            break
        # Remove the second element (oldest non-system)
        history.pop(1)

    return history


def stream_generate(prompt: str, history: list[dict]) -> str:
    """
    Send a chat request to Ollama with streaming and return the full response text.
    Prints tokens to stdout in real time.
    """
    messages = build_messages(history + [{"role": "user", "content": prompt}])

    url = f"{OLLAMA_BASE_URL}/api/chat"
    payload = {
        "model": MODEL,
        "messages": messages,
        "stream": True,
        "options": {
            "num_ctx": 4096,
            "num_predict": MAX_TOKENS_PER_TURN,   # hard cap per turn
            "temperature": 0.8,
            "top_p": 0.92,
            "repeat_penalty": 1.35,               # strong repetition suppression
            "repeat_last_n": 128,
        },
        "stop": [BREAK_MARKER],                   # Ollama stops generation at break
    }

    import json

    full_response = ""
    # Rolling window of the last ~200 chars for live repetition detection
    _window: list[str] = []
    _sentence_counts: dict[str, int] = {}
    _aborted = False

    def _check_repetition(text: str) -> bool:
        """Return True if we should abort — same sentence seen REPEAT_THRESHOLD times."""
        # Split on sentence-ending punctuation to get rough sentences
        sentences = re.split(r'(?<=[.!?\n])\s+', text.strip())
        for s in sentences:
            s = s.strip().lower()
            if len(s) < 20:          # ignore very short fragments
                continue
            _sentence_counts[s] = _sentence_counts.get(s, 0) + 1
            if _sentence_counts[s] >= REPEAT_THRESHOLD:
                return True
        return False

    try:
        with requests.post(url, json=payload, stream=True, timeout=300) as resp:
            resp.raise_for_status()
            for line in resp.iter_lines():
                if not line:
                    continue
                chunk = json.loads(line)
                token = chunk.get("message", {}).get("content", "")
                if token:
                    print(token, end="", flush=True)
                    full_response += token
                    # Live repetition check on accumulated text
                    if len(full_response) % 200 < len(token):  # check every ~200 chars
                        if _check_repetition(full_response):
                            print("\n[REPEAT GUARD] Loop detected — aborting this turn.",
                                  file=sys.stderr)
                            _aborted = True
                            break
                if chunk.get("done"):
                    break
    except requests.exceptions.RequestException as e:
        print(f"\n[ERROR] Ollama request failed: {e}", file=sys.stderr)
        raise

    print()  # newline after streaming ends

    if _aborted:
        # Trim the repeated tail so we don't write garbage to the file
        # Keep only content up to the first repeated sentence
        lines = full_response.split("\n")
        seen: set[str] = set()
        clean_lines: list[str] = []
        for ln in lines:
            key = ln.strip().lower()
            if key and key in seen and len(key) > 20:
                break
            clean_lines.append(ln)
            if key:
                seen.add(key)
        full_response = "\n".join(clean_lines)
        full_response += "\n\n---BREAK---"  # force a break so we commit what we have

    return full_response


def git_commit(message: str) -> None:
    """Stage the thoughts file and commit to git."""
    try:
        subprocess.run(
            ["git", "add", str(OUTPUT_FILE.relative_to(REPO_ROOT))],
            cwd=REPO_ROOT, check=True, capture_output=True
        )
        subprocess.run(
            ["git", "commit", "-m", message],
            cwd=REPO_ROOT, check=True, capture_output=True
        )
        print(f"\n[GIT] Committed: {message}")
    except subprocess.CalledProcessError as e:
        print(f"\n[GIT WARNING] Commit failed: {e.stderr.decode().strip()}", file=sys.stderr)


def git_push() -> None:
    """Push committed changes to origin."""
    try:
        subprocess.run(
            ["git", "push"],
            cwd=REPO_ROOT, check=True, capture_output=True
        )
        print("[GIT] Pushed to origin.")
    except subprocess.CalledProcessError as e:
        print(f"\n[GIT WARNING] Push failed: {e.stderr.decode().strip()}", file=sys.stderr)


def init_output_file(prompt_title: str) -> None:
    """Create or append a header to the thoughts markdown file."""
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    if not OUTPUT_FILE.exists():
        with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
            f.write(f"# The Pensieve — Thought Experiments\n\n")
            f.write(f"> *An AI mind reasoning through human history's deepest questions,*  \n")
            f.write(f"> *one observation at a time.*\n\n")
            f.write(f"---\n\n")

    with open(OUTPUT_FILE, "a", encoding="utf-8") as f:
        f.write(f"\n## {prompt_title}\n\n")
        f.write(f"*Session started: {timestamp}*  \n")
        f.write(f"*Model: {MODEL}*\n\n")
        f.write(f"---\n\n")


def append_thought(text: str, turn: int) -> None:
    """Append a thought segment to the output markdown file."""
    # Strip the BREAK_MARKER from the text before writing
    clean_text = text.replace(BREAK_MARKER, "").strip()
    if not clean_text:
        return

    # Strip <think>...</think> tags (DeepSeek-R1's internal scratchpad)
    # We keep them as a collapsible section for transparency
    think_pattern = re.compile(r"<think>(.*?)</think>", re.DOTALL)
    think_match = think_pattern.search(clean_text)

    if think_match:
        inner_thought = think_match.group(1).strip()
        visible_text = think_pattern.sub("", clean_text).strip()

        with open(OUTPUT_FILE, "a", encoding="utf-8") as f:
            f.write(f"### Turn {turn}\n\n")
            if inner_thought:
                f.write(f"<details>\n<summary>🧠 Internal reasoning</summary>\n\n")
                f.write(f"{inner_thought}\n\n")
                f.write(f"</details>\n\n")
            if visible_text:
                f.write(f"{visible_text}\n\n")
            f.write(f"---\n\n")
    else:
        with open(OUTPUT_FILE, "a", encoding="utf-8") as f:
            f.write(f"### Turn {turn}\n\n")
            f.write(f"{clean_text}\n\n")
            f.write(f"---\n\n")


# ─────────────────────────────────────────────
# MAIN LOOP
# ─────────────────────────────────────────────

def main() -> None:
    print("=" * 60)
    print("  THE PENSIEVE — Autonomous Thought Experiment Runner")
    print(f"  Model : {MODEL}")
    print(f"  Output: {OUTPUT_FILE}")
    print("=" * 60)
    print()

    # Check Ollama is running
    try:
        r = requests.get(f"{OLLAMA_BASE_URL}/api/tags", timeout=5)
        r.raise_for_status()
        print("[OK] Ollama is running.\n")
    except Exception:
        print("[ERROR] Cannot reach Ollama at", OLLAMA_BASE_URL)
        print("        Make sure `ollama serve` is running.")
        sys.exit(1)

    # Initialise markdown file
    experiment_title = "What Are Things Made Of? — A Prehistoric Mind Wonders"
    init_output_file(experiment_title)

    # Build the initial conversation history
    history: list[dict] = [
        {"role": "system", "content": SYSTEM_PROMPT}
    ]

    turn = 1
    commits_made = 0

    first_prompt = (
        "Begin. Sit by your fire. Look around you. "
        "Start with one thing you can see right now — and begin to wonder about it. "
        "Remember to write ---BREAK--- when you've completed one whole thought."
    )

    current_prompt = first_prompt

    while turn <= MAX_TURNS:
        print(f"\n{'─' * 60}")
        print(f"  TURN {turn} / {MAX_TURNS}")
        print(f"{'─' * 60}\n")

        # Trim history to stay within context limits
        history = trim_history(history, MAX_CONTEXT_TOKENS)

        # Stream the response
        try:
            response = stream_generate(current_prompt, history)
        except Exception as e:
            print(f"\n[FATAL] Could not get response: {e}")
            break

        # Update history with the exchange
        history.append({"role": "user", "content": current_prompt})
        history.append({"role": "assistant", "content": response})

        # Write to the markdown file
        append_thought(response, turn)

        # Check for break marker
        hit_break = BREAK_MARKER in response

        if hit_break:
            commit_msg = (
                f"pensieve: turn {turn} — thought segment committed\n\n"
                f"[auto-commit by thinker.py at "
                f"{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}]"
            )
            git_commit(commit_msg)
            git_push()
            commits_made += 1
            print(f"\n[BREAK] Model took a breath. Commits so far: {commits_made}")
            time.sleep(INTER_TURN_SLEEP)

        # If the model didn't emit a break but we're past turn 1, nudge it
        # to either continue or wrap up
        current_prompt = CONTINUE_PROMPT
        turn += 1

        # Small rest between turns to avoid overwhelming Ollama
        time.sleep(INTER_TURN_SLEEP)

    # Final commit of whatever remains
    print(f"\n{'=' * 60}")
    print(f"  DONE — {turn - 1} turns completed, {commits_made} auto-commits made.")
    print(f"  Thoughts written to: {OUTPUT_FILE}")
    print(f"{'=' * 60}")

    final_msg = (
        f"pensieve: final commit — {turn - 1} turns, {commits_made} break commits\n\n"
        f"[session ended at {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}]"
    )
    git_commit(final_msg)
    git_push()


if __name__ == "__main__":
    main()
