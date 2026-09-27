# CraneFleet

Site operations coordination backend for the crane fleet: anti-collision
zones, wind interlocks, travel limits and the operator console.

## Run

    python -m cranefleet --host 0.0.0.0 --port 8080 --data-dir /app/data --web-root web

## Boot preflight

The service powers on together with the machine, so every boot runs a
preflight cross-check before the HTTP server starts:

- `data-dir` exists and is writable; a stored `snapshot.json` must decode
  and match the current snapshot version.
- `web-root` holds the console pages.
- The listen host/port are usable.
- The stored `site_name` is non-empty and matches `--expect-site` when that
  argument is given — a mismatch means the data directory belongs to
  another site.
- `safe_separation` is positive, fits inside the zone boundary and does not
  exceed the closest baseline crane pair.
- The wind alarm threshold sits inside its sane range.

Every item is logged as `[OK]` or `[FAIL]`. If any item fails, the service
logs the failing items, exits with a non-zero status and leaves the data
directory untouched — it never serves with a known-bad setup.

When the site is legitimately renamed through `POST /api/config`, update
the `--expect-site` boot argument to match, or the next boot will refuse
to start.

## Tests

    python -m unittest discover -s tests
