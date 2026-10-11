# Time: making a fake feel live

Real APIs return times that move. A fake that returns the recording's timestamps is instantly wrong
(`GET /current_time` must return the actual time). FauxCall's fake runs on a **clock** (real time by
default; frozen/offset/accelerated per evaluation through `/__fauxcall/clock`) and every timestamp
field gets a **policy** saying how it follows that clock.

## Contents

- [Find out how each field behaves](#find-out-how-each-field-behaves)
- [Policies](#policies)
- [Formats](#formats)
- [Calendars: market hours and publications](#calendars-market-hours-and-publications)
- [Worked examples](#worked-examples)
- [Values that move: prices](#values-that-move-prices)

## Find out how each field behaves

1. Record a `time_profile` (the same request 4–6 times, 10–30 s apart — see [recording](recording.md)).
2. `fauxcall analyze endpoint <name> "<E>"` prints a **Timestamp behavior** table:
   `static` (didn't move), `now` (tracks the clock; shows the lag), `relative` (keeps a constant
   distance from now), `periodic` (snaps to a period), `calendar?` (a date that equals the last
   business/publication day), `changing` (moves, but not with the clock).
3. Combine it with the docs and common sense: a `static`-looking `as_of` recorded on a Saturday is a
   **market** timestamp frozen at Friday's close, not a static field.

## Policies

Add to the endpoint's `fields` (path relative to each record, or `"scope": "body"` for the whole body):

| kind | value | e.g. |
|---|---|---|
| (none) / `static` | as recorded | `created_at`, `birthdate`, historical data |
| `now` | the clock (minus `lag_ms`) | server time, `as_of` of live data, `Date`-like fields |
| `relative` | keeps its recorded distance from now | `dueDate` = now + 1 h; `last_login` = 2 days ago; token `expires_at` |
| `calendar` | a calendar event: `last_tick` (now − lag while open, else last close), `last_close`, `last_open`, `last_publication`, `session_date` | quote times, FX fixing dates |
| `periodic` | now snapped down to `period_seconds` (+ `phase_seconds`) | candles, "updated every 5 min" |
| `random` | fresh each response (`uuid4`, `uuid7`, `hex`, `traceparent`, `digits`, `alnum`, `base64url`, `choice`) | request ids, trace ids, nonces |
| `counter` | per-session sequence | trade ids, sequence numbers |
| `echo` | copied from the request (`query.x`, `path.x`, `header.x`, `body.path`, `param.x`) with `transform` | `base`, `amount`, `symbol` |
| `series` | replays a recorded series (opt-in) | see [prices](#values-that-move-prices) |
| `volatile` | frozen at the last recording; parity compares type only | prices (default) |
| `pseudonym` | personal data (set automatically) | emails, names |
| `computed` | set by your handler | derived values |
| `ignore` | as recorded; never compared | noise |

Zones (`zone`): an IANA name, `param:<name>` (from the request, e.g. `param:timeZone`), `field:<path>`
(from the record), or `calendar` (calendar policies).

## Formats

Every time policy needs the **exact** recorded spelling. `analyze endpoint` shows the inferred format;
the model stores it as e.g.:

```json
{"kind": "iso", "fraction_digits": 7, "fraction_mode": "fixed", "tz_style": "Z"}
```

`kind`: `iso`, `date`, `time`, `epoch` (`epoch_unit` s/ms/us/ns, `epoch_decimals`, `as_string`),
`http_date`, `strftime` (`pattern`). `fraction_mode`: `fixed`, `trim` (JS-style), `omit_if_zero`
(Python `isoformat`). `tz_style`: `Z` (Z for UTC, ±HH:MM otherwise — .NET), `+HH:MM`, `+HHMM`, `+HH`,
`none`. 7- and 9-digit fractions are preserved.

## Calendars: market hours and publications

```json
"calendars": [{
  "name": "nyse", "zone": "America/New_York", "weekdays": [0, 1, 2, 3, 4],
  "sessions": [{"open": "09:30", "close": "16:00"}],
  "holidays": ["2026-11-26", "2026-12-25"], "early_closes": {"2026-11-27": "13:00"}
}, {
  "name": "ecb", "zone": "Europe/Berlin", "publications": ["16:00"], "holidays": ["2026-12-25", "2026-12-26"]
}]
```

Several `sessions` model lunch breaks (Tokyo 09:00–11:30, 12:30–15:30). Take holidays from an
authoritative source for the years the fake will run in, and say where they came from in `note`.

## Worked examples

**Current time** (`GET /time/current/zone?timeZone=Asia/Tokyo` →
`{"date_time": "2026-10-11T10:17:26.379953+09:00", "timezone": "Asia/Tokyo", …}`):

```json
"fields": [
  {"kind": "now", "scope": "body", "path": "date_time", "zone": "param:timeZone",
   "format": {"kind": "iso", "fraction_digits": 6, "tz_style": "+HH:MM"}},
  {"kind": "now", "scope": "body", "path": "date", "zone": "param:timeZone", "format": {"kind": "date"}}
]
```

Fields that depend on the zone in non-trivial ways (`dst_active`, `day_of_week`, offsets) need a
`computed` handler using `zoneinfo` — never replay them.

**Ticker quote** — the price stays as recorded; the time tracks the market:

```json
{"kind": "calendar", "path": "as_of", "calendar": "nyse", "event": "last_tick", "lag_ms": 1500,
 "format": {"kind": "iso", "fraction_digits": 6}},
{"kind": "volatile", "path": "price"}
```

During the session `as_of` is now − 1.5 s; overnight, weekends and holidays it stays at the last
close — exactly what the real feed does.

**Reference-rate date** (`{"date": "2026-10-09", …}` published 16:00 CET on business days):
`{"kind": "calendar", "path": "date", "calendar": "ecb", "event": "last_publication", "zone": "calendar", "format": {"kind": "date"}}`.

**Relative** (`dueDate` = now + id hours): `{"kind": "relative", "path": "dueDate", "format": …}` —
each record keeps its recorded offset from the moment it was recorded.

**Async jobs** — state and lifecycle timestamps come from the JobModel (measured durations), not
from policies; see [consistency](consistency.md#async-jobs).

## Values that move: prices

Default: prices are **frozen** at their last recording (`volatile`), while their timestamps move.
Optional, when the user wants movement: record the endpoint repeatedly (purpose `series`) and write
`data/series/<name>.jsonl` lines `{"t_ns": <recorded ns>, "value": <price>}` (use `fauxcall sql` to
extract them), then `{"kind": "series", "path": "price", "series": "<name>", "mode": "loop"}`. The fake
replays the observed sequence aligned to its clock — realistic, yet repeatable.
