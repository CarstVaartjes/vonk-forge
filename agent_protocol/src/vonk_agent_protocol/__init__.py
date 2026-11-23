"""Agent wire protocol; import every name from the submodule that defines it.

Importing ``contracts`` first keeps the package's import cycle (contracts ->
recipe operations -> host helper) entered from its one safe end, whichever
submodule a caller imports first.
"""

from . import contracts as contracts
