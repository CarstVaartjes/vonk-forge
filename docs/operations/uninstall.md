# Remove Vonk Forge

This is the supported way to remove Vonk Forge. There is no uninstall command:
the installers only create the things listed below, so removal is a short,
explicit list of commands. Every path and name here is taken from what the
installers create (`deploy/compose/`, `nas_setup/`, `packaging/`,
`rust/crates/vonk-spark-setup`). Run the Spark steps first, then the Controller.

Unused data is removed. The Controller's model cache is the authoritative copy
and is kept unless you take the optional step that removes it; a Spark's model
cache is a copy of it and goes with the Spark. The parts are independent: you can remove one Spark and keep the rest.

| Part | Removed by the default steps | Kept by default | Removed by the optional step |
| --- | --- | --- | --- |
| Controller (NAS) | containers, networks, images | named volumes (database, model cache, CA state) and the `vonk-forge/` project directory with `.env` and `secrets/` | volumes, then the directory |
| Spark | package (agent, helper, monitor), units, configuration, identity, workloads, runtime images, swap file, firewall rules | the `vonk-agent` account | the `vonk-agent` account |

## 1. Remove a Spark

Do this for each Spark. Steps marked `sudo` need an administrator account on the
Spark; the owner runs those.

### 1a. Tell the Controller (optional, on your workstation)

```sh
vonkctl fleet remove SPARK_NAME --yes
```

This retires the Spark's identity at the Controller and revokes its
certificates. It changes nothing on the Spark itself. Skip it if you are
removing the whole Controller.

### 1b. Remove the package and its services (`sudo`)

Removal supersedes a Controller package upgrade that is still armed, so nothing needs stopping first.

```sh
sudo dpkg --purge vonk-forge-agent
```

The package's `prerm` is a three-line shim that runs
`vonk-agent-helper --package-hook prerm remove` (package lifecycle logic lives
in `vonk-agent-helper::package_hooks`). On `remove` and on `purge` (which
removes first) the hook stops the upgrade finisher, then `vonk-forge-agent`,
`vonk-forge-monitor`, `vonk-forge-package-helper` (socket and service),
`vonk-forge-docker-firewall` and the rollback watchdog
`vonk-forge-package-rollback`, retires an armed rollback transaction (so the
watchdog cannot reinstall the package at its deadline, nor after a reboot),
disables those units, and disables lingering for the `vonk-agent`
account. Every step is best effort: removal never fails because a unit is
already gone or the helper binary is damaged, but with a damaged or missing
binary nothing is stopped, disabled or retired, so the services keep running
until the next reboot and an armed upgrade can still be rolled back at its
deadline (stop `vonk-forge-package-rollback.service` by hand in that case). The shim does nothing on `upgrade`. Purge then deletes the package files (`/usr/lib/vonk-forge`, the unit
files under `/lib/systemd/system`, `/usr/share/keyrings/vonk-forge-release.pub`,
`/usr/share/doc/vonk-forge-agent`) and the package conffile
`/etc/vonk-forge-agent/containers-storage.conf`. Its `postrm` is shell and only
forgets the units' enablement state on purge (the binary is gone by then), so
everything below is cleaned up by hand.

### 1c. Remove workloads and runtime images (`sudo`)

Workloads run in the host Docker engine. The helper labels every container it
starts `ai.vonkforge.managed=true` and names its images `localhost/vonk/*`:

```sh
sudo docker ps -aq --filter label=ai.vonkforge.managed=true | xargs -r sudo docker rm -f
sudo docker images -q --filter 'reference=localhost/vonk/*' | sort -u | xargs -r sudo docker rmi -f
```

Docker itself is not installed by Vonk Forge and stays.

### 1d. Remove the firewall rules (`sudo`)

The firewall unit adds a `VONK-FORGE` chain (jumped to from `DOCKER-USER`) and a
`VONK-FORGE-HOST` chain (jumped to from `INPUT`). Stopping the unit does not
remove them; a reboot does (the unit is `PartOf` Docker, and a Docker restart re-applies
them). To remove them now:

```sh
while sudo iptables -w -D DOCKER-USER -j VONK-FORGE 2>/dev/null; do :; done
sudo iptables -w -F VONK-FORGE 2>/dev/null; sudo iptables -w -X VONK-FORGE 2>/dev/null
while sudo iptables -w -D INPUT -j VONK-FORGE-HOST 2>/dev/null; do :; done
sudo iptables -w -F VONK-FORGE-HOST 2>/dev/null; sudo iptables -w -X VONK-FORGE-HOST 2>/dev/null
```

