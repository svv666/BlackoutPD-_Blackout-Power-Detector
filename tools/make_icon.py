from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "assets"


def make_icon(size: int = 256) -> Image.Image:
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    s = size / 256.0

    def box(*values):
        return tuple(round(value * s) for value in values)

    draw.rounded_rectangle(
        box(6, 6, 250, 250),
        radius=round(58 * s),
        fill="#0A111A",
        outline="#243244",
        width=max(2, round(5 * s)),
    )

    shield = [
        (128 * s, 34 * s),
        (204 * s, 67 * s),
        (196 * s, 143 * s),
        (128 * s, 221 * s),
        (60 * s, 143 * s),
        (52 * s, 67 * s),
    ]
    draw.polygon(shield, fill="#111C27")
    draw.line(
        shield + [shield[0]],
        fill="#22C55E",
        width=max(3, round(5 * s)),
        joint="curve",
    )

    bolt = [
        (145 * s, 68 * s),
        (99 * s, 140 * s),
        (124 * s, 140 * s),
        (109 * s, 190 * s),
        (160 * s, 111 * s),
        (133 * s, 111 * s),
    ]
    draw.polygon(bolt, fill="#22C55E")

    draw.ellipse(
        box(188, 188, 220, 220),
        fill="#22C55E",
        outline="#0A111A",
        width=max(2, round(5 * s)),
    )
    return image


def main() -> None:
    ASSETS.mkdir(parents=True, exist_ok=True)
    icon = make_icon(256)
    icon.save(
        ASSETS / "app.ico",
        format="ICO",
        sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
    )
    icon.save(ASSETS / "app.png", format="PNG")
    print(f"Created {ASSETS / 'app.ico'}")


if __name__ == "__main__":
    main()
