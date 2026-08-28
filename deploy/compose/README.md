# Controller-host deployment

The supported controller installation starts on an ordinary Linux or macOS
workstation. The eventual controller host can be this same laptop or any local
NAS or server that runs Docker Compose. Run one command as your normal user:

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

No Docker daemon, Git checkout, root access, SSH connection, mounted share, or
repository file is needed on the preparation workstation. Keep the whole
`vonk-forge/` directory on this computer or move it to another local controller
host without changing its internal layout. Start `docker-compose.yaml` with the
host's shell or Docker/Compose application. The Compose file uses relative paths,
so the three entries must remain together.

The `/nas` segment in the public installer URL is historical; it is not a
hardware restriction.

The installer prompts for:

- the controller host's LAN address and Spark management/fabric CIDRs;
- the control, enrollment, agent, and registry hostnames;
- Tailscale OAuth credentials;
- the LiteLLM upstream provider key;
- whether to enable optional Hermes, plus its values when selected.

Passwords, service tokens, signing keys, database URLs, and a coherent Step CA
PKI are generated locally unless the prompt explicitly offers an import. Secret
values are written only under `secrets/`; `.env` contains non-secret site
configuration and relative secret paths.

## Start and verify

In a Docker UI, select the complete directory as one Compose project, pull the
referenced images, and start it. On any controller host with a shell, the
equivalent is:

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
inputs. Place the resulting directory over the controller project, pull, and
redeploy. Keep named volumes during normal upgrades.

Development and production use this exact topology and configuration contract.
They differ only in the immutable application image and Spark package versions
selected by the installer publication channel. Hermes is the sole optional
service.
