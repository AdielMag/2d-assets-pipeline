"""Composes the final image-generation prompt from art style + asset-type rules + user prompt."""
from .models import Asset, Project

# Magenta chroma background: Nano Banana (and any chat-based image model pasted the
# prompt manually) has no native alpha, so we ask for a solid key color and strip it in
# post-processing (transparency.remove_background). Shared with gemini_provider.py and
# with external_prompt() below, so the copy-pasteable prompt always matches what the
# API path actually sends.
CHROMA_HINT = (
    " The background must be a completely solid, uniform, pure magenta color "
    "(#FF00FF) with no gradients, shadows, or texture on it — the subject must be "
    "extracted cleanly without its original background, and the subject itself must not "
    "contain any magenta."
)

# Agentic image CLIs (Antigravity, Gemini) don't just call a model — they can run tools
# afterwards, and they will helpfully "fix up" the output to match any pixel size the
# prompt mentions. That resize is non-uniform, so a correctly drawn asset arrives
# stretched: a 2.5:1 banner squeezed into a 601x250 file came back with its subject at
# 6:1 and its avatar an oval. The real size is applied deterministically downstream by
# processing.trim.fit_to_resolution, so the model never needs to hit a pixel target.
NO_RESIZE_HINT = (
    " Save the image exactly as generated, at the model's own native resolution — do not "
    "resize, rescale, stretch, crop or pad it to any particular pixel dimensions "
    "afterwards, as that distorts the artwork and turns circles into ovals."
)

# Reference mode: instead of free text, the user ticks operations and we compose the
# prompt from them. Faithfully reproducing a reference is a different job from inventing
# art, and it fails in a specific way — the model treats the reference as loose
# inspiration and quietly redesigns. So the base instruction below is uncompromising
# about fidelity, and each op says only what to change. key -> (UI label, instruction).
REFERENCE_BASE = (
    "Recreate the element shown in the reference image as closely as possible. This is a "
    "faithful reproduction task, NOT a redesign: keep the exact same shapes, proportions, "
    "layout, colors, materials, decoration and artwork as the reference."
)

