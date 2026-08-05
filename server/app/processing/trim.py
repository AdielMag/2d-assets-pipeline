import numpy as np
from PIL import Image

MAX_RESOLUTION_DIM = 2048  # sanity cap for repeated /upscale calls


def resize_rgba(img: Image.Image, new_size: tuple[int, int]) -> Image.Image:
    """Lanczos resize that premultiplies alpha first, so colour hiding under alpha=0 can
    never bleed into the visible edge.

    PIL resamples channels independently, which means a keyed generation — where
    `remove_background` zeroes the *alpha* of the magenta backdrop but leaves its RGB in
    place — averages that magenta back into every edge pixel on the way down to the export
    size. Measured on a real 1688x2570 asset scaled to 1345x2048: 71,322 visible pixels
    came out within ΔRGB 45 of the key colour, against 249 in the source. Premultiplying
    weights each contribution by its own coverage, so a fully transparent pixel
    contributes nothing regardless of what colour it stores.

    The extraction path already decontaminates its transparent region for this same reason
    (see `estimate_foreground_ml` in processing/extract.py); this makes the guarantee hold
    for every path into a resize, including chroma-keyed generations and uploads."""
    if img.mode != "RGBA":
        return img.resize(new_size, Image.LANCZOS)
    arr = np.asarray(img).astype(np.float32)
    alpha = arr[..., 3:4] / 255.0
    premul = np.concatenate([arr[..., :3] * alpha, arr[..., 3:4]], axis=-1)
    small = np.asarray(
        Image.fromarray(np.clip(premul, 0, 255).astype(np.uint8), "RGBA").resize(new_size, Image.LANCZOS)
    ).astype(np.float32)
    out_a = small[..., 3:4]
    # Un-premultiply only where there is coverage; fully transparent pixels get a clean
    # zero rather than a divide-by-zero blowup of whatever rounding left behind.
    out_rgb = np.where(out_a > 0, small[..., :3] * 255.0 / np.maximum(out_a, 1e-6), 0.0)
    out = np.concatenate([np.clip(out_rgb, 0, 255), out_a], axis=-1)
    return Image.fromarray(out.astype(np.uint8), "RGBA")


def parse_resolution(resolution: str) -> tuple[int, int]:
    try:
        w, h = resolution.lower().split("x")
        return max(1, int(w)), max(1, int(h))
    except (ValueError, AttributeError):
        return 256, 256


def parse_aspect_ratio(aspect_ratio: str | None) -> float | None:
    """Parse a "W:H" string into a width/height ratio, or None if unset/invalid."""
    if not aspect_ratio:
        return None
    try:
        w, h = aspect_ratio.split(":")
        w, h = float(w), float(h)
        return (w / h) if w > 0 and h > 0 else None
    except (ValueError, AttributeError):
        return None


def fit_to_resolution(
    img: Image.Image, resolution: str | None, allow_upscale: bool = False
) -> Image.Image:
    """Scale img to fit within the `resolution` ("WxH") box, preserving aspect ratio —
    the final, deterministic enforcement of an asset's target size, independent of
    whatever pixel dimensions the provider actually generated at. No-op if resolution
    is unset.

    Upscaling is OFF by default. An extracted sprite only ever has as much detail as the
    screenshot it was cut from, and Lanczos-enlarging it to a 1440px-device target
    invents nothing while measurably destroying fidelity — extracted assets scored 74-98
    against the reference at native size and 32-76 after being enlarged and drawn back
    down. Store what we actually have; enlarge at export time, with super-resolution
    (see processing/upres.py), when a device target genuinely needs more pixels.
    """
    if not resolution:
        return img
    target_w, target_h = parse_resolution(resolution)
    scale = min(target_w / img.width, target_h / img.height)
    if scale > 1.0 and not allow_upscale:
        return img
    new_size = (max(1, round(img.width * scale)), max(1, round(img.height * scale)))
    if new_size == img.size:
        return img
    return resize_rgba(img, new_size)


