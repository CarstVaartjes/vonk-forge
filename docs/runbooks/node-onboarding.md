# Add a Vonk Forge GPU node

This runbook adds exactly one Vonk Forge GPU node to a fleet. It has no assumptions about
the fleet size, hostnames, or IP addresses. Repeat it independently for every
new node.

## Safety boundary

Trusted first contact requires comparing the serial digest and SSH host-key
fingerprints shown at the physical console. Do not accept values learned only
over the network. The installer pauses for this assertion and quarantines a
mismatch. Confirm working console or out-of-band recovery before enabling SSH
hardening.

The commands are dry runs unless `--apply` is present. Journals and sanitized,
content-addressed evidence live under `.state/node-install`; credentials remain
behind `secret://` references.

## Clean-machine prerequisites

Before first network trust, prepare one clean operator workstation and one
clean GPU node install state:

- Export only the public half of the administrator SSH key from 1Password or
  the equivalent approved secret store; do not reveal or copy the private key
  into shell output.
- Add the management-LAN names to `/etc/hosts` on the NAS and on the GPU node:

  ```text
  <NAS_MANAGEMENT_IP> <ENROLLMENT_HOSTNAME> <CONTROLLER_HOSTNAME> <REGISTRY_HOSTNAME>
  ```

- Confirm the NAS firewall allows only the GPU node management network to
  reach `<NAS_MANAGEMENT_IP>:8443`.

These placeholders are per-site inputs. Local DNS is optional; the runbooks in
this repository assume `/etc/hosts` works even when no LAN DNS exists.

## Start and inspect

```bash
bin/node-install node start \
  --host NEW_NODE_ADDRESS --user ADMIN_USER \
  --credential-ref secret://ssh/admin \
  --display-name DISPLAY_NAME --label purpose=inference --json
```

Review the plan, then repeat it with `--apply` plus the console assertion,
administrator public-key path/fingerprint, and `--recovery-verified`. A missing
operator assertion produces a waiting journal rather than a partial acceptance.

```bash
bin/node-install node status NODE_ID --json
bin/node-install node resume NODE_ID --apply \
  --trusted-serial-sha256 SERIAL_DIGEST \
  --trusted-host-key-fingerprint HOST_KEY_FINGERPRINT \
  --admin-public-key /safe/path/admin.pub \
  --admin-key-fingerprint ADMIN_KEY_FINGERPRINT \
  --recovery-verified --json
```

Use `retry NODE_ID --apply` only after correcting a recorded failure. Use
`verify NODE_ID` to require the accepted terminal state.

## Record the enrollment metadata

```bash
bin/node-install node emit-record NODE_ID >node-record.toml
```

This command does not modify Git. PostgreSQL enrollment is authoritative for
Fleet membership and display metadata. Review the sanitized record, then let
the enrollment workflow persist the node in PostgreSQL. Keep physical links
and fabric relationships in the separate topology document; adding a node must
not invent topology.

If verification or recovery access fails, stop. Restore access through the
physical console, inspect the journal, and resume only after the trusted facts
match again.

## Install and pair the agent

After the host record is accepted, install the package and pair the agent by
following [Install the Vonk Forge agent](../operations/install-vonk-agent.md).

1. Registration is the authority: manual `agent.toml` editing is unsupported,
   and Fleet **Add Spark** is the next implementation step. It records the
   node-bound bootstrap grant and runtime inputs, but the bootstrap action is
   not an operator command currently available, so the supported runbook stops
   at that reviewed registration boundary until the emitter lands.

## How an accepted GPU node appears online

Bootstrap and hardening remain a manual, one-node-at-a-time operation using the
repository installer above. Acceptance binds the immutable `spk_` node ID to
its client certificate; it does not trust a hostname or IP address.

After the agent is installed, its normal outbound mTLS claim/long-poll requests
announce presence to the control plane. Caddy records the direct LAN peer
address on that authenticated channel, and the control plane accepts it only
inside `VONK_MANAGEMENT_CIDRS` and outside `VONK_DIRECT_FABRIC_CIDRS`. The address
is a short-lived observation used for routing, never node identity and never a
fleet-membership decision.

DHCP reservations are recommended for the NAS and GPU nodes because they make
operations easier, but route publication does not depend on hard-coded per-node
addresses. There is no subnet scan, mDNS trust, SSH discovery, or automatic
acceptance. An unaccepted machine cannot join merely by appearing on the LAN.

After all nodes are paired and inventory is fresh, continue with the complete
[development agent workload acceptance](development-agent-workloads.md). It
uses the public control and agent APIs; routine workload operation does not use
the bootstrap SSH path.