# The "keep only X" ops are a different task from every other op, and REFERENCE_BASE is
# actively wrong for them. The other ops modify an image that is otherwise preserved, so
# "keep the exact same layout and artwork as the reference / change ONLY the following"
# is exactly right. An extraction op does the opposite: most of the reference must NOT
# survive. Composed together, the base's "keep the artwork" wins on anything the op did
# not name by noun — measured on asset 44 ("Element 10") with ops
# ["text_only", "clean_edges", "keep_colors"], gpt_image_2 reproduced the caption
# together with the smoky background band behind it, because a haze is not one of the
# "button, frame, icon" the op lists, and the base said to keep the artwork. So the base
# is chosen from the ticked ops rather than fixed, and the extraction framing states the
# deletion as the task instead of as an exception to it.
#
# The wording below is the one that measured best (see REFERENCE_VARIANTS for the five
# that lost and by how much). Two things had to be true at once and pull in opposite
# directions: given a tight crop the model re-composes — the band goes, but the caption
# re-wraps onto two lines — and given a letterboxed crop it copies, so the layout holds
# and the band comes with it. Asking for neither is what works: the deletion is posed as
# *finishing a magenta background fill that has already been started*, which is a
# fill-continuation task rather than a judgment about what counts as design, and which
# leaves composition untouched by construction.
#
# It states that the reference arrives already keyed onto magenta, which is a promise the
# caller has to keep — `processing.reference.letterbox_reference` is what keeps it, and
# routers.generate/mockups run every extraction reference through it.
REFERENCE_EXTRACT_BASE = (
    "The reference image has already had its background replaced with solid magenta "
    "(#FF00FF) — but the job was left unfinished. A ragged patch of the original "
    "screenshot is still stuck around the subject: a band of colour with torn, scalloped "
    "edges hugging it. Finish the job. Flood that entire leftover patch with the same "
    "solid magenta, right up to the subject's own edge, so that when you are done the "
    "ONLY non-magenta pixels in the image are the subject itself. That patch is leftover "
    "screenshot, not a design element — not a banner, brush stroke, plate or backdrop, "
    "however much it looks like one — so do not keep, soften, fade or redraw any part of "
    "it. "
    # The fill has to be FLAT, and saying "solid magenta" is not enough to get that: asked
    # only to flood the patch, the model painted a magenta *glow* around the lettering
    # instead — magenta at the edge fading through purple into the letters. That is worse
    # than the band it replaced, because transparency.remove_background keys a flat colour
    # and a gradient leaves a purple fringe welded to the glyphs.
    "Every magenta pixel must be exactly the same flat #FF00FF: one uniform colour with "
    "no gradient, glow, halo, bloom, shading, texture or soft fade anywhere, least of all "
    "where it meets the subject, where it must stop dead at a hard edge. "
    # And it has to stay a fill rather than becoming a re-shoot. Left implicit, the model
    # treats the magenta as empty space to be used: it zooms the subject up to fill the
    # frame and re-wraps a one-line caption onto two.
    "Keep the framing exactly as given: the subject stays the same size and in the same "
    "place within the canvas, surrounded by the same margins, occupying the same fraction "
    "of the frame as it does in the reference. Do not zoom, crop, enlarge, re-centre or "
    "re-flow it to fill the space — the magenta area is not spare room to grow into. "
    "Change absolutely nothing else: the subject keeps the same shapes, proportions, "
    "colors, materials, decoration, scale, position and layout it has in the reference. "
    "This is a background fill, not a redesign and not a re-layout."
)
REFERENCE_OPS: dict[str, tuple[str, str]] = {
    "upscale": (
        "Upscale / sharpen",
        "Redraw it at markedly higher fidelity than the reference: crisp linework and "
        "smooth, clean gradients at full resolution, resolving the detail that the "
        "low-resolution reference only implies — without inventing detail it does not have.",
    ),
    "remove_text": (
        "Remove text & labels",
        "Remove every piece of text, lettering, numeral and label, filling the surface "
        "behind it cleanly as if the text had never been there.",
    ),
    "remove_icons": (
        "Remove icons & emblems",
        "Remove any icons, emblems, avatars or pictorial symbols, leaving their "
        "containers and frames intact and cleanly empty.",
    ),
    "clean_edges": (
        "Clean crisp edges",
        "Render every edge sharp, clean and crisp, with no blur, fuzz, halo or "
        "antialiasing artifacts. Also remove any small stray marks, smudges, speckles or "
        "discoloured patches that are leftover fill/compositing noise rather than part of "
        "the actual design — a flat or gradient surface should read as clean and "
        "intentional, not textured with unexplained blemishes.",
    ),
    "keep_colors": (
        "Preserve exact colors",
        "Keep exactly the reference's colors — same hues, saturation and shading. Do not "
        "recolor, restyle or shift the palette.",
    ),
    "fix_symmetry": (
        "Fix border symmetry",
        "Correct the geometry so the borders are perfectly even: left border thickness "
        "equal to right, top equal to bottom, and all four corners the same radius.",
    ),
    "flatten": (
        "Flatten centre detail",
        "Flatten the interior — remove bevels, gloss, inner shadows and surface texture "
        "from the centre so it reads as a clean flat fill — while keeping the outer frame.",
    ),
    "text_only": (
        "Extract lettering only",
        "Reproduce ONLY the lettering, numerals and labels exactly as they appear in the "
        "reference — same words, font, weight, color, outline, gradient and shadow, at the "
        "same scale and position. Keep only what the letters are made of: their fill, the "
        "hard-edged outline drawn around them, and the crisp shadow attached to them. "
        "Everything else is backing and must be gone — not only buttons, frames, panels "
        "and icons, but any soft haze, mist, smoke, glow, halo, blur, coloured wash or "
        "cloudy band lying around or behind the words, even where it looks like it belongs "
        "to the text. Every pixel outside the letters' own outlined silhouette must be "
        "empty background. Keep the words on exactly the same number of lines, in the same "
        "order and with the same line breaks as the reference — never re-wrap, re-flow, "
        "re-space or re-balance them onto a different number of lines.",
    ),
    "element_only": (
        "Extract element only",
        "Reproduce ONLY the element itself — the single foreground subject the image is of "
        "— exactly as it appears in the reference: same silhouette, proportions, colors, "
        "materials, decoration and any lettering carried on it. Delete everything behind "
        "and around it: the background, backdrop, scene, any panel, plate, card or "
        "surface it is sitting on or in front of, and any soft haze, mist, smoke, glow, "
        "halo, blur or coloured wash lying around it. Everywhere outside the element's own "
        "silhouette must be empty background. Do not replace what you removed with a new "
        "backing shape, frame, shadow or glow, and do not move, rescale or re-centre the "
        "element to fill the space it leaves behind.",
    ),
}

