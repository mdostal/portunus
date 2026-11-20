"""Read back every row for (origin, username) -- digests only, never values."""
import hashlib, json, sqlite3, shutil, sys, tempfile
from pathlib import Path
sys.path.insert(0, sys.argv[1])  # portunus src
from portunus import chrome_discovery, chrome_crypto
from portunus.backend import ChromeBackend
udd = Path(sys.argv[2]); user = sys.argv[3]
def _linux_basic_key():
    # Harness-local: Linux Chromium's basic (no-keyring) store key = PBKDF2-SHA1("peanuts", "saltysalt", 1 iteration).
    # Linux uses 1 iteration (macOS 1003) -- chrome_crypto currently uses 1003 everywhere; see spike-results F0.
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    return PBKDF2HMAC(algorithm=hashes.SHA1(), length=16, salt=b"saltysalt", iterations=1).derive(b"peanuts")
d = lambda s: hashlib.sha256(s.encode()).hexdigest()[:16]
out = {}
for store in ("Login Data", "Login Data For Account"):
    p = udd / "Default" / store
    if not p.exists(): out[store] = "absent"; continue
    with tempfile.TemporaryDirectory() as t:
        c = Path(t) / "x"; shutil.copy2(p, c)
        con = sqlite3.connect(f"file:{c}?mode=ro", uri=True)
        rows = con.execute("SELECT origin_url, signon_realm, username_value, password_value, date_created, date_last_used, date_password_modified FROM logins WHERE username_value=?", (user,)).fetchall()
        con.close()
    lst = []
    for o, realm, u, blob, dc, dlu, dpm in rows:
        try:
            pk = chrome_crypto.decrypt_blob(_linux_basic_key(), blob)  # basic-store key
            dg = d(pk)
        except Exception as e:
            dg = f"err:{type(e).__name__}"
        lst.append({"origin_url": o, "signon_realm": realm, "prefix": blob[:3].decode(errors="replace"), "sha256_16": dg,
                    "date_created": dc, "date_password_modified": dpm})
    out[store] = lst
# ChromeBackend read path (the epic's verify path)
origins = sorted({r["origin_url"] for r in (out["Login Data"] if isinstance(out["Login Data"], list) else [])})
cb = {}
for o in origins:
    try:
        cb[o] = d(ChromeBackend(user_data_dir=udd).access(f"chrome:Default:{o}:{user}"))
    except Exception as e:
        cb[o] = f"err:{type(e).__name__}:{str(e)[:120]}"
out["ChromeBackend.access"] = cb
out["profiles"] = [p.dir_name for p in chrome_discovery.discover_profiles(udd)]
print(json.dumps(out, indent=1))
