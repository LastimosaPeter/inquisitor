"""Light theme, derived from the dark stylesheet.

static/style.css is the single source of truth. The light stylesheet is produced from it on demand: every colour that is
written in a CSS declaration has its lightness flipped (hue kept, neutrals tinted warm, so greys become a "dirty white"
family), transparent overlays flip with it, and shadows are kept dark but softer. A few explicit palette variables and
rules are appended on top. Nothing here changes the dark theme.
"""
from __future__ import annotations

import math
import re

# ---- colour maths (OKLab) ----------------------------------------------------------------------------------------
def _to_lin(c: float) -> float:
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def _from_lin(c: float) -> float:
    c = max(0.0, min(1.0, c))
    return 12.92 * c if c <= 0.0031308 else 1.055 * c ** (1 / 2.4) - 0.055


def rgb_to_oklab(r: float, g: float, b: float):
    r, g, b = _to_lin(r), _to_lin(g), _to_lin(b)
    l = (0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b) ** (1 / 3)
    m = (0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b) ** (1 / 3)
    s = (0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b) ** (1 / 3)
    return (0.2104542553 * l + 0.7936177850 * m - 0.0040720468 * s,
            1.9779984951 * l - 2.4285922050 * m + 0.4505937099 * s,
            0.0259040371 * l + 0.7827717662 * m - 0.8086757660 * s)


def oklab_to_rgb(L: float, a: float, b: float):
    l = (L + 0.3963377774 * a + 0.2158037573 * b) ** 3
    m = (L - 0.1055613458 * a - 0.0638541728 * b) ** 3
    s = (L - 0.0894841775 * a - 1.2914855480 * b) ** 3
    return (_from_lin(4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s),
            _from_lin(-1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s),
            _from_lin(-0.0041960863 * l - 0.7034186147 * m + 1.7076147010 * s))


# dark-theme lightness -> light-theme lightness (piecewise linear, monotone: contrast relationships are preserved)
_L_POINTS = [(0.00, 0.985), (0.14, 0.966), (0.20, 0.945), (0.30, 0.872), (0.45, 0.700), (0.62, 0.500), (0.75, 0.380), (0.90, 0.235), (1.00, 0.130)]
_WARM_HUE = math.radians(85)      # the tint of the "dirty white": a touch of yellow
_WARM_CHROMA = 0.011


def _map_lightness(L: float) -> float:
    for (x0, y0), (x1, y1) in zip(_L_POINTS, _L_POINTS[1:]):
        if L <= x1:
            return y0 + (y1 - y0) * (L - x0) / (x1 - x0)
    return _L_POINTS[-1][1]


def flip_rgb(r: float, g: float, b: float) -> tuple[float, float, float]:
    L, a, bb = rgb_to_oklab(r, g, b)
    C, h = math.hypot(a, bb), math.atan2(bb, a)
    if C < 0.035:                  # a grey: make it a warm grey
        C, h = _WARM_CHROMA * (1 - C / 0.035) + C * 0.6, _WARM_HUE
    else:                          # a real colour: keep its hue, slightly calmer on a light page
        C *= 0.92
    L2 = _map_lightness(L)
    return oklab_to_rgb(L2, C * math.cos(h), C * math.sin(h))


# ---- parsing CSS values -----------------------------------------------------------------------------------------
_HEX = re.compile(r"#([0-9a-fA-F]{8}|[0-9a-fA-F]{6}|[0-9a-fA-F]{3,4})\b")
_FUNC = re.compile(r"rgba?\(\s*([^)]*?)\s*\)")
_NAMED = {"white": (1.0, 1.0, 1.0), "black": (0.0, 0.0, 0.0)}
_NAMED_RE = re.compile(r"(?<![\w#-])(white|black)(?![\w-])")
_SHADOW_PROPS = {"box-shadow", "text-shadow"}


def _fmt(rgb, alpha: float) -> str:
    r, g, b = (max(0, min(255, round(v * 255))) for v in rgb)
    return f"#{r:02x}{g:02x}{b:02x}" if alpha >= 0.999 else f"rgba({r},{g},{b},{alpha:.3g})"


def _parse_hex(h: str):
    if len(h) in (3, 4):
        h = "".join(c * 2 for c in h)
    r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
    return (r, g, b), (int(h[6:8], 16) / 255 if len(h) == 8 else 1.0)


def _parse_func(body: str):
    parts = [p for p in re.split(r"[\s,/]+", body.strip()) if p]
    if len(parts) < 3 or any(p.startswith("var(") for p in parts):
        return None
    vals = []
    for p in parts[:3]:
        if p.endswith("%"):
            vals.append(float(p[:-1]) / 100)
        else:
            vals.append(float(p) / 255)
    alpha = 1.0
    if len(parts) > 3:
        alpha = float(parts[3][:-1]) / 100 if parts[3].endswith("%") else float(parts[3])
    return tuple(vals), alpha


