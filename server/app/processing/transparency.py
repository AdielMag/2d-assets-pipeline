"""Background removal for providers without native alpha (Gemini/Nano Banana).

Strategy: the generation prompt asks for a solid magenta background. If the image
borders are dominated by one flat color, chroma-key it out (with soft threshold and
defringe). If the borders are not uniform (model ignored the instruction), fall back
to rembg when installed.

`remove_background` above is the *generation* path and keys the one reserved colour it
asks for, #FF00FF. `remove_flat_background` at the bottom is the *import* path, for art
made elsewhere on whatever flat backdrop that tool happened to use — see its docstring.
"""
import numpy as np
from PIL import Image


def _corner_pixels(rgb: np.ndarray, frac: float = 0.06) -> np.ndarray:
    """Pixels from the four corner patches. Corners are reliably background for a
    centered, isolated subject even when the subject bleeds to the top/bottom edges
    (a wide button), unlike a full 2px border strip."""
    h, w = rgb.shape[:2]
    ch, cw = max(2, int(h * frac)), max(2, int(w * frac))
    return np.concatenate([
        rgb[:ch, :cw].reshape(-1, 3), rgb[:ch, -cw:].reshape(-1, 3),
        rgb[-ch:, :cw].reshape(-1, 3), rgb[-ch:, -cw:].reshape(-1, 3),
    ])


def _border_color(rgb: np.ndarray) -> tuple[np.ndarray, float]:
    """Median color of the corner patches and the fraction of them near it."""
    border = _corner_pixels(rgb)
    color = np.median(border, axis=0)
    dist = np.linalg.norm(border.astype(np.float32) - color, axis=1)
    return color, float((dist < 40).mean())


def _magenta_score(rgb: np.ndarray) -> np.ndarray:
    """How magenta a pixel is: how much red and blue both exceed green. Pure magenta
    (#FF00FF) scores 255; reds, purples, golds, blues and whites score far lower, so
    this separates the magenta key from the art even when Euclidean distance is small."""
    r, g, b = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    return (r + b) / 2.0 - g


def remove_background(img: Image.Image) -> Image.Image:
    rgba = img.convert("RGBA")
    arr = np.array(rgba)
    rgb = arr[..., :3].astype(np.float32)

    color, uniform_frac = _border_color(arr[..., :3])
    score = _magenta_score(rgb)

    # The key is always magenta (#FF00FF), whose score (>=170) is far above any subject
    # color our art uses (gold/red/blue/purple all score < ~110). So detect the key by an
    # absolute magenta threshold rather than by sampling the border — this is robust when
    # the subject fills the corners/edges (e.g. a trophy's laurel wreath) and would fool
    # corner sampling. Proceed if a real magenta backdrop is present, OR if there is even a
    # small patch of near-pure magenta residue: an "empty frame" (a pill/panel) can cover
    # almost the whole canvas with its own art, leaving the magenta key only in the rounded
    # corners — a few hundred pixels, well under 2% — which must still be keyed out rather
    # than baked in as pink corner triangles. #FF00FF is a reserved key color, never art,
    # so any near-pure-magenta pixel is always background.
    magenta_frac = float((score > 160).mean())
    residue_px = int((score > 195).sum())
    if magenta_frac > 0.02 or residue_px >= 4:
        color = np.median(rgb[score > 170], axis=0)  # actual key color, for defringing
        opaque_at, transp_at = 115.0, 155.0  # subjects below 115, magenta above 155
        # 1 = keep (score at/below opaque_at), 0 = drop (score at/above transp_at)
        alpha = 1.0 - np.clip((score - opaque_at) / (transp_at - opaque_at), 0.0, 1.0)

        from scipy import ndimage
        # Second pass: a gradient key (darker magenta shadow under the subject) can score
        # below transp_at. Remove any magenta-ish pixel *connected to the image border*
        # (the true backdrop) down to a lower score. Enclosed magenta subjects (a rune
        # ring sealed by its own glow) are not border-connected, so they survive.
        strong = score > 120.0
        labels, n = ndimage.label(strong)
        if n:
            edge = set(labels[0]) | set(labels[-1]) | set(labels[:, 0]) | set(labels[:, -1])
            edge.discard(0)
            if edge:
                alpha = np.where(np.isin(labels, list(edge)), 0.0, alpha)

        # Erode the opaque region by 2px to shave the anti-aliased magenta halo that rings
        # the subject where its edge blended into the key color.
        alpha = alpha * ndimage.binary_erosion(alpha > 0.25, iterations=2)

        out = arr.copy()
        out[..., 3] = (alpha * 255).astype(np.uint8)
        # defringe: pull soft-edge pixels away from the key color
        edge = (alpha > 0) & (alpha < 1)
        if edge.any():
            blend = alpha[edge][..., None]
            keyed = rgb[edge]
            defringed = (keyed - color * (1 - blend)) / np.maximum(blend, 0.2)
            out[..., :3][edge] = np.clip(defringed, 0, 255).astype(np.uint8)
        return Image.fromarray(out)

    # Not the reserved key. Before guessing a silhouette, try keying whatever flat backdrop
    # the art *actually* arrived on — art imported from an external tool keeps that tool's
    # own backdrop, and Higgsfield's dusty pink (~#B72B78) scores 136 here, under the 160
    # threshold above, so it never took the chroma path. Re-deriving such an asset (
    # /upscale, /downscale, regenerate) used to land straight on rembg and hand back the
    # backdrop it was supposed to remove: measured on a real Higgsfield import, 116,659
    # visible pixels within ΔRGB 45 of the backdrop colour, against 249 for the sampling
    # keyer the upload path uses. `remove_flat_background` falls through to rembg itself
    # when the borders genuinely aren't flat, so nothing that reached rembg for a good
    # reason stops doing so.
    return remove_flat_background(rgba)


