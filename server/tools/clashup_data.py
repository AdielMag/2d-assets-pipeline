"""ClashUp POC data: project style, the de-duplicated asset set, and the two screen
layouts. Layout rects are authored in reference pixels on a 1080x1920 portrait canvas
(top-left origin) read from the two source screenshots; the driver normalizes them to
0..1 for the screen-export endpoint.

Shared HUD + nav chrome is generated once and reused across both screens.
"""

REF_W, REF_H = 1080, 1920

# The two source screenshots, used as per-element reference images for faithful
# generation (each asset is cropped from the screen it appears in).
SCREEN_FILES = {
    "Lobby": r"C:\Users\Adiel\Downloads\Generated Image July 20, 2026 - 9_29PM.jpg",
    "Searching": r"C:\Users\Adiel\Downloads\Generated Image July 20, 2026 - 9_28PM.jpg",
}
# Which screen to crop each asset's reference from (defaults to Lobby; the driver
# falls back to whichever screen contains the asset).
REFERENCE_SCREEN = {
    "RuneCircle": "Searching",
    "CancelButton": "Searching",
}

PROJECT = {
    "name": "ClashUp",
    "style_description": (
        "Vibrant Clash-Royale-style mobile game UI art. Bold cartoon vector shapes with "
        "thick dark outlines, glossy highlights, chunky beveled edges, rich saturated "
        "colors, gold trim and rivets, soft inner shadows, clean readable silhouettes. "
        "Polished mobile game UI asset."
    ),
    "palette": ["#3d6fd6", "#f5b820", "#d63a3a", "#8a4fd6", "#2a3550", "#7fd0ff"],
    "ppu": 100,
    "filter_mode": "bilinear",
    "wrap_mode": "clamp",
    "power_of_two": False,  # keep exact authored sizes for faithful UI layout
}

# Only these stretch (buttons/panels sized to variable text or screen width), so only
# these get a 9-slice border. Icons, badges, the rune sprite and the fixed-size nav
# highlight are placed at native size (Simple/Single), never 9-sliced.
NINE_SLICE = {
    "PlayButton", "GameModeButton", "CancelButton",
    "NamePlate", "CurrencyCapsule", "NavBar",
}

# Domain/atlas tree: Common (root) holds every asset shared by both screens (HUD chrome,
# nav bar + icons); Lobby and Searching hold only what's unique to that screen. A screen
# built in Unity loads its own atlas + Common — never a per-sprite texture.
DOMAINS = {"Common": None, "Lobby": "Common", "Searching": "Common"}

ASSET_DOMAIN = {
    # shared HUD + nav chrome
    "NamePlate": "Common", "AvatarFrame": "Common", "LevelBadge": "Common",
    "CurrencyCapsule": "Common", "IconGem": "Common", "IconCoin": "Common",
    "NavBar": "Common", "NavHighlight": "Common",
    "IconShop": "Common", "IconCards": "Common", "IconBattle": "Common",
    "IconSocial": "Common", "IconEvents": "Common",
    # Lobby-only
    "PlayButton": "Lobby", "GameModeButton": "Lobby", "IconSword": "Lobby", "IconArrow": "Lobby",
    # Searching-only
    "CancelButton": "Searching", "RuneCircle": "Searching",
}

