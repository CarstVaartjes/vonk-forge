# Lab mode quickstart

Lab mode runs the controller on a LAN without Tailscale. It is an explicit
choice; the installer's default is Secure remote (Tailscale). HTTPS uses a local CA
created by the installer, and all internal passwords and signing keys are
generated locally. LiteLLM's upstream provider key and the Hugging Face token
are optional; without an upstream key, local model routes still work.

## Install

On macOS or Linux, run the signed NAS installer:

```sh
curl -fsSL https://install.vonkforge.ai/nas | sh
```

Answer `lab` at the install mode prompt (the default, Secure remote, needs
Tailscale), enter the NAS's reserved LAN IPv4 address, and
optionally enter a Hugging Face token for gated models. The project is created
in `vonk-forge/`. The administrator username is `admin`; its generated password
is saved in `vonk-forge/secrets/admin-password`.

The default Lab mode management network is `192.168.1.0/24`. If the LAN uses a
different subnet, edit `VONK_MANAGEMENT_CIDRS` in `.env` before starting the
project. Add LAN DNS records for `vonk-forge.local`, `enroll.vonk-forge.local`,
`agents.vonk-forge.local`, and `registry.vonk-forge.local`, all pointing to the
NAS address. On a small lab network, equivalent host-file entries can be used.

## Start and trust HTTPS

```sh
cd vonk-forge
docker compose pull
docker compose up -d --wait --remove-orphans
```

Import `secrets/step-ca/root-certificate` into the trust store used by your
browser and Spark clients. Then open `https://vonk-forge.local:8443` and sign
in as `admin` using the password in `secrets/admin-password`.

The LAN listener is bound to the address you entered. Keep the host firewall
limited to your trusted LAN and do not forward port 8443 from the internet.

## Add a Spark

Create a one-use enrollment grant in **Fleet**. On the Spark, run the generated
install command; it already carries the NAS LAN address. The Spark must resolve the three `enroll`, `agents`, and `registry` hostnames above
to that same address. Import the local CA certificate on any client that needs
to connect to the controller or Spark-facing HTTPS endpoints.

## Enable secure remote access later

The installer's default, **Secure remote**, enables the existing
Tailscale gateway and retains its MagicDNS, scoped OAuth, Services, and grants
requirements. For a Lab install, do not turn it on by starting Tailscale
services manually; create a Secure remote project through the installer so its
scoped credentials and environment are configured together.