# A few ops answer the same question with different objects ("keep only WHAT?"), so the UI
# shows them as one toggle with a picker rather than N near-identical chips — ticking one
# choice is meant to exclude the others (keeping only the text and only the element at once
# is a contradiction the model resolves by guessing). This is only a presentation grouping:
# the members stay ordinary REFERENCE_OPS keys, so anything that stores or replays ops —
# saved assets, the mockup pipeline's ["text_only", "upscale"], experiment plans — is
# untouched. The catalogue endpoint drops grouped keys from its flat list so a key can
# never render as both a standalone chip and a picker option.
REFERENCE_OP_GROUPS: list[dict] = [
    {
        "key": "keep_only",
        "label": "Keep only",
        "help": "Keep just this part of the reference and drop everything else, "
                "background included.",
        "exclusive": True,
        "choices": [
            {"key": "element_only", "label": "Element"},
            {"key": "text_only", "label": "Text"},
        ],
    },
]


def grouped_op_keys() -> set[str]:
    """Op keys that belong to a REFERENCE_OP_GROUPS picker rather than the flat chip list."""
    return {c["key"] for g in REFERENCE_OP_GROUPS for c in g["choices"]}


def extraction_op_keys() -> set[str]:
    """Ops that delete most of the reference instead of modifying it, and so need
    REFERENCE_EXTRACT_BASE rather than REFERENCE_BASE.

    Derived from the "keep only" group rather than listed separately, so adding a third
    thing to keep (say "frame only") gets the right framing without a second edit here."""
    return {c["key"] for g in REFERENCE_OP_GROUPS if g["key"] == "keep_only"
            for c in g["choices"]}


