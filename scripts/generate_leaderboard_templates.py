"""Generate static leaderboard artwork assets and empty blueprint previews."""

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps


ROOT = Path(__file__).resolve().parents[1]
PUBLIC_ROOT = ROOT / "A.R.C. Public"
FONT_PATH = ROOT / "assets" / "fonts" / "Montserrat[wght].ttf"
BACKGROUND_PATH = ROOT / "assets" / "leaderboard-background.png"
TEMPLATE_DIR = ROOT / "assets" / "leaderboard-templates"
PUBLIC_TEMPLATE_DIR = PUBLIC_ROOT / "assets" / "leaderboard-templates"
EXPORT_DIR = ROOT / "exports" / "leaderboard-blueprints"
CANVAS_EXPORT_DIR = EXPORT_DIR / "empty-canvases"

TEAM_COUNTS = (16, 18, 20, 22, 24)
ORIENTATIONS = ("vertical", "horizontal")
WIDTHS = {"vertical": 1080, "horizontal": 1920}
OUTER_MARGIN = 28
SECTION_GAP = 22
TABLE_HEADER_HEIGHT = 64
ROW_HEIGHT = 48
DEFAULT_HEADER_HEIGHT = 180
DEFAULT_FOOTER_HEIGHT = 120
BODY_WEIGHT = 400

COLORS = {
    "panel": (17, 25, 38, 255),
    "panel_alt": (21, 32, 47, 255),
    "header": (13, 39, 57, 255),
    "outline": (34, 70, 96, 255),
    "divider": (37, 58, 76, 255),
    "heading": (0, 174, 255, 255),
    "rank": (143, 177, 202, 255),
    "badge": (13, 39, 57, 255),
    "badge_outline": (37, 107, 141, 255),
    "sheet_bg": (241, 245, 248),
    "sheet_ink": (21, 40, 54),
    "sheet_note": (89, 113, 130),
    "measure": (0, 126, 180),
}


def font(size: int, weight: int = 400) -> ImageFont.FreeTypeFont:
    result = ImageFont.truetype(str(FONT_PATH), size)
    result.set_variation_by_name(
        {400: "Regular", 700: "Bold", 800: "ExtraBold"}[weight]
    )
    return result


def date_badge() -> Image.Image:
    draw_surface = Image.new("RGBA", (1, 1), (0, 0, 0, 0))
    draw = ImageDraw.Draw(draw_surface)
    date_font = font(18)
    months = ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")
    text_width = max(
        draw.textbbox((0, 0), f"30 {month} 2026", font=date_font)[2]
        for month in months
    )
    width = text_width + 32
    height = 40
    image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    ImageDraw.Draw(image).rounded_rectangle(
        (0, 0, width - 1, height - 1),
        radius=10,
        fill=COLORS["badge"],
        outline=COLORS["badge_outline"],
        width=1,
    )
    return image


def table_dimensions(team_count: int, orientation: str) -> tuple[int, int, int]:
    width = WIDTHS[orientation]
    columns = 2 if orientation == "horizontal" else 1
    capacity = (team_count + 1) // 2 if columns == 2 else team_count
    height = TABLE_HEADER_HEIGHT + capacity * ROW_HEIGHT
    return width, height, capacity


