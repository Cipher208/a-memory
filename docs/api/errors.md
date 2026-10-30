# API error statuses

The HTTP API maps a small set of failures to explicit statuses. Anything not
listed here is an unhandled exception and still falls through to the
framework's default `500`.

| Status | Meaning | Where |
|---|---|---|
| 401 | unauthorized | every authenticated route (see the exemptions below) |
| 429 | rate limit exceeded | every authenticated route |
| 400 | malformed input | `hooks.py` (unknown event) |
| 503 | a dependency is unavailable; the request was well-formed and may be retried after operator action | `endpoints/auth.py` (unreadable key store) |

## Unauthenticated routes

`/health`, `/ready` and `/alive` are liveness probes: `AuthMiddleware` exempts
them and their handlers never call `check_auth`, so they answer `200` without
credentials — that is the point, since a probe that needed a key could not tell
a healthy process from a locked one. Every other route, `/metrics` included,
returns `401` without a valid credential.

## 503 — dependency unavailable

`503` is the API's first mapped `5xx`. It is raised by `endpoints/auth.py` when
the key store exists but cannot be read or decoded:
`features/auth/store.py::StoreUnreadable` is caught there and answered with
`503` and the `ERROR_KEY_STORE_UNREADABLE` body.

It is deliberately not `500`. A `500` says the server failed on a request it
should have handled; here the request was well-formed and the caller did
nothing wrong — an operator-side dependency (the key store file) is broken.
The caller may retry once the operator has repaired it, and the status says so.

Every other unhandled exception has no mapping yet and still surfaces as the
framework's `500`.
