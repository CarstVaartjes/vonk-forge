# vonkctl build identity and accepted updates

`vonkctl --version` works without Controller credentials or a network connection.
`vonkctl --json --version` prints the installed release version and exact source
commit. Development wheels without a release stamp print `unstamped` as their
source identity.

To check the accepted stable channel, or install it, with the installer release
signing public key the CLI ships with (there is no key to configure):

```sh
vonkctl update
vonkctl update --apply
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

CLI updates default to the `stable` channel. Set
`VONK_CLI_UPDATE_CHANNEL=dev` for a CLI installed from the accepted development
channel; the same channel is used by `vonkctl update` and notices. Ordinary
interactive commands start a short-lived background check of that signed
accepted release. The command
itself does not wait for the network; a newly found update may appear on the
next interactive command. Verified results are cached for one day under the
user cache directory, and failed checks retry after 15 minutes. JSON and
noninteractive commands, including offline `--version`, never start a check.
Notices never install an update.
An accepted release without a CLI wheel is invalid under the current schema-2
publication contract.