# key -> (display name, type, subject prompt). Style/type rules are added by the server.
ASSETS = {
    # --- UI elements (9-sliceable buttons & panels) ---
    "PlayButton": ("Play Button", "ui_element",
        "A wide horizontal glossy blue play button with a thick gold border, beveled 3D "
        "edge and a bright highlight sweep across the top. Blank face, no text."),
    "GameModeButton": ("Game Mode Button", "ui_element",
        "A glossy golden-orange rectangular button with a thick darker-gold border and a "
        "beveled top shine. Blank face, no text."),
    "CancelButton": ("Cancel Button", "ui_element",
        "A wide glossy red button with a thick dark-red border, beveled 3D edge and a top "
        "highlight. Blank face, no text."),
    "CurrencyCapsule": ("Currency Capsule", "ui_element",
        "A dark rounded pill-shaped resource-counter background, deep navy with a subtle "
        "gold rim and inset shadow, made to stretch horizontally. Empty, no text or icons."),
    "NamePlate": ("Name Plate", "ui_element",
        "A dark rounded horizontal name-plate banner, deep navy with gold trim and a soft "
        "inner shadow, made to stretch horizontally. Empty, no text or portrait."),
    "NavBar": ("Nav Bar", "ui_element",
        "A wide horizontal bottom navigation bar panel, glossy royal blue with a gold top "
        "edge and soft beveled shading, made to stretch horizontally. Blank, no icons."),
    "NavHighlight": ("Nav Highlight", "ui_element",
        "A soft rounded golden highlight plate for a selected navigation tab, warm glow "
        "with a gold rim and a lighter translucent center. No icon or text."),
    # --- Icons ---
    "IconShop": ("Icon Shop", "icon",
        "A colorful market shop-stall icon with a red-and-white striped awning."),
    "IconCards": ("Icon Cards", "icon",
        "A fanned stack of glossy playing cards, blue and teal."),
    "IconBattle": ("Icon Battle", "icon",
        "Two crossed silver swords with golden hilts, a battle icon."),
    "IconSocial": ("Icon Social", "icon",
        "Two friendly cartoon person silhouettes grouped together, a social icon, blue tones."),
    "IconEvents": ("Icon Events", "icon",
        "A shiny golden trophy cup, an events icon."),
    "IconGem": ("Icon Gem", "icon",
        "A single simple faceted purple gem crystal with bright highlights, with no metal "
        "frame, setting, cradle or holder around it."),
    "IconCoin": ("Icon Coin", "icon",
        "A shiny golden coin with an embossed star."),
    "IconSword": ("Icon Sword", "icon",
        "A single upright silver sword with a golden hilt."),
    "IconArrow": ("Icon Arrow", "icon",
        "A bold rounded golden play arrow, a solid triangle pointing right."),
    # --- Badges (icon type) ---
    "AvatarFrame": ("Avatar Frame", "icon",
        "A circular ornate golden avatar frame ring with a fully empty transparent center."),
    "LevelBadge": ("Level Badge", "icon",
        "A small round golden level-badge medallion with a beveled rim and a plain empty "
        "center. No number or text."),
    # --- Decorative sprite ---
    "RuneCircle": ("Rune Circle", "sprite",
        "A glowing circular arcane rune ring made of thin luminous lines and runic glyphs, "
        "colored ONLY bright cyan and electric blue (like a glowing blue hologram), with a "
        "fully transparent empty center. Absolutely no purple, pink, violet or magenta "
        "anywhere. Fantasy magic style."),
}

# Each screen: background hex (behind the UI, standing in for the excluded scene) and a
# list of (asset_key, x, y, w, h) in reference pixels, ordered back-to-front (z).
SCREENS = {
    "Lobby": {
        "bg": "#3f5170",
        "elements": [
            ("NamePlate",       40,   40, 430,  90),
            ("AvatarFrame",     28,   26, 112, 112),
            ("LevelBadge",      46,  104,  58,  58),
            ("CurrencyCapsule", 780,  38, 265,  70),
            ("IconGem",         750,  30,  88,  88),
            ("CurrencyCapsule", 780, 122, 265,  70),
            ("IconCoin",        752, 118,  84,  84),
            ("PlayButton",      70, 1500, 560, 150),
            ("IconSword",      110, 1533,  84,  84),
            ("GameModeButton", 670, 1500, 340, 150),
            ("IconArrow",      915, 1548,  62,  56),
            ("NavBar",           0, 1740, 1080, 180),
            ("NavHighlight",   430, 1742, 220, 178),
            ("IconShop",        63, 1772,  90,  90),
            ("IconCards",      279, 1772,  90,  90),
            ("IconBattle",     490, 1762, 100, 100),
            ("IconSocial",     711, 1772,  90,  90),
            ("IconEvents",     927, 1772,  90,  90),
        ],
    },
    "Searching": {
        "bg": "#1b2338",
        "elements": [
            ("RuneCircle",     310, 150, 460, 460),
            ("NamePlate",       40,   40, 430,  90),
            ("AvatarFrame",     28,   26, 112, 112),
            ("LevelBadge",      46,  104,  58,  58),
            ("CurrencyCapsule", 780,  38, 265,  70),
            ("IconGem",         750,  30,  88,  88),
            ("CurrencyCapsule", 780, 122, 265,  70),
            ("IconCoin",        752, 118,  84,  84),
            ("CancelButton",   230, 1470, 620, 160),
            ("NavBar",           0, 1740, 1080, 180),
            ("NavHighlight",   430, 1742, 220, 178),
            ("IconShop",        63, 1772,  90,  90),
            ("IconCards",      279, 1772,  90,  90),
            ("IconBattle",     490, 1762, 100, 100),
            ("IconSocial",     711, 1772,  90,  90),
            ("IconEvents",     927, 1772,  90,  90),
        ],
    },
}
