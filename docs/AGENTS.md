# Documentation guide

Error handling and diagnostics must follow [error-reporting.md](error-reporting.md).
Document operation, endpoint path, received status, safe canonical code,
request ID, source classification, and retry/defer/exit decision when those
values are available. Never document credentials, private keys, cookies,
authorization headers, token-bearing URLs, or raw request and response bodies.
Keep repository, Controller, and physical Spark evidence boundaries explicit;
do not claim route-wide or Rust coverage until the owning producer and
consumer tests have passed.
