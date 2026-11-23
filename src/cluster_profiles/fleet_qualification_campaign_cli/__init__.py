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
