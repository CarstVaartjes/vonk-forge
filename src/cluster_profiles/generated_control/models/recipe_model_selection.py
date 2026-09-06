from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast

if TYPE_CHECKING:
  from ..models.model_reference import ModelReference
  from ..models.recipe_model_file import RecipeModelFile





T = TypeVar("T", bound="RecipeModelSelection")



@_attrs_define
class RecipeModelSelection:
    """
        Attributes:
            files (list['RecipeModelFile']):
            id (str):
            model (ModelReference):
     """

    files: list['RecipeModelFile']
    id: str
    model: 'ModelReference'





    def to_dict(self) -> dict[str, Any]:
        from ..models.model_reference import ModelReference
        from ..models.recipe_model_file import RecipeModelFile
        files = []
        for files_item_data in self.files:
            files_item = files_item_data.to_dict()
            files.append(files_item)



        id = self.id

        model = self.model.to_dict()


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "files": files,
            "id": id,
            "model": model,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.model_reference import ModelReference
        from ..models.recipe_model_file import RecipeModelFile
        d = dict(src_dict)
        files = []
        _files = d.pop("files")
        for files_item_data in (_files):
            files_item = RecipeModelFile.from_dict(files_item_data)



            files.append(files_item)


        id = d.pop("id")

        model = ModelReference.from_dict(d.pop("model"))




        recipe_model_selection = cls(
            files=files,
            id=id,
            model=model,
        )

        return recipe_model_selection
