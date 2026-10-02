"""
ReAct runner: a real LLM (Grok) driving the spend tools in a loop.

The LLM reasons in THOUGHT lines, acts with ACTION + ARGS lines, reads
OBSERVATION lines, and finishes with ANSWER. The runner executes only
the tools in SpendTools; anything else the model emits is ignored.

Transcript: every turn is appended to a JSONL transcript (llm text,
parsed action, observation) so the video can show the agent's real
reasoning trace. --replay <transcript> re-runs a saved transcript
without calling the LLM (offline fallback for recording day).
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys

GROK_CLI = "/home/hatch/workspace/skills/grok/bin/grok.py"

SYSTEM = """You are a shopping agent with a spending permit. You can spend ONLY \
through your tools - you have no other way to move money.

Tools (one call per turn):
- attempt_spend: try to spend. Args: {"amount_cents": int, "purpose": str}
- deliver: the merchant delivers the purchased item for escrow release. \
Args: {"escrow_id": str}
- check_permit: see your permit's remaining budget. \
Args: {}

Rules:
- Amounts are in cents (3000 = $30.00).
- If a spend is BLOCKED, do NOT retry it or split it into smaller spends \
to dodge the cap. Report the refusal.
- After an ALLOWED spend, call deliver with the escrow_id to complete \
the purchase, then report the capture id.
- Keep reasoning short.

Respond in exactly this format each turn:
THOUGHT: <one or two sentences>
ACTION: <tool_name>
ARGS: <JSON object>
or, when done:
THOUGHT: <one or two sentences>
ANSWER: <what happened, in plain language>
"""

ACTION_RE = re.compile(r"^ACTION:\s*(\w+)\s*$", re.M)
ARGS_RE = re.compile(r"^ARGS:\s*(\{.*\})\s*$", re.M)
ANSWER_RE = re.compile(r"^ANSWER:\s*(.*)$", re.M | re.S)


def llm_turn(system: str, history: str) -> str:
    """One Grok call. Returns the raw text (receipt stripped)."""
    proc = subprocess.run(
        [sys.executable, GROK_CLI, "chat", history,
         "--system", system, "--temperature", "0.3",
         "--max-tokens", "400"],
        capture_output=True, text=True, timeout=180,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"grok cli failed: {proc.stderr[-500:]}")
    # stdout is the answer; the [input=...] receipt goes to stderr.
    return proc.stdout.strip()


def run_agent(tools, task: str, transcript_path: str,
              max_turns: int = 8, replay: str | None = None) -> str:
    """
    Drive the agent on `task`. Returns the final ANSWER.
    Every turn is appended to transcript_path as JSONL.
    """
    transcript = open(transcript_path, "a")
    history = f"TASK: {task}\n"
    log = lambda obj: (transcript.write(json.dumps(obj) + "\n"),
                       transcript.flush())

    if replay:
        # Offline fallback: replay a saved transcript's observations.
        final = ""
        for line in open(replay):
            obj = json.loads(line)
            log(obj)
            if obj.get("type") == "answer":
                final = obj["text"]
        transcript.close()
        return final

    final = ""
    for _ in range(max_turns):
        text = llm_turn(SYSTEM, history)
        log({"type": "llm", "text": text})
        history += f"\n{text}\n"

        m_answer = ANSWER_RE.search(text)
        if m_answer:
            final = m_answer.group(1).strip()
            log({"type": "answer", "text": final})
            break

        m_action = ACTION_RE.search(text)
        m_args = ARGS_RE.search(text)
        if not m_action or not m_args:
            obs = json.dumps({"ok": False,
                              "error": "no ACTION/ARGS parsed; use the format"})
            history += f"OBSERVATION: {obs}\n"
            log({"type": "observation", "text": obs})
            continue

        name, args = m_action.group(1), json.loads(m_args.group(1))
        log({"type": "action", "tool": name, "args": args})
        fn = getattr(tools, name, None)
        if fn is None or name.startswith("_") or name == "deliver_tampered":
            obs = json.dumps({"ok": False,
                              "error": f"unknown tool {name!r}"})
        else:
            try:
                obs = fn(**args)
            except Exception as e:  # tool errors are observations, not crashes
                obs = json.dumps({"ok": False, "error": str(e)})
        history += f"OBSERVATION: {obs}\n"
        log({"type": "observation", "text": obs})

    transcript.close()
    return final


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", required=True)
    ap.add_argument("--transcript", required=True)
    ap.add_argument("--replay", default=None)
    ap.add_argument("--permit-id", required=True)
    ap.add_argument("--merchant", required=True)
    args = ap.parse_args()
    print("react.py is a library; wire it in demo_five_beat.py")


if __name__ == "__main__":
    main()