def _rembg_fallback(rgba: Image.Image) -> Image.Image:
    try:
        from rembg import remove
    except ImportError:
        # No uniform background and no rembg — return unchanged rather than fail.
        return rgba
    return remove(rgba)


# --- import path: key whatever flat backdrop the art actually arrived on ---------------

# All in CIE76 ΔE against the sampled backdrop colour. Measured on real Higgsfield
# character art: backdrop pixels sit under ΔE 5, solid art starts around ΔE 56, and
# genuinely translucent passages (smoke, glow, flame) land in between.
BACKDROP_DE = 8.0   # at or below this a pixel IS the backdrop — fully transparent
SOLID_DE = 55.0     # at or above this a pixel is opaque art, colour left untouched
MIN_UNIFORM = 0.6   # fraction of the sampled border that must agree for a flat backdrop
TOUCH_RADIUS = 2    # px from the backdrop within which a pixel may be partly transparent
UNKNOWN = 0.5       # trimap value for "the key isn't sure" — resolved by matting
MATTE_MAX_DIM = 900     # longest side the matting solve runs at (~4s for a 4MP source)
MATTE_MIN_BAND = 0.02   # skip the solve when the uncertain band is this small a fraction


def _to_lab(rgb: np.ndarray) -> np.ndarray:
    from skimage.color import rgb2lab

    return rgb2lab(rgb.astype(np.float32) / 255.0)


def _despill(rgb: np.ndarray, alpha: np.ndarray, backdrop_lab: np.ndarray) -> np.ndarray:
    """Pull unmixed colour back out of the backdrop's opposite hue.

    Unmixing divides by alpha, so wherever alpha is a little low the subtraction overshoots
    and the pixel lands *past* neutral, in the hue opposite the backdrop — pink backdrops
    leave green-tinged smoke. The overshoot is the chroma component pointing away from the
    backdrop's own hue, so removing that component is a targeted correction, not a general
    desaturation: art that leans the same way as the backdrop (the banshee's purple flames
    against pink) projects positive and is left exactly as it was.

    Scaled by `1 - alpha`: the more transparent the pixel, the larger the division error
    and the less its colour was ever trustworthy. Near-opaque pixels are left alone, so a
    genuinely complementary translucent colour is only touched where it is already faint."""
    from skimage.color import lab2rgb, rgb2lab

    chroma = backdrop_lab[1:]
    norm = float(np.linalg.norm(chroma))
    if norm < 1e-6:  # a grey backdrop has no hue to overshoot past
        return rgb
    direction = chroma / norm
    lab = rgb2lab(rgb.reshape(-1, 1, 3) / 255.0).reshape(-1, 3)
    overshoot = np.clip(-(lab[:, 1:] @ direction), 0.0, None)
    lab[:, 1:] += direction * (overshoot * (1.0 - alpha))[:, None]
    return np.clip(lab2rgb(lab.reshape(-1, 1, 3)).reshape(-1, 3) * 255.0, 0, 255)


def _border_ring(shape: tuple[int, int], frac: float = 0.015) -> np.ndarray:
    h, w = shape
    t = max(2, int(min(h, w) * frac))
    ring = np.zeros((h, w), bool)
    ring[:t, :] = ring[-t:, :] = ring[:, :t] = ring[:, -t:] = True
    return ring


