"""Dynamic portrait daily cover image generator ("The Quiet Folio") using Pillow."""

from __future__ import annotations

from datetime import date
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

# 9:16 portrait canvas for modern phone screens in Moon+ Reader Pro
COVER_WIDTH = 1080
COVER_HEIGHT = 1920

# The Quiet Folio palette (warm bookish minimalism, >= 4.5:1 text contrast)
BG_COLOR = (247, 243, 236)       # Warm folio paper (#F7F3EC)
CARD_BG_COLOR = (239, 233, 223)  # Soft tinted folio card (#EFE9DF)
INK_COLOR = (26, 26, 26)         # Deep charcoal (#1A1A1A)
MUTED_COLOR = (92, 87, 80)       # Warm slate (#5C5750)
ACCENT_COLOR = (154, 74, 46)     # Warm terracotta (#9A4A2E)
RULE_COLOR = (214, 206, 195)     # Subtle hairline (#D6CEC3)

_SERIF_FONT_CANDIDATES = (
    "/System/Library/Fonts/Supplemental/Charter.ttc",
    "/System/Library/Fonts/Palatino.ttc",
    "/System/Library/Fonts/Supplemental/Georgia.ttf",
    "/System/Library/Fonts/NewYork.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSerif-Regular.ttf",
)

_SERIF_BOLD_FONT_CANDIDATES = (
    "/System/Library/Fonts/Supplemental/Georgia Bold.ttf",
    "/System/Library/Fonts/Supplemental/Times New Roman Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSerif-Bold.ttf",
)

_SERIF_ITALIC_FONT_CANDIDATES = (
    "/System/Library/Fonts/Supplemental/Georgia Italic.ttf",
    "/System/Library/Fonts/Supplemental/Times New Roman Italic.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Italic.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSerif-Italic.ttf",
    *_SERIF_FONT_CANDIDATES,
)

_SANS_FONT_CANDIDATES = (
    "/System/Library/Fonts/Helvetica.ttc",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
)


def _load_font(
    candidates: tuple[str, ...],
    size: int,
) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for path in candidates:
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, size=size)
            except OSError:
                continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def format_edition_date(edition_date: str) -> str:
    """Format an ISO date string (YYYY-MM-DD) into a readable masthead date."""
    try:
        parsed = date.fromisoformat(edition_date)
        return parsed.strftime("%A, %B %-d, %Y")
    except ValueError:
        return edition_date


def format_reading_time(total_minutes: int) -> str:
    """Format total reading time in minutes into a concise human-readable string."""
    minutes = max(0, int(total_minutes))
    if minutes < 60:
        return f"{minutes} min read"
    hours, rem = divmod(minutes, 60)
    if rem == 0:
        return f"{hours}h read"
    return f"{hours}h {rem}m read"


def _wrap_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    max_width: int,
    max_lines: int = 5,
) -> list[str]:
    words = text.split()
    if not words:
        return []

    lines: list[str] = []
    current = words[0]
    for word in words[1:]:
        candidate = f"{current} {word}"
        bbox = draw.textbbox((0, 0), candidate, font=font)
        if bbox[2] - bbox[0] <= max_width:
            current = candidate
        else:
            lines.append(current)
            current = word
            if len(lines) == max_lines - 1:
                break

    if len(lines) < max_lines:
        lines.append(current)

    consumed_words = sum(len(line.split()) for line in lines)
    if consumed_words < len(words) and lines:
        last = lines[-1]
        while last and draw.textbbox((0, 0), f"{last}...", font=font)[2] > max_width:
            parts = last.rsplit(" ", 1)
            if len(parts) == 1:
                last = last[:-1]
            else:
                last = parts[0]
        lines[-1] = f"{last}..." if last else "..."

    return lines


def _draw_centered_text(
    draw: ImageDraw.ImageDraw,
    y: int,
    text: str,
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    fill: tuple[int, int, int],
) -> int:
    bbox = draw.textbbox((0, 0), text, font=font)
    text_w = bbox[2] - bbox[0]
    x = (COVER_WIDTH - text_w) // 2
    draw.text((x, y), text, fill=fill, font=font)
    return y + (bbox[3] - bbox[1])


