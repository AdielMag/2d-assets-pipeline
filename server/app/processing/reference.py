"""Preparing a reference image for an extraction ("keep only X") generation.

An extracted sprite arrives as a tight, transparent-background PNG at whatever aspect its
own content happens to be. Handed to an image model that way, two things go wrong, and
both were measured on asset 44 ("Element 10", a 16.68:1 caption strip):

1. **The canvas cannot hold it.** Every model declares a fixed menu of output shapes —
   nano_banana_pro tops out at 21:9, gpt_image_2 at 16:9 — so a 16.68:1 strip is asking
   for a composition the frame does not have. The model resolves that by re-laying-out the
   artwork to fill the frame: the caption came back re-wrapped onto two lines, which is a
   redesign of the very thing it was told to reproduce.
2. **The prompt's premise is false.** The extraction wording (prompting.REFERENCE_BASE's
   extraction counterpart) works by telling the model that the background has *already*
   been keyed to magenta and one ragged patch was missed — turning "decide what counts as
   background and delete it" into "continue this fill", which is a far easier instruction
   to follow. That sentence has to be true of the image the model actually receives.

Letterboxing the reference onto a magenta canvas of the nearest shape the model supports
fixes both: the composition it is copying now fits the frame it must draw into, and the
magenta premise is true by construction. The padding colour is the same #FF00FF the chroma
hint already reserves, so the padding doubles as a worked example of the answer, and
`transparency.remove_background` strips it from the result exactly as it always has.
"""
from __future__ import annotations

import math
from pathlib import Path

from PIL import Image

# The one reserved key colour, shared with prompting.CHROMA_HINT and
# processing.transparency.remove_background.
CHROMA = (255, 0, 255, 255)


def snap_ratio(width: int, height: int, allowed: list[str]) -> str | None:
    """The `allowed` entry closest to a real w:h, compared in log space.

    Log space so a ratio and its reciprocal are equidistant from square — picking by raw
    difference biases toward the landscape end of the enum. Same rule as the Higgsfield
    provider's `auto` aspect resolution, kept here so the two cannot disagree about which
    canvas a given reference belongs in.
    """
    if not allowed or width <= 0 or height <= 0:
        return None
    target = math.log(width / height)
    best, best_err = None, None
    for opt in allowed:
        try:
            a, b = opt.split(":")
            err = abs(math.log(float(a) / float(b)) - target)
        except (ValueError, ZeroDivisionError):
            continue
        if best_err is None or err < best_err:
            best, best_err = opt, err
    return best


def letterbox_reference(src: Path, dest: Path, ratio: str | None = None) -> Path:
    """Write `src` centred on a solid-magenta canvas of `ratio`, and return `dest`.

    `ratio` is a "w:h" string, normally from `snap_ratio` against the target model's
    declared aspect enum. With `ratio` None — a provider that declares no canvas menu, so
    there is no shape to reconcile — the canvas is the reference's own size and the only
    effect is flattening transparency onto the key colour, which is still needed to keep
    the prompt's "background is already magenta" premise true.

    The canvas never shrinks the artwork: it grows whichever side is short, so the subject
    is copied at its original pixel size rather than resampled before the model sees it.
    """
    with Image.open(src) as im:
        ref = im.convert("RGBA")

    canvas_w, canvas_h = ref.width, ref.height
    if ratio:
        try:
            rw, rh = (float(v) for v in ratio.split(":"))
            if rw > 0 and rh > 0:
                canvas_h = max(canvas_h, round(canvas_w * rh / rw))
                canvas_w = max(canvas_w, round(canvas_h * rw / rh))
        except (ValueError, ZeroDivisionError):
            pass  # a malformed enum entry is not worth failing a generation over

    canvas = Image.new("RGBA", (canvas_w, canvas_h), CHROMA)
    canvas.alpha_composite(ref, ((canvas_w - ref.width) // 2, (canvas_h - ref.height) // 2))
    dest.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(dest)
    return dest
