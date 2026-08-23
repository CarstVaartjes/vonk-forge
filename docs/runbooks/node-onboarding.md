# Add a Spark

1. Open Vonk Forge in the browser and create a one-use Spark pairing grant.
2. Copy the generated command from that grant and run it on the Spark. For a
   private NAS it includes the reserved LAN address, for example:

   ```sh
   curl -fsSL https://install.vonkforge.ai/spark | VONK_CONTROLLER_ADDRESS=192.168.1.231 sh
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
package-helper, and agent configuration itself. Repeat the generated command
later to upgrade. Routine operation is outbound mTLS and never uses SSH.

Repeat this flow independently for every Spark; the product has no fixed fleet
size or repository-owned node list.
