"""
Instagram Story image generator for ambassadors.

Produces a 1080x1920 PNG with:
- Custom background (loaded from app/static/story_bg.{png,jpg}) if present.
  Drop a branded image at one of those paths to use your own design.
- Fallback: a dark Matrix-style default so the feature works on day one.
- A visible URL "card" — a rounded white rectangle with the ambassador's
  referral URL in big readable text. NO QR — QR on mobile screens isn't
  scannable by other mobile viewers (primary IG story use case). QR lives
  only in the Festival card in the dashboard.
- Minimal title / subtitle text (default background only).

The function is pure and pillow-only — no Flask context needed. Call it with
the referral URL; get back a BytesIO PNG.
"""

import os
import io
from PIL import Image, ImageDraw, ImageFont

BRAND_GREEN = (46, 219, 153)
WHITE = (255, 255, 255)
DARK = (10, 15, 10)
TEXT_LIGHT = (220, 220, 220)

CANVAS_W, CANVAS_H = 1080, 1920

# Where the custom background image lives. Drop a 1080x1920 PNG/JPG here to
# use your own branded design. Falls back to the default Matrix gradient
# below if neither path exists.
_HERE = os.path.dirname(os.path.abspath(__file__))
_APP_DIR = os.path.dirname(_HERE)
_BACKGROUND_CANDIDATES = [
    os.path.join(_APP_DIR, "static", "story_bg.png"),
    os.path.join(_APP_DIR, "static", "story_bg.jpg"),
    os.path.join(_APP_DIR, "static", "story_bg.jpeg"),
]


def _load_background():
    """Return a 1080x1920 RGB background image, either custom or default."""
    for path in _BACKGROUND_CANDIDATES:
        if os.path.exists(path):
            try:
                bg = Image.open(path).convert("RGB")
                if bg.size != (CANVAS_W, CANVAS_H):
                    bg = bg.resize((CANVAS_W, CANVAS_H), Image.LANCZOS)
                return bg, True  # custom
            except Exception:
                continue
    # Default: dark gradient with brand accents
    bg = Image.new("RGB", (CANVAS_W, CANVAS_H), DARK)
    draw = ImageDraw.Draw(bg)
    # Top + bottom brand strips
    draw.rectangle([(0, 0), (CANVAS_W, 12)], fill=BRAND_GREEN)
    draw.rectangle([(0, CANVAS_H - 12), (CANVAS_W, CANVAS_H)], fill=BRAND_GREEN)
    return bg, False


_ORBITRON_VAR = os.path.join(_APP_DIR, "static", "fonts", "Orbitron-var.ttf")

_BOLD_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "C:\\Windows\\Fonts\\arialbd.ttf",
]
_REGULAR_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "C:\\Windows\\Fonts\\arial.ttf",
]


def _find_font(candidates, size):
    """Return an ImageFont trying each candidate path; fall back to Pillow default."""
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except (OSError, IOError):
            continue
    return ImageFont.load_default()


def _orbitron(size, variation="Black"):
    """Orbitron variable font at the requested weight (Black | SemiBold | Bold...).
    Falls back to DejaVu Bold if the bundled font is missing."""
    if os.path.exists(_ORBITRON_VAR):
        try:
            f = ImageFont.truetype(_ORBITRON_VAR, size)
            try:
                f.set_variation_by_name(variation)
            except Exception:
                pass
            return f
        except (OSError, IOError):
            pass
    return _find_font(_BOLD_CANDIDATES, size)


