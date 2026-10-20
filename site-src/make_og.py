"""Generates site/og.png (1200x630). Run: python3 site-src/make_og.py"""
from PIL import Image, ImageDraw, ImageFont
import pathlib
W, H = 1200, 630
BG, SURF, BORDER, TEXT, MUTED, ACC = "#0a0c10", "#11151b", "#2f3846", "#e8ecf2", "#a3adbb", "#5eead4"
def font(paths, size):
    for p, idx in paths:
        try: return ImageFont.truetype(p, size, index=idx)
        except Exception: pass
    return ImageFont.load_default()
SANS = [("/System/Library/Fonts/HelveticaNeue.ttc", 1), ("/System/Library/Fonts/Helvetica.ttc", 1)]
SANSR = [("/System/Library/Fonts/HelveticaNeue.ttc", 0), ("/System/Library/Fonts/Helvetica.ttc", 0)]
MONO = [("/System/Library/Fonts/Menlo.ttc", 1), ("/System/Library/Fonts/Monaco.ttf", 0)]
img = Image.new("RGB", (W, H), BG)
d = ImageDraw.Draw(img)
d.rounded_rectangle((40, 40, W - 40, H - 40), 28, fill=SURF, outline=BORDER, width=2)
d.text((90, 90), "filebridge", font=font(MONO, 40), fill=ACC)
f = font(SANS, 68)
y = 170
for line in ["Upload any-size files into", "the browser you're already", "signed in to, from an AI agent."]:
    d.text((90, y), line, font=f, fill=TEXT); y += 82
d.text((90, 440), "MCP server and CLI for Claude Code and Codex", font=font(SANSR, 32), fill=MUTED)
# bridge mark
cx, cy = 1010, 120
d.line((cx - 60, cy + 40, cx + 60, cy + 40), fill=ACC, width=8)
for x in (-40, 0, 40): d.line((cx + x, cy + 40, cx + x, cy + 10), fill=ACC, width=8)
d.arc((cx - 60, cy - 30, cx + 60, cy + 50), 180, 360, fill=ACC, width=8)
# command pill
d.rounded_rectangle((90, 505, 1110, 565), 14, fill=BG, outline=BORDER, width=2)
d.text((115, 521), "$ filebridge serve ~/Movies/final.mp4", font=font(MONO, 22), fill=TEXT)
out = pathlib.Path(__file__).resolve().parent.parent / "site" / "og.png"
img.save(out, optimize=True); print(out, img.size)
