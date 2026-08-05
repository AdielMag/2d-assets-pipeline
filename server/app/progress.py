"""Server-Sent-Events progress plumbing for long generation jobs.

The generation endpoints are otherwise blocking request->final-JSON calls. To let the
UI show live, opt-in progress (what step we're on, plus intermediate images), a
`/stream` variant runs the same work in a worker thread and pushes structured events
onto a queue that an SSE StreamingResponse drains.

Events are JSON objects: {step, status, message, image?, index?, total?, data?}.
Terminal events use step "__done__" (status "done", carries `data`) or "__error__"
(status "error", carries `message`). The client re-fetches the affected entity on done.
"""
import json
import queue
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable

from PIL import Image

from .storage import project_dir, rel_path
from .db import SessionLocal
from .models import Mockup

STEP_PREVIEW_MAX = 320  # px long edge for intermediate step thumbnails (keep disk light)

_QUOTA_ERROR_RE = re.compile(r"quota|rate.?limit|429|resource_exhausted", re.IGNORECASE)


def is_quota_error(message: str) -> bool:
    """Whether a provider error looks like a quota/rate-limit exhaustion rather than a
    one-off failure, so the UI can flag it distinctly (and batch runs can halt instead of
    burning through every remaining item against a provider that's already failing)."""
    return bool(_QUOTA_ERROR_RE.search(message or ""))


class Emitter:
    """Handed to the work function so it can report progress. Thread-safe (queue)."""

    def __init__(self, q: "queue.Queue[dict]", run_dir: Path):
        self._q = q
        self._run_dir = run_dir
        self._n = 0
        self._stopped = False
        self._events: list[dict] = []
        self._lock = threading.Lock()

    def stop(self) -> None:
        self._stopped = True

    @property
    def is_stopped(self) -> bool:
        return self._stopped

    def emit(
        self,
        step: str,
        status: str = "running",
        message: str = "",
        image: str | None = None,
        index: int | None = None,
        total: int | None = None,
        data: dict | None = None,
    ) -> None:
        # `stop()` fires not only from the explicit Stop button but from *any* SSE client
        # disconnect (event_stream's `except (GeneratorExit, Exception)` below) — e.g. the
        # user simply switching to a different screen while a detect/build is still running
        # server-side in its background thread. The work keeps going either way, so this
        # must keep recording it: `is_stopped` is for loops deciding whether to start more
        # *optional* work (see mockups.py), not a reason to blank out history for a run
        # that's still actively producing steps.
        payload = {
            "step": step,
            "status": status,
            "message": message,
            "image": image,
            "index": index,
            "total": total,
            "timestamp": int(time.time() * 1000),
            "data": data.copy() if isinstance(data, dict) else data,
        }
        with self._lock:
            self._events.append(payload)
            self._save_log()
        self._q.put(payload)

    def _save_log(self) -> None:
        try:
            log_file = self._run_dir / "events.json"
            log_file.write_text(json.dumps(self._events, indent=2), encoding="utf-8")
        except Exception:
            pass

    def preview(self, img: Image.Image, name: str) -> str:
        """Save a small thumbnail of an intermediate image into the run folder and return
        its storage-relative path (client prefixes with /storage/)."""
        self._n += 1
        thumb = img.convert("RGBA")
        thumb.thumbnail((STEP_PREVIEW_MAX, STEP_PREVIEW_MAX), Image.LANCZOS)
        dest = self._run_dir / f"{self._n:02d}-{name}.png"
        thumb.save(dest)
        return rel_path(dest)