# Alternative wordings of the reference-mode prompt, for A/B measurement against the
# fidelity scorer (see tools/sweep_polish_text.py). Only the *instruction text* varies —
# REFERENCE_OPS above stays the single source of the UI labels, so a variant can never
# fork what the checkboxes are called.
#
# "v1" is today's wording and is the default everywhere, so behaviour is unchanged unless
# a caller explicitly asks for another variant. Each variant states the hypothesis it is
# testing; `ops` holds only the keys whose text it actually changes, and anything absent
# falls back to v1 — so a diff between variants reads as the experiment itself.
REFERENCE_VARIANTS: dict[str, dict] = {
    "v1": {
        "why": "The wording currently shipped. Extraction framing measured best on asset 44 (0.3% leftover band, vs 15.2% for the pre-2026-08-04 wording).",
        "base": REFERENCE_BASE,
        "join": lambda base, chosen: (
            f"{base} Change ONLY the following, and nothing else: "
            + " ".join(f"({i}) {t}" for i, t in enumerate(chosen, 1))
        ),
        "bare": lambda base: f"{base} Reproduce it as-is, changing nothing.",
        # "Change ONLY the following, and nothing else" is the wrong sentence for an
        # extraction: it promises that everything unlisted survives, which is the opposite
        # of what a keep-only op asks for. The extraction join instead presents the points
        # as the definition of the subject being kept.
        "extract_base": REFERENCE_EXTRACT_BASE,
        "extract_join": lambda base, chosen: (
            f"{base} The subject to keep is: "
            + " ".join(f"({i}) {t}" for i, t in enumerate(chosen, 1))
        ),
        "ops": {},
    },
    # ---- extraction wordings that lost, kept as the record of the experiment ----
    #
    # Measured on asset 44 ("Element 10") with ops [text_only, clean_edges, keep_colors],
    # scored as the share of opaque output pixels that are neither caption-warm nor
    # near-neutral — i.e. the leftover band, which is desaturated blue-grey and green. The
    # input reference itself scores 6.9%, and the wording shipped before this work scored
    # 15.2% on gpt_image_2/low: it made the band *worse* than doing nothing.
    #
    # The headline finding is that on gpt_image_2 no wording worked — every candidate
    # landed between 8.7% and 21.7%, indistinguishable failures, at `quality: low` and at
    # `quality: high` alike. flux_kontext misspelled the caption (26.0%) and seedream_v4_5
    # returned a near-blank image (0.3% only because there was almost nothing to score,
    # which is why the metric is never read without looking at the image). Only
    # nano_banana_pro could act on any of it, and only the shipped wording above, on a
    # letterboxed reference, cleared the band without re-wrapping the caption: 0.3%.
    "x-cutout": {
        # Hypothesis: the model needs a mental model it already has. "Extraction" is
        # abstract; a cutout/alpha-mask is a concrete operation whose result is
        # unambiguous — pixels are either kept verbatim or fully transparent, with no
        # third option for "keep a bit of the background because it looks attached".
        "why": "LOST (10.9%). Frames keep-only as a cutout/alpha mask rather than a redraw — tested whether a concrete masking metaphor beats a described one. It did not, and the caption re-wrapped onto two lines.",
        "extract_base": (
            "Treat the reference image as a layered composite and perform a cutout. Exactly "
            "one thing in it is the subject; everything else — background, backdrop, panels, "
            "plates, and every soft haze, glow, blur or coloured wash lying around the "
            "subject — is a separate layer to be thrown away entirely. Output the subject "
            "layer alone, pixel-faithful to the reference: identical shapes, proportions, "
            "colors, materials, decoration, scale and position, with nothing redesigned, "
            "restyled or tidied. Where a thrown-away layer used to be there must be empty "
            "background — not a fill, not a fade, not a replacement shape. If part of the "
            "reference is ambiguous, ask whether it is made of the subject's own material; "
            "if it is not, it goes."
        ),
        "extract_join": lambda base, chosen: (
            f"{base} The subject to cut out, and how to treat it: "
            + " ".join(f"({i}) {t}" for i, t in enumerate(chosen, 1))
        ),
        "ops": {},
    },
    "x-torn": {
        # Round 1+2 finding: every model kept the band, so it is not being read as
        # "background" at all. A ragged-edged colour patch behind a title is a real
        # game-UI idiom (a grunge brush-stroke banner), and the reference genuinely looks
        # like one — so "delete the background" leaves it standing, and "delete the soft
        # haze" misses it because its edges are torn, not soft. This wording names it as a
        # cutting artefact and explicitly denies it design-element status.
        "why": "LOST on gpt_image_2 (8.7%), but the first wording nano_banana_pro could act on (1.0%, band gone) — the finding that moved this from a prompt problem to a model problem. Rejected only because it re-wrapped the caption onto two lines.",
        "extract_base": (
            "Output ONLY the subject named below, isolated on empty background. Be aware of "
            "how the reference was made: it was crudely cut out of a screenshot, and the cut "
            "left a ragged patch of the original screen still stuck to the subject — a "
            "band of colour behind and around it with torn, scalloped, frayed edges. That "
            "patch is leftover screenshot, NOT a design element. It is not a banner, brush "
            "stroke, paint smear, plaque, ribbon, plate or backdrop, no matter how much it "
            "looks like one, and it must not appear anywhere in your output. Delete it "
            "completely and put nothing in its place — no fill, no fade, no glow, no "
            "replacement shape. The subject itself is reproduced faithfully: same shapes, "
            "proportions, colors, materials, decoration, scale and position as the "
            "reference, redesigned in no way."
        ),
        "extract_join": lambda base, chosen: (
            f"{base} The subject is: " + " ".join(f"({i}) {t}" for i, t in enumerate(chosen, 1))
        ),
        "ops": {},
    },
    "x-glyphs": {
        # Every wording so far describes what to delete, which requires the model to first
        # agree that the band is deletable. This one never argues: it states a pixel-level
        # acceptance test for the output, so anything that is not the subject fails by
        # construction rather than by classification.
        "why": "LOST, worst of the extraction wordings (17.6%). Stated a pixel-level acceptance test instead of describing what to delete; the model answered it by turning the band into a spiky starburst — i.e. by classifying it as design even harder.",
        "extract_base": (
            "Produce a cut-out sprite. Acceptance test for your output, applied pixel by "
            "pixel: a pixel may be filled ONLY if it is part of the subject named below — "
            "its own fill, its own outline, or the crisp shadow drawn tight against it. "
            "Every other pixel in the image must be completely empty background. There is "
            "no third category: anything in the reference that is not the subject — "
            "backdrop, panel, plate, band, patch, wash, haze, glow, ragged colour fragment "
            "— fails the test and must be absent, whether or not it looks intentional and "
            "whether or not it touches the subject. Do not fill, fade or replace what you "
            "leave out. Within the subject, copy the reference exactly: same shapes, "
            "proportions, colors, materials, decoration, scale and position, nothing "
            "redesigned, restyled, re-laid-out or tidied."
        ),
        "extract_join": lambda base, chosen: (
            f"{base} The subject is: " + " ".join(f"({i}) {t}" for i, t in enumerate(chosen, 1))
        ),
        "ops": {},
    },
    "x-negative": {
        # Hypothesis: v1's extraction base still spends most of its words on fidelity, and
        # the failure was never infidelity — it was over-inclusion. This one leads with the
        # deletion, names the exact defect class observed (a soft band that reads as part
        # of the artwork), and puts fidelity second.
        "why": "LOST (15.9%, no better than the wording it replaced). Deletion-first wording naming the observed failure — but it describes the band as soft/hazy, and the band's edges are torn, not soft, which is what x-torn went on to fix.",
        "extract_base": (
            "Output ONLY the subject described below, isolated on empty background. The "
            "reference image is NOT clean: around and behind the subject it carries "
            "leftover background — soft haze, smoke, mist, glow, blur, coloured bands and "
            "stray patches. That leftover is the thing you are being asked to get rid of. "
            "It is not part of the artwork, however attached it may look, and reproducing "
            "it is the single failure mode of this task. Everything that is not the subject "
            "itself must be fully empty background, with no replacement fill, plate, fade "
            "or glow. The subject itself is reproduced faithfully: same shapes, "
            "proportions, colors, materials, decoration, scale and position as the "
            "reference, redesigned in no way."
        ),
        "extract_join": lambda base, chosen: (
            f"{base} The subject is: " + " ".join(f"({i}) {t}" for i, t in enumerate(chosen, 1))
        ),
        "ops": {},
    },
    "v2": {
        # The observed failure is not that the model ignores the reference — it is that it
        # treats it as a starting point and improves on it (a plain nav bar came back as a
        # bordered panel with an invented header stripe). v1 says what to keep; v2 adds
        # what is forbidden, since a positive instruction alone leaves "tidying up" open.
        "why": "Hard negative constraints on top of v1 — targets the model redesigning rather than reproducing.",
        "base": (
            REFERENCE_BASE
            + " Do not add, remove, move or resize any element. Do not change the outline "
            "silhouette, the corner radii, or the border thickness. Keep the same "
            "orientation and framing, with the subject occupying the same portion of the "
            "canvas as in the reference. If you are unsure whether a detail belongs, keep "
            "it exactly as the reference has it — never invent, complete or tidy anything."
        ),
        "join": lambda base, chosen: (
            f"{base} Change ONLY the following, and nothing else: "
            + " ".join(f"({i}) {t}" for i, t in enumerate(chosen, 1))
        ),
        "bare": lambda base: f"{base} Reproduce it as-is, changing nothing.",
        "ops": {
            "upscale": (
                "Redraw it at markedly higher fidelity than the reference: crisp linework "
                "and smooth, clean gradients at full resolution, resolving the detail that "
                "the low-resolution reference only implies. Add no detail, ornament or "
                "texture that is not already visibly present in the reference.",
            )[0],
            "remove_text": (
                "Remove every piece of text, lettering, numeral and label, filling the "
                "surface behind it with a continuation of the exact surrounding material — "
                "same colour, same gradient direction, same texture — as if the text had "
                "never been there. Do not add a plate, panel, shadow or highlight where the "
                "text used to be.",
            )[0],
            "text_only": (
                "Reproduce ONLY the lettering, numerals and labels exactly as they appear in "
                "the reference — same words, spelling, capitalisation, font, weight, colour, "
                "outline, gradient and shadow, at the same scale and position. Every other "
                "pixel must be empty background: no button, frame, icon, plate, glow or "
                "backing shape of any kind. Do not re-letter, re-spell or re-space the words.",
            )[0],
        },
    },
    "v3": {
        # v1 is one long multi-clause paragraph in which the fidelity requirement competes
        # with everything else for attention. v3 keeps the same requirements but front-loads
        # the task in one line and reduces each op to a terse imperative, testing whether
        # the dilution — not the content — is what costs fidelity.
        "why": "Same requirements as v1, restructured into short imperatives — tests whether prose dilution costs fidelity.",
        "base": (
            "Copy the reference image exactly. Output the same element, redrawn — this is a "
            "reproduction, not a redesign.\n"
            "KEEP IDENTICAL: silhouette, proportions, layout, colours, materials, "
            "decoration, artwork.\n"
            "CHANGE: nothing that is not listed below."
        ),
        "join": lambda base, chosen: (
            f"{base}\nCHANGE ONLY:\n" + "\n".join(f"{i}. {t}" for i, t in enumerate(chosen, 1))
        ),
        "bare": lambda base: f"{base}\nCHANGE ONLY: nothing. Reproduce it as-is.",
        "ops": {
            "upscale": "Draw it sharper and at higher resolution. Resolve detail the reference implies; invent none.",
            "remove_text": "Delete all text, letters and numerals. Continue the surrounding surface behind them.",
            "remove_icons": "Delete all icons, emblems and avatars. Leave their containers intact and empty.",
            "clean_edges": "Make every edge sharp and clean. No blur, halo or stray speckles.",
            "keep_colors": "Use the reference's exact colours. No recolouring or palette shift.",
            "fix_symmetry": "Make borders even: left thickness equals right, top equals bottom, all corner radii equal.",
            "flatten": "Flatten the interior to a clean fill. Keep the outer frame.",
            "text_only": "Draw ONLY the lettering, same words and styling, same scale and position. Everything else empty.",
            "element_only": "Draw ONLY the foreground element, unchanged and in place. Delete the background and any panel behind it. Add no replacement backing.",
        },
    },
}

