import time

RECHECK_SECONDS = 3  # a message that beat the connection can reach the database after the first read
LOGIN_POLL_SECONDS = 5

def read_new(read, since):
    for attempt in range(3):
        try:
            return [m for m in read() if m["createTime"] > since]
        except Exception as exc:
            if attempt == 2 or getattr(exc, "details", {}).get("status") == "locked":
                raise

def wait_login(*, ready, max_minutes=1440, stopped=lambda: False, now=time.time, sleep=time.sleep):
    """Hold without touching WeChat until the account is back; returns the resulting status."""
    if ready():
        return "logged_in"
    deadline = now() + max_minutes * 60
    while now() < deadline:
        if stopped():
            return "stopped"
        sleep(min(LOGIN_POLL_SECONDS, max(0, deadline - now())))
        if ready():
            return "login_restored"
    return "timed_out"

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
    def wakes(messages):
        return bool(messages) and (include_self or messages[-1].get("senderRole") != "self")
    try:
        new = read_new(read, since)
        new.sort(key=lambda m: m["createTime"])
        status = "timed_out"
        if wakes(new):
            first = last = now()
        recheck_at = now() + RECHECK_SECONDS
        while now() < deadline:
            current = now()
            if first is not None and current >= min(last + settle_seconds, first + cap):
                # Private-chat self events never arrive, so the record decides whether the owner already answered.
                try:
                    latest = sorted(read_new(read, since), key=lambda m: m["createTime"])
                except Exception:
                    latest = []
                if latest:
                    new = latest
                    if not wakes(latest):
                        first = last = None
                        continue
                status = "new_messages"
                break
            if recheck_at is not None and current >= recheck_at:
                recheck_at = None
                if first is None:
                    try:
                        new = sorted(read_new(read, since), key=lambda m: m["createTime"])
                    except Exception as exc:
                        if getattr(exc, "details", {}).get("status") == "locked":
                            raise
                        extra["recheckError"] = str(exc)
                    if wakes(new):
                        first = last = now()
                    continue
            due = min(deadline, current + 1, min(last + settle_seconds, first + cap) if first is not None else deadline)
            events = event(max(0, due - current)) if event else sleep(max(0, due - current)) or []
            for e in events or []:
                if not include_self and e.get("is_self"):
                    # The event only hints: recheck so an unmatched self echo cannot swallow the other side's message.
                    first = last = None
                    recheck_at = now() + RECHECK_SECONDS
                else:
                    last = now()
                    if first is None:
                        first = last
    except Exception as exc:
        extra = {"error": str(exc), **getattr(exc, "details", {})}
        fallback = "login_required" if extra.get("requiredAction") == "owner_relogin" else "connection_lost"
        status = extra.pop("status", fallback if status == "timed_out" else "read_failed")
        if status == "locked":
            new = []
    new.sort(key=lambda m: m["createTime"])
    return {"status": status, "now": now(), "since": since, "waitedSeconds": round(now() - started, 3),
            "new": new, "nextSince": max((m["createTime"] for m in new), default=since), **extra}
