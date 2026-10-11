# Parity report — PASS

Overall compliance **99.96%** (threshold 98%) across 189 scenarios.

| endpoint | compliance | weight |
|---|---|---|
| `GET /api/calculation/custom/increment` | 100.00% | 6.0/6.0 |
| `GET /api/conversion/dayoftheweek/` | 100.00% | 6.0/6.0 |
| `GET /api/conversion/dayoftheweek/{date}` | 100.00% | 120.0/120.0 |
| `GET /api/conversion/dayoftheyear/{date}` | 100.00% | 120.0/120.0 |
| `GET /api/health/check` | 100.00% | 18.0/18.0 |
| `GET /api/time/current/coordinate` | 100.00% | 92.5/92.5 |
| `GET /api/time/current/ip` | 100.00% | 62.0/62.0 |
| `GET /api/time/current/zone` | 100.00% | 169.0/169.0 |
| `GET /api/timezone/availabletimezones` | 100.00% | 210.0/210.0 |
| `GET /api/timezone/coordinate` | 100.00% | 246.0/246.0 |
| `GET /api/timezone/ip` | 100.00% | 360.0/360.0 |
| `GET /api/timezone/zone` | 100.00% | 844.5/844.5 |
| `GET /api/v1/time/current/coordinate` | 100.00% | 372.0/372.0 |
| `GET /api/v1/time/current/ip` | 100.00% | 298.5/298.5 |
| `GET /api/v1/time/current/unix` | 100.00% | 30.0/30.0 |
| `GET /api/v1/time/current/unix_ms` | 100.00% | 10.0/10.0 |
| `GET /api/v1/time/current/unix_ns` | 100.00% | 10.0/10.0 |
| `GET /api/v1/time/current/unix_us` | 100.00% | 10.0/10.0 |
| `GET /api/v1/time/current/utc` | 100.00% | 20.0/20.0 |
| `GET /api/v1/time/current/zone` | 100.00% | 368.0/368.0 |
| `GET /api/v1/time/current/zone/x` | 100.00% | 6.0/6.0 |
| `GET /api/v1/timezone/availabletimezones` | 100.00% | 630.0/630.0 |
| `GET /api/v1/timezone/coordinate` | 100.00% | 351.5/351.5 |
| `GET /api/v1/timezone/zone` | 100.00% | 644.5/644.5 |
| `GET /api/v2/time` | 100.00% | 6.0/6.0 |
| `HEAD /api/v1/time/current/utc` | 100.00% | 6.0/6.0 |
| `POST /api/calculation/current/decrement` | 100.00% | 42.5/42.5 |
| `POST /api/calculation/current/increment` | 100.00% | 38.0/38.0 |
| `POST /api/calculation/custom/decrement` | 100.00% | 46.0/46.0 |
| `POST /api/calculation/custom/increment` | 100.00% | 23.0/23.0 |
| `POST /api/conversion/converttimezone` | 100.00% | 24.0/24.0 |
| `POST /api/conversion/translate` | 95.24% | 40.0/42.0 |

## Failed checks

| scenario | call | check | detail |
|---|---|---|---|
| tests/e2e/test_posts.py::test_translate_languages[2026-05-05 05:05:05-ko] | `POST /api/conversion/translate` | value $.friendlyDateTime | '2026년 5월 5일 화요일 오전 5:05:05' vs '2026년 5월 5일 화요일 AM 5:05:05' |
| tests/e2e/test_posts.py::test_translate_languages[2026-05-05 05:05:05-ko] | `POST /api/conversion/translate` | value $.friendlyTime | '오전 5:05:05' vs 'AM 5:05:05' |
