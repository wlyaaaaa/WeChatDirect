"""Timing and stop rules for sending chat bubbles at a fast human typing pace.

Pure logic: wechat_cli wires in the real reader and native send. The first bubble is
immediate; later bubbles wait for thinking plus typing time. New messages
stop the run and hand the unsent bubbles back; an uncertain send stops and is never resent.
"""
import random
import re
import time

CHARS_PER_MINUTE = 100.0
NEXT_THINK_SECONDS = (0.5, 2.0)
EMOJI_SECONDS = 1.5  # picking one from the emoji panel
TYPING_SPREAD = (0.7, 1.3)  # each bubble gets its own speed, so equal lengths do not take equal time
CHECK_LEAD_SECONDS = 2.0  # one local context read takes about this long
_EMOJI = re.compile(r"\[[^\[\]]{1,6}\]")  # WeChat built-in emoji written as [name]


def typing_seconds(text, chars_per_minute=CHARS_PER_MINUTE):
    plain = _EMOJI.sub("", text)
    return len(plain) * 60 / chars_per_minute + len(_EMOJI.findall(text)) * EMOJI_SECONDS


def make_plan(bubbles, *, chars_per_minute=CHARS_PER_MINUTE, next_think=NEXT_THINK_SECONDS,
              typing_spread=TYPING_SPREAD, rng=random):
    plan = []
    for index, text in enumerate(bubbles):
        think = rng.uniform(*next_think) if index else 0
        typing = typing_seconds(text, chars_per_minute) * rng.uniform(*typing_spread) if index else 0
        plan.append({"text": text, "thinkSeconds": round(think, 1), "typingSeconds": round(typing, 1),
                     "waitSeconds": round(think + typing, 1)})
    return plan


def run_plan(plan, *, started_at, send, newer_messages, now=time.time, sleep=time.sleep, stopped=lambda: False):
    """Send each step at its due time; newer_messages(index) lists what stops the run."""
    sent = []
    last = started_at
    for index, step in enumerate(plan):
        unsent = [item["text"] for item in plan[index:]]
        due = last + step["waitSeconds"]
        sleep(max(0.0, due - CHECK_LEAD_SECONDS - now()))
        try:
            newer = newer_messages(index)
        except Exception as exc:
            return {"status": "check_failed", "error": str(exc), "sent": sent, "unsent": unsent}
        if newer:
            return {"status": "chat_moved", "new": newer, "sent": sent, "unsent": unsent}
        sleep(max(0.0, due - now()))
        if stopped():
            return {"status": "stopped", "sent": sent, "unsent": unsent}
        try:
            receipt = send(step["text"])
        except Exception as exc:
            details = getattr(exc, "details", {})
            if details.get("requiredAction"):  # raised before the native call, so this bubble did not go out
                return {"status": "login_required", "error": str(exc), **details, "sent": sent, "unsent": unsent}
            receipt = {"status": "unknown", "error": str(exc)}
        last = now()
        if receipt.get("status") != "success" or receipt.get("wechatAccepted") is not True:
            return {"status": "send_uncertain", "receipt": receipt, "sent": sent,
                    "uncertain": step["text"], "unsent": unsent[1:]}
        sent.append({"text": step["text"], "acceptedAt": last})
    return {"status": "sent", "sent": sent}
