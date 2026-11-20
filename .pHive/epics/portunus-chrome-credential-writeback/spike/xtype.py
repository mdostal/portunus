# Harness-only OS-level input (X11 XTEST on Xvfb): Ctrl+L, type a path, Enter. Used ONLY to observe CSV-import conflict behaviour.
import sys, time
from Xlib import display, X, XK
from Xlib.ext import xtest
dpy = display.Display(sys.argv[1])
def key(sym, shift=False):
    kc = dpy.keysym_to_keycode(XK.string_to_keysym(sym))
    sh = dpy.keysym_to_keycode(XK.string_to_keysym('Shift_L'))
    if shift: xtest.fake_input(dpy, X.KeyPress, sh)
    xtest.fake_input(dpy, X.KeyPress, kc); xtest.fake_input(dpy, X.KeyRelease, kc)
    if shift: xtest.fake_input(dpy, X.KeyRelease, sh)
    dpy.sync(); time.sleep(0.02)
# focus the GTK dialog (no WM on Xvfb): click inside its file list area first
xtest.fake_input(dpy, X.MotionNotify, x=600, y=790); dpy.sync(); time.sleep(0.2)
xtest.fake_input(dpy, X.ButtonPress, 1); xtest.fake_input(dpy, X.ButtonRelease, 1); dpy.sync(); time.sleep(0.5)
ctl = dpy.keysym_to_keycode(XK.string_to_keysym('Control_L'))
xtest.fake_input(dpy, X.KeyPress, ctl); key('l'); xtest.fake_input(dpy, X.KeyRelease, ctl); dpy.sync(); time.sleep(0.5)
names = {'/': 'slash', '-': 'minus', '.': 'period', '_': 'underscore'}
for ch in sys.argv[2]:
    if ch in names: key(names[ch], shift=(ch == '_'))
    elif ch.isupper(): key(ch.lower(), shift=True)
    else: key(ch)
time.sleep(0.3)
import subprocess; subprocess.run(['python3','/home/node/spike-work/shot.py',sys.argv[1],'/home/node/spike-work/shots/import-typed.png'])
xtest.fake_input(dpy, X.MotionNotify, x=1075, y=798); dpy.sync(); time.sleep(0.2); xtest.fake_input(dpy, X.ButtonPress, 1); xtest.fake_input(dpy, X.ButtonRelease, 1); dpy.sync()
