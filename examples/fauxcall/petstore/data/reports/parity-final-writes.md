# Parity report — PASS

Overall compliance **98.48%** (threshold 98%) across 80 scenarios.

| endpoint | compliance | weight |
|---|---|---|
| `DELETE /api/v3/pet/{petId}` | 100.00% | 46.0/46.0 |
| `DELETE /api/v3/store/order/{orderId}` | 100.00% | 41.0/41.0 |
| `DELETE /api/v3/user/{username}` | 100.00% | 10.5/10.5 |
| `GET /api/v3/Pet/1` | 100.00% | 11.0/11.0 |
| `GET /api/v3/Store/inventory` | 100.00% | 11.0/11.0 |
| `GET /api/v3/does-not-exist` | 100.00% | 11.0/11.0 |
| `GET /api/v3/pet` | 100.00% | 11.0/11.0 |
| `GET /api/v3/pet/findByStatus` | 99.14% | 115.0/116.0 |
| `GET /api/v3/pet/findByTags` | 100.00% | 67.0/67.0 |
| `GET /api/v3/pet/{petId}` | 95.44% | 356.0/373.0 |
| `GET /api/v3/store/inventory` | 100.00% | 62.5/62.5 |
| `GET /api/v3/store/order` | 100.00% | 11.0/11.0 |
| `GET /api/v3/store/order/{orderId}` | 100.00% | 130.0/130.0 |
| `GET /api/v3/user` | 100.00% | 11.0/11.0 |
| `GET /api/v3/user/login` | 85.71% | 6.0/7.0 |
| `GET /api/v3/user/logout` | 100.00% | 7.0/7.0 |
| `GET /api/v3/user/{username}` | 99.19% | 122.5/123.5 |
| `GET /api/v4/pet/1` | 100.00% | 11.0/11.0 |
| `POST /api/v3/pet` | 100.00% | 64.5/64.5 |
| `POST /api/v3/pet/{petId}` | 100.00% | 42.0/42.0 |
| `POST /api/v3/pet/{petId}/uploadImage` | 100.00% | 28.5/28.5 |
| `POST /api/v3/store/order` | 100.00% | 48.0/48.0 |
| `POST /api/v3/user` | 100.00% | 21.5/21.5 |
| `POST /api/v3/user/createWithList` | 100.00% | 17.5/17.5 |
| `PUT /api/v3/pet` | 100.00% | 21.5/21.5 |
| `PUT /api/v3/user/{username}` | 100.00% | 10.5/10.5 |

## Failed checks

| scenario | call | check | detail |
|---|---|---|---|
| tests/e2e/test_scenarios.py::test_find_by_status_as_xml | `GET /api/v3/pet/findByStatus` | text body | text differs |
| tests/e2e/test_scenarios.py::test_get_pet[1] | `GET /api/v3/pet/1` | shape | $.category: missing in fake |
| tests/e2e/test_scenarios.py::test_get_pet[1] | `GET /api/v3/pet/1` | length $.photoUrls | 1 vs 2 |
| tests/e2e/test_scenarios.py::test_get_pet[1] | `GET /api/v3/pet/1` | length $.tags | 1 vs 0 |
| tests/e2e/test_scenarios.py::test_get_pet[1] | `GET /api/v3/pet/1` | value $.name | 'doggie' vs 'Pet1' |
| tests/e2e/test_scenarios.py::test_get_pet[1] | `GET /api/v3/pet/1` | value $.photoUrls[0] | 'fill some value' vs 'test1' |
| tests/e2e/test_scenarios.py::test_get_pet[1] | `GET /api/v3/pet/1` | value $.status | 'pending' vs 'available' |
| tests/e2e/test_scenarios.py::test_get_pet[10] | `GET /api/v3/pet/10` | status | 404 vs 200 |
| tests/e2e/test_scenarios.py::test_get_pet[10] | `GET /api/v3/pet/10` | content-type | 'application/xml' vs 'application/json' |
| tests/e2e/test_scenarios.py::test_get_pet[10] | `GET /api/v3/pet/10` | shape | $: null vs object |
| tests/e2e/test_scenarios.py::test_seeded_user_as_xml | `GET /api/v3/user/user2` | text body | text differs |
| tests/e2e/test_scenarios.py::test_login_always_succeeds | `GET /api/v3/user/login` | text body | text differs |
