"""Write a caption bar onto rendered PNGs (needs Pillow): ``python3 label_png.py TEXT FILE...``.

kicad-cli renders carry no caption, and a caption on the board's silkscreen would be fabricated,
so integrate.py's renders are labelled here instead.
"""

import sys

from PIL import Image, ImageDraw, ImageFont


def _font(size):
    for name in (
        "DejaVuSans.ttf",
        "Helvetica.ttc",
        "Arial.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
    ):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def label(path, text, sub):
    img = Image.open(path).convert("RGB")
    w, h = img.size
    bar = max(40, h // 18)
    out = Image.new("RGB", (w, h + bar), (24, 24, 24))
    out.paste(img, (0, bar))
    draw = ImageDraw.Draw(out)
    draw.text((bar // 3, bar // 5), text, fill=(255, 255, 255), font=_font(int(bar * 0.55)))
    if sub:
        f = _font(int(bar * 0.35))
        tw = draw.textlength(sub, font=f)
        draw.text((w - tw - bar // 3, bar // 3), sub, fill=(190, 190, 190), font=f)
    out.save(path)


def main(argv):
    text, files = argv[1], argv[2:]
    for f in files:
        view = f.rsplit("-", 1)[-1].rsplit(".", 1)[0]
        label(f, text, "radar60 Rev A, yapnr placement, %s view" % view)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