def _sample_backdrop(lab: np.ndarray) -> tuple[np.ndarray, float] | None:
    """Median Lab of the frame's outer ring and the fraction of it that agrees, or None
    when nothing flat enough is there to key.

    The ring is tried first because it samples all four edges; if the subject bleeds off
    one of them the corners are tried instead, which is what the magenta path already
    relies on for wide elements."""
    best = None
    for mask in (_border_ring(lab.shape[:2]), _corner_mask(lab.shape[:2])):
        samples = lab[mask]
        colour = np.median(samples, axis=0)
        uniform = float((np.linalg.norm(samples - colour, axis=-1) < BACKDROP_DE).mean())
        if best is None or uniform > best[1]:
            best = (colour, uniform)
    return best if best and best[1] >= MIN_UNIFORM else None


def _corner_mask(shape: tuple[int, int], frac: float = 0.06) -> np.ndarray:
    h, w = shape
    ch, cw = max(2, int(h * frac)), max(2, int(w * frac))
    mask = np.zeros((h, w), bool)
    mask[:ch, :cw] = mask[:ch, -cw:] = mask[-ch:, :cw] = mask[-ch:, -cw:] = True
    return mask


def _min_alpha(rgb: np.ndarray, backdrop: np.ndarray) -> np.ndarray:
    """The smallest alpha that can explain each pixel as `alpha * F + (1-alpha) * backdrop`
    for some in-gamut foreground colour F.

    This is what recovers translucent art. Smoke drawn over a pink backdrop is not "80%
    pink art", it is white art at ~65% opacity, and only unmixing tells the two apart —
    thresholding on colour distance alone has to call it either opaque (keeping the pink
    tint, which is the residue) or background (deleting the smoke). Solving F's gamut
    bound per channel gives the alpha below which no valid F exists, so unmixing at
    exactly this alpha can never produce an out-of-range colour."""
    img = rgb.astype(np.float32)
    bg = backdrop.astype(np.float32)
    # F >= 0 needs alpha >= (bg - I) / bg; F <= 255 needs alpha >= (I - bg) / (255 - bg).
    below = (bg - img) / np.maximum(bg, 1e-6)
    above = (img - bg) / np.maximum(255.0 - bg, 1e-6)
    return np.clip(np.maximum(below, above).max(axis=-1), 0.0, 1.0)


def _trimap(distance: np.ndarray) -> np.ndarray:
    """0 = certainly backdrop, 1 = certainly art, 0.5 = the solver's problem.

    Only the certain bands are asserted here, and they are drawn conservatively. A pixel is
    called art only if it is BOTH far from the backdrop in colour AND out of reach of it in
    space: colour alone cannot tell solid art in shadow from translucent glow (on the
    banshee the shaded dress sits at ΔE 45 and her purple flames at ΔE 40), while distance
    from the backdrop says whether a pixel could be *blended with* it at all — an
    antialiased edge and a wisp of smoke are next to backdrop, the middle of a leg is not."""
    from scipy import ndimage

    backdrop = distance <= BACKDROP_DE
    reachable = ndimage.binary_dilation(backdrop, iterations=TOUCH_RADIUS)
    tri = np.full(distance.shape, UNKNOWN, np.float32)
    tri[(distance >= SOLID_DE) & ~reachable] = 1.0
    tri[backdrop] = 0.0
    return tri


def _matted_alpha(rgb: np.ndarray, tri: np.ndarray) -> np.ndarray | None:
    """Alpha for the unknown band from a matting solve, or None if pymatting is missing.

    Thresholding a single pixel's colour cannot resolve the unknown band — the information
    isn't in that pixel. A matting solver uses the neighbourhood instead: within a small
    window, a blend of backdrop and one foreground colour falls on a line in colour space,
    so smoke reading as 65% white over pink is separable from a solid blue-grey leg that
    merely happens to sit a similar distance from the backdrop. That is the part of this
    problem worth handing to a real algorithm.

    Solved at reduced resolution and scaled back up: the unknown band is edges and soft
    passages, both low-frequency in alpha, and a full 4MP solve costs minutes for a result
    that differs by a fraction of a percent of the frame."""
    try:
        from pymatting import estimate_alpha_knn
    except ImportError:
        return None

    h, w = tri.shape
    scale = min(1.0, MATTE_MAX_DIM / max(h, w))
    small = (int(h * scale), int(w * scale))
    image = np.asarray(Image.fromarray(rgb).resize(small[::-1], Image.LANCZOS)) / 255.0
    # Nearest-neighbour so the solve is never handed an invented certainty on a band edge.
    guide = np.asarray(
        Image.fromarray((tri * 255).astype(np.uint8)).resize(small[::-1], Image.NEAREST)
    ) / 255.0
    alpha = np.clip(estimate_alpha_knn(image.astype(np.float64), guide.astype(np.float64)), 0.0, 1.0)
    return np.asarray(
        Image.fromarray((alpha * 255).astype(np.uint8)).resize((w, h), Image.BILINEAR)
    ) / 255.0