def estimate_tokens(
    prompt: str | None = None,
    output: str | None = None,
    thinking: str | None = None,
    image_count: int = 0,
) -> dict:
    """Helper to calculate/estimate input, output, thinking, and total tokens."""
    in_tok = (len(prompt) // 4 if prompt else 0) + (image_count * 258)
    out_tok = len(output) // 4 if output else 0
    th_tok = len(thinking) // 4 if thinking else 0
    tot_tok = in_tok + out_tok + th_tok
    return {
        "input": max(0, in_tok),
        "output": max(0, out_tok),
        "thinking": max(0, th_tok),
        "total": max(0, tot_tok),
    }


class NoEmit:
    """No-op emitter so the shared generation cores can run on the plain (non-streaming)
    path without reporting progress."""
    _stopped = False
    def stop(self) -> None: pass
    @property
    def is_stopped(self) -> bool: return False
    def emit(self, *a, **k) -> None: ...
    def preview(self, *a, **k) -> str: return ""


def _run_dir(project_id: int, entity_type: str | None = None, entity_id: int | None = None) -> Path:
    d = project_dir(project_id) / "runs" / str(int(time.time() * 1000))
    d.mkdir(parents=True, exist_ok=True)
    # Recorded up front (before any work happens) so a run stays attributable to its
    # asset/mockup even if the generation errors, is stopped, or the process is killed
    # mid-run — matching used to rely solely on entity ids showing up inside step event
    # payloads, which never happened on the failure/abort paths and made the run vanish
    # from that entity's history.
    if entity_type is not None and entity_id is not None:
        try:
            (d / "meta.json").write_text(
                json.dumps({"entity_type": entity_type, "entity_id": entity_id}), encoding="utf-8"
            )
        except Exception:
            pass
    return d


def make_emitter(project_id: int, entity_type: str | None = None, entity_id: int | None = None) -> Emitter:
    """A real (disk-logging) Emitter for the plain, non-streaming endpoints — same run
    folder/meta.json/events.json as the SSE path, just without anything draining the
    queue live. Without this, a caller that hits the plain REST endpoints directly
    (a script, an external agent) instead of the browser's `/stream` UI left NoEmit()
    doing nothing, so that generation never showed up in the entity's run history."""
    return Emitter(queue.Queue(), _run_dir(project_id, entity_type, entity_id))


def get_stored_runs(project_id: int, entity_id: int | None = None, entity_type: str = "asset") -> list[dict]:
    """Retrieve stored generation runs for a project and optional entity."""
    runs_dir = project_dir(project_id) / "runs"
    if not runs_dir.exists():
        return []
    result = []
    # Inspect subdirectories newest first. Bounded at 20 *matching* runs, not 20 dirs overall
    # — a project can easily accumulate 20+ runs across OTHER assets/mockups between two runs
    # for the one entity being asked about, so slicing the directory listing before filtering
    # by entity used to silently drop older-but-still-relevant runs. MAX_DIRS_SCANNED just
    # bounds worst-case I/O if this entity has no runs at all in a very large project.
    subdirs = sorted([d for d in runs_dir.iterdir() if d.is_dir()], key=lambda d: d.name, reverse=True)
    MAX_DIRS_SCANNED = 500
    for d in subdirs[:MAX_DIRS_SCANNED]:
        if len(result) >= 20:
            break
        events_file = d / "events.json"
        if events_file.exists():
            try:
                events = json.loads(events_file.read_text(encoding="utf-8"))
                if not events:
                    continue
                # match asset_id or mockup_id
                matched = True
                if entity_id is not None:
                    found = False

                    meta_file = d / "meta.json"
                    if meta_file.exists():
                        try:
                            meta = json.loads(meta_file.read_text(encoding="utf-8"))
                            found = meta.get("entity_type") == entity_type and meta.get("entity_id") == entity_id
                        except Exception:
                            found = False

                    # Fallback for runs recorded before meta.json existed: infer the
                    # entity from whatever ids happen to appear inside event payloads.
                    if not found and not meta_file.exists():
                        for ev in events:
                            edata = ev.get("data")
                            if isinstance(edata, dict):
                                if entity_type == "asset" and edata.get("asset_id") == entity_id:
                                    found = True
                                    break
                                if entity_type == "mockup" and edata.get("mockup_id") == entity_id:
                                    found = True
                                    break
                        if not found and entity_type == "mockup":
                            with SessionLocal() as db:
                                mock = db.query(Mockup).filter(Mockup.id == entity_id).first()
                                if mock and mock.regions:
                                    region_names = {r.name for r in mock.regions}
                                    for ev in events:
                                        msg = ev.get("message", "")
                                        if any(rn in msg for rn in region_names):
                                            found = True
                                            break
                    if not found:
                        matched = False
                if matched:
                    timestamp = int(d.name) if d.name.isdigit() else int(time.time() * 1000)
                    result.append({
                        "id": d.name,
                        "timestamp": timestamp,
                        "events": [e for e in events if e.get("step") not in ("__done__", "__error__")],
                    })
            except Exception:
                continue
    return result


def sse_response(
    project_id: int,
    work: Callable[[Emitter], dict | None],
    entity_type: str | None = None,
    entity_id: int | None = None,
):
    """Return a StreamingResponse that runs `work(emitter)` in a thread and streams its
    progress as SSE. `work` should create its own DB session (it runs off-request-thread).
    Pass `entity_type`/`entity_id` (e.g. "asset"/asset.id) so this run is reliably
    attributable to that entity's history even if it errors or is stopped."""
    from fastapi.responses import StreamingResponse

    q: "queue.Queue[dict]" = queue.Queue()
    emitter = Emitter(q, _run_dir(project_id, entity_type, entity_id))

    def runner() -> None:
        try:
            result = work(emitter)
            emitter.emit("__done__", "done", "", data=result or {})
        except Exception as e:  # surface any failure as a terminal error event
            msg = str(e)
            emitter.emit("__error__", "error", msg, data={"quota_exceeded": True} if is_quota_error(msg) else None)

    threading.Thread(target=runner, daemon=True).start()

    def event_stream():
        # initial comment flushes headers so the client shows the panel immediately
        yield ": open\n\n"
        try:
            while True:
                ev = q.get()
                yield f"data: {json.dumps(ev)}\n\n"
                if ev["step"] in ("__done__", "__error__"):
                    break
        except (GeneratorExit, Exception):
            emitter.stop()

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )

