"""Generate dimensioned leaderboard blueprints for every supported profile."""

from __future__ import annotations

from math import ceil
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


ROOT = Path(__file__).resolve().parents[1]
FONT_PATH = ROOT / "assets" / "fonts" / "Montserrat[wght].ttf"
OUTPUT_DIRS = (
    ROOT / "assets" / "leaderboard-blueprints",
    ROOT / "A.R.C. Public" / "assets" / "leaderboard-blueprints",
)

TEAM_COUNTS = (16, 18, 20, 22, 24)
ORIENTATIONS = ("vertical", "horizontal")
VERTICAL_WIDTH = 1080
HORIZONTAL_WIDTH = 1920
OUTER_MARGIN = 28
HEADER_HEIGHT = 180
SECTION_GAP = 10
TABLE_HEADER_HEIGHT = 64
VERTICAL_ROW_HEIGHT = 60
HORIZONTAL_ROW_HEIGHT = 80
FOOTER_HEIGHT = 120
COLUMN_GAP = 24
FIELD_NAMES = ("RANK", "TEAM NAME", "WIN", "KILLS", "PLACE", "TOTAL")
VERTICAL_FIELD_WIDTHS = (48, 500, 120, 120, 120, 116)


def _row_height(orientation: str) -> int:
    return VERTICAL_ROW_HEIGHT if orientation == "vertical" else HORIZONTAL_ROW_HEIGHT


BG = (4, 17, 36)
PANEL = (6, 23, 43)
HEADER = (8, 43, 70)
ROW_DARK = (7, 27, 49)
ROW_LIGHT = (6, 23, 43)
GRID = (30, 72, 100)
BORDER = (38, 109, 143)
CYAN = (68, 198, 231)
PALE = (224, 236, 246)
MUTED = (156, 183, 201)
SAMPLE = (231, 196, 117)


def _font(size: int, *, bold: bool = False) -> ImageFont.FreeTypeFont:
    font = ImageFont.truetype(str(FONT_PATH), size=size)
    font.set_variation_by_name("Bold" if bold else "Regular")
    return font


def _canvas_dimensions(team_count: int, orientation: str) -> tuple[int, int]:
    width = VERTICAL_WIDTH if orientation == "vertical" else HORIZONTAL_WIDTH
    rows = team_count if orientation == "vertical" else ceil(team_count / 2)
    row_height = _row_height(orientation)
    height = (
        2 * OUTER_MARGIN
        + HEADER_HEIGHT
        + SECTION_GAP
        + TABLE_HEADER_HEIGHT
        + rows * row_height
        + SECTION_GAP
        + FOOTER_HEIGHT
    )
    return width, height


def _field_widths(table_width: int) -> tuple[int, ...]:
    # Scale the approved Vertical 16 fields proportionally for each horizontal
    # half, distributing rounding pixels while keeping the exact column width.
    exact = [width * table_width / sum(VERTICAL_FIELD_WIDTHS) for width in VERTICAL_FIELD_WIDTHS]
    widths = [int(width) for width in exact]
    remaining = table_width - sum(widths)
    order = sorted(
        range(len(widths)),
        key=lambda index: exact[index] - widths[index],
        reverse=True,
    )
    for index in order[:remaining]:
        widths[index] += 1
    return tuple(widths)


def _draw_rulers(image: Image.Image) -> None:
    draw = ImageDraw.Draw(image)
    small = _font(9)
    for x in range(0, image.width + 1, 100):
        draw.line((x, 0, x, 22), fill=GRID, width=1)
        anchor = "mt"
        if x == 0:
            anchor = "lt"
        elif x >= image.width - 40:
            anchor = "rt"
        draw.text((x, 2), str(x), fill=MUTED, font=small, anchor=anchor)
    for y in range(0, image.height + 1, 100):
        draw.line((0, y, 20, y), fill=GRID, width=1)
        anchor = "lm"
        draw.text((1, y), str(y), fill=MUTED, font=small, anchor=anchor)


