from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.library_filter_values_sort_type_0 import check_library_filter_values_sort_type_0
from ..models.library_filter_values_sort_type_0 import LibraryFilterValuesSortType0
from ..types import UNSET, Unset
from typing import cast






T = TypeVar("T", bound="LibraryFilterValues")



@_attrs_define
class LibraryFilterValues:
    """ The filters that produced a Library page, echoed to the client.

    This is a typed echo rather than a free-form map so the request and the
    response describe the same vocabulary. Every field is optional, so a page
    that applied no filter stays valid without inventing values.

        Attributes:
            alignment (list[str] | Unset):
            cached (bool | None | Unset):
            creator (list[str] | Unset):
            engine (list[str] | Unset):
            family (list[str] | Unset):
            fits_fleet (bool | None | Unset):
            model (list[str] | Unset):
            publisher (list[str] | Unset):
            quantization (list[str] | Unset):
            ready (bool | None | Unset):
            search (None | str | Unset):
            sort (LibraryFilterValuesSortType0 | None | Unset):
            sparks (list[int] | Unset):
            updated_since (None | str | Unset):
            usage (list[str] | Unset):
            version (list[str] | Unset):
     """

    alignment: list[str] | Unset = UNSET
    cached: bool | None | Unset = UNSET
    creator: list[str] | Unset = UNSET
    engine: list[str] | Unset = UNSET
    family: list[str] | Unset = UNSET
    fits_fleet: bool | None | Unset = UNSET
    model: list[str] | Unset = UNSET
    publisher: list[str] | Unset = UNSET
    quantization: list[str] | Unset = UNSET
    ready: bool | None | Unset = UNSET
    search: None | str | Unset = UNSET
    sort: LibraryFilterValuesSortType0 | None | Unset = UNSET
    sparks: list[int] | Unset = UNSET
    updated_since: None | str | Unset = UNSET
    usage: list[str] | Unset = UNSET
    version: list[str] | Unset = UNSET





    def to_dict(self) -> dict[str, Any]:
        alignment: list[str] | Unset = UNSET
        if not isinstance(self.alignment, Unset):
            alignment = self.alignment



        cached: bool | None | Unset
        if isinstance(self.cached, Unset):
            cached = UNSET
        else:
            cached = self.cached

        creator: list[str] | Unset = UNSET
        if not isinstance(self.creator, Unset):
            creator = self.creator



        engine: list[str] | Unset = UNSET
        if not isinstance(self.engine, Unset):
            engine = self.engine



        family: list[str] | Unset = UNSET
        if not isinstance(self.family, Unset):
            family = self.family



        fits_fleet: bool | None | Unset
        if isinstance(self.fits_fleet, Unset):
            fits_fleet = UNSET
        else:
            fits_fleet = self.fits_fleet

        model: list[str] | Unset = UNSET
        if not isinstance(self.model, Unset):
            model = self.model



        publisher: list[str] | Unset = UNSET
        if not isinstance(self.publisher, Unset):
            publisher = self.publisher



        quantization: list[str] | Unset = UNSET
        if not isinstance(self.quantization, Unset):
            quantization = self.quantization



        ready: bool | None | Unset
        if isinstance(self.ready, Unset):
            ready = UNSET
        else:
            ready = self.ready

        search: None | str | Unset
        if isinstance(self.search, Unset):
            search = UNSET
        else:
            search = self.search

        sort: None | str | Unset
        if isinstance(self.sort, Unset):
            sort = UNSET
        elif isinstance(self.sort, str):
            sort = self.sort
        else:
            sort = self.sort

        sparks: list[int] | Unset = UNSET
        if not isinstance(self.sparks, Unset):
            sparks = self.sparks



        updated_since: None | str | Unset
        if isinstance(self.updated_since, Unset):
            updated_since = UNSET
        else:
            updated_since = self.updated_since

        usage: list[str] | Unset = UNSET
        if not isinstance(self.usage, Unset):
            usage = self.usage



        version: list[str] | Unset = UNSET
        if not isinstance(self.version, Unset):
            version = self.version




        field_dict: dict[str, Any] = {}

        field_dict.update({
        })
        if alignment is not UNSET:
            field_dict["alignment"] = alignment
        if cached is not UNSET:
            field_dict["cached"] = cached
        if creator is not UNSET:
            field_dict["creator"] = creator
        if engine is not UNSET:
            field_dict["engine"] = engine
        if family is not UNSET:
            field_dict["family"] = family
        if fits_fleet is not UNSET:
            field_dict["fits_fleet"] = fits_fleet
        if model is not UNSET:
            field_dict["model"] = model
        if publisher is not UNSET:
            field_dict["publisher"] = publisher
        if quantization is not UNSET:
            field_dict["quantization"] = quantization
        if ready is not UNSET:
            field_dict["ready"] = ready
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


        def _parse_cached(data: object) -> bool | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(bool | None | Unset, data)

        cached = _parse_cached(d.pop("cached", UNSET))


        creator = cast(list[str], d.pop("creator", UNSET))


        engine = cast(list[str], d.pop("engine", UNSET))


        family = cast(list[str], d.pop("family", UNSET))


        def _parse_fits_fleet(data: object) -> bool | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(bool | None | Unset, data)

        fits_fleet = _parse_fits_fleet(d.pop("fits_fleet", UNSET))


        model = cast(list[str], d.pop("model", UNSET))


        publisher = cast(list[str], d.pop("publisher", UNSET))


        quantization = cast(list[str], d.pop("quantization", UNSET))


        def _parse_ready(data: object) -> bool | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(bool | None | Unset, data)

        ready = _parse_ready(d.pop("ready", UNSET))


        def _parse_search(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        search = _parse_search(d.pop("search", UNSET))


        def _parse_sort(data: object) -> LibraryFilterValuesSortType0 | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                sort_type_0 = check_library_filter_values_sort_type_0(data)



                return sort_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(LibraryFilterValuesSortType0 | None | Unset, data)

        sort = _parse_sort(d.pop("sort", UNSET))


        sparks = cast(list[int], d.pop("sparks", UNSET))


        def _parse_updated_since(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        updated_since = _parse_updated_since(d.pop("updated_since", UNSET))


        usage = cast(list[str], d.pop("usage", UNSET))


        version = cast(list[str], d.pop("version", UNSET))


        library_filter_values = cls(
            alignment=alignment,
            cached=cached,
            creator=creator,
            engine=engine,
            family=family,
            fits_fleet=fits_fleet,
            model=model,
            publisher=publisher,
            quantization=quantization,
            ready=ready,
            search=search,
            sort=sort,
            sparks=sparks,
            updated_since=updated_since,
            usage=usage,
            version=version,
        )

        return library_filter_values
