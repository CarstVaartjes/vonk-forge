from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.library_filter_values_sort_type_0 import check_library_filter_values_sort_type_0
from ..models.library_filter_values_sort_type_0 import LibraryFilterValuesSortType0
from ..types import UNSET, Unset
from typing import cast
from typing import cast, Union
from typing import Union






T = TypeVar("T", bound="LibraryFilterValues")



@_attrs_define
class LibraryFilterValues:
    """ The filters that produced a Library page, echoed to the client.

    This is a typed echo rather than a free-form map so the request and the
    response describe the same vocabulary. Every field is optional, so a page
    that applied no filter stays valid without inventing values.

        Attributes:
            alignment (Union[Unset, list[str]]):
            all_models (Union[None, Unset, bool]):
            family (Union[Unset, list[str]]):
            local_only (Union[None, Unset, bool]):
            model (Union[Unset, list[str]]):
            publisher (Union[Unset, list[str]]):
            quantization (Union[Unset, list[str]]):
            search (Union[None, Unset, str]):
            sort (Union[LibraryFilterValuesSortType0, None, Unset]):
            sparks (Union[Unset, list[int]]):
            updated_since (Union[None, Unset, str]):
            usage (Union[Unset, list[str]]):
            version (Union[Unset, list[str]]):
     """

    alignment: Union[Unset, list[str]] = UNSET
    all_models: Union[None, Unset, bool] = UNSET
    family: Union[Unset, list[str]] = UNSET
    local_only: Union[None, Unset, bool] = UNSET
    model: Union[Unset, list[str]] = UNSET
    publisher: Union[Unset, list[str]] = UNSET
    quantization: Union[Unset, list[str]] = UNSET
    search: Union[None, Unset, str] = UNSET
    sort: Union[LibraryFilterValuesSortType0, None, Unset] = UNSET
    sparks: Union[Unset, list[int]] = UNSET
    updated_since: Union[None, Unset, str] = UNSET
    usage: Union[Unset, list[str]] = UNSET
    version: Union[Unset, list[str]] = UNSET





    def to_dict(self) -> dict[str, Any]:
        alignment: Union[Unset, list[str]] = UNSET
        if not isinstance(self.alignment, Unset):
            alignment = self.alignment



        all_models: Union[None, Unset, bool]
        if isinstance(self.all_models, Unset):
            all_models = UNSET
        else:
            all_models = self.all_models

        family: Union[Unset, list[str]] = UNSET
        if not isinstance(self.family, Unset):
            family = self.family



        local_only: Union[None, Unset, bool]
        if isinstance(self.local_only, Unset):
            local_only = UNSET
        else:
            local_only = self.local_only

        model: Union[Unset, list[str]] = UNSET
        if not isinstance(self.model, Unset):
            model = self.model



        publisher: Union[Unset, list[str]] = UNSET
        if not isinstance(self.publisher, Unset):
            publisher = self.publisher



        quantization: Union[Unset, list[str]] = UNSET
        if not isinstance(self.quantization, Unset):
            quantization = self.quantization



        search: Union[None, Unset, str]
        if isinstance(self.search, Unset):
            search = UNSET
        else:
            search = self.search

        sort: Union[None, Unset, str]
        if isinstance(self.sort, Unset):
            sort = UNSET
        elif isinstance(self.sort, str):
            sort = self.sort
        else:
            sort = self.sort

        sparks: Union[Unset, list[int]] = UNSET
        if not isinstance(self.sparks, Unset):
            sparks = self.sparks



        updated_since: Union[None, Unset, str]
        if isinstance(self.updated_since, Unset):
            updated_since = UNSET
        else:
            updated_since = self.updated_since

        usage: Union[Unset, list[str]] = UNSET
        if not isinstance(self.usage, Unset):
            usage = self.usage



        version: Union[Unset, list[str]] = UNSET
        if not isinstance(self.version, Unset):
            version = self.version




        field_dict: dict[str, Any] = {}

        field_dict.update({
        })
        if alignment is not UNSET:
            field_dict["alignment"] = alignment
        if all_models is not UNSET:
            field_dict["all_models"] = all_models
        if family is not UNSET:
            field_dict["family"] = family
        if local_only is not UNSET:
            field_dict["local_only"] = local_only
        if model is not UNSET:
            field_dict["model"] = model
        if publisher is not UNSET:
            field_dict["publisher"] = publisher
        if quantization is not UNSET:
            field_dict["quantization"] = quantization
        if search is not UNSET:
            field_dict["search"] = search
        if sort is not UNSET:
            field_dict["sort"] = sort
        if sparks is not UNSET:
            field_dict["sparks"] = sparks
        if updated_since is not UNSET:
            field_dict["updated_since"] = updated_since
        if usage is not UNSET:
            field_dict["usage"] = usage
        if version is not UNSET:
            field_dict["version"] = version

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        alignment = cast(list[str], d.pop("alignment", UNSET))


        def _parse_all_models(data: object) -> Union[None, Unset, bool]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, bool], data)

        all_models = _parse_all_models(d.pop("all_models", UNSET))


        family = cast(list[str], d.pop("family", UNSET))


        def _parse_local_only(data: object) -> Union[None, Unset, bool]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, bool], data)

        local_only = _parse_local_only(d.pop("local_only", UNSET))


        model = cast(list[str], d.pop("model", UNSET))


        publisher = cast(list[str], d.pop("publisher", UNSET))


        quantization = cast(list[str], d.pop("quantization", UNSET))


        def _parse_search(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        search = _parse_search(d.pop("search", UNSET))


        def _parse_sort(data: object) -> Union[LibraryFilterValuesSortType0, None, Unset]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                sort_type_0 = check_library_filter_values_sort_type_0(data)



                return sort_type_0
            except: # noqa: E722
                pass
            return cast(Union[LibraryFilterValuesSortType0, None, Unset], data)

        sort = _parse_sort(d.pop("sort", UNSET))


        sparks = cast(list[int], d.pop("sparks", UNSET))


        def _parse_updated_since(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        updated_since = _parse_updated_since(d.pop("updated_since", UNSET))


        usage = cast(list[str], d.pop("usage", UNSET))


        version = cast(list[str], d.pop("version", UNSET))


        library_filter_values = cls(
            alignment=alignment,
            all_models=all_models,
            family=family,
            local_only=local_only,
            model=model,
            publisher=publisher,
            quantization=quantization,
            search=search,
            sort=sort,
            sparks=sparks,
            updated_since=updated_since,
            usage=usage,
            version=version,
        )

        return library_filter_values
