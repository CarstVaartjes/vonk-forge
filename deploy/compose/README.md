# NAS control-plane deployment

The supported NAS installation starts on an ordinary Linux or macOS
workstation. Run one command as your normal user:

```sh
curl -fsSL https://install.vonkforge.ai/nas | sh
```

The command downloads a verified native setup program, asks for the site
values and imported credentials through the terminal, generates everything
else locally, and creates exactly:

```text
vonk-forge/
├── docker-compose.yaml
├── .env
└── secrets/
```

No Docker daemon, Git checkout, root access, SSH connection, mounted NAS share,
or repository file is needed on the workstation. Copy or drag the whole
`vonk-forge/` directory to the NAS without changing its internal layout, then
import `docker-compose.yaml` in the NAS Docker/Compose application and start the
project. The Compose file uses relative paths, so the three entries must remain
together.

The installer prompts for:

- the NAS LAN address and Spark management/fabric CIDRs;
- the control, enrollment, agent, and registry hostnames;
- Tailscale OAuth credentials;
- the LiteLLM upstream provider key;
- whether to enable optional Hermes, plus its values when selected.

Passwords, service tokens, signing keys, database URLs, and a coherent Step CA
PKI are generated locally unless the prompt explicitly offers an import. Secret
values are written only under `secrets/`; `.env` contains non-secret site
configuration and relative secret paths.

## Start and verify

In a NAS UI, select the uploaded directory as one Compose project, pull the
referenced images, and start it. On a NAS with a shell, the equivalent is:

```sh
cd vonk-forge
docker compose pull
docker compose up -d --wait --remove-orphans
docker compose ps
```

Do not add host-path overrides. Persistent data belongs to the named Docker
volumes declared by the generated project. Only Caddy publishes a host port;
all browser-facing services remain private behind the generated Tailscale
gateway. PostgreSQL is the control authority and Step CA is the agent identity
authority. There is no runtime Git checkout, host updater, migration helper,
one-shot initializer, or A/B agent supervisor.

## Upgrade

Run the same workstation command from the directory that already contains
`vonk-forge/`:

```sh
curl -fsSL https://install.vonkforge.ai/nas | sh
```

Upgrade mode preserves `.env`, `secrets/`, and site identity while atomically
replacing the release-controlled Compose file and adding any newly required
inputs. Upload the resulting directory over the NAS project, pull, and
redeploy. Keep named volumes during normal upgrades.

Development and production use this exact topology and configuration contract.
They differ only in the immutable application image and Spark package versions
selected by the installer publication channel. Hermes is the sole optional
service.
