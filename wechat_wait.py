import time

def read_new(read, since):
    for attempt in range(3):
        try:
            return [m for m in read() if m["createTime"] > since]
        except Exception as exc:
            if attempt == 2 or getattr(exc, "details", {}).get("status") == "locked":
                raise

def wait_new(*, since, read, event=None, settle_seconds=15, max_minutes=1440,
             include_self=False, now=time.time, sleep=time.sleep):
    if not all(0 < value < float("inf") for value in (settle_seconds, max_minutes)):
        raise ValueError("wait_intervals_must_be_positive_and_finite")
    started = now()
    deadline = started + max_minutes * 60
    cap = max(60, settle_seconds)
    first = last = None
    new, extra = [], {}
    status = "read_failed"
    try:
        new = read_new(read, since)
        new.sort(key=lambda m: m["createTime"])
        status = "timed_out"
        if new and (include_self or new[-1].get("senderRole") != "self"):
            first = last = now()
        while now() < deadline:
            current = now()
            if first is not None and current >= min(last + settle_seconds, first + cap):
                status = "new_messages"
                break
            due = min(deadline, current + 1, min(last + settle_seconds, first + cap) if first is not None else deadline)
            events = event(max(0, due - current)) if event else sleep(max(0, due - current)) or []
            for e in events or []:
                if not include_self and e.get("is_self"):
                    first = last = None
                else:
                    last = now()
                    if first is None:
                        first = last
    except Exception as exc:
        extra = {"error": str(exc), **getattr(exc, "details", {})}
        status = extra.pop("status", "connection_lost" if status == "timed_out" else "read_failed")
        if status == "locked":
            new = []
    new.sort(key=lambda m: m["createTime"])
    return {"status": status, "now": now(), "since": since, "waitedSeconds": round(now() - started, 3),
            "new": new, "nextSince": max((m["createTime"] for m in new), default=since), **extra}
