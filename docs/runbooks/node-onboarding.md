# Add a Spark

1. Open Vonk Forge in the browser and create a one-use Spark pairing grant.
2. On the Spark, run:

   ```sh
   curl -fsSL https://install.vonkforge.ai/spark | sh
   ```

3. Enter the values requested by the installer. Review the pending Spark's
   hardware, host-key, agent, and CSR evidence in the browser and approve it.

The command remains active while approval is pending. It finishes only after
the direct Rust agent is paired, running, and verified. Repeat the same command
later to upgrade. Routine operation is outbound mTLS and never uses SSH.

Repeat this flow independently for every Spark; the product has no fixed fleet
size or repository-owned node list.