def build_table_template(team_count: int, orientation: str) -> Image.Image:
    width, height, capacity = table_dimensions(team_count, orientation)
    columns = 2 if orientation == "horizontal" else 1
    column_gap = 24 if columns == 2 else 0
    column_width = (
        width - 2 * OUTER_MARGIN - column_gap * (columns - 1)
    ) // columns
    image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image, "RGBA")
    row_font_size = 21 if columns == 1 else 18
    header_font_size = 17 if columns == 1 else 15

    for column_index in range(columns):
        x = OUTER_MARGIN + column_index * (column_width + column_gap)
        draw.rounded_rectangle(
            (x, 0, x + column_width, height - 1),
            radius=12,
            fill=COLORS["panel"],
            outline=COLORS["outline"],
            width=2,
        )
        draw.rounded_rectangle(
            (x + 2, 2, x + column_width - 2, TABLE_HEADER_HEIGHT),
            radius=10,
            fill=COLORS["header"],
        )
        rank_start = column_index * capacity
        rank_x = x + 22
        team_x = x + (74 if columns == 1 else 62)
        wins_x = x + column_width - (390 if columns == 1 else 330)
        kills_x = x + column_width - (280 if columns == 1 else 245)
        place_x = x + column_width - (160 if columns == 1 else 135)
        total_x = x + column_width - 20
        header_y = 22

        for label, label_x, anchor in (
            ("#", rank_x, "la"),
            ("TEAM", team_x, "la"),
            ("W", wins_x, "ra"),
            ("KILLS", kills_x, "ra"),
            ("PLACE", place_x, "ra"),
            ("TOTAL", total_x, "ra"),
        ):
            draw.text(
                (label_x, header_y),
                label,
                font=font(header_font_size),
                fill=COLORS["heading"],
                anchor=anchor,
            )

        for row_index in range(capacity):
            y = TABLE_HEADER_HEIGHT + row_index * ROW_HEIGHT
            if row_index % 2 == 1:
                draw.rectangle(
                    (x + 3, y, x + column_width - 3, y + ROW_HEIGHT),
                    fill=COLORS["panel_alt"],
                )
            draw.line(
                (x + 14, y + ROW_HEIGHT - 1, x + column_width - 14, y + ROW_HEIGHT - 1),
                fill=COLORS["divider"],
                width=1,
            )
            draw.text(
                (rank_x, y + ROW_HEIGHT // 2),
                f"{rank_start + row_index + 1:02d}",
                font=font(row_font_size),
                fill=COLORS["rank"],
                anchor="lm",
            )

    return image


def canvas_size(team_count: int, orientation: str) -> tuple[int, int]:
    _, table_height, _ = table_dimensions(team_count, orientation)
    visible_rows = team_count if orientation == "vertical" else (team_count + 1) // 2
    height = (
        2 * OUTER_MARGIN
        + DEFAULT_HEADER_HEIGHT
        + SECTION_GAP
        + TABLE_HEADER_HEIGHT
        + visible_rows * ROW_HEIGHT
        + SECTION_GAP
        + DEFAULT_FOOTER_HEIGHT
    )
    return WIDTHS[orientation], height


def build_empty_canvas(
    team_count: int,
    orientation: str,
    table: Image.Image,
    badge: Image.Image,
) -> Image.Image:
    width, height = canvas_size(team_count, orientation)
    with Image.open(BACKGROUND_PATH) as source:
        image = ImageOps.fit(
            source.convert("RGB"),
            (width, height),
            method=Image.Resampling.LANCZOS,
        ).convert("RGBA")

    table_top = OUTER_MARGIN + DEFAULT_HEADER_HEIGHT + SECTION_GAP
    image.alpha_composite(table, dest=(0, table_top))
    badge_x = width - OUTER_MARGIN - badge.width
    badge_y = OUTER_MARGIN + DEFAULT_HEADER_HEIGHT // 2 - badge.height // 2
    image.alpha_composite(badge, dest=(badge_x, badge_y))
    return image


def draw_dimension_arrow(
    draw: ImageDraw.ImageDraw,
    start: tuple[int, int],
    end: tuple[int, int],
) -> None:
    color = COLORS["measure"]
    draw.line((start, end), fill=color, width=2)
    if start[1] == end[1]:
        y = start[1]
        for x, direction in ((start[0], 1), (end[0], -1)):
            draw.polygon(
                ((x, y), (x + 9 * direction, y - 5), (x + 9 * direction, y + 5)),
                fill=color,
            )
    else:
        x = start[0]
        for y, direction in ((start[1], 1), (end[1], -1)):
            draw.polygon(
                ((x, y), (x - 5, y + 9 * direction), (x + 5, y + 9 * direction)),
                fill=color,
            )


def build_blueprint_sheet(
    team_count: int,
    orientation: str,
    canvas: Image.Image,
) -> Image.Image:
    width, height = canvas.size
    side_margin = 120
    canvas_top = 178
    bottom_margin = 76
    sheet = Image.new(
        "RGB",
        (width + 2 * side_margin, canvas_top + height + bottom_margin),
        COLORS["sheet_bg"],
    )
    draw = ImageDraw.Draw(sheet)
    body_size = 21 if orientation == "vertical" else 18
    label_size = 17 if orientation == "vertical" else 15
    draw.text(
        (side_margin, 20),
        f"{team_count} TEAMS  ·  {orientation.upper()} BLUEPRINT",
        font=font(28, 800),
        fill=COLORS["sheet_ink"],
    )
    draw.text(
        (side_margin, 62),
        (
            f"Only runtime text: date, team names, and scores · "
            f"Montserrat Regular {body_size}px · labels {label_size}px · "
            f"row height {ROW_HEIGHT}px"
        ),
        font=font(16),
        fill=COLORS["sheet_note"],
    )
    draw.text(
        (side_margin, 90),
        "Dark table, column labels, and ranks are static artwork; dynamic content cells are empty.",
        font=font(16),
        fill=COLORS["sheet_note"],
    )

    canvas_x, canvas_y = side_margin, canvas_top
    arrow_y = canvas_top - 17
    draw_dimension_arrow(
        draw,
        (canvas_x, arrow_y),
        (canvas_x + width, arrow_y),
    )
    dimension_font = font(20, 800)
    width_label = f"{width} px"
    bounds = draw.textbbox((0, 0), width_label, font=dimension_font)
    label_width = bounds[2] - bounds[0]
    label_x = canvas_x + width // 2 - label_width // 2
    draw.rectangle(
        (label_x - 8, arrow_y - 28, label_x + label_width + 8, arrow_y - 4),
        fill=COLORS["sheet_bg"],
    )
    draw.text(
        (label_x, arrow_y - 28),
        width_label,
        font=dimension_font,
        fill=COLORS["measure"],
    )

    arrow_x = canvas_x - 28
    draw_dimension_arrow(
        draw,
        (arrow_x, canvas_y),
        (arrow_x, canvas_y + height),
    )
    vertical_label = Image.new("RGBA", (120, 34), (0, 0, 0, 0))
    ImageDraw.Draw(vertical_label).text(
        (2, 0),
        f"{height} px",
        font=dimension_font,
        fill=COLORS["measure"],
    )
    vertical_label = vertical_label.rotate(90, expand=True)
    sheet.paste(
        vertical_label,
        (8, canvas_y + (height - vertical_label.height) // 2),
        vertical_label,
    )
    sheet.paste(canvas.convert("RGB"), (canvas_x, canvas_y))
    draw.text(
        (side_margin, canvas_y + height + 22),
        f"Native design area: {width} × {height} px  |  1 image pixel = 1 design pixel",
        font=font(20, 800),
        fill=COLORS["sheet_ink"],
    )
    return sheet


def main() -> None:
    TEMPLATE_DIR.mkdir(parents=True, exist_ok=True)
    PUBLIC_TEMPLATE_DIR.mkdir(parents=True, exist_ok=True)
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    CANVAS_EXPORT_DIR.mkdir(parents=True, exist_ok=True)

    badge = date_badge()
    badge.save(TEMPLATE_DIR / "date-badge.png", optimize=True)
    badge.save(PUBLIC_TEMPLATE_DIR / "date-badge.png", optimize=True)

    previews = []
    for orientation in ORIENTATIONS:
        for team_count in TEAM_COUNTS:
            table = build_table_template(team_count, orientation)
            table_name = f"table-{team_count}-{orientation}.png"
            table.save(TEMPLATE_DIR / table_name, optimize=True)
            table.save(PUBLIC_TEMPLATE_DIR / table_name, optimize=True)

            canvas = build_empty_canvas(team_count, orientation, table, badge)
            canvas_name = f"leaderboard-{team_count}-team-{orientation}-empty.png"
            canvas.save(CANVAS_EXPORT_DIR / canvas_name, optimize=True)

            sheet = build_blueprint_sheet(team_count, orientation, canvas)
            sheet.save(
                EXPORT_DIR / f"leaderboard-{team_count}-team-{orientation}-blueprint.png",
                optimize=True,
            )
            previews.append((team_count, orientation, canvas.copy()))

    margin = 24
    card_width = 680
    card_height = 370
    gap_x = 20
    gap_y = 16
    header_height = 142
    overview = Image.new(
        "RGB",
        (
            margin * 2 + card_width * 2 + gap_x,
            header_height + margin + card_height * 5 + gap_y * 4 + margin,
        ),
        COLORS["sheet_bg"],
    )
    draw = ImageDraw.Draw(overview)
    draw.text(
        (margin, 22),
        "Dark Leaderboard Blueprint Canvases",
        font=font(34, 800),
        fill=COLORS["sheet_ink"],
    )
    draw.text(
        (margin, 68),
        "16 · 18 · 20 · 22 · 24 teams  |  vertical + horizontal",
        font=font(18),
        fill=COLORS["sheet_note"],
    )
    draw.text(
        (margin, 98),
        "Static dark table artwork · empty date/team/score fields · native dimensions shown",
        font=font(18),
        fill=COLORS["sheet_note"],
    )
    for team_count, orientation, canvas in previews:
        row = TEAM_COUNTS.index(team_count)
        column = 0 if orientation == "vertical" else 1
        x = margin + column * (card_width + gap_x)
        y = header_height + margin + row * (card_height + gap_y)
        draw.rounded_rectangle(
            (x, y, x + card_width, y + card_height),
            radius=12,
            fill=(255, 255, 255),
            outline=(203, 214, 220),
            width=2,
        )
        draw.text(
            (x + 18, y + 14),
            f"{team_count} TEAMS  ·  {orientation.upper()}  ·  {canvas.width} × {canvas.height} px",
            font=font(18, 800),
            fill=COLORS["measure"],
        )
        preview = canvas.copy()
        preview.thumbnail((card_width - 36, card_height - 62), Image.Resampling.LANCZOS)
        preview_x = x + (card_width - preview.width) // 2
        preview_y = y + 52 + (card_height - 62 - preview.height) // 2
        overview.paste(preview, (preview_x, preview_y))

    overview.save(EXPORT_DIR / "leaderboard-blueprints-overview.png", optimize=True)
    print(
        f"Generated {len(TEAM_COUNTS) * len(ORIENTATIONS)} static table templates, "
        "one static date badge, and dark empty blueprints."
    )


if __name__ == "__main__":
    main()