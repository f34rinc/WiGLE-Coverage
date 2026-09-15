#!/usr/bin/env python3
"""Vendor Leaflet into ../vendor/leaflet/ for wigle_coverage.py to INLINE into the generated
map, so the page is self-contained (no CDN request - which a hardened browser blocks on file://
pages - and it works offline).

Downloads leaflet.js + leaflet.css for the pinned version, verifies both against the published
Subresource Integrity (SRI) hashes before trusting them, and inlines every url(images/...) the
CSS references (the layers-control icon + retina variant, marker icon) as data URIs so the
vendored CSS needs no images/ folder. Re-run to bump the version (update VER + EXPECT).

    python scripts/vendor_leaflet.py

Leaflet is BSD-2-Clause; the vendored leaflet.js keeps its license header.
"""
import os
import re
import base64
import hashlib
import urllib.request

VER = "1.9.4"
BASE = f"https://unpkg.com/leaflet@{VER}/dist/"
OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "vendor", "leaflet")
# Published SRI hashes (sha256, base64) for this version - integrity gate on the download.
EXPECT = {"leaflet.js":  "20nQCchB9co0qIjJZRGuk2/Z9VM+kNiyxNV1lvTlZBo=",
          "leaflet.css": "p4NxAoJBhIIN+hmNHrzRCf9tD/miZyoHS5obTRR9BMY="}


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "wigle-coverage vendoring"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


def sri(b):
    return base64.b64encode(hashlib.sha256(b).digest()).decode()


def main():
    os.makedirs(OUT, exist_ok=True)
    js = get(BASE + "leaflet.js")
    css = get(BASE + "leaflet.css").decode("utf-8")
    assert sri(js) == EXPECT["leaflet.js"], f"leaflet.js hash mismatch: {sri(js)}"
    assert sri(css.encode()) == EXPECT["leaflet.css"], f"leaflet.css hash mismatch: {sri(css.encode())}"
    print("integrity OK (matches published SRI)")

    for img in sorted(set(re.findall(r"url\(\s*images/([A-Za-z0-9._-]+)\s*\)", css))):
        data = get(BASE + "images/" + img)
        ext = img.rsplit(".", 1)[-1].lower()
        mime = {"png": "image/png", "svg": "image/svg+xml", "gif": "image/gif"}.get(ext, "application/octet-stream")
        uri = f"data:{mime};base64," + base64.b64encode(data).decode()
        css = re.sub(r"url\(\s*images/" + re.escape(img) + r"\s*\)", f"url({uri})", css)
        print(f"  inlined images/{img}")

    with open(os.path.join(OUT, "leaflet.js"), "w", encoding="utf-8", newline="\n") as fh:
        fh.write(js.decode("utf-8"))
    with open(os.path.join(OUT, "leaflet.css"), "w", encoding="utf-8", newline="\n") as fh:
        fh.write(css)
    print(f"wrote {OUT} (leaflet {VER})")


if __name__ == "__main__":
    main()
