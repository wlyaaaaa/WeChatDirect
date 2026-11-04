"""Timing and stop rules for sending chat bubbles at a fast human typing pace.

Pure logic: wechat_cli wires in the real reader and native send. Every bubble waits for
thinking plus typing time, including the first one after a scheduled start. New messages
stop the run and hand the unsent bubbles back; an uncertain send stops and is never resent.
"""
import random
import re
import time

CHARS_PER_MINUTE = 100.0
FIRST_THINK_SECONDS = (6.0, 15.0)
NEXT_THINK_SECONDS = (1.0, 3.0)
EMOJI_SECONDS = 2.0  # picking one from the emoji panel
CHECK_LEAD_SECONDS = 2.0  # one local context read takes about this long
_EMOJI = re.compile(r"\[[^\[\]]{1,6}\]")  # WeChat built-in emoji written as [name]


def typing_seconds(text, chars_per_minute=CHARS_PER_MINUTE):
    plain = _EMOJI.sub("", text)
    return len(plain) * 60 / chars_per_minute + len(_EMOJI.findall(text)) * EMOJI_SECONDS


def make_plan(bubbles, *, chars_per_minute=CHARS_PER_MINUTE, first_think=FIRST_THINK_SECONDS,
              next_think=NEXT_THINK_SECONDS, rng=random):
    plan = []
    for index, text in enumerate(bubbles):
        think = rng.uniform(*(first_think if index == 0 else next_think))
        typing = typing_seconds(text, chars_per_minute)
        plan.append({"text": text, "thinkSeconds": round(think, 1), "typingSeconds": round(typing, 1),
                     "waitSeconds": round(think + typing, 1)})
    return plan


def run_plan(plan, *, started_at, send, newer_messages, now=time.time, sleep=time.sleep):
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
        try:
            receipt = send(step["text"])
        except Exception as exc:
            receipt = {"status": "unknown", "error": str(exc)}
        last = now()
        if receipt.get("status") != "success" or receipt.get("wechatAccepted") is not True:
            return {"status": "send_uncertain", "receipt": receipt, "sent": sent,
                    "uncertain": step["text"], "unsent": unsent[1:]}
        sent.append({"text": step["text"], "acceptedAt": last})
    return {"status": "sent", "sent": sent}
