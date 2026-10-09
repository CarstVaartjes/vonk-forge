"""Batch qualification of an exact recipe authority against a Controller.

Every batch is an ordinary whole-Fleet load of one dedicated profile:

* ``load`` saves the batch assignments, loads the profile (install and start)
  and smokes every lane;
* ``recover`` waits for the operator's physical failure (host restart or rank
  loss), for the Controller to heal the workload, and smokes it again;
* ``stop`` loads the empty profile, which stops the batch and advances the
  campaign to the next batch.

Each outcome is one JSON line in a plain results log. Rerunning a step simply
repeats it; the latest line for a recipe and step wins. A shared recovery group
member whose representative already passed that failure mode in this campaign
is recorded as covered instead of being disrupted again.
"""

from .application import load as load
from .cli import _arguments as _arguments
from .cli import _notify as _notify
from .cli import main as main
from .cli import run as run
from .contracts import _PASSED as _PASSED
from .contracts import _TERMINAL_APPLICATIONS as _TERMINAL_APPLICATIONS
from .contracts import AUTHORITY_SCHEMA as AUTHORITY_SCHEMA
from .contracts import FAILURE_MODES as FAILURE_MODES
from .contracts import MANIFEST_SCHEMA as MANIFEST_SCHEMA
from .contracts import PROFILE_LABEL as PROFILE_LABEL
from .contracts import Batch as Batch
from .contracts import Campaign as Campaign
from .contracts import Lane as Lane
from .contracts import RecoveryRef as RecoveryRef
from .contracts import ResultsLog as ResultsLog
from .contracts import Row as Row
from .contracts import _read_contract as _read_contract
from .contracts import _validator as _validator
from .contracts import load_campaign as load_campaign
from .lanes import _lanes_from_profile as _lanes_from_profile
from .lanes import _lanes_from_sparks as _lanes_from_sparks
from .profiles import _alias as _alias
from .profiles import _apply_profile as _apply_profile
from .profiles import _fleet_nodes as _fleet_nodes
from .profiles import _profile as _profile
from .profiles import _quote as _quote
from .profiles import _save_profile as _save_profile
from .profiles import _serving_run as _serving_run
from .profiles import _wait_serving as _wait_serving
from .recovery import _boot_id as _boot_id
from .recovery import _online as _online
from .recovery import _recovery_targets as _recovery_targets
from .recovery import _wait_disruption as _wait_disruption
from .recovery import recover as recover
from .smoke import _smoke as _smoke
from .smoke import _smoke_lanes as _smoke_lanes
from .status import _modes as _modes
from .status import _next_step as _next_step
from .status import _recipe_summary as _recipe_summary
from .status import _select_batch as _select_batch
from .status import status as status
from .stop import stop as stop
