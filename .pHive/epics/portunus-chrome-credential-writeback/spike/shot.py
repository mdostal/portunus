import sys
from Xlib import display, X
from PIL import Image
dpy = display.Display(sys.argv[1]); root = dpy.screen().root; g = root.get_geometry()
raw = root.get_image(0, 0, g.width, g.height, X.ZPixmap, 0xffffffff)
Image.frombytes("RGB", (g.width, g.height), raw.data, "raw", "BGRX").save(sys.argv[2])
