# Add a Spark

1. Open Vonk Forge in the browser and create a one-use Spark pairing grant.
2. Copy the generated command from that grant and run it on the Spark. For a
   private NAS it includes the reserved LAN address, for example:

   ```sh
   curl -fsSL https://install.vonkforge.ai/spark | VONK_CONTROLLER_ADDRESS=192.168.1.231 sh -s -- --enroll
   ```

3. Enter the enrollment values plus the Spark management address, this Spark's
   fabric address, and its peer's fabric address. The generated command supplies
   the NAS address; safe service ports and 200 Gbit/s fabric bandwidth have
   defaults. The one-use token authorizes that enrollment and is exchanged
   directly for the Spark identity.

The command finishes only after the direct Rust agent is paired, running, and
verified. It preserves the TLS hostnames and creates the required NAS LAN
hostname mapping itself. Do not install Tailscale or edit DNS or `/etc/hosts`
manually on the Spark. The installer writes and starts the required firewall,
package-helper, and agent configuration itself. For a later package-only upgrade,
rerun the channel command without `--enroll`. Routine operation is outbound mTLS
and never uses SSH.

Repeat this flow independently for every Spark; the product has no fixed fleet
size or repository-owned node list.

## Re-enroll an installed Spark

Use **Re-enroll Spark** in Fleet when the controller database was restored or
reset, when an installed Spark's certificate expired after it could not rotate,
or when a stale staged generation needs replacement. Run the
generated command; it uses the NAS installation's own publication channel and
the same generic enrollment intent used for a new Spark:

```sh
curl -fsSL https://install.vonkforge.ai/dev/spark | VONK_CONTROLLER_ADDRESS=192.168.1.231 sh -s -- --enroll
```

The re-enrollment flow preserves the locally generated node ID, replaces its
controller certificate atomically, retires stale local rotation pointers, clears
systemd's failed-start limit, and verifies sustained readiness. When the node row
still exists, start the flow from that node's details so the grant is bound to
its ID. After a complete controller database reset, use the Fleet-header action;
the grant is then bound to the node ID in the Spark's signed CSR. Do not rename
credential files, edit `setup-state`, or manually restart the service.
If readiness fails after certificate replacement, rerun the command: the setup
marker remains in recovery state and the installer repairs the service without
asking for another pairing token.

Re-enrollment retains previous rotation directories for audit. If a later
Controller-issued generation reuses an old number, the agent verifies the full
stored key, certificate, chain and identity metadata before treating it as a
replay. Different unselected material is preserved under the private
`credentials/retired-generations/` directory before the new generation is staged.
A generation selected by an active or staged pointer is never replaced with
different material. Do not copy archived directories back into the active
credential namespace or manually edit its pointers.

Archived material keeps its existing private ownership and permissions and is
not automatically pruned. Interrupted writes may also leave private temporary
directories; they are not selected as identities or automatically swept. Treat
these files as sensitive retained audit material when applying a separate
operator retention policy.

Incomplete generation directories, unsafe file permissions, symlinks and
non-directory generation paths fail closed rather than being repaired by
rotation. Re-enrollment does not repair damaged retained storage. If such a
condition prevents rotation, preserve the diagnostic state and use a separately
reviewed storage recovery procedure; do not repeatedly re-enroll or edit pointers
to bypass the storage checks.
