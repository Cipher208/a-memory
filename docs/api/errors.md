# API error statuses

The HTTP API maps a small set of failures to explicit statuses. Anything not
listed here is an unhandled exception and still falls through to the
framework's default `500`.

| Status | Meaning | Where |
|---|---|---|
| 401 | unauthorized | every endpoint |
| 429 | rate limit exceeded | every endpoint |
| 400 | malformed input | `hooks.py` (unknown event) |
| 503 | a dependency is unavailable; the request was well-formed and may be retried after operator action | `endpoints/auth.py` (unreadable key store) |

## 503 — dependency unavailable

`503` is the API's first mapped `5xx`. It is raised by `endpoints/auth.py` when
the key store exists but cannot be read or decoded: `features/auth/store.py::StoreUnreadable`
is caught there and answered with `503` and the `ERROR_KEY_STORE_UNREADABLE`
body.

It is deliberately not `500`. A `500` says the server failed on a request it
should have handled; here the request was well-formed and the caller did
nothing wrong — an operator-side dependency (the key store file) is broken.
The caller may retry once the operator has repaired it, and the status says so.

Every other unhandled exception has no mapping yet and still surfaces as the
framework's `500`.
