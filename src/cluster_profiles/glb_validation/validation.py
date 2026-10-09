"""Validation for glb validation."""

from __future__ import annotations

import math
from pathlib import Path

from .accessors import _accessors, _parse
from .common import PROFILES, Accessor, _array, _finite, _index, _object
from .meshes import _reachable_meshes, _triangle_primitive
from .textures import _texture_index, _texture_source, _textures, _validate_materials


def validate_mesh_glb_bytes(
    data: bytes, *, profile: str = "geometry"
) -> dict[str, int]:
    """Validate one in-memory artifact and return bounded structural metadata."""
    if profile not in PROFILES:
        raise ValueError(f"unsupported GLB validation profile: {profile}")
    document, blob = _parse(data)
    if "animations" in document:
        raise ValueError("GLB animations are not supported by this artifact contract")
    if "cameras" in document:
        raise ValueError("GLB cameras are not supported by this artifact contract")
    accessors, views = _accessors(document, blob)
    images, textures = _textures(document, blob, views, accessors)
    materials = _validate_materials(document, textures)
    meshes = [
        _object(item, "mesh") for item in _array(document, "meshes", required=True)
    ]
    skins = [_object(item, "skin") for item in _array(document, "skins")]
    reachable, reachable_nodes = _reachable_meshes(document, len(meshes), len(skins))
    if reachable != set(range(len(meshes))):
        raise ValueError(
            "GLB contains a mesh that is unreachable from the default scene"
        )

    skinned_primitives: list[tuple[Accessor, dict[str, object]]] = []
    attribute_accessors: set[int] = set()
    index_accessors: set[int] = set()
    primitive_count = 0
    for mesh in meshes:
        if "weights" in mesh:
            raise ValueError(
                "GLB morph weights are not supported by this artifact contract"
            )
        primitives = mesh.get("primitives")
        if not isinstance(primitives, list) or not primitives:
            raise ValueError("GLB mesh has no primitives")
        for raw in primitives:
            primitive_count += 1
            primitive = _object(raw, "primitive")
            if "material" in primitive:
                _index(primitive["material"], len(materials), "primitive material")
            position, attributes = _triangle_primitive(primitive, accessors)
            index_accessors.add(
                _index(primitive.get("indices"), len(accessors), "indices accessor")
            )
            attribute_accessors.update(
                _index(value, len(accessors), f"{name} accessor")
                for name, value in attributes.items()
            )
            if profile in {"textured", "textured-pbr"}:
                uv = accessors[
                    _index(
                        attributes.get("TEXCOORD_0"),
                        len(accessors),
                        "TEXCOORD_0 accessor",
                    )
                ]
                if (
                    uv.component_type != 5126
                    or uv.kind != "VEC2"
                    or uv.count != position.count
                ):
                    raise ValueError(
                        "GLB TEXCOORD_0 must be FLOAT VEC2 matching POSITION count"
                    )
                _finite(uv, "TEXCOORD_0")
                material = materials[
                    _index(
                        primitive.get("material"), len(materials), "primitive material"
                    )
                ]
                pbr = _object(
                    material.get("pbrMetallicRoughness"),
                    "material pbrMetallicRoughness",
                )
                base_texture = _texture_index(
                    pbr.get("baseColorTexture"), textures, "baseColorTexture"
                )
                if profile == "textured-pbr":
                    metallic_texture = _texture_index(
                        pbr.get("metallicRoughnessTexture"),
                        textures,
                        "metallicRoughnessTexture",
                    )
                    if base_texture == metallic_texture or _texture_source(
                        textures[base_texture], len(images)
                    ) == _texture_source(textures[metallic_texture], len(images)):
                        raise ValueError(
                            "GLB PBR textures must use distinct embedded images"
                        )
            if profile == "skinned":
                skinned_primitives.append((position, attributes))

    if any(
        accessor.interleaved and index not in attribute_accessors
        for index, accessor in enumerate(accessors)
    ):
        raise ValueError(
            "GLB byteStride is only permitted for vertex attribute accessors"
        )
    attribute_views = {accessors[index].view_index for index in attribute_accessors}
    index_views = {accessors[index].view_index for index in index_accessors}
    if attribute_views & index_views:
        raise ValueError("GLB bufferView must not mix vertex attributes and indices")
    for view in attribute_views:
        view_accessors = {
            index
            for index in attribute_accessors
            if accessors[index].view_index == view
        }
        if len(view_accessors) > 1 and not all(
            accessors[index].interleaved for index in view_accessors
        ):
            raise ValueError(
                "GLB shared vertex-attribute bufferView must declare byteStride"
            )

    if profile in {"textured", "textured-pbr"} and not images:
        raise ValueError("GLB textured mesh contains no embedded images")
    if profile == "textured-pbr" and (len(images) < 2 or len(textures) < 2):
        raise ValueError(
            "GLB PBR mesh must contain base-color and metallic-roughness images"
        )
    if profile == "skinned":
        if len(skins) != 1:
            raise ValueError("GLB SkinTokens profile requires exactly one skin")
        nodes = [
            _object(item, "node") for item in _array(document, "nodes", required=True)
        ]
        mesh_nodes = [node for node in nodes if "mesh" in node]
        if not mesh_nodes or any(node.get("skin") != 0 for node in mesh_nodes):
            raise ValueError("every GLB skinned mesh node must bind a skin")
        for skin in skins:
            joints = skin.get("joints")
            if not isinstance(joints, list) or not joints:
                raise ValueError("GLB skin has no joints")
            if len(joints) != len(set(joints)):
                raise ValueError("GLB skin contains duplicate joints")
            for joint in joints:
                joint_index = _index(joint, len(nodes), "skin joint")
                if joint_index not in reachable_nodes:
                    raise ValueError(
                        "GLB skin joint is unreachable from the default scene"
                    )
            if skin.get("skeleton") is not None:
                skeleton = _index(skin["skeleton"], len(nodes), "skin skeleton")
                if skeleton not in joints:
                    raise ValueError("GLB skin skeleton must be one of its joints")
                parents: dict[int, int] = {}
                for parent, node in enumerate(nodes):
                    raw_children = node.get("children", [])
                    if not isinstance(raw_children, list):
                        raise ValueError("GLB node children must be an array")  # noqa: TRY004
                    for child in raw_children:
                        parents[_index(child, len(nodes), "node child")] = parent
                for joint in joints:
                    cursor = int(joint)
                    while cursor != skeleton and cursor in parents:
                        cursor = parents[cursor]
                    if cursor != skeleton:
                        raise ValueError(
                            "GLB skin skeleton must be an ancestor of every joint"
                        )
            inverse = accessors[
                _index(
                    skin.get("inverseBindMatrices"),
                    len(accessors),
                    "inverseBindMatrices accessor",
                )
            ]
            if inverse.target is not None:
                raise ValueError(
                    "GLB inverseBindMatrices bufferView must not declare a target"
                )
            if inverse.view_index in attribute_views | index_views:
                raise ValueError(
                    "GLB inverseBindMatrices bufferView must have a unique role"
                )
            if (
                inverse.component_type != 5126
                or inverse.kind != "MAT4"
                or inverse.count != len(joints)
            ):
                raise ValueError(
                    "GLB inverseBindMatrices must be FLOAT MAT4 matching joint count"
                )
            _finite(inverse, "inverseBindMatrices")
        first_joints = _object(skins[0], "skin").get("joints")
        assert isinstance(first_joints, list)
        joint_count = len(first_joints)
        for position, attributes in skinned_primitives:
            joints = accessors[
                _index(attributes.get("JOINTS_0"), len(accessors), "JOINTS_0 accessor")
            ]
            weights = accessors[
                _index(
                    attributes.get("WEIGHTS_0"), len(accessors), "WEIGHTS_0 accessor"
                )
            ]
            normals = accessors[
                _index(attributes.get("NORMAL"), len(accessors), "NORMAL accessor")
            ]
            if (
                joints.component_type not in {5121, 5123}
                or joints.kind != "VEC4"
                or joints.normalized
                or weights.component_type != 5126
                or weights.kind != "VEC4"
                or normals.component_type != 5126
                or normals.kind != "VEC3"
                or joints.count != position.count
                or weights.count != position.count
                or normals.count != position.count
            ):
                raise ValueError(
                    "GLB skin vertex attributes do not match POSITION count"
                )
            _finite(normals, "NORMAL")
            for joint_value, weight_value in zip(
                joints.values(), weights.values(), strict=True
            ):
                if any(int(value) >= joint_count for value in joint_value):
                    raise ValueError("GLB JOINTS_0 references an unknown joint")
                if any(
                    not math.isfinite(float(value)) or float(value) < 0
                    for value in weight_value
                ):
                    raise ValueError("GLB WEIGHTS_0 contains invalid weights")
                if not math.isclose(
                    sum(float(value) for value in weight_value), 1.0, abs_tol=1e-3
                ):
                    raise ValueError("GLB WEIGHTS_0 weights are not normalized")
    return {
        "mesh_count": len(meshes),
        "primitive_count": primitive_count,
        "accessor_count": len(accessors),
        "binary_bytes": len(blob),
        "material_count": len(materials),
        "texture_count": len(textures),
        "image_count": len(images),
        "skin_count": len(skins),
    }


def validate_mesh_glb(path: Path, *, profile: str = "geometry") -> dict[str, int]:
    """Validate one atomic artifact before publication."""
    return validate_mesh_glb_bytes(path.read_bytes(), profile=profile)
