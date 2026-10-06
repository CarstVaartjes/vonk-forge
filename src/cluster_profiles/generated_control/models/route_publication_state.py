from typing import Literal

RoutePublicationState = Literal['completed', 'failed', 'publication-pending', 'routes-withdrawn', 'withdrawal-pending']

ROUTE_PUBLICATION_STATE_VALUES: set[RoutePublicationState] = { 'completed', 'failed', 'publication-pending', 'routes-withdrawn', 'withdrawal-pending',  }

def check_route_publication_state(value: str) -> RoutePublicationState:
    if value in ROUTE_PUBLICATION_STATE_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {ROUTE_PUBLICATION_STATE_VALUES!r}")
