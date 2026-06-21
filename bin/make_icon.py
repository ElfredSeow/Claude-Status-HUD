"""Generate the 'Claude Traffic Light' app icon (traffic_light.ico)."""
import os
from PIL import Image, ImageDraw

S = 1024  # supersample, downscaled for smooth edges
img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(img)

# Rounded housing
body = (S * 0.30, S * 0.10, S * 0.70, S * 0.90)
d.rounded_rectangle(body, radius=S * 0.12, fill=(28, 31, 38, 255),
                    outline=(70, 76, 88, 255), width=int(S * 0.012))

# Three lamps (all lit, glowing) - red, amber, green top to bottom
cx = S * 0.50
r = S * 0.105
ys = [S * 0.275, S * 0.50, S * 0.725]
colors = [(235, 45, 45), (255, 160, 0), (45, 205, 95)]
for cy, col in zip(ys, colors):
    # soft glow
    for gr, a in ((r * 1.7, 60), (r * 1.35, 110)):
        d.ellipse((cx - gr, cy - gr, cx + gr, cy + gr), fill=col + (a,))
    d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=col + (255,))
    # highlight
    hr = r * 0.4
    d.ellipse((cx - r * 0.45 - hr, cy - r * 0.45 - hr,
               cx - r * 0.45 + hr, cy - r * 0.45 + hr),
              fill=(255, 255, 255, 90))

out = os.path.join(os.path.dirname(__file__), "traffic_light.ico")
sizes = [256, 128, 64, 48, 32, 16]
base = img.resize((256, 256), Image.LANCZOS)
base.save(out, sizes=[(s, s) for s in sizes])
print("wrote", out)