def subject_aspect_mismatch(
    img: Image.Image, aspect_ratio: str | None, tol: float = 0.25
) -> tuple[float, float] | None:
    """(subject_ratio, target_ratio) when the keyed subject's own bounding box is more
    than `tol` off the asset's declared aspect ratio — otherwise None.

    Padding guarantees we never *add* distortion downstream, but it cannot undo art that
    arrived the wrong shape. The usual cause is the provider resizing its output to a
    literal pixel size: that stretch turns circles into ovals and thins borders on one
    axis, and the result is a squashed asset that padding will happily preserve. Detect
    it so the run reports it instead of quietly shipping."""
    target = parse_aspect_ratio(aspect_ratio)
    if not target:
        return None
    bbox = img.convert("RGBA").getchannel("A").getbbox()
    if bbox is None:
        return None
    w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
    if h <= 0:
        return None
    actual = w / h
    return (actual, target) if abs(actual - target) / target > tol else None


def trim_to_content(img: Image.Image, padding: int = 2) -> Image.Image:
    """Crop to the alpha bounding box, keeping `padding` transparent pixels."""
    rgba = img.convert("RGBA")
    bbox = rgba.getchannel("A").getbbox()
    if bbox is None:
        return rgba
    left = max(0, bbox[0] - padding)
    top = max(0, bbox[1] - padding)
    right = min(rgba.width, bbox[2] + padding)
    bottom = min(rgba.height, bbox[3] + padding)
    return rgba.crop((left, top, right, bottom))


def trim_for_fit(img: Image.Image, sliced: bool, aspect_ratio: str | None = None) -> Image.Image:
    """Crop a keyed generation to its subject *before* fit_to_resolution, so the target
    box is filled by art rather than the provider's transparent margin (generations come
    back with the subject centered in a large frame; fitting the whole frame is what
    leaves the actual art a tiny fraction of the box). 9-sliced elements get a tight trim
    so the art meets the rect edges and slice guides map correctly (Unity stretches them
    to any rect at runtime, so their stored aspect ratio doesn't matter); icons/sprites
    are re-centered with a uniform margin and, since nothing stretches them at runtime,
    padded out to `aspect_ratio` so a content bbox far from square can't silently distort
    the asset's declared shape."""
    return (
        trim_to_content(img, padding=0)
        if sliced
        else trim_and_pad(img, aspect_ratio=aspect_ratio)
    )


def trim_and_pad(
    img: Image.Image, pad_frac: float = 0.08, min_pad: int = 8, aspect_ratio: str | None = None
) -> Image.Image:
    """Crop tight to content, then re-center it on a transparent canvas with a uniform
    margin (pad_frac of the content's larger side). Guarantees the subject is centered
    with breathing room even if it originally sat off-center or against an edge. Use for
    icons/sprites; 9-sliced UI keeps a tight trim so it fills its rect.

    If `aspect_ratio` ("W:H") is given, the shorter axis gets extra transparent padding
    (beyond pad_frac) so the canvas matches it exactly. Without this, a generation whose
    keyed content sits in a much wider or taller box than the requested aspect ratio (the
    model ignored the aspect instruction, or padding above/below wasn't proportional to
    padding left/right) comes out squashed: `fit_to_resolution` scales whatever shape
    this function returns uniformly into the target box, so if that shape's aspect is
    wrong here, it stays wrong all the way to the final asset."""
    rgba = img.convert("RGBA")
    bbox = rgba.getchannel("A").getbbox()
    if bbox is None:
        return rgba
    content = rgba.crop(bbox)
    ratio = parse_aspect_ratio(aspect_ratio)
    if ratio:
        # Pad each axis in proportion to its own length, so the margin doesn't itself
        # skew the canvas ratio. A uniform pixel margin is a far bigger *fraction* of a
        # wide sprite's height than of its width, which drags the canvas toward square
        # and makes the correction below inflate the width instead — leaving correctly
        # shaped art marooned in fat side margins.
        canvas_w = content.width + 2 * max(min_pad, round(pad_frac * content.width))
        canvas_h = content.height + 2 * max(min_pad, round(pad_frac * content.height))
        if canvas_w / canvas_h < ratio:
            canvas_w = round(canvas_h * ratio)
        else:
            canvas_h = round(canvas_w / ratio)
    else:
        pad = max(min_pad, round(pad_frac * max(content.width, content.height)))
        canvas_w = content.width + 2 * pad
        canvas_h = content.height + 2 * pad

    canvas = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))
    canvas.paste(content, ((canvas_w - content.width) // 2, (canvas_h - content.height) // 2))
    return canvas
