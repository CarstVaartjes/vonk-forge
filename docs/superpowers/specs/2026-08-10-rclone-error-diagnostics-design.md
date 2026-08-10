# Redacted Rclone Error Diagnostics Design

## Context

APT publication uses `rclone` to persist immutable state and public objects in
Cloudflare R2. The wrapper currently captures `rclone` stderr but replaces every
failure with `object store operation failed`. This prevents operators from
distinguishing authorization, missing-bucket, and transport failures.

## Design

`RcloneStore` will identify each invocation as a `list`, `read`, or `write`
operation and include that operation and the subprocess exit code in failures.
It will append only a sanitized final non-empty stderr line. Sanitization will
replace every configured R2 credential and endpoint value, URL user information,
and URL query strings before the message can reach GitHub Actions logs. Empty
stderr will retain a useful operation-and-exit-code message.

The wrapper will not log command arguments, object payloads, environment
contents, or unredacted subprocess output.

## Testing

Unit tests will mock failed `rclone` processes and verify that:

- operation and exit status are reported;
- access keys, secret keys, endpoints, URL credentials, and query strings are
  absent from the exception;
- a recognizable non-secret provider error remains visible; and
- failures without stderr remain actionable.

After local verification, the change will be published through a pull request.
The failed development APT publication job will then be rerun to reveal the
safe provider-side cause.
