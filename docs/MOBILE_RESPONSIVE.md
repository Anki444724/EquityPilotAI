# Mobile Responsiveness

Support for 320px through desktop, verified empirically on both browser
engines rather than asserted from the CSS.

## Summary

| | Chromium (Android Chrome) | WebKit (iOS Safari) |
|---|---|---|
| Checks run | 248 | 252 |
| Failures | **0** | **0** |
| Baseline before this work | 74 failures | not run |

Desktop layout was independently fingerprinted before and after: **88 element
boxes across 16 page captures at 1440px and 1920px, zero drift beyond 1px.**

Widths exercised: 320 (iPhone SE), 360 (the most common Android viewport),
390 (iPhone 14/15), 768 (iPad portrait), 1440 (desktop control).
Routes exercised: all 8 top-level pages plus all 7 company research tabs.

## What was actually broken

The baseline run found real defects, not theoretical ones.

### NAV-001 — navigation was unreachable on mobile (critical)

The sidebar is `hidden lg:flex` and **nothing replaced it below 1024px**.
Enumerating every visible link at 360px returned only company rows:
Dashboard, Documents, Portfolio, Watchlist, Reports, Administration and
Platform Ops had no reachable link on a phone at all. The platform was
effectively a company browser on mobile.

Fixed with a slide-over drawer behind a hamburger in the header. The drawer
renders the *same* `NavList` component as the desktop rail — extracted rather
than duplicated, because a hand-maintained second copy is the one that goes
stale when a module is added.

### OVERFLOW-001 — `overflow-x: auto` alone does not clip

This is the root cause behind most of the measured overflow, and it is subtle.

A flex or grid child defaults to `min-width: auto`, which means it **refuses
to shrink below its content**. A card containing a twelve-column table
therefore reported the table's full width to its grid track, the track
widened, and the document scrolled sideways — *even though the table's
wrapper already had `overflow-x: auto`*. The wrapper scrolled internally and
pushed the page out at the same time.

Measured: the dashboard was **644px wide inside a 320px viewport**.

Worse, this defeated the sticky first column. Sticky positions against the
nearest scroll container; when the *page* is what scrolls, the pinned column
slides away with everything else, which is precisely what it exists to
prevent.

Fixed by adding `min-w-0` to the shared `Card` primitive and a `min-w-0-all`
helper to the ten `grid-cols-[...]` layouts, so the scroll containers can
actually clip.

### TABS-001 — tab strips wrapped or overflowed

The nine-item Financials strip wrapped to multiple rows and pushed the
statement below the fold. The Portfolio strip did not wrap at all — it simply
overflowed the document to **587px**.

Replaced with a single `TabStrip` primitive (`flex-wrap: nowrap` plus
`overflow-x: auto`) applied to all seven in-page strips and the company
research strip.

A scrolling strip introduces a second problem it must also solve: at 320px
only about two and a half of seven tabs are visible, so a user landing on
`/documents` would see the strip scrolled to "Overview" with no indication of
where they are. Each strip scrolls its active tab into view on mount.

### TEXT-001 — unbreakable strings

Registered addresses arrive from filings as single unbroken tokens. A 404px
address string pushed the company Documents page to 432px inside 320px. No
amount of `min-width: 0` helps when content genuinely cannot wrap;
`overflow-wrap: anywhere` was applied on mobile only.

### CHART-001 — charts never resized

Highcharts listens for `window.resize` only. That misses every case that
matters here: a grid collapsing from three columns to one at a breakpoint,
panels mounting as their queries resolve, and the nav drawer changing the
main column's width. All three change the container without changing the
window, leaving the chart at a stale pixel width.

Each chart now observes its own container with a `ResizeObserver`, coalesced
to one reflow per animation frame.

### TAP-001 — controls below the reliable-touch threshold

Pagination, filter chips and export buttons measured 26–30px, several of them
adjacent, so a mis-tap ran the wrong action rather than doing nothing. Raised
to 36px on mobile only, excluding inline links inside table cells and running
text — those need spacing, not height.

## Approach

- **Density reduced, alignment preserved.** Padding and font size drop inside
  `@media (max-width: 1023px)`; numerals stay tabular, because a financial
  table that loses column alignment stops being readable at any size.