# A variant only has to spell out what it actually changes; everything it leaves out is
# v1's. Without this, a wording that exists purely to test one extraction paragraph would
# still have to restate the base, join, bare and ops it does not touch — and a diff
# between two variants would stop being readable as the experiment.
for _spec in REFERENCE_VARIANTS.values():
    for _k, _v in REFERENCE_VARIANTS["v1"].items():
        _spec.setdefault(_k, _v)
del _spec, _k, _v


def reference_instruction(
    ops: list[str] | None, variant: str = "v1", dynamic: dict[str, str] | None = None,
) -> str:
    """The whole user-facing prompt for reference mode, derived from ticked ops.

    `variant` selects a wording from REFERENCE_VARIANTS; an unknown name falls back to
    "v1" rather than raising, so a stale variant name saved on a region can never break a
    build — it just runs the shipped prompt.

    `dynamic` overrides a specific op's instruction text for this one call only — used by
    the Text step to name the exact captions to erase/keep (see
    `build_text_removal_instruction`) instead of the static "remove all text" wording.
    Takes priority over the variant's own override, but only for keys actually passed."""
    spec = REFERENCE_VARIANTS.get(variant) or REFERENCE_VARIANTS["v1"]
    overrides = spec["ops"]
    dynamic = dynamic or {}
    picked = [k for k in (ops or []) if k in REFERENCE_OPS]
    # An extraction op reframes the whole task, so it is stated first and picks the base:
    # read in the other order ("keep the colors... and by the way output only the text"),
    # the model has already been told the job is reproduction by the time it gets there.
    extracting = extraction_op_keys() & set(picked)
    picked.sort(key=lambda k: k not in extracting)
    chosen = [dynamic.get(k) or overrides.get(k) or REFERENCE_OPS[k][1] for k in picked]
    # A variant that has not been given an extraction framing falls back to its own base
    # and join, so older/experimental wordings keep composing exactly as they did.
    base = (spec.get("extract_base") or spec["base"]) if extracting else spec["base"]
    join = (spec.get("extract_join") or spec["join"]) if extracting else spec["join"]
    if not chosen:
        return spec["bare"](spec["base"])
    return join(base, chosen)


