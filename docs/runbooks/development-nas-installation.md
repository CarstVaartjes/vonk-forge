# Prepare a NAS deployment

Development and production are release channels for the same deployment. They
use the same services, networks, volumes, hostnames, ports, secrets, Step CA,
and startup behavior; only immutable release identities differ.

Prepare a complete upload directory on a Linux or macOS workstation:

```sh
curl -fsSL https://install.vonkforge.ai/dev/nas | sh
```

The interactive command creates `vonk-forge/docker-compose.yaml`,
`vonk-forge/.env`, and `vonk-forge/secrets/`. It does not require Docker, Git,
sudo, SSH, a NAS mount, or direct NAS access. Drag the entire directory into the
NAS Docker application and start it as one Compose project.

The wizard asks whether to enable Hermes. If it is not selected, Hermes remains
the only disabled optional profile; no unused helper containers are created.

To update a prepared project, rerun the same command from its parent directory.
Normal upgrades preserve the existing Hermes selection without prompting. To
change it explicitly, append `--enable-hermes` or `--disable-hermes` after
`sh -s --`; enabling Hermes for the first time prompts only for any missing
Hermes values or secrets. The dedicated LiteLLM client key is generated only
when Hermes is enabled, then preserved across normal upgrades and
disable/re-enable cycles. The installer preserves the other local
configuration, credentials, and PKI while updating the immutable
release-controlled Compose file. Upload the refreshed directory and redeploy
without deleting named volumes.

See [NAS control-plane deployment](../../deploy/compose/README.md) for startup
and verification.
