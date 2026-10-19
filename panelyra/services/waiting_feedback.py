"""Honest, local-only waiting feedback; never infer progress from a timer."""
import time

NOTES = (
    "You can keep browsing. Your queue stays here.",
    "Finished chapters appear in Reader while the rest download.",
    "Already saved chapters are skipped, not downloaded twice.",
    "Checking the pages before saving keeps broken chapters out of Reader.",
)


def duration(seconds):
    seconds = max(0, int(seconds))
    return f"{seconds // 60}m {seconds % 60:02d}s" if seconds >= 60 else f"{seconds}s"


def note(elapsed):
    return NOTES[int(max(0, elapsed)) // 8 % len(NOTES)]


def queue_feedback(job, *, paused=False, running=False, now=None):
    now = time.time() if now is None else now
    chapters = job.get("chapters", {})
    done = sum(c.get("status") in {"downloaded", "already_saved", "already_queued"} for c in chapters.values())
    elsewhere = sum(c.get("status") == "already_queued" for c in chapters.values())
    current = next(((n, c) for n, c in chapters.items() if c.get("status") == "downloading"), None)
    active = job.get("status") in {"queued", "running", "interrupted"}
    # Only measured downloads count toward ETA, never skipped chapters or queue time.
    samples = [c["duration_seconds"] for c in chapters.values()
               if c.get("status") == "downloaded" and c.get("duration_seconds", 0) > 0]
    elapsed = max(0, now - current[1].get("started_at", now)) if current else 0
    remaining = max(0, len(chapters) - done)
    eta = None
    if active and not paused and running and len(samples) >= 2:
        average = sum(samples) / len(samples)
        # A large/stalled chapter invalidates the recent throughput sample.
        # Do not keep showing the same optimistic countdown indefinitely.
        if elapsed <= average * 2:
            eta = max(1, average * remaining - min(elapsed, average))
    if paused:
        label = "Paused · your chapters are queued safely"
    elif active and not running:
        label = "Queued · waiting for the download engine"
    elif current:
        label = f"Chapter {current[0]} · {current[1].get('stage', 'Receiving pages')}"
    elif job.get("status") == "completed" and elsewhere:
        label = "Batch handled · some chapters are queued in another job"
    elif job.get("status") == "completed":
        label = "Ready · open Reader to read offline"
    else:
        label = job.get("status", "queued").replace("_", " ").capitalize()
    return {"done": done, "ready": done - elsewhere, "elsewhere": elsewhere, "total": len(chapters), "fraction": done / max(1, len(chapters)),
            "label": label, "elapsed": elapsed, "eta": eta, "active": active}
