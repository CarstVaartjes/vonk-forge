# Stable release and channel switch

How a stable release is cut, what becomes public when, and how an operator
moves a deployment between the `dev` and `stable` channels. The publication
machinery is described in [Platform release publication](platform-release-publication.md)
and [Agent package release](../operations/agent-package-release.md); this page is
the order of work.

Prerequisites in code: the cross-channel agent package lookup (#2898), the
undeclared-table retention (#2899), the vonkctl open-word fix (#2896), the
stable compatibility gate (#2904) and the preflight (#2902) are merged and in
the tagged commit.

## Version policy

- The workspace version is the next release. The tag equals it (`v1.0.0` for
  version `1.0.0`).
- Right after a tag, bump the patch at once, so a fix-forward `vX.Y.(Z+1)` stays
  possible.
- A breaking response shape or a deprecation bumps the minor in the same PR.
- Development builds are `X.Y.Z~dev.<seconds>+g<sha>` and sort below `X.Y.Z`:
  `0.1.1 < 1.0.0~dev.N < 1.0.0`.

## Support statement

- From 1.0.0 the stable channel answers the previous stable `vonkctl` and agent
  for one minor release (N-1).
- A deprecated API field or CLI flag keeps working for one minor and is announced
  in the release notes.
- Database tables a release no longer declares are kept for one minor.
- A breaking response shape ships only in a minor release, consumer first: the
  `vonkctl` that tolerates it is in a published stable release before the
  Controller change lands. The `client-compatibility-report` CI job enforces
  this against the published stable contract (`docs/testing-and-ci.md`).
- The dev channel has no compatibility promise.
- Rollback is fix-forward. The previous minor's tables survive the newer one.

## Before the tag

1. The tag commit is a main commit whose own Release published
   `:dev-sha-<sha>` for the API, worker and LiteLLM images. Its tree is deployed
   to the owner fleet and swept.
2. Run the read-only preflight. It prints every problem and exits 1 if there is
   any (2 if state cannot be read):

   ```sh
   scripts/release/stable-preflight --repo CarstVaartjes/vonk-forge --tag vX.Y.Z --sha <sha>
   ```

   It needs `gh` (logged in with read access to the repository, its
   environments and Actions variables) and `skopeo` on the PATH; a missing tool
   or unreadable state exits 2, not 1.

   It checks the release switches, the stable environments and their deployment
   policies, name parity with the dev twins, the acceptance and agent key
   fingerprints, the owner reviewer, that the workspace version at `<sha>`
   equals the tag, that `<sha>` is on main with a successful Release, the three
   image attestations, and the signers file. The reviewer is expected on
   `installer-acceptance-stable`; pass `--reviewer-environment` to name another.
3. Fix whatever it reports. Environment settings are in
   [Agent package release](../operations/agent-package-release.md#one-time-publication-authority-setup).

## Tag and approvals

Only the owner signs and pushes the tag, with the key listed in
`.github/release-allowed-signers`:

The tag must be signed with the allowed release signer: principal
`cvaartjes@visualfabriq.com` in `.github/release-allowed-signers`. A plain
`git tag -s` signs with the machine's default key, which is not that signer and
fails the release's signature check. Name the key explicitly. Replace
`<allowed release signing key>` with the public key of the allowed release
signer (the line the signing agent lists for it); the key's location is
filled in by the owner:

```sh
git checkout main && git pull --ff-only origin main
git -c gpg.format=ssh -c user.signingkey='<allowed release signing key>' \
  tag -s vX.Y.Z <sha> -m "Vonk Forge X.Y.Z"
git -c gpg.format=ssh -c gpg.ssh.allowedSignersFile=.github/release-allowed-signers verify-tag vX.Y.Z
git push origin vX.Y.Z
```

If the signing program is not the default (for example the 1Password SSH
agent), add `-c gpg.ssh.program=<path of its ssh-sign program>` to the `tag`
command. Run `verify-tag` in an up-to-date main checkout, because it reads
`.github/release-allowed-signers` from the working tree. It must print
`Good "git" signature` for that principal before you push. To retry, delete the
local tag with `git tag -d vX.Y.Z` and sign again; never push a tag that failed
verification.

The owner then approves two deployments in the Actions run:

- `apt-release`, once the GitHub Release is published;
- `installer-acceptance-stable`, after every canary passed. It signs the
  acceptance right before promotion, so approving it means "promote this
  release". The reviewer is not on `installer-promotion-stable`: the daily
  `installer-maintenance` refresh runs in that environment from `main` and would
  otherwise wait for a click every day.

## What becomes public when

1. During CI: the immutable `vX.Y.Z` image tags and the GitHub Release.
2. After the `apt-release` approval: the stable apt package.
3. At promotion, last: `:latest` and the signed installer pointer
   `artifacts/stable/current.manifest`. `:latest` never moves before
   acceptance.
4. At the first stable promotion, the unqualified `/nas`, `/spark` and `/vonkctl`
   endpoints flip from dev to stable and stay stable. Use the `/dev/` endpoints
   for dev from then on.

Release notes are the GitHub Release body, generated from the merged PR titles.
Announce deprecations there.

## When a run fails

- **Before anything immutable is public** (no `vX.Y.Z` image tag written, the
  GitHub Release still a draft): fix the settings, or land a fix on main and
  deploy it. For a code fix the owner deletes the tag
  (`git push origin :refs/tags/vX.Y.Z`, and `gh release delete vX.Y.Z --yes`
  for a draft) and tags the fixed commit.
- **A settings fix only, after publication started:** fix the settings, then
  rerun the failed jobs of the same run. Publication is idempotent and
  reconciles an already accepted set.
- **A code fix after anything immutable is public:** bump the patch and release
  that version. Never move a published tag.

## Switching a deployment between channels

Each hop must be a Debian upgrade of the agent (`0.1.1~dev.N` to `0.1.1` to
`1.0.0~dev.M`); do not rely on `dpkg` accepting a downgrade. So dev to stable is
an upgrade only between the tag and the next version bump, while dev builds are
still `X.Y.Z~dev.N`; after the bump dev sorts above stable, and moving back to
dev is the upgrade. Before switching a
working fleet, wait for an idle artifact queue, announce the change with UTC
times, and copy `.env`, `docker-compose.yaml`, `secrets/` and the newest
PostgreSQL backup aside.

To stable:

1. Check that `curl -fsS https://install.vonkforge.ai/artifacts/stable/current.manifest`
   names version `X.Y.Z`. Then rerun the installer from the directory that
   contains `vonk-forge/`. The unqualified endpoint is stable once a stable
   release exists:

   ```sh
   curl -fsSL https://install.vonkforge.ai/nas | sh
   ```

   `.env` now has `VONK_INSTALL_CHANNEL=stable` and the compose file uses
   `:latest` Vonk images.
2. Redeploy the Compose project. This recreates the Controller; serving runs
   carry across it.
3. `vonkctl --json platform` has no channel field. Check that its
   `api_source_tree` equals `git rev-parse vX.Y.Z^{tree}` (the stable images are
   the tagged commit's), and take the channel from `.env`.
4. `vonkctl fleet upgrade --all --yes --detach`. The Controller finds each
   installed agent package under either channel, so the Sparks move to the
   stable agent through the relay, with no installer rerun and no sudo.
5. Verify both agents report the new package, a served model still has all its
   ranks, and a chat completion works through the gateway with a client key.

Back to dev is the same with the `/dev/` endpoint:

```sh
curl -fsSL https://install.vonkforge.ai/dev/nas | sh
```

Then `.env` has `VONK_INSTALL_CHANNEL=dev`; redeploy, check `vonkctl --json
platform` (its `api_source_tree` equals the tree of the commit you moved to;
the channel is the one in `.env`), and run the fleet upgrade again. A Spark's apt source keeps the
channel it was installed with; that only matters for a manual `apt upgrade`.
Rerunning the `/spark` installer (`/dev/spark` for dev) on a Spark switches it
and needs that host's administrative rights.

`vonkctl` follows `VONK_CLI_UPDATE_CHANNEL` (default `stable`; set `dev` for a
development CLI), independent of the deployment channel. While the unqualified
URLs serve stable, every repair rerun against a dev deployment must say `/dev/`.

## Proof record

One dated line once the stable-to-dev round trip has run:

- _Not yet recorded._
