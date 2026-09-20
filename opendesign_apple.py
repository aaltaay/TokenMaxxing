"""OpenDesign Apple design-system tokens.

Source of truth for TokenMaxxing chrome. Values are the published
OpenDesign Apple contract (https://open-design.ai/plugins/design-system-apple/).
Do not invent a second accent palette. Blue is for primary actions,
links, and the active segment only.

SF Pro is not assumed to be installed. Display / body / mono stacks
use the Windows substitutes from the HUD brief, then system fallbacks.
"""
from __future__ import annotations

# Surfaces
BG = "#ffffff"
SURFACE = "#f5f5f7"
SURFACE_WARM = "#fbfbfd"

# Text
FG = "#1d1d1f"
FG_2 = "#424245"
MUTED = "#6e6e73"
META = "#86868b"

# Border
BORDER = "#d2d2d7"
BORDER_SOFT = "#e8e8ed"

# Accent (primary actions / links / active segment only)
ACCENT = "#0071e3"
ACCENT_ON = "#ffffff"
ACCENT_HOVER = "#0077ed"
ACCENT_ACTIVE = "#0066cc"

# Semantic (OpenDesign contract -- used sparingly for meters / status)
SUCCESS = "#16a34a"
WARN = "#eab308"
DANGER = "#dc2626"

# Windows substitutes (SF Pro is not installed)
FONT_DISPLAY = (
    '"Segoe UI Variable Display", "Segoe UI Semibold", "Segoe UI", '
    "system-ui, sans-serif"
)
FONT_BODY = '"Segoe UI Variable Text", "Segoe UI", system-ui, sans-serif'
FONT_MONO = '"Cascadia Mono", Consolas, monospace'

# Type scale
TEXT_XS = "12px"
TEXT_SM = "14px"
TEXT_BASE = "17px"
TEXT_LG = "21px"
TEXT_XL = "28px"
TEXT_2XL = "40px"
TEXT_3XL = "56px"
TEXT_4XL = "80px"
LEADING_BODY = "1.47"
LEADING_TIGHT = "1.05"
TRACKING_DISPLAY = "-0.015em"
WEIGHT_BODY = "400"
WEIGHT_EMPHASIS = "600"

# Spacing
SPACE_1 = "4px"
SPACE_2 = "8px"
SPACE_3 = "12px"
SPACE_4 = "16px"
SPACE_5 = "20px"
SPACE_6 = "24px"
SPACE_8 = "32px"
SPACE_12 = "48px"

# Radius
RADIUS_SM = "8px"
RADIUS_MD = "12px"
RADIUS_LG = "18px"
RADIUS_PILL = "980px"

# Elevation
ELEV_FLAT = "none"
ELEV_RING = f"0 0 0 1px {BORDER}"
ELEV_RAISED = "0 12px 32px rgba(0, 0, 0, 0.08)"

# Focus / motion
FOCUS_RING = f"0 0 0 4px color-mix(in oklab, {ACCENT}, transparent 65%)"
MOTION_FAST = "150ms"
MOTION_BASE = "220ms"
EASE_STANDARD = "cubic-bezier(0.28, 0, 0.22, 1)"

# Aliases used by meter / status helpers (same hex as SUCCESS / WARN / DANGER)
OK = SUCCESS
SOON = WARN
NOW = DANGER

CSS_VARS: dict[str, str] = {
    "bg": BG,
    "surface": SURFACE,
    "surface-warm": SURFACE_WARM,
    "fg": FG,
    "fg-2": FG_2,
    "muted": MUTED,
    "meta": META,
    "border": BORDER,
    "border-soft": BORDER_SOFT,
    "accent": ACCENT,
    "accent-on": ACCENT_ON,
    "accent-hover": ACCENT_HOVER,
    "accent-active": ACCENT_ACTIVE,
    "success": SUCCESS,
    "warn": WARN,
    "danger": DANGER,
    "font-display": FONT_DISPLAY,
    "font-body": FONT_BODY,
    "font-mono": FONT_MONO,
    "text-xs": TEXT_XS,
    "text-sm": TEXT_SM,
    "text-base": TEXT_BASE,
    "text-lg": TEXT_LG,
    "text-xl": TEXT_XL,
    "text-2xl": TEXT_2XL,
    "text-3xl": TEXT_3XL,
    "text-4xl": TEXT_4XL,
    "leading-body": LEADING_BODY,
    "leading-tight": LEADING_TIGHT,
    "tracking-display": TRACKING_DISPLAY,
    "weight-body": WEIGHT_BODY,
    "weight-emphasis": WEIGHT_EMPHASIS,
    "space-1": SPACE_1,
    "space-2": SPACE_2,
    "space-3": SPACE_3,
    "space-4": SPACE_4,
    "space-5": SPACE_5,
    "space-6": SPACE_6,
    "space-8": SPACE_8,
    "space-12": SPACE_12,
    "radius-sm": RADIUS_SM,
    "radius-md": RADIUS_MD,
    "radius-lg": RADIUS_LG,
    "radius-pill": RADIUS_PILL,
    "elev-flat": ELEV_FLAT,
    "elev-ring": ELEV_RING,
    "elev-raised": ELEV_RAISED,
    "focus-ring": FOCUS_RING,
    "motion-fast": MOTION_FAST,
    "motion-base": MOTION_BASE,
    "ease-standard": EASE_STANDARD,
}


def css_root() -> str:
    """`:root` block so the HTML shell stays on these tokens."""
    lines = [":root {"]
    for name, value in CSS_VARS.items():
        lines.append(f"  --{name}: {value};")
    lines.append("}")
    return "\n".join(lines) + "\n"
