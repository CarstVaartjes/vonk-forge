# Release signing

The release key for `release@vonkforge.ai` lives in the maintainer's 1Password
SSH agent. In the tag command of
[Stable release and channel switch](../runbooks/stable-release.md#tag-and-approvals),
the signing program is `op-ssh-sign` in the `MacOS` folder of the 1Password app
bundle; on macOS:

```sh
export SSH_SIGNING_PROGRAM=/Applications/1Password.app/Contents/MacOS/op-ssh-sign
```

1Password asks for approval when it signs.
