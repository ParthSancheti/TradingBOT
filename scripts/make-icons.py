"""Shared application mark, drawn at native icon resolutions."""
from pathlib import Path
from PIL import Image, ImageDraw
root=Path(__file__).resolve().parents[1]
def icon(size):
    s=512;im=Image.new("RGBA",(s,s));d=ImageDraw.Draw(im)
    d.rounded_rectangle((10,10,502,502),radius=122,fill="#6253c7")
    d.line([(323,138),(323,297)],fill="white",width=62)
    d.arc((139,204,354,381),0,180,fill="white",width=62)
    d.ellipse((368,96,411,139),fill="#a9e0cb")
    return im.resize((size,size),Image.Resampling.LANCZOS)
for size in (16,32,48,128):
    out=root/"extension"/"icons"/f"jarvis-{size}.png";out.parent.mkdir(exist_ok=True);icon(size).save(out)
icon(256).save(root/"desktop"/"renderer"/"icon.png")
icon(256).save(root/"desktop"/"icon.ico",sizes=[(16,16),(32,32),(48,48),(128,128),(256,256)])
