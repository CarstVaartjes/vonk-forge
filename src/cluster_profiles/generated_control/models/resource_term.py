from typing import Literal

ResourceTerm = Literal['batch', 'concurrency', 'context']

RESOURCE_TERM_VALUES: set[ResourceTerm] = { 'batch', 'concurrency', 'context',  }

def check_resource_term(value: str) -> ResourceTerm:
    if value in RESOURCE_TERM_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {RESOURCE_TERM_VALUES!r}")
