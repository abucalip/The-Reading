"""Dynamic portrait daily cover image generator using Pillow."""

from __future__ import annotations

from datetime import date
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

COVER_WIDTH = 1080
COVER_HEIGHT = 1620

BG_COLOR = (250, 248, 244)       # Warm editorial paper (#FAF8F4)
INK_COLOR = (26, 26, 26)         # Deep charcoal (#1A1A1A)
MUTED_COLOR = (96, 96, 96)       # Slate gray (#606060)
ACCENT_COLOR = (139, 46, 46)     # Editorial burgundy (#8B2E2E)
RULE_COLOR = (212, 206, 196)     # Soft warm rule (#D4CEC4)

_SERIF_FONT_CANDIDATES = (
    "/System/Library/Fonts/Supplemental/Georgia.ttf",
    "/System/Library/Fonts/Supplemental/Times New Roman.ttf",
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

    # If we hit max_lines and there are still remaining words, add an ellipsis.
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


def generate_cover_image(
    edition_date: str,
    lead_headline: str,
    total_reading_minutes: int,
    article_count: int = 0,
    title: str = "The Reading",
    output_path: Path | None = None,
) -> bytes:
    """Generate a portrait PNG daily cover image and return its raw bytes."""
    image = Image.new("RGB", (COVER_WIDTH, COVER_HEIGHT), BG_COLOR)
    draw = ImageDraw.Draw(image)

    margin_x = 96
    content_width = COVER_WIDTH - 2 * margin_x

    # Top accent bar
    draw.rectangle([0, 0, COVER_WIDTH, 22], fill=ACCENT_COLOR)

    # Fonts
    font_kicker = _load_font(_SANS_FONT_CANDIDATES, 28)
    font_masthead = _load_font(_SERIF_BOLD_FONT_CANDIDATES, 112)
    font_date = _load_font(_SANS_FONT_CANDIDATES, 34)
    font_label = _load_font(_SANS_FONT_CANDIDATES, 26)
    font_headline = _load_font(_SERIF_BOLD_FONT_CANDIDATES, 66)
    font_meta = _load_font(_SANS_FONT_CANDIDATES, 34)
    font_footer = _load_font(_SERIF_FONT_CANDIDATES, 28)

    y = 110
    draw.text((margin_x, y), "DAILY EDITION", fill=ACCENT_COLOR, font=font_kicker)
    y += 54

    # Masthead
    draw.text((margin_x, y), title, fill=INK_COLOR, font=font_masthead)
    masthead_bbox = draw.textbbox((margin_x, y), title, font=font_masthead)
    y = masthead_bbox[3] + 32

    # Double editorial rule
    draw.line([(margin_x, y), (COVER_WIDTH - margin_x, y)], fill=INK_COLOR, width=4)
    y += 12
    draw.line([(margin_x, y), (COVER_WIDTH - margin_x, y)], fill=RULE_COLOR, width=2)
    y += 32

    # Date line
    formatted_date = format_edition_date(edition_date)
    draw.text((margin_x, y), formatted_date, fill=MUTED_COLOR, font=font_date)
    y += 140

    # Lead story section
    draw.text((margin_x, y), "LEAD ESSAY", fill=ACCENT_COLOR, font=font_label)
    y += 52

    headline_text = lead_headline.strip() or "Today's Curated Longform Reading"
    headline_lines = _wrap_text(draw, headline_text, font_headline, content_width, max_lines=6)
    for line in headline_lines:
        draw.text((margin_x, y), line, fill=INK_COLOR, font=font_headline)
        line_bbox = draw.textbbox((margin_x, y), line, font=font_headline)
        y = line_bbox[3] + 22

    # Bottom metadata panel
    bottom_rule_y = COVER_HEIGHT - 270
    draw.line(
        [(margin_x, bottom_rule_y), (COVER_WIDTH - margin_x, bottom_rule_y)],
        fill=RULE_COLOR,
        width=2,
    )

    reading_str = format_reading_time(total_reading_minutes)
    if article_count > 0:
        noun = "article" if article_count == 1 else "articles"
        meta_line = f"{article_count} {noun}  •  {reading_str}"
    else:
        meta_line = reading_str

    draw.text((margin_x, bottom_rule_y + 42), meta_line, fill=INK_COLOR, font=font_meta)
    draw.text(
        (margin_x, bottom_rule_y + 108),
        "Curated for unhurried reading",
        fill=MUTED_COLOR,
        font=font_footer,
    )

    # Bottom accent bar
    draw.rectangle([0, COVER_HEIGHT - 16, COVER_WIDTH, COVER_HEIGHT], fill=ACCENT_COLOR)

    buf = BytesIO()
    image.save(buf, format="PNG", optimize=True)
    png_bytes = buf.getvalue()

    if output_path is not None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(png_bytes)

    return png_bytes