- **Everything mobile lives inside a max-width query**, so the desktop layout
  is unchanged by construction, not by inspection. The exceptions —
  `min-w-0` on `Card` and the chart ResizeObserver — apply at all widths and
  are exactly why the desktop fingerprint comparison exists.
- **`overscroll-behavior-x: contain`** on every scroller. Without it a
  sideways swipe reaching the end of a table is handed to the page, which on
  Android Chrome triggers back-navigation — a user scrolling a balance sheet
  would leave the company.
- **Zoom is deliberately not locked.** `maximum-scale` is the usual reflex
  once a layout is responsive and it is an accessibility regression: a user
  who needs to magnify a figure must be allowed to.

## Verification

```bash
export VERIFY_EMAIL='...' VERIFY_PASSWORD='...'

# Mobile — Android Chrome engine
python3 deploy/verify_responsive.py --url <frontend-url> --company <uuid>

# Mobile — iOS Safari engine
python3 deploy/verify_responsive.py --url <frontend-url> --company <uuid> \
        --engine webkit

# Desktop regression guard: capture on each side of the change, then diff
python3 deploy/verify_desktop_unchanged.py --url <url> --out before.json
python3 deploy/verify_desktop_unchanged.py --url <url> --out after.json
python3 deploy/verify_desktop_unchanged.py --compare before.json after.json
```

`--allow-cross-origin` is for local iteration only: it makes the harness
rewrite CORS headers so a `localhost:3000` build can talk to the deployed
API. The backend allowlist is correct and is not widened for testing.

### Sticky column, proven under real scroll

Both engines, TCS financials at 360px, table scrolled to its right extreme:

| Engine | Scroller width | Scrolled | Pinned cell moved | Page scrollLeft |
|---|---|---|---|---|
| Chromium | 696px in 334px | 362px | **0px** | 0 |
| WebKit | 697px in 324px | 373px | **0px** | 0 |

## Harness defects found and corrected

Reported because a wrong harness produces confidently wrong conclusions:

1. **Sign-in raced first paint** — three of five viewports silently stayed on
   the sign-in page and the harness measured *that*, labelling the rows
   "Dashboard". The tell was `tap-target-height` reporting
   'Forgot password?' on `/platform`.
2. **Desktop fingerprint keyed by ordinal** and selected on `.scroll-x` /
   `.tab-strip` — classes that exist only *after* the change. The two runs
   were never comparing like with like, and inserting one element renumbered
   everything after it. Reported 108 false differences; re-keyed by DOM path
   over tag landmarks, it reports zero.
3. **Tap-target check was over-broad** — it flagged ticker links inside table
   cells and "View all" text links as undersized buttons. 60 failures that
   were almost entirely noise.
4. **SVG internals counted as overflow** — a Highcharts `<g>` reports a
   bounding box including the clipped plot area, so it measured wider than
   the viewport while nothing was visible outside it.
5. **`__doc` row crashed the comparison** — two values where every other key
   has three.

Separately, two runs failed against a **stale `next start` process** still
serving a deleted `.next` directory (500s and `text/plain` MIME types). That
was sandbox tooling, not product code, and was confirmed by re-serving and
re-measuring.

## Caveats

- Verification ran against **Playwright's** Chromium and WebKit builds, not
  physical handsets. WebKit is the engine iOS Safari uses, so layout,
  sticky positioning and overscroll behaviour are representative; genuine
  device chrome (dynamic URL bar resize, iOS rubber-banding, real touch
  latency) is not exercised.
- Safe-area insets for notched iPhones are applied but **cannot be verified
  in this environment** — headless WebKit reports zero insets. The rules are
  inert where insets are zero, so there is no risk to non-notched devices,
  but the notch case itself is untested.
- The `/companies/[id]/*` tabs were verified against **TCS only**. Companies
  with markedly different data shapes (a bank's balance sheet, a company with
  no financials) were not individually swept.
- The tap-target threshold used is **32px**, below the WCAG 2.5.5 target of
  44px. This is a deliberate trade against institutional density; the
  controls are now 36px.
