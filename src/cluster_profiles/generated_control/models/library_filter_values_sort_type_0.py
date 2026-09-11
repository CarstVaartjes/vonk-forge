from typing import Literal, cast

LibraryFilterValuesSortType0 = Literal['name', 'updated']

LIBRARY_FILTER_VALUES_SORT_TYPE_0_VALUES: set[LibraryFilterValuesSortType0] = { 'name', 'updated',  }

def check_library_filter_values_sort_type_0(value: str) -> LibraryFilterValuesSortType0:
    if value in LIBRARY_FILTER_VALUES_SORT_TYPE_0_VALUES:
        return cast(LibraryFilterValuesSortType0, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {LIBRARY_FILTER_VALUES_SORT_TYPE_0_VALUES!r}")
