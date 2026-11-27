# Lab mode quickstart

Lab mode runs the controller on a LAN without Tailscale. It is an explicit
choice; the installer's default is Secure remote (Tailscale). HTTPS uses a local CA
created by the installer, and all internal passwords and signing keys are
generated locally. LiteLLM's upstream provider key and the Hugging Face token
are optional; without an upstream key, local model routes still work.

## Install

On Intel or ARM macOS or Linux, provide `curl`, `openssl` and installed
`uv`, then run the signed installer. It verifies one portable Python setup
wheel and its payload; `uv` manages the isolated Python 3.15 runtime:

```sh
curl -fsSL https://install.vonkforge.ai/nas | sh
```

Answer `lab` at the install mode prompt (the default, Secure remote, needs
Tailscale), enter the controller host's reserved LAN IPv4 address, and optionally enter a
LiteLLM upstream key and a Hugging Face token for gated models. The project is created
in `vonk-forge/`. The administrator username is `admin`; its generated password
is saved in `vonk-forge/secrets/admin-password`.

If you ran the installer on a workstation rather than on the controller host,
move the complete `vonk-forge/` directory to the controller host before you
continue.

The Spark management network defaults to the controller host's own /24. To narrow it (for
example to the Sparks' exact addresses), edit `VONK_MANAGEMENT_CIDRS` in `.env`. Add LAN DNS records for `vonk-forge.internal`, `enroll.vonk-forge.internal`,
and `agents.vonk-forge.internal`, all pointing to the
controller host address. On a small lab network, equivalent host-file entries can be used.
Earlier installs keep their `vonk-forge.local` names; `VONK_CONTROL_HOSTNAME`
in `.env` holds the name your install uses.

## Start and trust HTTPS

```sh
cd vonk-forge
docker compose pull
docker compose up -d --wait --remove-orphans
```

On Linux, prefix `sudo` when your user is not in the `docker` group or the
project is root-owned.

Import `secrets/step-ca/root-certificate` into the trust store used by your
browser and Spark clients. Then open `https://vonk-forge.internal:8443` and sign
in as `admin` using the password in `secrets/admin-password`.

Port 8443 is published on all NAS interfaces; Docker-published ports bypass
the host's INPUT firewall rules. Keep the NAS on a trusted LAN and do not
forward port 8443 from the internet.

## Add a Spark

Create a one-use enrollment grant with **Enroll Spark** on the Overview page,
or on the command line with `vonkctl fleet enroll NAME --output grant.json`.
On the Spark, run the generated install command; it already carries the
controller host's LAN address. The Spark must resolve the `enroll` and `agents` hostnames above
to that same address. Import the local CA certificate on any client that needs
to connect to the controller or Spark-facing HTTPS endpoints.

The Sparks pin the controller address in `/etc/hosts` and in their Docker
firewall configuration, so give the controller host a reserved address. If it
changes anyway, follow
[Controller address changed](../README.md#controller-address-changed).

## Enable secure remote access later

The installer's default, **Secure remote**, enables the existing
Tailscale gateway and retains its MagicDNS, scoped OAuth, Services, and grants
requirements. For a Lab install, do not turn it on by starting Tailscale
services manually; create a Secure remote project through the installer so its
scoped credentials and environment are configured together.

## Remove it

To remove a lab controller, follow [Remove Vonk Forge](operations/uninstall.md).
