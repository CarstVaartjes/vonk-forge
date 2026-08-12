# Persistent fabric rendezvous design

Date: 2026-08-12

## Context

The accepted two-node development recipe runs two independent DS4 replicas.
Before serving, rank 1 sends a bounded `vonk-fabric-v1` HELLO to rank 0 over
the declared direct-fabric address and rank 0 returns an acknowledgement. Rank
0 must retain that coordinator while its DS4 process runs so restarting only
rank 1 can repeat the same exchange.

Physical acceptance proved the initial exchange but found that the coordinator
was gone after model warm-up. BusyBox documents `nc -lk -e` as persistent, but
the recipe also supplied `-w 60`; on the accepted BusyBox build that timeout
expires the idle listening process. The timeout was intended to bound a client
that connects without completing a HELLO, not to bound coordinator lifetime.

## Design

Keep the existing protocol, Bash wrapper, BusyBox transport, direct-fabric
publication, and rank lifecycle. Move the timeout to the connection handler:

- the `VONK_FABRIC_SERVE=1` handler uses Bash `read -t` with the validated
  `VONK_FABRIC_RENDEZVOUS_SECONDS` value;
- the persistent `nc -lk -e` coordinator has no listener-wide `-w` option;
- malformed, incomplete, and idle accepted clients still terminate within the
  configured 1–300 second bound;
- listener lifetime remains coupled to rank 0's `model-smoke` wrapper, whose
  existing signal/DS4-exit cleanup terminates and waits for the coordinator;
- a recovered rank 1 reconnects to the same rank-0 container and repeats the
  exact HELLO/ack before starting DS4.

This is preferable to a loop of one-shot listeners, which creates reconnect
gaps and duplicates process supervision, and to restarting rank 0, which would
weaken the rank-only recovery gate.

## Verification

Extend the executable socket-level rendezvous tests to model BusyBox's
listener timeout semantics. A regression test starts a one-second coordinator,
idles beyond one second, then requires a valid rank-1 join and acknowledgement.
It fails while `-w` remains on the listener. A second test leaves an accepted
handler input open and requires the handler to fail within the one-second
bound, proving timeout removal does not permit a stuck connection.

After repository checks and accepted GitHub publication, physical acceptance
must build and distribute the changed source bundle, start both ranks, prove
the address-specific rendezvous and inference, stop only rank 1's managed
container, observe fresh failed-rank state and route withdrawal, start that
same container, observe HELLO/ack and route republication, and recover
inference. Restart-persistence and normal API stop/uninstall remain mandatory.
