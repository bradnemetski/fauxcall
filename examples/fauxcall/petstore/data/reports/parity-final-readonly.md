# Parity report — PASS

Overall compliance **99.71%** (threshold 98%) across 96 scenarios.

| endpoint | compliance | weight |
|---|---|---|
| `GET /api/v3/Pet/1` | 100.00% | 11.0/11.0 |
| `GET /api/v3/Store/inventory` | 100.00% | 11.0/11.0 |
| `GET /api/v3/does-not-exist` | 100.00% | 18.0/18.0 |
| `GET /api/v3/openapi.json` | 100.00% | 9.0/9.0 |
| `GET /api/v3/openapi.yaml` | 100.00% | 7.0/7.0 |
| `GET /api/v3/pet` | 100.00% | 11.0/11.0 |
| `GET /api/v3/pet/findByStatus` | 99.07% | 106.0/107.0 |
| `GET /api/v3/pet/findByTags` | 100.00% | 74.0/74.0 |
| `GET /api/v3/pet/{petId}` | 100.00% | 340.0/340.0 |
| `GET /api/v3/store/inventory` | 100.00% | 34.0/34.0 |
| `GET /api/v3/store/order` | 100.00% | 18.0/18.0 |
| `GET /api/v3/store/order/{orderId}` | 100.00% | 144.0/144.0 |
| `GET /api/v3/user` | 100.00% | 11.0/11.0 |
| `GET /api/v3/user/login` | 85.71% | 6.0/7.0 |
| `GET /api/v3/user/logout` | 100.00% | 7.0/7.0 |
| `GET /api/v3/user/{username}` | 100.00% | 208.5/208.5 |
| `GET /api/v4/pet/1` | 100.00% | 11.0/11.0 |
| `HEAD /api/v3/pet/9` | 100.00% | 6.0/6.0 |
| `OPTIONS /api/v3/pet/9` | 85.71% | 6.0/7.0 |

## Failed checks

| scenario | call | check | detail |
|---|---|---|---|
| tests/e2e/test_scenarios.py::test_find_by_status_as_xml | `GET /api/v3/pet/findByStatus` | text body | text differs |
| tests/e2e/test_scenarios.py::test_login_always_succeeds | `GET /api/v3/user/login` | text body | text differs |
| tests/e2e/test_scenarios.py::test_options_describes_the_resource | `OPTIONS /api/v3/pet/9` | text body | text differs |
