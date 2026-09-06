from typing import Literal, cast

ModelLineageRelation = Literal['derived', 'official', 'quantized']

MODEL_LINEAGE_RELATION_VALUES: set[ModelLineageRelation] = { 'derived', 'official', 'quantized',  }

def check_model_lineage_relation(value: str) -> ModelLineageRelation:
    if value in MODEL_LINEAGE_RELATION_VALUES:
        return cast(ModelLineageRelation, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {MODEL_LINEAGE_RELATION_VALUES!r}")
