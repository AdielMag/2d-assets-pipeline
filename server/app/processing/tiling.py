from PIL import Image


def tile_preview(img: Image.Image, repeats: int = 3) -> Image.Image:
    """Render the image tiled repeats×repeats with a half-tile offset so seams sit
    in the middle of the preview — any discontinuity becomes obvious."""
    rgba = img.convert("RGBA")
    w, h = rgba.size
    out = Image.new("RGBA", (w * repeats, h * repeats))
    for row in range(repeats + 1):
        for col in range(repeats + 1):
            out.alpha_composite(rgba, (col * w - w // 2, row * h - h // 2))
    return out
