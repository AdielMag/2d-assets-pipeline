"""Image-provider metadata + live status, feeding the Settings ("Providers") page.

Static description of each image provider (label, cost tier, how it's used, what
it needs) plus a runtime probe of availability, configuration and enabled state.
Text-LLM CLIs come from `llm.registry`; this module only covers image generation.
"""
import json
import shutil
import subprocess
import time

from .. import config
from . import prefs

# Static metadata. `cost` is "free" (subscription / local login, no per-image
# billing) or "paid" (per-request API billing you can rack up a bill on).
IMAGE_META: dict[str, dict] = {
    "antigravity": {
        "label": "Antigravity",
        "cost": "free",
        "models": [
            "gemini-3.6-flash-high",
            "gemini-3.6-flash-medium",
            "gemini-3.6-flash-low",
            "gemini-3.5-flash-high",
            "gemini-3.5-flash-medium",
            "gemini-3.5-flash-low",
            "gemini-3.1-pro-high",
            "gemini-3.1-pro-low",
        ],
        "default_model": "gemini-3.6-flash-high",
        "visual_models": [
            "auto",
            "gemini-3.1-flash-image",
            "gemini-3-pro-image-preview",
        ],
        "default_visual_model": "auto",
        "how_used": (
            "Shells out to the Antigravity CLI agent, which generates images on "
            "your Google AI Pro/Ultra subscription. No per-image API charge. "
            "Magenta chroma-key is stripped afterwards for transparency."
        ),
        "requires": "Antigravity CLI installed + signed in with your Google account.",
    },
    "higgsfield": {
        "label": "Higgsfield",
        "cost": "paid",
        # Fallback list, replaced by the live probe below (`higgsfield model list`) whenever
        # that succeeds. Used as-is when the probe fails — CLI missing, not signed in, or
        # (as happens periodically) higgsfield.ai's own API erroring — so a transient outage
        # doesn't collapse the picker down to two models and hide whichever one is actually
        # selected (e.g. gpt_image_2, the measured default — see prefs.DEFAULTS). Sourced
        # from the model set this app actually knows how to drive (tools/sweep_polish_text.py
        # CANDIDATES + the web-unlimited snapshot below), not just the first two.
        "models": [
            "nano_banana_flash", "nano_banana_pro", "nano_banana",
            "gpt_image_2", "seedream_v4_5", "seedream_v5_lite",
            "kling_omni_image", "flux_kontext", "flux_2", "text2image_soul_v2",
        ],
        "default_model": "nano_banana_flash",
        "how_used": (
            "Shells out to the official Higgsfield CLI, signed in with your own "
            "Higgsfield plan — no API key, same shape as Antigravity. Every "
            "generation here spends plan credits: per Higgsfield's own docs, "
            "\"unlimited\" models only apply on higgsfield.ai's website with its "
            "toggle switched on, not through the CLI/API this app uses. Supports "
            "reference images. Magenta chroma-key is stripped afterwards for "
            "transparency."
        ),
        "requires": "Higgsfield CLI (`npm i -g @higgsfield/cli`) installed + `higgsfield auth login`.",
    },
}


# --- Higgsfield live probe: CLI presence, sign-in, plan/credits, model list --------
# Spawns the CLI (and, for account/model, its own network calls), so briefly cached
# to keep repeated Settings-page loads from re-running it constantly.
_HIGGSFIELD_CLI_TIMEOUT_S = 8
_HIGGSFIELD_CACHE_TTL_S = 30.0
_higgsfield_cache: dict | None = None
_higgsfield_cache_ts: float = 0.0