### 1e. Release the managed swap file and the GPU clock lock (`sudo`)

The helper can create swap files named `/var/lib/vonk-forge/swap/swap-<id>.img`
and activate them with `swapon`. The OS swap (`/swap.img`, fstab) is never
touched by Vonk Forge. Release the managed files before deleting them:

```sh
swapon --show=NAME | grep '^/var/lib/vonk-forge/swap/' | xargs -r sudo swapoff
```

If `swapoff` fails because its pages do not fit in memory, stop other work and
retry. The helper also applies a GPU clock range with `nvidia-smi -lgc`. It
holds until the next boot; to clear it now run `sudo nvidia-smi -rgc`.

### 1f. Remove configuration, state and the hosts entry (`sudo`)

```sh
sudo rm -rf /etc/vonk-forge-agent /var/lib/vonk-forge
sudo sed -i '/^# BEGIN VONK FORGE MANAGED HOSTS$/,/^# END VONK FORGE MANAGED HOSTS$/d' /etc/hosts
sudo rm -rf /var/lib/vonk-forge-agent
```

- `/etc/vonk-forge-agent` holds `agent.toml`, `controller-ca.pem`,
  `host-helper-authority.pub`, `docker-firewall.conf` and the retained
  `enrollment.toml`, `enrollment-firewall.conf`, `setup-state`.
- `/var/lib/vonk-forge` holds helper state, rollback and upgrade records, the
  swap directory and the runtime-image receipts.
- The `sed` removes the controller hostname block the Spark installer wrote.
- `/var/lib/vonk-forge-agent` is the agent's data directory: the agent
  identity (`credentials/`), its journal (`state.sqlite`), the installations,
  runs, build data, the rootless Podman storage (`containers/`) and the Spark
  model cache (`distribution/`). The authoritative copy of every model is on the
  Controller, so the Spark copy is removed with the rest.

The Spark is now clean except for the account.

### 1g. Optional: remove the account (`sudo`)

```sh
sudo loginctl terminate-user vonk-agent 2>/dev/null
sudo userdel vonk-agent
```

`userdel` also drops the account's subordinate ID ranges from `/etc/subuid` and
`/etc/subgid`; check both files for a leftover `vonk-agent` line. Packages the
agent depends on (podman, crun, and so on) are left installed; remove them with
`apt-get autoremove` only if nothing else uses them.

### 1h. Check and revert the headless boot target

```sh
find /etc/systemd/system /lib/systemd/system -name 'vonk-forge*'
```

should list only the link below. The purge's `postrm` forgets the enablement records, but the enablement link
`/etc/systemd/system/multi-user.target.wants/vonk-forge-package-rollback.service`
can be left dangling. Remove it and the package's enablement records,
then reload:

```sh
sudo find /etc/systemd/system -type l -name 'vonk-forge*' -delete
sudo rm -f /var/lib/systemd/deb-systemd-helper-*/vonk-forge* /var/lib/systemd/deb-systemd-helper-*/*/vonk-forge*
sudo systemctl daemon-reload
```

The helper sets the default boot target to `multi-user.target` and stops the
display manager. On a desktop host, restore the graphical session with:

```sh
sudo systemctl set-default graphical.target
```

### Files an older package's upgrade recovery left behind

Packages before the one-recovery-path release staged a forward recovery
capsule during Controller upgrades, and removal could refuse while one was
armed. The current package stages none of it and removes what an older one
left when it is configured. If a Spark never reached the current package, remove
`/var/lib/vonk-forge/package-upgrade` (step 1f), then re-run step 1b, or remove
the files below by hand. The old preinst recovery wrote
`/var/lib/vonk-forge/package-upgrade/` (`intent`, `lock`, `agent-blocked`,
`recovery-capsule/`) and `/var/lib/vonk-forge/helper-upgrade.pending`, which 1f
removes, and these systemd files, which it does not:

