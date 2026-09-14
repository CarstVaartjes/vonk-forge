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
source commit differs from the currently installed CLI. It uses `pip` in the
Python environment running `vonkctl`, with dependency resolution disabled; that
environment must already have the CLI dependencies. The command neither edits
Controller credentials nor changes the chosen channel or installer signing key.
It does not update the Controller or Sparks. A failed verification leaves the
installed CLI intact. An update may require a writable virtual environment;
repeat the command from the environment you intend to update.

Set `VONK_INSTALLER_PUBLIC_KEY_FILE` to avoid repeating `--public-key`. Set
`VONK_CLI_UPDATE_NOTICES=1` to opt into a once-per-day interactive notice after
ordinary CLI commands. Notices are cached under the user cache directory,
suppressed for JSON and noninteractive commands, and never install an update.
An accepted release without a CLI wheel is invalid under the current schema-2
publication contract.
