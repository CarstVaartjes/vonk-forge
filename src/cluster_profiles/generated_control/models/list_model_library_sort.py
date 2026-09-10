from typing import Literal, cast

ListModelLibrarySort = Literal['name', 'updated']

LIST_MODEL_LIBRARY_SORT_VALUES: set[ListModelLibrarySort] = { 'name', 'updated',  }

def check_list_model_library_sort(value: str) -> ListModelLibrarySort:
    if value in LIST_MODEL_LIBRARY_SORT_VALUES:
        return cast(ListModelLibrarySort, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {LIST_MODEL_LIBRARY_SORT_VALUES!r}")
