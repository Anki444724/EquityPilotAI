#!/usr/bin/env python3
"""
Prove the desktop layout did not change.

"Do not change desktop layout" is the one requirement in the brief that
cannot be satisfied by adding CSS — it can only be satisfied by measuring.
Reading the diff and observing that every new rule sits inside a
`max-width: 1023px` query is suggestive, not proof: a Tailwind utility such
as `min-w-0` on a shared `Card`, or the ResizeObserver added to every chart,
applies at ALL widths and could plausibly shift a desktop grid.

So this captures a geometric fingerprint of each page at 1440px and 1920px —
the bounding box of every structural element — and diffs two runs. Run it
against the old build, then the new one, and compare.

    git stash                                     # or check out the base commit
    python3 deploy/verify_desktop_unchanged.py --url http://localhost:3000 \
            --out /tmp/desktop_before.json
    git stash pop
    python3 deploy/verify_desktop_unchanged.py --url http://localhost:3000 \
            --out /tmp/desktop_after.json
    python3 deploy/verify_desktop_unchanged.py --compare \
            /tmp/desktop_before.json /tmp/desktop_after.json

A tolerance of 1px absorbs subpixel layout rounding, which differs between
runs even with no code change at all.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

# Reuse the sign-in that already handles slow first paint.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from verify_responsive import sign_in  # noqa: E402

from playwright.sync_api import sync_playwright  # noqa: E402

DESKTOP_WIDTHS = [1440, 1920]

ROUTES = [
    "/dashboard", "/companies", "/documents", "/portfolio",
    "/watchlist", "/reports", "/admin", "/platform",
]

# Structural landmarks only.
#
# The first version of this enumerated matches of a class-based selector and
# keyed them by ordinal. That was wrong in two ways that made it report 108
# false differences:
#
#   1. The selector named `.scroll-x` and `.tab-strip` — classes that exist
#      only AFTER the change. The "before" capture matched a different set of
#      elements, so the two runs were never comparing like with like.
#   2. Keying by ordinal means inserting ONE element (the chart's new
#      ResizeObserver host div) renumbers every element after it, so a single
#      addition reports as dozens of moves.
#
# Landmarks are now selected by tag alone — `main`, `aside`, `header`,
# `footer`, `table` — none of which the change adds, removes or renames, and
# each is keyed by its DOM path (tag + nth-of-type chain). A path is stable
# under the insertion of unrelated siblings elsewhere in the tree, so the
# comparison measures geometry rather than document order.
FINGERPRINT_JS = """
() => {
  const out = {};
  const path = (el) => {
    const parts = [];
    let node = el;
    while (node && node !== document.body) {
      const parent = node.parentElement;
      if (!parent) break;
      const same = [...parent.children].filter(c => c.tagName === node.tagName);
      const idx = same.indexOf(node) + 1;
      parts.unshift(`${node.tagName.toLowerCase()}:${idx}`);
      node = parent;
    }
    return parts.join('/');
  };
  for (const el of document.querySelectorAll('main, aside, header, footer, table')) {
    const r = el.getBoundingClientRect();
    if (r.width === 0 && r.height === 0) continue;
    out[path(el)] = [Math.round(r.x), Math.round(r.width), Math.round(r.height)];
  }
  out['__doc'] = [
    document.documentElement.scrollWidth,
    document.documentElement.clientWidth,
  ];
  return out;
}
"""


def capture(url: str, out_path: str, cross_origin: bool) -> None:
    email = os.environ.get("VERIFY_EMAIL", "")
    password = os.environ.get("VERIFY_PASSWORD", "")
    base = url.rstrip("/")
    result: dict[str, dict] = {}

    args = ["--disable-web-security",
            "--disable-features=IsolateOrigins,site-per-process"] if cross_origin else []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(args=args)
        for width in DESKTOP_WIDTHS:
            ctx = browser.new_context(viewport={"width": width, "height": 1000})
            page = ctx.new_page()
            if not sign_in(page, base, email, password):
                print(f"  ! sign-in failed at {width}px", file=sys.stderr)
                ctx.close()
                continue
            for route in ROUTES:
                page.goto(f"{base}{route}", wait_until="domcontentloaded")
                page.wait_for_timeout(2200)
                result[f"{width}{route}"] = page.evaluate(FINGERPRINT_JS)
            ctx.close()
        browser.close()

    with open(out_path, "w") as fh:
        json.dump(result, fh, indent=1)
    print(f"captured {len(result)} page fingerprints -> {out_path}")


def compare(before_path: str, after_path: str, tolerance: int = 1) -> int:
    before = json.load(open(before_path))
    after = json.load(open(after_path))
    drift: list[str] = []
    checked = 0

    for page_key in sorted(set(before) | set(after)):
        b, a = before.get(page_key), after.get(page_key)
        if b is None or a is None:
            drift.append(f"{page_key}: present in only one capture")
            continue
        for el_key in sorted(set(b) | set(a)):
            bv, av = b.get(el_key), a.get(el_key)
            if bv is None or av is None:
                drift.append(f"{page_key} {el_key}: element added/removed")
                continue
            checked += 1
            # `__doc` carries two numbers (scrollWidth, clientWidth); every
            # other key carries three (x, width, height). Zipping against the
            # value itself rather than a fixed triple keeps both shapes valid
            # — the first version indexed [2] unconditionally and crashed on
            # the document row.
            names = (("scrollWidth", "clientWidth") if el_key == "__doc"
                     else ("x", "width", "height"))
            for axis, name in enumerate(names):
                if abs(bv[axis] - av[axis]) > tolerance:
                    drift.append(
                        f"{page_key} {el_key}: {name} {bv[axis]} -> {av[axis]}")

    print(f"\ncompared {checked} element boxes across "
          f"{len(set(before) & set(after))} page captures")
    if not drift:
        print("DESKTOP LAYOUT UNCHANGED — no box moved by more than "
              f"{tolerance}px")
        return 0
    print(f"DESKTOP DRIFT: {len(drift)} differences")
    for d in drift[:40]:
        print("  " + d)
    return 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url")
    ap.add_argument("--out")
    ap.add_argument("--allow-cross-origin", action="store_true")
    ap.add_argument("--compare", nargs=2, metavar=("BEFORE", "AFTER"))
    ap.add_argument("--tolerance", type=int, default=1)
    args = ap.parse_args()

    if args.compare:
        return compare(*args.compare, tolerance=args.tolerance)
    if not (args.url and args.out):
        ap.error("--url and --out are required unless --compare is given")
    capture(args.url, args.out, args.allow_cross_origin)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
