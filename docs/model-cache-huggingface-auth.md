# Hugging Face model-cache authentication

The Controller downloads model artifacts into its verified NAS model cache. A
public Hugging Face artifact is requested anonymously. If the canonical
`huggingface.co` resolve endpoint returns `401` or `403`, the Controller may
retry once with the bearer token supplied through the optional `HF_TOKEN_FILE`
Compose file secret. This supports gated and private repositories without
putting credentials in recipe metadata, the database, public API documents,
logs, CLI output, or Spark payloads.

The deployment projects the optional secret through the existing normalized
runtime volume as `VONK_HF_TOKEN_FILE`. The token is sent only to the canonical
Hugging Face authority. Resolve redirects are followed manually only when the
destination is an HTTPS Hugging Face CDN authority; the bearer header is
removed before each CDN request. Redirects to any other host fail closed.

Configure a token by creating a protected file and setting
`HF_TOKEN_FILE=./secrets/hf-token` in the controller `.env`. The token should
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
