from typing import Literal

ResourceTermProblem = Literal['evidence_invalid', 'evidence_unknown', 'unknown', 'unsupported']

RESOURCE_TERM_PROBLEM_VALUES: set[ResourceTermProblem] = { 'evidence_invalid', 'evidence_unknown', 'unknown', 'unsupported',  }

def check_resource_term_problem(value: str) -> ResourceTermProblem:
    if value in RESOURCE_TERM_PROBLEM_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {RESOURCE_TERM_PROBLEM_VALUES!r}")