def remove_flat_background(img: Image.Image) -> Image.Image:
    """Key out whatever flat backdrop an imported image was drawn on — pink, magenta,
    grey, anything — rather than only the #FF00FF the generation path asks for.

    Art made in an external tool arrives on that tool's own backdrop. Higgsfield's, for
    instance, is a dusty pink (~#B72B78) that scores nowhere near the reserved key, so
    `remove_background` skipped its chroma path entirely and fell through to rembg, which
    guesses a silhouette from the art: it ghosted 12% of a goblin's own body away and left
    2% of the backdrop baked around a banshee. Sampling the actual backdrop instead makes
    the cut exact, because the separation in a flat-backdrop image is enormous (ΔE < 5 vs.
    ΔE > 55) — there is nothing to guess.

    The key states only what it is sure of (`_trimap`) and hands the rest to a matting
    solve (`_matted_alpha`) floored by the unmixing bound (`_min_alpha`), because the
    uncertain band is where every visible failure lives: too eager and the smoke leaving a
    gun barrel disappears, too cautious and a pink halo rides around every edge. Colour
    inside the band is then unmixed and despilled, so what survives carries none of the
    backdrop with it.

    No erosion pass: shaving 2px off the alpha is what eats thin art (wisps of smoke, hair,
    whiskers), and unmixing already removes the halo erosion was there to hide.

    Falls back to rembg when the borders aren't a flat colour at all (a photo, a full-bleed
    illustration) — there, guessing really is the only option."""
    rgba = img.convert("RGBA")
    arr = np.array(rgba)
    rgb = arr[..., :3]
    lab = _to_lab(rgb)

    sampled = _sample_backdrop(lab)
    if sampled is None:
        return _rembg_fallback(rgba)
    backdrop_lab, _ = sampled
    # The colour to unmix against has to be the RGB the Lab median actually came from.
    distance = np.linalg.norm(lab - backdrop_lab, axis=-1)
    backdrop_rgb = np.median(rgb[distance <= BACKDROP_DE], axis=0)

    tri = _trimap(distance)
    unknown = tri == UNKNOWN
    # A clean sprite is nearly all certainty — a band of a few percent is edges only, and
    # the ramp resolves those as well as a solve would, for none of the seconds.
    solved = _matted_alpha(rgb, tri) if unknown.mean() >= MATTE_MIN_BAND else None
    if solved is None:
        solved = np.clip((distance - BACKDROP_DE) / (SOLID_DE - BACKDROP_DE), 0.0, 1.0)

    # `_min_alpha` is a floor, not a vote: it is the provable minimum opacity of a pixel
    # that is not the backdrop colour. The solver is free to raise alpha above it (solid art
    # in shadow) but never to erase something the colour says is there — left to itself the
    # solver regularises the goblin's smoke away to alpha 0.001.
    alpha = np.where(unknown, np.maximum(solved, _min_alpha(rgb, backdrop_rgb)), tri)
    # Snap the backdrop itself to fully clear: _min_alpha reads sensor/compression noise as
    # a percent or two of opacity, which would otherwise glaze the whole frame in pale pink.
    alpha[distance <= BACKDROP_DE] = 0.0

    out = arr.copy()
    out[..., 3] = np.rint(alpha * 255).astype(np.uint8)
    partial = (alpha > 0) & (alpha < 1)
    if partial.any():
        a = alpha[partial]
        unmixed = np.clip(
            (rgb[partial].astype(np.float32) - backdrop_rgb * (1 - a[..., None])) / a[..., None], 0, 255
        )
        out[..., :3][partial] = np.rint(_despill(unmixed, a, backdrop_lab)).astype(np.uint8)
    return Image.fromarray(out)


def has_real_alpha(img: Image.Image) -> bool:
    if img.mode != "RGBA":
        return False
    alpha = np.array(img)[..., 3]
    return bool((alpha < 250).any())
