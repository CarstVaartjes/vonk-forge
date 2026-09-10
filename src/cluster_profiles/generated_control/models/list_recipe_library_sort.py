from typing import Literal, cast

ListRecipeLibrarySort = Literal['name', 'updated']

LIST_RECIPE_LIBRARY_SORT_VALUES: set[ListRecipeLibrarySort] = { 'name', 'updated',  }

def check_list_recipe_library_sort(value: str) -> ListRecipeLibrarySort:
    if value in LIST_RECIPE_LIBRARY_SORT_VALUES:
        return cast(ListRecipeLibrarySort, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {LIST_RECIPE_LIBRARY_SORT_VALUES!r}")
