# Prepare a NAS deployment

Development and production are release channels for the same deployment. They
use the same services, networks, volumes, hostnames, ports, secrets, Step CA,
and startup behavior; only immutable release identities differ.

Prepare a complete upload directory on an Intel or ARM Linux or macOS
workstation with `curl`, `openssl` and installed `uv`. The signed
installer verifies the portable Python setup wheel and payload before `uv`
runs it in an isolated Python 3.14 environment:

Before running the command, complete the
[Tailscale fresh-install preflight](tailscale.md#fresh-install-preflight). The
wizard repeats the checklist before requesting OAuth values. Operator tailnets
use only the canonical unsuffixed Service names.

```sh
curl -fsSL https://install.vonkforge.ai/dev/nas | sh
```

The interactive command creates `vonk-forge/docker-compose.yaml`,
`vonk-forge/.env`, and `vonk-forge/secrets/`. It does not require Docker, Git,
sudo, SSH, a NAS mount, or direct NAS access. Drag the entire directory into the
NAS Docker application and start it as one Compose project.

The written `docker-compose.yaml` holds exactly the selected services (no
`profiles:` key), so a Docker UI or a plain `docker compose up -d` deploys the
same set; unselected services are absent and no unused helper containers are
created.

To update a prepared project, rerun the same command from its parent directory.
The installer preserves the local
configuration, credentials, and PKI while updating the immutable
release-controlled Compose file. Upload the refreshed directory and redeploy
without deleting named volumes.

See [NAS control-plane deployment](../../deploy/compose/README.md) for startup
and verification.