def _convert(rgb, alpha, shadow: bool) -> str:
    L = rgb_to_oklab(*rgb)[0]
    if shadow:
        if L < 0.5:                                   # a dark shadow stays dark, just softer on a light page
            return _fmt(rgb, alpha * 0.5)
        return _fmt(flip_rgb(*rgb), alpha * 0.6)     # a light glow becomes a faint dark one
    return _fmt(flip_rgb(*rgb), alpha)


def flip_value(prop: str, value: str) -> str:
    shadow = prop.strip().lower() in _SHADOW_PROPS

    def hex_sub(m):
        rgb, alpha = _parse_hex(m.group(1))
        return _convert(rgb, alpha, shadow)

    def func_sub(m):
        parsed = _parse_func(m.group(1))
        if not parsed:
            return m.group(0)
        return _convert(parsed[0], parsed[1], shadow)

    def named_sub(m):
        return _convert(_NAMED[m.group(1)], 1.0, shadow)

    value = _HEX.sub(hex_sub, value)
    value = _FUNC.sub(func_sub, value)
    return _NAMED_RE.sub(named_sub, value)


# ---- walking the stylesheet -------------------------------------------------------------------------------------
_COMMENT = re.compile(r"/\*.*?\*/", re.S)


def _transform_declarations(block: str) -> str:
    out = []
    for decl in _split_declarations(block):
        if ":" in decl and decl.strip():
            prop, _, value = decl.partition(":")
            if prop.strip().startswith("--") or re.fullmatch(r"[A-Za-z-]+", prop.strip()):
                out.append(prop + ":" + flip_value(prop, value))
                continue
        out.append(decl)
    return ";".join(out)


def _split_declarations(block: str) -> list[str]:
    parts, depth, cur = [], 0, []
    for ch in block:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == ";" and depth == 0:
            parts.append("".join(cur)); cur = []
        else:
            cur.append(ch)
    parts.append("".join(cur))
    return parts


def flip_stylesheet(css: str) -> str:
    css = _COMMENT.sub("", css)
    out, i, n = [], 0, len(css)
    while i < n:
        j = css.find("{", i)
        if j < 0:
            out.append(css[i:]); break
        prelude = css[i:j]
        # find the matching close brace
        depth, k = 1, j + 1
        while k < n and depth:
            depth += {"{": 1, "}": -1}.get(css[k], 0); k += 1
        inner = css[j + 1:k - 1]
        if "{" in inner:                                  # a container (@media, @keyframes): recurse, keep the prelude as is
            out.append(prelude + "{" + flip_stylesheet(inner) + "}")
        else:                                             # a rule: only its declarations hold colours
            out.append(prelude + "{" + _transform_declarations(inner) + "}")
        i = k
    return "".join(out)


# ---- the light palette ------------------------------------------------------------------------------------------
PALETTE = """
:root{
  --bg:#edeae4;--sidebar:#e6e2da;--surface:#f6f3ee;--surface-2:#f1eee8;--surface-3:#e9e5dd;
  --line:#d3cec3;--line-soft:#e0dbd1;--text:#25282c;--text-strong:#121416;--muted:#5b6168;--faint:#7f848b;
  --green:#1f7a4c;--red:#b3342f;--amber:#946200;
}
html,body{background:var(--bg)}
body{background:var(--bg);color:var(--text)}
/* the logos are white artwork made for a dark page */
.brand-wordmark,.login-wordmark,.partner-lockup,.footer-identity img,.side-footer img{filter:invert(1) brightness(.18)}
/* sign-in with Google keeps Google's own white button, with a visible edge on a light page */
.google-btn{background:#fff;color:#1f1f1f;border-color:#c9c4b8;box-shadow:0 1px 2px rgba(0,0,0,.08)}
.google-btn:hover{background:#f4f1ea}
/* medals keep their metal on a light page */
.rank-medal.rank-1{background:radial-gradient(circle at 35% 30%,#f3d77a,#c99a1c 70%);border-color:#a87e10;color:#3a2a00}
.rank-medal.rank-2{background:radial-gradient(circle at 35% 30%,#eceff2,#9aa1a9 70%);border-color:#7b838c;color:#262a2e}
.rank-medal.rank-3{background:radial-gradient(circle at 35% 30%,#e6a870,#a8662f 70%);border-color:#86501f;color:#2f1a08}
::selection{background:#cfd8e8}
/* the faintest labels were too pale once flipped */
.footer-bottom span,.side-footer small{color:#666b72}
"""

ACCENTS = {
    # primary buttons and active states in graphite ink: the same neutral feel as the dark theme
    "ink": """
.button{background:#26292e;border-color:#26292e;color:#f6f3ee}
.button:hover{background:#34383e}
""",
    # the same, in a deep blue
    "blue": """
:root{--accent:#1f4f99}
.button{background:#1f4f99;border-color:#1f4f99;color:#fbfaf7}
.button:hover{background:#2a5fb0}
.text-link,.auth-switch a,.staff-signin>summary:hover{color:#1f4f99}
.sidebar nav a.active,.sidebar nav a[aria-current=page]{box-shadow:inset 3px 0 0 #1f4f99}
""",
}


def build_light_css(css: str, accent: str = "ink") -> str:
    return flip_stylesheet(css) + PALETTE + ACCENTS.get(accent, ACCENTS["ink"])
