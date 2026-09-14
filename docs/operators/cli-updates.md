# vonkctl build identity and accepted updates

`vonkctl --version` works without Controller credentials or a network connection.
`vonkctl --json --version` prints the installed release version and exact source
commit. Development wheels without a release stamp print `unstamped` as their
source identity.

To check the accepted stable channel, provide the trusted installer signing
public key already verified during installer setup:

```sh
vonkctl update --public-key /path/to/installer-public.pem
vonkctl update --apply --public-key /path/to/installer-public.pem
```

The command verifies the unexpired signed channel pointer, immutable signed
release and exact CLI wheel digest. `--apply` installs only when the accepted
source commit differs from the currently installed CLI. It uses `uv pip` in the
exact Python virtual environment running `vonkctl`, with dependency resolution
disabled; `uv` must be available and that environment must already have the CLI
dependencies. The command neither edits
Controller credentials nor changes the chosen channel or installer signing key.
It does not update the Controller or Sparks. A failed verification leaves the
installed CLI intact. The virtual environment must be writable; run the command
from the environment you intend to update.

Set `VONK_INSTALLER_PUBLIC_KEY_FILE` to avoid repeating `--public-key`. Set
`VONK_CLI_UPDATE_NOTICES=1` to opt into an interactive notice after an explicit
stable-channel `vonkctl update` check. Notices are cached for one day under the user cache directory,
suppressed for JSON and noninteractive commands, and never install an update.
An accepted release without a CLI wheel is invalid under the current schema-2
publication contract.
