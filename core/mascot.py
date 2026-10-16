"""Grid-native beetle mascot: each logical pixel occupies an exact 2x2 screen block."""
from PIL import Image

SHELL = (38, 170, 156, 255)
SHADOW = (20, 110, 108, 255)
LIGHT = (118, 226, 204, 255)
GLOSS = (226, 255, 246, 255)
DARK = (16, 52, 60, 255)
OUTLINE = (255, 248, 232, 255)


def render(expression='open', size=64):
    # Round shell, two antennae with knobs and three short legs on each side.
    body = {(x, y) for x in range(32) for y in range(32)
            if ((x - 15.5) / 10.6) ** 2 + ((y - 15.5) / 9.4) ** 2 <= 1}
    antennae = {(11, 6), (10, 5), (10, 4), (9, 3), (20, 6), (21, 5), (21, 4), (22, 3)}
    knobs = {(8, 2), (9, 2), (8, 3), (22, 2), (23, 2), (23, 3)}
    legs = {(x, y) for y in (12, 16, 20) for x in (3, 4, 27, 28)}
    legs |= {(3, 13), (3, 17), (3, 21), (28, 13), (28, 17), (28, 21)}
    solid = body | antennae | knobs | legs
    outline = {(x + dx, y + dy) for x, y in solid
               for dx, dy in ((0, 0), (-1, 0), (1, 0), (0, -1), (0, 1))}
    image = Image.new('RGBA', (32, 32))
    for point in outline:
        image.putpixel(point, OUTLINE)
    for point in antennae | legs:
        image.putpixel(point, DARK)
    for point in knobs:
        image.putpixel(point, SHELL)
    for x, y in body:
        color = SHELL
        if (x, y + 1) not in body or (x + 1, y) not in body:
            color = SHADOW
        elif (x, y - 1) not in body or (x - 1, y) not in body:
            color = LIGHT
        image.putpixel((x, y), color)
    # Wing seam on the lower half of the shell and a small gloss spot.
    for y in range(16, 25):
        if (15, y) in body:
            image.putpixel((15, y), SHADOW)
            image.putpixel((16, y), SHADOW)
    for point in ((9, 9), (10, 9), (9, 10)):
        image.putpixel(point, GLOSS)
    if expression == 'recording':
        # Squeezed > < eyes.
        eyes = {(10 + dx, 10 + dy) for dx, dy in ((0, 0), (1, 1), (2, 2), (1, 3), (0, 4))}
        eyes |= {(21 - dx, 10 + dy) for dx, dy in ((0, 0), (1, 1), (2, 2), (1, 3), (0, 4))}
        for point in eyes:
            image.putpixel(point, DARK)
    else:
        for left in (10, 18):
            for x in range(left, left + 4):
                for y in range(9, 14):
                    image.putpixel((x, y), OUTLINE)
            for x in range(left + 1, left + 3):
                for y in range(10, 13):
                    image.putpixel((x, y), DARK)
    return image.resize((size, size), Image.Resampling.NEAREST)