def build_text_removal_instruction(remove: list[str], keep: list[str]) -> str:
    """The `remove_text` op's instruction for one region's base redraw, naming exactly
    which captions to erase and which to leave alone — the Text step's per-caption
    Keep/Remove/Extract choice is only meaningful if the model is actually told which
    words are which, rather than the blanket "delete all text" REFERENCE_OPS wording,
    which cannot express "erase this caption but leave that one exactly as it is" on an
    element that carries both.

    Falls back to the blanket wording if `remove` is somehow empty — callers only build
    this when there is at least one caption marked Remove or Extract, so this is a safety
    net, not the normal path."""
    remove_list = ", ".join(f'"{t}"' for t in remove if t)
    if not remove_list:
        return REFERENCE_OPS["remove_text"][1]
    instr = (
        f"Remove exactly this lettering, filling the surface behind it cleanly as if it "
        f"had never been there: {remove_list}."
    )
    keep_list = ", ".join(f'"{t}"' for t in keep if t)
    if keep_list:
        instr += f" Leave all other text exactly as it currently appears, unchanged: {keep_list}."
    return instr


TYPE_RULES = {
    "ui_element": (
        "Blank UI element, no text or labels unless explicitly requested. "
        "Isolated single element on a plain background, centered. "
        "CRITICAL: the element MUST have perfectly SYMMETRIC borders — the left border "
        "must be exactly the same thickness as the right border, and the top border must "
        "be exactly the same thickness as the bottom border. All four corners must have "
        "identical corner radius. The element must look perfectly uniform and balanced "
        "so it can be 9-slice scaled without distortion. "
        "All edges must be sharp, clean, and crisp with no fuzziness, blur, or antialiasing artifacts."
    ),
    "icon": (
        "Single game icon, isolated on a plain background, centered, clear silhouette, "
        "no text unless explicitly requested. "
        "Edges must be sharp, clean, and crisp — no fuzziness, blur, or soft antialiasing artifacts."
    ),
    "sprite": (
        "Single game sprite, isolated on a plain background, centered, full subject "
        "visible with a small margin, no ground shadow unless explicitly requested. "
        "Edges must be sharp, clean, and crisp — no fuzziness, blur, or soft antialiasing artifacts."
    ),
    "tile": (
        "Seamlessly tileable texture, edges wrap perfectly on all sides, uniform "
        "lighting with no vignette, fills the entire canvas."
    ),
    "sprite_sheet": (
        "Sprite sheet of animation frames arranged in a uniform grid with equal-sized "
        "cells and consistent subject placement per cell, plain background, no labels."
    ),
}


