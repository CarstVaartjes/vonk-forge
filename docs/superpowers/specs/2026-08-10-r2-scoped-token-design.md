# R2 Scoped Token Compatibility Design

## Context

Development APT publication uses a bucket-scoped Cloudflare R2 Object Read &
Write token. Object listing succeeds, but `rclone copyto` fails with HTTP 403.
Cloudflare's official rclone guidance requires `no_check_bucket = true` for
object-level tokens because those tokens cannot perform bucket-level checks.

## Design

Every R2 environment used by the APT state helper will set
`RCLONE_CONFIG_R2_NO_CHECK_BUCKET` to the string `true`. The access key remains
bucket-scoped; no permission is broadened. A workflow contract test will require
the setting alongside every R2 remote configuration so prepare, private-state
commit, and public publication cannot drift apart.

## Verification

The workflow contract suite must pass locally. After merge, the development APT
workflow must write immutable state, publish the exact public tree, and advance
its latest pointer using the existing scoped token.
