# Bounded recipe-image upload timeout design

## Outcome

Large recipe images can be uploaded from a Spark builder to the controller
without inheriting the 75-second timeout used for ordinary agent requests.
The upload remains bounded and all other controller requests retain their
existing timeout behavior.

## Observed failure

Physical DS4 acceptance produced a 2,592,110,592-byte Docker archive. The
Spark builder completed successfully, but the Rust agent disconnected after
75 seconds while the controller had received approximately 2.0 GB. The
controller recorded `starlette.requests.ClientDisconnect`. The agent client
sets a 75-second total timeout globally and the image upload did not override
it. The smaller 154,803,712-byte synthetic archive completed inside that
limit, which is why CI and synthetic acceptance did not expose the defect.

## Design

Keep the existing 75-second default for claims, results, inventory, metadata,
and bounded artifact ranges. Set a one-hour total timeout only on the
`PUT /agent/v1/recipe-builds/{build_id}/image` request. A transport failure
still terminates promptly when the connection closes; the one-hour value is a
hard upper bound for a connected but non-completing upload. The existing
heartbeat task continues renewing the operation lease independently while the
upload is active.

Do not remove the shared timeout, change the controller size/digest checks, or
introduce chunked/resumable transport in this repair. Resumable image upload is
a separate protocol feature and is not needed for the accepted development
runtime size or current network.

## Verification

A Rust client test uses a deliberately shorter shared client timeout and a
real local HTTP server that delays its upload response past that timeout. It
must fail before the production change and pass only when the upload applies
its request-specific bound. Existing protocol, executor, and workspace tests
must remain green. Physical verification then uploads the exact 2.59 GB DS4
archive, activates the resulting accepted agent package canary-first, and
resumes the durable single-node acceptance slice.
