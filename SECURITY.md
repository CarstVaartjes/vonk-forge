# Security policy

## Reporting a vulnerability

Please report security problems privately through GitHub. Open the
repository's **Security** tab and choose **Report a vulnerability**
(<https://github.com/CarstVaartjes/vonk-forge/security/advisories/new>). Do not put
the details in a public issue, pull request or discussion.

If the form is not available, open a public issue titled "Security contact
request" with no details. The maintainer then opens a private advisory with
you.

## Supported versions

Security fixes ship forward in the next stable release; there are no
backports. Run the latest stable release. The stable Controller still serves
the previous minor's `vonkctl` and agent (N-1, see the
[support statement](docs/runbooks/stable-release.md#support-statement)), so
upgrade those within one minor. The dev channel is fixed forward on main.

## Scope

The Controller (API, Web UI, worker), the Spark agent and its helper,
`vonkctl`, the installers, and the published images and packages. The
[security threat model](docs/security/threat-model.md) describes the trust
boundaries. Recipes and the website have their own repositories and policies:
[vonk-forge-recipes](https://github.com/CarstVaartjes/vonk-forge-recipes/security)
and [vonk-forge-web](https://github.com/CarstVaartjes/vonk-forge-web/security).
