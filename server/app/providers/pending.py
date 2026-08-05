"""A journal of in-flight image generations, so a run that dies mid-call can discover it
already paid for an image instead of buying it a second time.

A generation is billed the moment the provider accepts the job, not when we manage to
download it. Everything between those two points can fail without the credits coming
back: the CLI's `--wait` can time out while the job keeps running server-side, the shell
can be killed, the process can crash after the download but before the version is
committed. In every one of those cases the picture exists on the provider's account and
re-running the step pays for it again.

So each call records an *intent* before it starts and closes it when the image is safely
in hand. An intent left open is a call whose outcome we never learned, and it carries
exactly what a later attempt needs to go looking for that outcome:

    key         identifies the call (provider + model + prompt + reference bytes), so the
                same request on a re-run lands on the same journal entry
    started_at  the earliest unclosed attempt — the lower bound for "a job created by us",
                which is what keeps a recovery search from reaching back into unrelated
                history
    prompt      matched against the provider's own record of the job

`consumed` is the other half: every job id whose image we did store. A recovery must
never hand back a job we already banked, or one element's artwork ends up on another.

Scope: one process (the app's uvicorn worker, plus a sweep run from the CLI). The lock is
in-process and the file is rewritten whole, so two *processes* generating at once can lose
an intent to a last-writer-wins race. That costs a recovery opportunity, never a wrong
image — the consumed list is the thing that guards correctness, and a lost write there
only makes recovery more conservative (the job stays unmatched and is skipped as
ambiguous, or is simply never looked for).
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
from pathlib import Path

from ..config import STORAGE_DIR

JOURNAL = Path(STORAGE_DIR) / "pending_generations.json"

# How stale an unfinished intent may be and still be worth chasing. A day covers "it died
# last night, I re-ran it this morning"; past that, result URLs start expiring and the
# odds that an unrelated job coincidentally matches grow, so an old intent is dropped
# rather than acted on.
MAX_RECOVER_AGE_S = 24 * 3600
# Closed intents are kept briefly for diagnosis ("did that call really go through?") and
# then pruned; they have no operational use once closed.
DONE_TTL_S = 3 * 24 * 3600
MAX_CONSUMED = 2000

_lock = threading.Lock()


def file_sha(path: Path | str) -> str:
    """SHA-256 of a file's bytes, or "" if it can't be read. Identifies the reference image
    a call was made from — the one input that distinguishes two otherwise identical calls
    (every region in a Polish run shares the same prompt, model and params)."""
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return ""


def key_for(provider: str, model: str | None, prompt: str, reference_shas: list[str]) -> str:
    """Stable identity for one generation request. Deliberately content-based rather than
    an id we mint: a re-run after a crash has no memory of the id it used last time, but it
    composes the identical prompt from the identical reference file, so it recomputes the
    identical key."""
    blob = "\x00".join([provider, model or "", prompt, *reference_shas])
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]


def _load() -> dict:
    try:
        data = json.loads(JOURNAL.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"intents": {}, "consumed": []}
    if not isinstance(data, dict):
        return {"intents": {}, "consumed": []}
    data.setdefault("intents", {})
    data.setdefault("consumed", [])
    return data


def _save(data: dict) -> None:
    now = time.time()
    data["intents"] = {
        k: v for k, v in data["intents"].items()
        if not (
            (v.get("state") == "done" and now - v.get("started_at", 0) > DONE_TTL_S)
            or now - v.get("started_at", 0) > max(DONE_TTL_S, MAX_RECOVER_AGE_S) * 2
        )
    }
    data["consumed"] = data["consumed"][-MAX_CONSUMED:]
    try:
        JOURNAL.parent.mkdir(parents=True, exist_ok=True)
        tmp = JOURNAL.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
        tmp.replace(JOURNAL)
    except OSError:
        pass  # the journal is an optimisation; never fail a generation over it


def pending(key: str) -> dict | None:
    """The still-open intent for this exact call, if there is one worth recovering."""
    with _lock:
        entry = _load()["intents"].get(key)
    if not entry or entry.get("state") != "in_flight":
        return None
    if time.time() - entry.get("started_at", 0) > MAX_RECOVER_AGE_S:
        return None
    return entry


def open_intent(key: str, *, provider: str, model: str | None, prompt: str, ref: str = "") -> dict:
    """Record that this call is about to be made. Re-arming an already-open intent keeps
    its ORIGINAL `started_at`: the recovery window has to reach back to the first attempt,
    since that is the one whose job may still be sitting completed and unclaimed."""
    now = time.time()
    with _lock:
        data = _load()
        prior = data["intents"].get(key)
        started = prior["started_at"] if prior and prior.get("state") == "in_flight" else now
        entry = {
            "state": "in_flight", "provider": provider, "model": model, "prompt": prompt,
            "ref": ref, "started_at": started, "last_attempt_at": now,
            "attempts": (prior or {}).get("attempts", 0) + 1,
        }
        data["intents"][key] = entry
        _save(data)
    return entry


def close_intent(key: str, *, job_id: str | None = None) -> None:
    """The image is in hand. Closes the intent so no later run tries to recover it, and
    banks the job id so a *different* call's recovery can't claim the same job."""
    with _lock:
        data = _load()
        entry = data["intents"].get(key)
        if entry:
            entry["state"] = "done"
            entry["job_id"] = job_id
            entry["closed_at"] = time.time()
        if job_id and job_id not in data["consumed"]:
            data["consumed"].append(job_id)
        _save(data)


def mark_consumed(job_id: str) -> None:
    if not job_id:
        return
    with _lock:
        data = _load()
        if job_id not in data["consumed"]:
            data["consumed"].append(job_id)
            _save(data)


def consumed() -> set[str]:
    with _lock:
        return set(_load()["consumed"])
