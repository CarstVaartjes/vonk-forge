# Add a Spark

1. Open Vonk Forge in the browser and create a one-use Spark pairing grant.
2. On the Spark, run:

   ```sh
   curl -fsSL https://install.vonkforge.ai/spark | sh
   ```

3. Enter the values requested by the installer. The one-use token authorizes
   that enrollment and is exchanged directly for the Spark identity.

The command finishes only after the direct Rust agent is paired, running, and
verified. Repeat the same command later to upgrade. Routine operation is
outbound mTLS and never uses SSH.

Repeat this flow independently for every Spark; the product has no fixed fleet
size or repository-owned node list.