def _run_hf(args: list[str]) -> dict | list | None:
    exe = shutil.which(config.HIGGSFIELD_BIN)
    if not exe:
        return None
    try:
        proc = subprocess.run(
            [exe, *args, "--json"],
            capture_output=True, text=True, timeout=_HIGGSFIELD_CLI_TIMEOUT_S,
            encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if proc.returncode != 0:
        return {"_error": (proc.stderr or proc.stdout).strip()[:300]}
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None


def _hf_signed_in(exe: str) -> bool:
    # `auth token` prints the bare token string even with --json (not a JSON value),
    # so it can't go through _run_hf's JSON parsing — a successful exit with non-empty
    # output is "signed in" here.
    try:
        proc = subprocess.run(
            [exe, "auth", "token"],
            capture_output=True, text=True, timeout=_HIGGSFIELD_CLI_TIMEOUT_S,
            encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL,
        )
    except (subprocess.TimeoutExpired, OSError):
        return False
    return proc.returncode == 0 and bool(proc.stdout.strip())


def _higgsfield_model_ids(payload) -> list[str]:
    items = payload if isinstance(payload, list) else (
        payload.get("models") or payload.get("data") or payload.get("items") or []
        if isinstance(payload, dict) else []
    )
    ids = []
    for m in items:
        if isinstance(m, dict):
            mid = m.get("job_type") or m.get("id") or m.get("name")
            if mid:
                ids.append(mid)
        elif isinstance(m, str):
            ids.append(m)
    return ids


def _higgsfield_model_name_map(payload) -> dict[str, list[str]]:
    """display_name -> [job_type, ...]. Transaction history (below) is keyed by the
    human display name, not the job_type the rest of this app uses, and several
    job_types can share one display name (e.g. "Nano Banana Pro" covers four)."""
    items = payload if isinstance(payload, list) else (
        payload.get("models") or payload.get("data") or payload.get("items") or []
        if isinstance(payload, dict) else []
    )
    out: dict[str, list[str]] = {}
    for m in items:
        if isinstance(m, dict):
            name = m.get("display_name") or m.get("name")
            job_type = m.get("job_type") or m.get("id")
            if name and job_type:
                out.setdefault(name, []).append(job_type)
    return out


# Higgsfield's own docs are explicit: "Unlimited only applies on the main website
# with that toggle on" — "Unlimited models and free generations are available only
# on higgsfield.ai. They are not included on MCP, CLI, Canvas, Supercomputer,
# Marketing Studio, or Shorts Studio." This app only ever talks to Higgsfield
# through the CLI, so a model being unlimited on the account/website does NOT mean
# a generation run from here will be free — it always spends real credits. What
# this still is: real information about the account's web-app entitlement, useful
# context (e.g. "you could generate this one for free on higgsfield.ai instead"),
# just never a claim about what this app's own Generate button will cost.
#
# There is no CLI/API endpoint that lists these entitlements directly (checked
# `account status`, `account transactions`, `workspace status/list`, `model
# list`/`model get`, and the official CLI README — none of them expose it). Two
# sources feed `web_unlimited_models` below:
#   1. Real billing history (`account transactions`) — a model with only ever
#      0-credit "spend" entries was very likely generated for free on the website.
#   2. A manual snapshot of the account's dashboard (Profile → Manage Account →
#      Subscription → Active unlimited models), for models not yet tried so
#      history has nothing to go on. Snapshot taken 2026-08-01 for the "plus"
#      plan; only the long-lived "365 Unlimited" auto-renewing grants are listed
#      here (image models only — this app doesn't do video) since the account's
#      short 7-day trial grants churn too fast for a hardcoded list to track
#      reliably and aren't visible anywhere the CLI can re-check on its own.
_HIGGSFIELD_WEB_UNLIMITED_SNAPSHOT = [
    "nano_banana",       # "Nano Banana [365 Unlimited]"
    "gpt_image_2",       # "GPT Image [365 Unlimited]"
    "seedream_v4_5",     # "Seedream 4.5 [365 Unlimited]"
    "seedream_v5_lite",  # "Seedream 5.0 Lite [365 Unlimited]"
    "kling_omni_image",  # "Kling O1 Image [365 Unlimited]"
    "flux_2",            # "FLUX.2 Pro [365 Unlimited]" (job_type has no separate "pro" variant)
]


def _higgsfield_web_unlimited_from_history(name_map: dict[str, list[str]]) -> list[str]:
    """Which job_types have only ever billed 0 credits in real account history —
    i.e. confirmed free *on the website*, per the module-level note above. `generate
    cost` returns a model's list price and has no idea the account's plan includes
    it for free on the web; the only place that shows up is what a past generation
    actually got charged. A display name mapping to more than one job_type is
    ambiguous (the log doesn't say which variant ran), so those are skipped rather
    than guessed at."""
    zero_spend: set[str] = set()
    nonzero_spend: set[str] = set()
    cursor = None
    for _ in range(5):  # bounded: 5 pages * 100 = 500 transactions is plenty for any account
        args = ["account", "transactions", "--size", "100"]
        if cursor:
            args += ["--cursor", cursor]
        payload = _run_hf(args)
        if not isinstance(payload, dict) or "_error" in payload:
            break
        for item in payload.get("items") or []:
            if not isinstance(item, dict) or item.get("action") != "spend":
                continue
            name = item.get("display_name")
            credits = item.get("credits")
            if not name or credits is None:
                continue
            (zero_spend if credits == 0 else nonzero_spend).add(name)
        cursor = payload.get("cursor")
        if not cursor:
            break

    confirmed = []
    for name in zero_spend - nonzero_spend:
        job_types = name_map.get(name) or []
        if len(job_types) == 1:
            confirmed.append(job_types[0])
    return confirmed


def _higgsfield_probe() -> dict:
    """Live state: CLI on PATH? signed in? plan/credits/unlimited models? full model
    list? Falls back to the static placeholders above on any failure (CLI missing,
    not signed in, timeout, unexpected output) rather than erroring the whole page."""
    global _higgsfield_cache, _higgsfield_cache_ts
    now = time.time()
    if _higgsfield_cache is not None and (now - _higgsfield_cache_ts) < _HIGGSFIELD_CACHE_TTL_S:
        return _higgsfield_cache

    result: dict = {"models": None, "status": None, "web_unlimited_models": None, "credits": None}
    exe = shutil.which(config.HIGGSFIELD_BIN)
    if not exe:
        result["status"] = {
            "configured": False,
            "detail": "Higgsfield CLI not on PATH — run `npm i -g @higgsfield/cli`, "
                      "then `higgsfield auth login`.",
        }
        _higgsfield_cache, _higgsfield_cache_ts = result, now
        return result

    if not _hf_signed_in(exe):
        result["status"] = {
            "configured": False,
            "detail": "Higgsfield CLI found but not signed in — run `higgsfield auth login`.",
        }
        _higgsfield_cache, _higgsfield_cache_ts = result, now
        return result

    detail_bits = ["Signed in"]
    account = _run_hf(["account", "status"])
    if isinstance(account, dict) and "_error" not in account:
        email = account.get("email")
        plan = account.get("plan") or account.get("plan_name") or account.get("subscription_plan_type")
        credits = account.get("credits") if account.get("credits") is not None else (
            account.get("credits_balance") if account.get("credits_balance") is not None
            else account.get("balance")
        )
        # `account status` doesn't actually report an unlimited-models field on a
        # live "plus" plan account (verified) — kept as a fallback in case some other
        # plan's response shape does. Real billing history (below) is what actually
        # works in practice.
        web_unlimited = account.get("unlimited_models") or account.get("unlimited")
        result["web_unlimited_models"] = web_unlimited
        result["credits"] = credits
        if email:
            detail_bits.append(f"as {email}")
        if plan:
            detail_bits.append(f"· Plan: {plan}")
        if credits is not None:
            detail_bits.append(f"· Credits: {credits}")
        if web_unlimited:
            names = ", ".join(web_unlimited) if isinstance(web_unlimited, list) else str(web_unlimited)
            detail_bits.append(f"· Unlimited on web: {names}")
    elif isinstance(account, dict) and "_error" in account:
        detail_bits.append(f"· {account['_error']}")

    result["status"] = {"configured": True, "detail": " ".join(detail_bits)}

    models_payload = _run_hf(["model", "list", "--image"])
    if models_payload is not None and not (isinstance(models_payload, dict) and "_error" in models_payload):
        ids = _higgsfield_model_ids(models_payload)
        if ids:
            result["models"] = ids
        confirmed = _higgsfield_web_unlimited_from_history(_higgsfield_model_name_map(models_payload))
        snapshot = [m for m in _HIGGSFIELD_WEB_UNLIMITED_SNAPSHOT if m in ids]
        merged = set(confirmed) | set(snapshot)
        if merged:
            existing = result.get("web_unlimited_models")
            existing_list = existing if isinstance(existing, list) else []
            result["web_unlimited_models"] = sorted(set(existing_list) | merged)

    _higgsfield_cache, _higgsfield_cache_ts = result, now
    return result


def _antigravity_status() -> dict:
    exe = shutil.which(config.ANTIGRAVITY_BIN)
    if exe:
        return {"configured": True, "detail": f"CLI found: {exe}"}
    return {
        "configured": False,
        "detail": f"'{config.ANTIGRAVITY_BIN}' CLI not on PATH — install Antigravity and sign in.",
    }


def _higgsfield_status() -> dict:
    return _higgsfield_probe()["status"]


def higgsfield_status() -> dict:
    """Public wrapper for the sidebar status card (routers/status.py), so its CLI
    probe reuses this cached one instead of shelling out a second time."""
    return _higgsfield_probe()["status"]


_STATUS_FN = {
    "antigravity": _antigravity_status,
    "higgsfield": _higgsfield_status,
}


def resolve_model(provider_name: str, requested: str | None = None) -> str:
    """The model a generation call will actually use: an explicit request wins,
    otherwise the user's saved preference, otherwise the provider's default."""
    if requested:
        return requested
    saved = prefs.get_provider_model(provider_name)
    if saved:
        return saved
    return IMAGE_META[provider_name]["default_model"]


# An extraction ("keep only X") generation is a different job from the rest of reference
# mode, and models are not interchangeable at it. Measured on asset 44 ("Element 10"),
# asked to keep only the caption from an already-extracted strip that still carried a
# ragged band of screenshot behind the words: gpt_image_2 reproduced the band under every
# one of six prompt wordings and at `quality: low` and `high` alike (8.7%-21.7% of output
# pixels still band, against 6.9% in the input — i.e. no better than not running the step),
# flux_kontext misspelled the caption, and seedream_v4_5 returned a near-blank image.
# nano_banana_pro was the only model that could act on the instruction at all, and it
# cleared the band to 0.3% while keeping the caption on one line.
#
# So the model is pinned per provider for this one job rather than left to the user's saved
# default, which is otherwise chosen for cost. It costs more (2 credits against 0.5 for
# gpt_image_2 at the saved low/1k), and that is the whole trade: the cheap call reliably
# produces an asset that has to be redone by hand. An explicitly requested model still
# wins — this is a better default, not a lock.
EXTRACTION_MODEL: dict[str, str] = {
    "higgsfield": "nano_banana_pro",
}


def resolve_extraction_model(provider_name: str, requested: str | None = None) -> str:
    """The model an extraction generation will use: an explicit request wins, otherwise
    the provider's pinned extraction model, otherwise ordinary model resolution."""
    if requested:
        return requested
    pinned = EXTRACTION_MODEL.get(provider_name)
    return pinned or resolve_model(provider_name)


def extraction_model_warning(provider_name: str, model: str) -> str | None:
    """A user-facing warning for an extraction call about to run on a model other than the
    one in EXTRACTION_MODEL — None when it's the pinned model, the common case.

    This only fires when an explicit `model` request overrode the pin (resolve_extraction_
    model already prefers the pin by default), so it is specifically catching "the user
    picked something else" rather than routine calls.
    """
    pinned = EXTRACTION_MODEL.get(provider_name)
    if not pinned or model == pinned:
        return None
    return (
        f"'{model}' has not been tested on this extraction (\"keep only\") task — every "
        f"wording tried on gpt_image_2 left leftover background behind it, at every "
        f"quality setting. '{pinned}' is the only model measured to remove it cleanly; "
        f"switch to it for this generation."
    )


def model_aspect_options(provider_name: str, model: str) -> list[str]:
    """The output canvas shapes a model declares, as "w:h" strings; [] when it declares
    none (or the CLI could not be reached). Used to letterbox an extraction reference into
    a frame the model can actually draw — see processing.reference."""
    spec = image_model_params(provider_name, model).get("aspect_ratio") or {}
    return [o for o in (spec.get("options") or []) if o != AUTO_ASPECT_OPTION]


def resolve_visual_model(provider_name: str, requested: str | None = None) -> str:
    """The visual model an Antigravity generation call will actually use."""
    if requested:
        return requested
    saved = prefs.get_provider_visual_model(provider_name)
    if saved:
        return saved
    return IMAGE_META.get(provider_name, {}).get("default_visual_model", "auto")


def resolve_params(provider_name: str, model: str, requested: dict | None = None) -> dict:
    """The per-model generation knobs a call will actually use: an explicit request wins
    outright, otherwise the user's saved preference for that model, otherwise the shipped
    default. Not merged — an explicit `{}` from a caller means "no params", which has to
    stay expressible."""
    if requested is not None:
        return requested
    return prefs.get_provider_params(provider_name, model)


# Params the model-picker offers as dropdowns. Restricted to enum-valued knobs: those have
# a known, safe option list, whereas free-form ones (`colors`, `background_color`,
# `custom_reference_id`) need a value the user would have to invent and are better left to
# the API. `prompt` and `image_references` are the call itself, not tuning.
_PARAM_SKIP = {"prompt", "image_references", "video_references", "audio_references"}

# Offered on `aspect_ratio` in addition to the model's own enum. Not a Higgsfield value —
# the provider resolves it per call from the reference image's real proportions, snapped to
# whatever ratios that model declares (HiggsfieldProvider.AUTO_ASPECT).
AUTO_ASPECT_OPTION = "auto"


def image_model_params(provider_name: str, model: str) -> dict:
    """Tunable params for one model, shaped for the picker: name -> {options, default}.

    Per-model and fetched on demand rather than bundled into `/api/providers`: the option
    lists come from `higgsfield model get`, one CLI round trip each, and there are 28 image
    models — fetching them all to render one dropdown would make the providers page take
    half a minute.
    """
    if provider_name != "higgsfield" or not model:
        return {}
    exe = shutil.which(config.HIGGSFIELD_BIN)
    if not exe:
        return {}
    from .higgsfield_provider import _model_spec

    out: dict[str, dict] = {}
    for name, spec in _model_spec(exe, model).items():
        if name in _PARAM_SKIP:
            continue
        options = list(spec.get("enum") or [])
        if name == "aspect_ratio":
            options = [AUTO_ASPECT_OPTION] + [o for o in options if o != AUTO_ASPECT_OPTION]
        if not options:
            continue
        out[name] = {"options": options, "default": spec.get("default")}
    return out


def image_providers_info() -> list[dict]:
    p = prefs.load()
    models_saved = p.get("provider_models", {})
    visual_models_saved = p.get("provider_visual_models", {})
    out = []
    for name, meta in IMAGE_META.items():
        meta = dict(meta)
        if name == "higgsfield":
            probe = _higgsfield_probe()
            if probe.get("models"):
                meta["models"] = probe["models"]
                if meta["default_model"] not in meta["models"]:
                    meta["default_model"] = meta["models"][0]
            meta["web_unlimited_models"] = probe.get("web_unlimited_models")
            meta["credits"] = probe.get("credits")
        status = _STATUS_FN[name]()
        out.append({
            "name": name,
            **meta,
            "selected_model": models_saved.get(name, meta.get("default_model")),
            "selected_visual_model": visual_models_saved.get(name, meta.get("default_visual_model", "auto")),
            "selected_params": prefs.get_provider_params(
                name, models_saved.get(name, meta.get("default_model"))
            ),
            "enabled": bool(p["enabled"].get(name, True)),
            "is_default": p["default_image_provider"] == name,
            # None when nothing has been measured for this provider yet — the client
            # only warns when there's an actual model to recommend switching to. See
            # EXTRACTION_MODEL for what's behind the pin.
            "extraction_model": EXTRACTION_MODEL.get(name),
            **status,
        })
    return out