def generate_cover_image(
    edition_date: str,
    lead_headline: str,
    total_reading_minutes: int,
    article_count: int = 0,
    title: str = "The Reading",
    lead_meta: str = "",
    output_path: Path | None = None,
) -> bytes:
    """Generate a portrait PNG daily cover image ("The Quiet Folio") and return its raw bytes."""
    image = Image.new("RGB", (COVER_WIDTH, COVER_HEIGHT), BG_COLOR)
    draw = ImageDraw.Draw(image)

    margin_x = 96
    card_pad = 56
    card_inner_width = COVER_WIDTH - 2 * margin_x - 2 * card_pad

    # Fonts
    font_kicker = _load_font(_SANS_FONT_CANDIDATES, 28)
    font_masthead = _load_font(_SERIF_BOLD_FONT_CANDIDATES, 116)
    font_label = _load_font(_SANS_FONT_CANDIDATES, 26)
    font_headline = _load_font(_SERIF_BOLD_FONT_CANDIDATES, 62)
    font_lead_meta = _load_font(_SANS_FONT_CANDIDATES, 30)
    font_meta = _load_font(_SANS_FONT_CANDIDATES, 34)
    font_footer = _load_font(_SERIF_ITALIC_FONT_CANDIDATES, 32)

    # Top centered masthead block
    formatted_date = format_edition_date(edition_date).upper()
    y = 200
    y = _draw_centered_text(draw, y, formatted_date, font_kicker, ACCENT_COLOR)
    y += 40
    y = _draw_centered_text(draw, y, title, font_masthead, INK_COLOR)
    y += 64

    # Short centered hairline divider
    rule_half = 72
    center_x = COVER_WIDTH // 2
    draw.line(
        [(center_x - rule_half, y), (center_x + rule_half, y)],
        fill=RULE_COLOR,
        width=3,
    )

    # Middle soft rounded Folio Card for the Lead Essay
    headline_text = lead_headline.strip() or "Today's Curated Longform Reading"
    headline_lines = _wrap_text(draw, headline_text, font_headline, card_inner_width, max_lines=6)

    # Measure card height dynamically so it sits centered in the folio
    line_heights = [
        draw.textbbox((0, 0), line, font=font_headline)[3]
        - draw.textbbox((0, 0), line, font=font_headline)[1]
        for line in headline_lines
    ]
    headline_block_h = sum(line_heights) + max(0, len(line_heights) - 1) * 24
    meta_block_h = 56 if lead_meta.strip() else 0
    card_h = card_pad + 48 + headline_block_h + meta_block_h + card_pad

    card_top = max(y + 120, (COVER_HEIGHT - card_h) // 2 + 24)
    card_bottom = card_top + card_h

    draw.rounded_rectangle(
        [margin_x, card_top, COVER_WIDTH - margin_x, card_bottom],
        radius=24,
        fill=CARD_BG_COLOR,
        outline=RULE_COLOR,
        width=2,
    )

    cy = card_top + card_pad
    cx = margin_x + card_pad
    draw.text((cx, cy), "LEAD ESSAY", fill=ACCENT_COLOR, font=font_label)
    cy += 52

    for line, lh in zip(headline_lines, line_heights):
        draw.text((cx, cy), line, fill=INK_COLOR, font=font_headline)
        cy += lh + 24

    if lead_meta.strip():
        cy += 12
        draw.text((cx, cy), lead_meta.strip(), fill=MUTED_COLOR, font=font_lead_meta)

    # Bottom centered edition metadata
    reading_str = format_reading_time(total_reading_minutes)
    if article_count > 0:
        noun = "article" if article_count == 1 else "articles"
        meta_line = f"{article_count} {noun}  ·  {reading_str}"
    else:
        meta_line = reading_str

    bottom_y = COVER_HEIGHT - 240
    bottom_y = _draw_centered_text(draw, bottom_y, meta_line, font_meta, INK_COLOR)
    bottom_y += 28
    _draw_centered_text(draw, bottom_y, "Curated for unhurried reading", font_footer, MUTED_COLOR)

    buf = BytesIO()
    image.save(buf, format="PNG", optimize=True)
    png_bytes = buf.getvalue()

    if output_path is not None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(png_bytes)

    return png_bytes
