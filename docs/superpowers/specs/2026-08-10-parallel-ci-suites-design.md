# Parallel CI Suites Design

## Context

The required `Catalog and service suites` check currently runs the full control,
agent, and web suites serially. Measured GitHub timings are about six minutes for
control, two minutes for agent, and seconds for web, making this single job the
PR critical path. A local file-parallel control run completed 1,519 applicable
tests in about 86 seconds; one Linux-boundary test cannot run in the current WSL
Python environment even when executed serially, so Ubuntu CI remains the final
platform verification.

## Design

Create independent `control-suite`, `agent-suite`, and `web-suite` jobs. Control
uses pinned `pytest-xdist==3.8.0` with Python 3.12 and `--dist loadfile`, keeping
each test module in one worker. Agent remains serial because its supervisor
integration harness has a known timing-sensitive readiness helper. Web tests and
the production build remain together to avoid a second npm installation.

Retain a lightweight job named `Catalog and service suites` with job id
`catalog-runtime`. It always runs after all three suite jobs and succeeds only
when each dependency succeeded. This preserves the existing required-check name
and downstream release gate while making failures visible in their owning suite.

## Verification

Repository workflow contracts will require all three jobs, pinned control
parallelism, complete suite commands, and the preserving aggregator. The pull
request's Ubuntu Actions run is the acceptance test: all suites must pass and the
aggregator's elapsed time must be negligible after the slowest suite completes.