def _center_text(draw, text, y, font, fill):
    """Draw `text` horizontally centered at y."""
    bbox = draw.textbbox((0, 0), text, font=font)
    w = bbox[2] - bbox[0]
    draw.text(((CANVAS_W - w) // 2, y), text, font=font, fill=fill)


def _split_url_for_display(referral_url):
    """Strip https:// and split long URLs into two lines for readable display."""
    display = referral_url.replace("https://", "").replace("http://", "")
    if "?" in display:
        host_path, _, query = display.partition("?")
        return host_path, "?" + query
    return display, ""


def _bottom_fade_overlay(panel_top):
    """Build a transparent→black gradient overlay to make the bottom panel
    text-readable over any background image."""
    panel_h = CANVAS_H - panel_top
    overlay = Image.new("RGBA", (CANVAS_W, panel_h), (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    ease_px = 120
    max_alpha = 205
    for y in range(panel_h):
        if y < ease_px:
            alpha = int(max_alpha * (y / ease_px))
        else:
            alpha = max_alpha
        od.rectangle([(0, y), (CANVAS_W, y + 1)], fill=(0, 0, 0, alpha))
    return overlay


def generate(referral_url):
    """Generate the shareable 1080x1920 PNG for the given referral URL.

    Layout (bottom-anchored): BECOME INEVITABLE ON THE DANCE FLOOR tagline,
    event dates (4 · 5 · 7 MAY), and the ambassador's QR in a white frame.
    Uses the custom background if app/static/story_bg.* exists, otherwise
    falls back to a Matrix-style default.

    Returns a BytesIO with the PNG bytes (positioned at 0, ready to send).
    """
    import qrcode

    bg, has_custom_bg = _load_background()

    # Darken the bottom section so overlaid text/QR are readable over any image
    panel_top = 1230
    bg_rgba = bg.convert("RGBA")
    bg_rgba.paste(_bottom_fade_overlay(panel_top), (0, panel_top), _bottom_fade_overlay(panel_top))
    bg = bg_rgba.convert("RGB")
    draw = ImageDraw.Draw(bg)

    # Fonts — Orbitron variable (Black for display, SemiBold for body)
    f_title = _orbitron(72, "Black")
    f_sub = _orbitron(36, "SemiBold")
    f_tagline = _orbitron(56, "Black")
    f_dates = _orbitron(54, "Black")
    f_subtitle = _orbitron(28, "SemiBold")

    # ─── Title + subtitle (default background only — custom bg has its own) ───
    if not has_custom_bg:
        _center_text(draw, "HACKING THE", 220, f_title, WHITE)
        _center_text(draw, "URBANKIZ CODE", 310, f_title, BRAND_GREEN)
        _center_text(draw, "Free Online Training Week with Jesus & Anni", 420, f_sub, TEXT_LIGHT)

    # ─── Tagline (2 lines, white bold Orbitron Black) ───
    _center_text(draw, "BECOME INEVITABLE", 1270, f_tagline, WHITE)
    _center_text(draw, "ON THE DANCE FLOOR", 1340, f_tagline, WHITE)

    # ─── Dates (brand green, Orbitron Black, wider tracking) ───
    _center_text(draw, "4  •  5  •  7   MAY", 1425, f_dates, BRAND_GREEN)

    # ─── Training-week subtitle (what the dates are) ───
    _center_text(draw, "Free Online Training Week with Jesus & Anni", 1495, f_subtitle, TEXT_LIGHT)

    # ─── QR code (scannable, white-framed) ───
    qr = qrcode.QRCode(
        version=1,
        box_size=14,
        border=2,
        error_correction=qrcode.constants.ERROR_CORRECT_H,
    )
    qr.add_data(referral_url)
    qr.make(fit=True)
    qr_img = qr.make_image(fill_color="black", back_color="white").convert("RGB")
    qr_size = 300
    qr_img = qr_img.resize((qr_size, qr_size), Image.NEAREST)
    qr_pad = 22
    qr_frame = Image.new("RGB", (qr_size + qr_pad * 2, qr_size + qr_pad * 2), WHITE)
    qr_frame.paste(qr_img, (qr_pad, qr_pad))
    qr_x = (CANVAS_W - qr_frame.width) // 2
    qr_y = 1550
    bg.paste(qr_frame, (qr_x, qr_y))

    buf = io.BytesIO()
    bg.save(buf, format="PNG", optimize=True)
    buf.seek(0)
    return buf
