from PIL import Image, ImageDraw

def draw_icon(size):
    s = size * 4
    img = Image.new('RGBA', (s, s), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    r = round(s * 0.22)
    draw.rounded_rectangle([0, 0, s - 1, s - 1], radius=r, fill='#22c55e')

    raw = [(18, 76), (36, 54), (54, 63), (82, 24)]
    pts = [(round(x / 100 * s), round(y / 100 * s)) for x, y in raw]

    lw = max(2, round(s * 0.055))
    draw.line(pts, fill='white', width=lw)

    dr = max(2, round(s * 0.052))
    for x, y in pts:
        draw.ellipse([x - dr, y - dr, x + dr, y + dr], fill='white')

    return img.resize((size, size), Image.LANCZOS)

img = draw_icon(256)
img.save('icon.ico', format='ICO', sizes=[(16, 16), (32, 32), (48, 48), (256, 256)])
print('icon.ico created!')