```sh
sudo rm -f /lib/systemd/system/vonk-forge-package-upgrade-recover-capsule.service \
  /lib/systemd/system/multi-user.target.wants/vonk-forge-package-upgrade-recover-capsule.service \
  /lib/systemd/system/vonk-forge-agent.service.d/10-package-upgrade-capsule.conf \
  /lib/systemd/system/vonk-forge-agent.service.d/20-package-upgrade-recovery.conf \
  /lib/systemd/system/vonk-forge-package-upgrade-recover.service.d/10-capsule-owner.conf \
  /lib/systemd/system/vonk-forge-package-helper.socket.d/20-package-upgrade-recovery.conf
sudo systemctl daemon-reload
```

## 2. Remove the Controller

Run these on the host where the `vonk-forge/` project runs, in that directory
(Compose needs `.env` and `secrets/` to resolve the project). Use `sudo` when
the project is root-owned. Remove or re-enroll the Sparks first; Sparks that
lose their Controller keep running workloads but cannot be managed.

### 2a. Stop and remove containers, networks and images

```sh
cd vonk-forge
sudo docker compose ls
sudo docker compose down --remove-orphans --rmi all
```

This keeps every named volume. Compose names the project after the directory
(`vonk-forge`); `docker compose ls` shows the actual name. In a NAS Docker UI,
stop and delete the project there instead; then check the volumes below, because
a UI's delete action does not always remove them.

The volumes are `postgres-data` (database), `control-state`, `agent-artifacts`,
`model-cache`, `caddy-data`, `caddy-config`, `litellm-supervisor-state`,
`prometheus-data`, `route-publications`,
`normalized-private-keys`, `runtime-assets`, `image-janitor-state`, and, for
Secure remote, `tailscale-state` and `tailscale-socket`. List yours:

```sh
sudo docker volume ls --filter label=com.docker.compose.project=vonk-forge
```

Installs from before the standing image registry was removed may keep an orphan
`registry-data` volume. It is unused; remove it with
`sudo docker volume rm vonk-forge_registry-data`.
If a redeploy did not remove the orphan container, check with
`sudo docker ps -a --filter label=com.docker.compose.service=registry` and
remove it with `sudo docker rm -f <container>`.

### 2b. Optional: remove the data

To remove everything including the database, CA state and the Controller model
cache, run `down` with volumes (this cannot be undone):

```sh
sudo docker compose down --remove-orphans --rmi all --volumes
```

To keep the model cache (the large part) and remove the rest, remove the other
volumes one by one instead:

```sh
sudo docker volume ls -q --filter label=com.docker.compose.project=vonk-forge \
  | grep -v '_model-cache$' | xargs -r sudo docker volume rm
```

### 2c. Optional, together with 2b: remove the project directory

The installer created `docker-compose.yaml`, `.env`, `secrets/`, `backups/`
and `backups-offhost/`, plus the hidden `.vonk-verified-secrets/` and
`.vonk-publication/` directories, and `../.vonk-setup.lock` next to the project
directory. `backups/` holds PostgreSQL dumps; copy it elsewhere first if you may
restore later. `secrets/` and `.env` are needed to reuse the volumes you kept
in 2b (the database password and the CA keys live there): if you kept volumes,
keep this directory too and skip this step.

```sh
cd .. && sudo rm -rf vonk-forge && sudo rm -f .vonk-setup.lock
```

If you set `VONK_BACKUP_OFFHOST_PATH` in `.env`, that directory is outside the
project and stays until you delete it.

### 2d. Secure remote only: Tailscale

Removing the project stops the gateway but does not remove it from your
tailnet. In the Tailscale admin console remove the `vonk-forge-gateway` machine
and the `svc:vonk-forge` Service, the grants and auto-approvers added for the
install, and the scoped OAuth client. See the
[Tailscale runbook](../runbooks/tailscale.md).

### 2e. Lab mode only: LAN names and trust

Remove the DNS or hosts entries for `vonk-forge.local`, `enroll.vonk-forge.local`,
`agents.vonk-forge.local` (older installs also `registry.vonk-forge.local`), and delete the
`secrets/step-ca/root-certificate` you imported into browsers and clients.

## 3. Remove `vonkctl`

```sh
uv tool uninstall vonk-cluster-profiles
```

(`uv tool list` shows the exact name.) Delete `~/.cache/vonkctl/` (or
`$XDG_CACHE_HOME/vonkctl/`), the update-notice cache, and the login
(`vonkctl auth logout`, or delete `~/.config/vonkctl/`; also any token file you
created for `VONK_CONTROL_TOKEN_FILE`).
