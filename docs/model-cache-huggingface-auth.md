# Hugging Face model-cache authentication

The Controller downloads model artifacts into its verified NAS model cache. A
public Hugging Face artifact is requested anonymously. If the canonical
`huggingface.co` resolve endpoint returns `401` or `403`, the Controller may
retry once with the bearer token supplied through the optional `HF_TOKEN_FILE`
Compose file secret. This supports gated and private repositories without
putting credentials in recipe metadata, the database, public API documents,
logs, CLI output, or Spark payloads.

The deployment projects the optional secret through the existing normalized
runtime volume as `/run/vonk-normalized-secrets/hf-token`. The token is sent only to the canonical
Hugging Face authority. Resolve redirects are followed manually only when the
destination is an HTTPS Hugging Face CDN authority; the bearer header is
removed before each CDN request. Redirects to any other host fail closed.

Configure a token by writing it to `secrets/hf-token` in the bundle (the
installer asks for it, optionally). The token should
have the smallest Hugging Face scope needed for the gated repositories. If a
gated artifact is requested without a token, the cache operation reports the
typed `model_cache.credentials_missing` blocker. A rejected token reports
`model_cache.credentials_denied`; neither error includes the token value.

The signed NAS installer declares `hf-token` as an optional secret. A fresh
install creates an empty regular `secrets/hf-token` file with owner-only
permissions and does not prompt for it, so public model downloads work by
default. Replace that file with the protected token and recreate the
Controller services when gated access is needed.

After the Controller verifies the artifact bytes and digest, Spark nodes
receive the cache payload through the existing tokenless distribution path.

See Hugging Face's guidance on [user access tokens](https://huggingface.co/docs/hub/security-tokens)
and [gated models](https://huggingface.co/docs/hub/models-gated) for account
and repository authorization requirements.

## Public GitHub release assets

A model may instead bind a public GitHub release by its numeric `release_id`
and bind each model file ID to one numeric release `asset_id`. Mutable release
tags and browser download URLs are not source identities. The Controller reads
the exact public release anonymously, checks the release ID and asset
membership, uploaded state, filename, size, and any SHA-256 digest GitHub
reports. The canonical `ModelFile` size and SHA-256 remain authoritative; the
Controller verifies the complete bytes against them before publishing.

Binary requests use GitHub's release-asset API for that exact asset ID. A
redirect is followed manually only to the `release-assets.githubusercontent.com`
host. Caller-level HTTP authorization and cookies are excluded from both the
API and CDN requests, and signed redirect URLs are not persisted or exposed in
cache errors. Restricted GitHub sources are refused because this source path
has no credential flow.

Verified local objects remain usable while GitHub is unavailable. Missing or
partial objects recheck the pinned release metadata, then resume through the
same durable range/checkpoint and content-verification path. GitHub release
metadata is an upstream consistency check, not a replacement for the model
file's exact content hash.

See GitHub's [release lookup](https://docs.github.com/en/rest/releases/releases)
and [release asset download](https://docs.github.com/en/rest/releases/assets)
API documentation for the public endpoints used by this provider.