def _draw_dimension_line(
    draw: ImageDraw.ImageDraw,
    x0: int,
    x1: int,
    y: int,
    label: str,
    *,
    font: ImageFont.FreeTypeFont,
) -> None:
    draw.line((x0, y, x1, y), fill=CYAN, width=1)
    draw.line((x0, y - 4, x0, y + 4), fill=CYAN, width=1)
    draw.line((x1, y - 4, x1, y + 4), fill=CYAN, width=1)
    draw.polygon(((x0, y), (x0 + 5, y - 2), (x0 + 5, y + 2)), fill=CYAN)
    draw.polygon(((x1, y), (x1 - 5, y - 2), (x1 - 5, y + 2)), fill=CYAN)
    draw.text(((x0 + x1) // 2, y - 8), label, fill=CYAN, font=font, anchor="ms")


def _draw_header(
    image: Image.Image,
    *,
    team_count: int,
    orientation: str,
    table_height: int,
) -> None:
    draw = ImageDraw.Draw(image)
    width, height = image.size
    content_width = width - 2 * OUTER_MARGIN
    label_font = _font(12)

    draw.text(
        (54, 38),
        f"IMAGE {width} × {height} PX",
        fill=PALE,
        font=_font(18, bold=True),
    )
    draw.text(
        (54, 63),
        f"HEADER {content_width} × {HEADER_HEIGHT} PX",
        fill=CYAN,
        font=label_font,
    )
    draw.text(
        (54, 83),
        f"TABLE {content_width} × {table_height} PX",
        fill=CYAN,
        font=label_font,
    )
    draw.text(
        (54, 103),
        f"SECTION GAP {SECTION_GAP} PX ABOVE + BELOW TABLE",
        fill=MUTED,
        font=_font(11),
    )

    badge_left = width - OUTER_MARGIN - 150
    draw.rounded_rectangle(
        (badge_left, 78, badge_left + 150, 118),
        radius=7,
        fill=HEADER,
        outline=BORDER,
        width=1,
    )
    draw.text(
        (badge_left + 75, 98),
        "24 SEP 2026",
        fill=PALE,
        font=_font(15),
        anchor="mm",
    )
    draw.text(
        (badge_left + 75, 128),
        "DATE BADGE 150 × 40 PX",
        fill=MUTED,
        font=_font(10),
        anchor="mm",
    )

    _draw_dimension_line(
        draw,
        OUTER_MARGIN,
        width - OUTER_MARGIN,
        157,
        f"HEADER WIDTH {content_width} PX",
        font=label_font,
    )
    _draw_dimension_line(
        draw,
        OUTER_MARGIN,
        width - OUTER_MARGIN,
        188,
        f"CONTENT / TABLE WIDTH {content_width} PX",
        font=label_font,
    )


def _draw_table(
    image: Image.Image,
    *,
    team_count: int,
    orientation: str,
    table_top: int,
) -> tuple[int, int, int]:
    draw = ImageDraw.Draw(image)
    canvas_width = image.width
    content_width = canvas_width - 2 * OUTER_MARGIN
    columns = 1 if orientation == "vertical" else 2
    rows_per_column = team_count if columns == 1 else ceil(team_count / 2)
    gap = 0 if columns == 1 else COLUMN_GAP
    row_height = _row_height(orientation)
    column_width = (content_width - gap * (columns - 1)) // columns
    field_widths = _field_widths(column_width)
    table_height = TABLE_HEADER_HEIGHT + rows_per_column * row_height
    table_bottom = table_top + table_height

    for column in range(columns):
        left = OUTER_MARGIN + column * (column_width + gap)
        right = left + column_width
        draw.rounded_rectangle(
            (left, table_top, right, table_bottom),
            radius=7,
            fill=ROW_DARK,
            outline=BORDER,
            width=1,
        )
        draw.rounded_rectangle(
            (left + 1, table_top + 1, right - 1, table_top + TABLE_HEADER_HEIGHT),
            radius=6,
            fill=HEADER,
        )
        draw.rectangle(
            (
                left + 1,
                table_top + 8,
                right - 1,
                table_top + TABLE_HEADER_HEIGHT,
            ),
            fill=HEADER,
        )

        x_positions = [left]
        for width in field_widths:
            x_positions.append(x_positions[-1] + width)

        for field_index, name in enumerate(FIELD_NAMES):
            x0, x1 = x_positions[field_index], x_positions[field_index + 1]
            draw.text(
                ((x0 + x1) // 2, table_top + TABLE_HEADER_HEIGHT // 2),
                name,
                fill=CYAN,
                font=_font(12, bold=True),
                anchor="mm",
            )

        draw.line(
            (left, table_top + TABLE_HEADER_HEIGHT, right, table_top + TABLE_HEADER_HEIGHT),
            fill=BORDER,
            width=1,
        )
        for x in x_positions[1:-1]:
            draw.line(
                (x, table_top, x, table_bottom),
                fill=GRID,
                width=1,
            )

        for row_index in range(rows_per_column):
            row_top = table_top + TABLE_HEADER_HEIGHT + row_index * row_height
            row_bottom = row_top + row_height
            draw.rectangle(
                (left + 1, row_top + 1, right - 1, row_bottom),
                fill=ROW_LIGHT if row_index % 2 == 0 else ROW_DARK,
            )
            draw.line((left, row_bottom, right, row_bottom), fill=GRID, width=1)
            rank = column * rows_per_column + row_index + 1
            center_y = row_top + row_height // 2
            draw.text(
                ((x_positions[0] + x_positions[1]) // 2, center_y),
                f"{rank:02d}",
                fill=MUTED,
                font=_font(17),
                anchor="mm",
            )
            if rank == 1:
                draw.text(
                    (x_positions[1] + 14, center_y),
                    "SAMPLE TEAM",
                    fill=SAMPLE,
                    font=_font(16),
                    anchor="lm",
                )
                for value, field_index in ((1, 2), (5, 3), (10, 4), (15, 5)):
                    draw.text(
                        (x_positions[field_index + 1] - 12, center_y),
                        str(value),
                        fill=PALE,
                        font=_font(16),
                        anchor="rm",
                    )

    return table_height, rows_per_column, column_width


def _draw_footer(
    image: Image.Image,
    *,
    orientation: str,
    rows_per_column: int,
    column_width: int,
    table_bottom: int,
) -> None:
    draw = ImageDraw.Draw(image)
    width = image.width
    content_width = width - 2 * OUTER_MARGIN
    footer_top = table_bottom + SECTION_GAP
    footer_bottom = footer_top + FOOTER_HEIGHT
    row_height = _row_height(orientation)
    body_height = rows_per_column * row_height
    field_widths = _field_widths(column_width)

    draw.rectangle(
        (
            OUTER_MARGIN,
            footer_top,
            width - OUTER_MARGIN,
            footer_bottom,
        ),
        fill=PANEL,
        outline=BORDER,
        width=1,
    )
    draw.text(
        (width // 2, footer_top + 14),
        f"FOOTER {content_width} × {FOOTER_HEIGHT} PX",
        fill=PALE,
        font=_font(14, bold=True),
        anchor="mm",
    )
    rows_label = (
        f"{rows_per_column} ROWS"
        if orientation == "vertical"
        else f"{rows_per_column} ROWS PER COLUMN"
    )
    draw.text(
        (width // 2, footer_top + 39),
        (
            f"TABLE HEADER ROW {column_width} × {TABLE_HEADER_HEIGHT} PX"
            f"  ·  DATA ROW {column_width} × {row_height} PX"
            f"  ·  {rows_label}"
        ),
        fill=CYAN,
        font=_font(11),
        anchor="mm",
    )

    header_fields = "  |  ".join(
        f"{name} {field_width}×{TABLE_HEADER_HEIGHT}"
        for name, field_width in zip(FIELD_NAMES, field_widths, strict=True)
    )
    body_fields = "  |  ".join(
        f"{name} {field_width}×{body_height}"
        for name, field_width in zip(FIELD_NAMES, field_widths, strict=True)
    )
    draw.text(
        (width // 2, footer_top + 64),
        f"FIELD HEADER SIZES (PX): {header_fields}",
        fill=MUTED,
        font=_font(10),
        anchor="mm",
    )
    draw.text(
        (width // 2, footer_top + 82),
        f"FIELD BODY SIZES (PX): {body_fields}",
        fill=MUTED,
        font=_font(10),
        anchor="mm",
    )
    draw.text(
        (width // 2, footer_top + 103),
        f"SECTION GAP {SECTION_GAP} PX  ·  OUTER MARGIN {OUTER_MARGIN} PX",
        fill=CYAN,
        font=_font(10),
        anchor="mm",
    )


def _draw_table_height_measure(image: Image.Image, top: int, height: int) -> None:
    draw = ImageDraw.Draw(image)
    x = image.width - 13
    draw.line((x, top, x, top + height), fill=CYAN, width=1)
    draw.line((x - 4, top, x + 4, top), fill=CYAN, width=1)
    draw.line((x - 4, top + height, x + 4, top + height), fill=CYAN, width=1)

    text_image = Image.new("RGBA", (180, 18), (0, 0, 0, 0))
    text_draw = ImageDraw.Draw(text_image)
    text_draw.text(
        (90, 9),
        f"TABLE HEIGHT {height} PX",
        fill=CYAN,
        font=_font(9),
        anchor="mm",
    )
    rotated = text_image.rotate(90, expand=True)
    image.paste(
        rotated,
        (x - rotated.width // 2, top + (height - rotated.height) // 2),
        rotated,
    )


def create_blueprint(team_count: int, orientation: str) -> Image.Image:
    width, height = _canvas_dimensions(team_count, orientation)
    image = Image.new("RGB", (width, height), BG)
    _draw_rulers(image)
    rows_per_column = team_count if orientation == "vertical" else ceil(team_count / 2)
    table_height = (
        TABLE_HEADER_HEIGHT + rows_per_column * _row_height(orientation)
    )
    table_top = OUTER_MARGIN + HEADER_HEIGHT + SECTION_GAP
    _draw_header(
        image,
        team_count=team_count,
        orientation=orientation,
        table_height=table_height,
    )
    actual_table_height, rows_per_column, column_width = _draw_table(
        image,
        team_count=team_count,
        orientation=orientation,
        table_top=table_top,
    )
    table_bottom = table_top + actual_table_height
    _draw_table_height_measure(image, table_top, actual_table_height)
    _draw_footer(
        image,
        orientation=orientation,
        rows_per_column=rows_per_column,
        column_width=column_width,
        table_bottom=table_bottom,
    )
    return image


def main() -> None:
    for orientation in ORIENTATIONS:
        for team_count in TEAM_COUNTS:
            image = create_blueprint(team_count, orientation)
            filename = f"{orientation}-{team_count}-complete-dimensions.png"
            for output_dir in OUTPUT_DIRS:
                output_dir.mkdir(parents=True, exist_ok=True)
                image.save(output_dir / filename, format="PNG", optimize=True)
            print(f"Generated {filename}: {image.width}×{image.height}")


if __name__ == "__main__":
    main()