def ratio_string(w: float, h: float) -> str:
    """Normalize a width/height pair into a compact 'W:H' aspect ratio string."""
    if w <= 0 or h <= 0:
        return "1:1"
    ratio = w / h
    if abs(ratio - 1) < 0.02:
        return "1:1"
    return f"{ratio:.2f}:1"


def resolution_instruction(resolution: str | None) -> str:
    """Soft design guidance (not a hard pixel-count promise — the actual size is
    enforced deterministically afterward by processing.trim.fit_to_resolution). Small
    target sizes get an extra nudge toward bold, simple shapes, since fine detail is
    invisible at icon scale anyway."""
    if not resolution:
        return ""
    try:
        w, h = (int(v) for v in resolution.lower().split("x"))
    except (ValueError, AttributeError):
        return ""
    note = (
        " Keep shapes bold and simple with a clear silhouette — fine detail will be "
        "lost at this size." if max(w, h) <= 320 else ""
    )
    return (
        f"The final asset will be displayed at roughly {w}x{h}px, so pitch the level of "
        f"detail for that size. This is a display hint only, NOT an output canvas spec: do "
        f"not resize, rescale, stretch, or squash the generated image to hit those exact "
        f"pixel dimensions — the correct size is applied afterwards.{note}"
    )


def aspect_instruction(aspect_ratio: str | None) -> str:
    """Explicit width:height instruction so the model doesn't fall back to its own
    default canvas shape — the #1 cause of an asset coming back the wrong shape for
    the UI slot it needs to fill, especially when pasted into an external LLM that has
    no idea what rect this is going into."""
    if not aspect_ratio:
        return ""
    return (
        f"Draw the subject itself at exactly {aspect_ratio} (width:height), filling the "
        "frame with no extra canvas, letterboxing, or padding outside its bounding box. "
        "Compose it at that shape from the start — never stretch, squash, or otherwise "
        "non-uniformly rescale the artwork to reach the ratio: circles must stay "
        "perfectly circular and borders keep an even thickness all the way round."
    )


