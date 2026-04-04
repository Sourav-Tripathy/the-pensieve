# The Pensieve 

> *"The mind is not a vessel to be filled, but a fire to be kindled."*  
> — Plutarch

 A 1.5B DeepSeek-R1 model is left alone with a
philosophical prompt — stripped of all modern knowledge, asked to reason only from
its senses — and told to think out loud.

Its entire journey: the doubts, the false starts, the sudden clarity, the loops back
into confusion — all written to a markdown journal and committed to this repo each time
the model reaches a natural resting point in its thinking.

---

## The Experiment

> You are a human. It is 1000 years ago.  
> You have never read a book. You have never heard the word "atom" or "matter".  
> You do not know what things are made of. Nobody does.  
> You live near a forest. You have fire. You have water. You have stone, wood,  
> bone, ash, mud, and your own two hands.  
>  
> But you are curious. Unusually so.  
>  
> Today you sit by your fire and begin to wonder — not about gods, not about spirits  
> — but about the **things themselves**. The fire. The wood. The stone. The water.  
> What are they, really? What are they made of?

**Rules the model must follow:**
- Reason only from what can be directly observed with the senses
- Never use modern words or concepts it could not have invented itself
- Think out loud — show every step, every doubt
- Run mental experiments — burn things, boil things, crush things in its mind
- Follow every observation to its deepest possible question
- Do not rush to a conclusion. Earn it.

---

## How It Works

```
thinker.py
    │
    ├─ Sends the thought experiment to DeepSeek-R1:1.5b via Ollama
    │    (streaming, so you see tokens in real time)
    │
    ├─ Manages a rolling context window (≤ 3000 tokens)
    │    to stay within the model's 4096-token limit
    │
    ├─ Detects ---BREAK--- markers that the model emits
    │    when it has completed a full thought segment
    │
    ├─ Writes each turn to thoughts.md with internal
    │    reasoning hidden in collapsible <details> blocks
    │
    └─ Commits + pushes to GitHub on every break
```

The model can take up to 40 turns before the session ends automatically.

---

## Setup

```bash
# 1. Make sure Ollama is running
ollama serve

# 2. Pull the model (if you haven't already)
ollama pull deepseek-r1:1.5b

# 3. Install Python dependency
pip install requests

# 4. Run the thinker
python thinker.py
```

The thoughts accumulate in [`thoughts.md`](./thoughts.md).

---

## Files

| File | Purpose |
|------|---------|
| `thinker.py` | Main runner — prompt, stream, write, commit |
| `thoughts.md` | Auto-generated journal of the AI's thought experiment |
| `README.md` | This file |

---

## Why?

Because the best way to understand how humans once wrestled with the deepest
questions of nature — *what is fire? what is stone? is there something beneath
all things?* — might be to watch a mind do it again, from scratch, constrained
to nothing but its senses and its stubbornness.

The commits are the breadcrumbs. The markdown is the journey.