def compose_sections(
    project: Project, asset_type: str, user_prompt: str,
    aspect_ratio: str | None = None, resolution: str | None = None,
    is_sliced: bool | None = None,
    override_entire_prompt: bool = False,
    prompt_mode: str = "generate", reference_ops: list[str] | None = None,
) -> dict:
    if override_entire_prompt:
        return {
            "style": "",
            "rules": "",
            "aspect": "",
            "resolution": "",
            "user": user_prompt.strip(),
            "prompt_mode": prompt_mode,
            "override_entire_prompt": True,
        }
    # Reference mode replaces the free-text prompt with one derived from the ticked ops,
    # and drops the project art-style block: restyling is the opposite of reproducing a
    # reference faithfully, and leaving it in is what makes the model "improve" the art
    # it was told to copy. TYPE_RULES is dropped for the same reason — every entry is
    # written for GENERATING a new element from scratch ("isolated on a plain background,
    # centered", "MUST have perfectly symmetric borders... all four corners identical
    # radius", "fills the entire canvas"), which is exactly backwards for a shape that is
    # already drawn and just needs reproducing: told to keep the reference's exact shape
    # AND to make its (non-symmetric, e.g. a bar rounded only on top) borders symmetric,
    # the model followed the CRITICAL/MUST-worded rule over the reference — measured on a
    # 5-tab nav bar, that is what turned a plain bottom bar into an invented bordered panel
    # with a fabricated header stripe. Nothing worth keeping is lost: the one universally
    # applicable bit, sharp/crisp edges, is already its own tickable op (`clean_edges`).
    if prompt_mode == "reference":
        # The aspect instruction is dropped for an extraction, and keeping it was a real
        # bug: it says to draw the subject "filling the frame with no extra canvas,
        # letterboxing, or padding outside its bounding box", which is the exact opposite
        # of what an extraction reference now is — a subject sitting inside a magenta
        # letterbox, whose framing the model is being asked to preserve. Told both, the
        # model obeyed the aspect line, enlarged a 16.68:1 caption strip until it filled a
        # 21:9 frame, and re-wrapped it onto two lines to make it fit. Nothing is lost by
        # dropping it: the model no longer chooses the framing (the letterboxed reference
        # does), and the asset's real aspect is applied deterministically afterwards by
        # processing.trim.trim_for_fit, exactly as it is for every other path.
        extracting = bool(extraction_op_keys() & set(reference_ops or []))
        return {
            "style": "",
            "rules": "",
            "aspect": "" if extracting else aspect_instruction(aspect_ratio),
            "resolution": resolution_instruction(resolution),
            "user": reference_instruction(reference_ops),
            "prompt_mode": "reference",
            "reference_ops": list(reference_ops or []),
            "override_entire_prompt": False,
        }
    rules = TYPE_RULES.get(asset_type, "")
    if is_sliced is True:
        rules += (" Designed as a 9-slice resizable UI element: the border/frame MUST be "
                  "perfectly symmetric on all four sides (left = right thickness, top = bottom "
                  "thickness, all corners identical) with a clean, flat, stretchable center region. "
                  "The element must look correct when its center is stretched horizontally or vertically.")
    elif is_sliced is False:
        rules += " Designed as a non-sliced single sprite image (standalone graphic, no 9-slice borders)."
    return {
        "style": project.style_description.strip(),
        "rules": rules.strip(),
        "aspect": aspect_instruction(aspect_ratio),
        "resolution": resolution_instruction(resolution),
        "user": user_prompt.strip(),
        "prompt_mode": "generate",
        "override_entire_prompt": False,
    }


def compose_prompt(
    project: Project, asset: Asset, user_prompt: str | None = None,
    override_entire_prompt: bool | None = None,
) -> str:
    is_override = override_entire_prompt if override_entire_prompt is not None else getattr(asset, "override_entire_prompt", False)
    prompt_text = user_prompt if user_prompt is not None else asset.prompt
    if is_override:
        return prompt_text.strip()

    sections = compose_sections(
        project, asset.type, prompt_text,
        asset.aspect_ratio, asset.resolution, is_sliced=(asset.nine_slice is not None),
        override_entire_prompt=False,
        prompt_mode=getattr(asset, "prompt_mode", "generate") or "generate",
        reference_ops=getattr(asset, "reference_ops", None),
    )
    # The project palette is a restyling instruction, so it belongs to generate mode only
    # — pushing it alongside "reproduce this reference exactly" is a contradiction, and
    # the model resolves it by recoloring the very thing it was told to copy.
    palette = ", ".join(project.palette) if project.palette and sections["prompt_mode"] != "reference" else ""
    parts = []
    if sections["style"]:
        parts.append(f"Art style: {sections['style']}")
    if palette:
        parts.append(f"Color palette: {palette}")
    if sections["rules"]:
        parts.append(sections["rules"])
    if sections["aspect"]:
        parts.append(sections["aspect"])
    if sections["resolution"]:
        parts.append(sections["resolution"])
    if sections["user"]:
        parts.append(sections["user"])
    return "\n".join(parts)


def external_prompt(
    project: Project, asset: Asset, user_prompt: str | None = None,
    override_entire_prompt: bool | None = None,
) -> str:
    """The exact text to copy-paste into an external LLM/chat UI by hand — the composed
    prompt plus the magenta chroma-key instruction the tool adds automatically when it
    calls an image API directly. Generating with this externally and uploading the
    result back (see /assets/{id}/upload-version) round-trips through the same
    background-removal pipeline as an in-tool generation."""
    return compose_prompt(project, asset, user_prompt, override_entire_prompt) + CHROMA_HINT
