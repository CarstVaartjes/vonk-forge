"""Meshes for glb validation."""

from __future__ import annotations

import math

from .common import Accessor, _array, _finite, _finite_numbers, _index, _object


def _reachable_meshes(
    document: dict[str, object], mesh_count: int, skin_count: int
) -> tuple[set[int], set[int]]:
    nodes = [_object(item, "node") for item in _array(document, "nodes", required=True)]
    children: list[list[int]] = []
    parents = [0] * len(nodes)
    for node in nodes:
        if "camera" in node:
            raise ValueError(
                "GLB camera nodes are not supported by this artifact contract"
            )
        if "weights" in node:
            raise ValueError(
                "GLB morph weights are not supported by this artifact contract"
            )
        matrix = node.get("matrix")
        transform_fields = ("translation", "rotation", "scale")
        if matrix is not None and any(field in node for field in transform_fields):
            raise ValueError("GLB node cannot combine matrix and TRS transforms")
        transform_sizes = {"matrix": 16, "translation": 3, "rotation": 4, "scale": 3}
        for field, size in transform_sizes.items():
            value = node.get(field)
            if value is not None and (
                not isinstance(value, list)
                or len(value) != size
                or any(
                    isinstance(item, bool)
                    or not isinstance(item, (int, float))
                    or not math.isfinite(float(item))
                    for item in value
                )
            ):
                raise ValueError(f"GLB node {field} transform is invalid")
        rotation = _finite_numbers(node.get("rotation"))
        if rotation is not None and not math.isclose(
            sum(item**2 for item in rotation),
            1.0,
            rel_tol=1e-6,
            abs_tol=1e-6,
        ):
            raise ValueError("GLB node rotation quaternion is not normalized")
        scale = _finite_numbers(node.get("scale"))
        if scale is not None and any(abs(item) <= 1e-12 for item in scale):
            raise ValueError("GLB node scale collapses reachable geometry")
        matrix_values = _finite_numbers(matrix)
        if matrix_values is not None:
            if any(
                abs(matrix_values[index]) > 1e-12 for index in (3, 7, 11)
            ) or not math.isclose(matrix_values[15], 1.0, abs_tol=1e-12):
                raise ValueError("GLB node matrix is not an affine transform")
            determinant = (
                matrix_values[0]
                * (
                    matrix_values[5] * matrix_values[10]
                    - matrix_values[6] * matrix_values[9]
                )
                - matrix_values[4]
                * (
                    matrix_values[1] * matrix_values[10]
                    - matrix_values[2] * matrix_values[9]
                )
                + matrix_values[8]
                * (
                    matrix_values[1] * matrix_values[6]
                    - matrix_values[2] * matrix_values[5]
                )
            )
            if abs(determinant) <= 1e-12:
                raise ValueError("GLB node matrix collapses reachable geometry")
            basis = (
                tuple(matrix_values[index] for index in (0, 1, 2)),
                tuple(matrix_values[index] for index in (4, 5, 6)),
                tuple(matrix_values[index] for index in (8, 9, 10)),
            )
            lengths = [
                math.sqrt(sum(item * item for item in column)) for column in basis
            ]
            for first, second in ((0, 1), (0, 2), (1, 2)):
                dot = sum(basis[first][axis] * basis[second][axis] for axis in range(3))
                if not math.isclose(
                    dot / (lengths[first] * lengths[second]), 0.0, abs_tol=1e-6
                ):
                    raise ValueError("GLB node matrix contains unsupported shear")
        raw_children = node.get("children", [])
        if not isinstance(raw_children, list):
            raise ValueError("GLB node children must be an array")  # noqa: TRY004
        node_children = [
            _index(item, len(nodes), "node child") for item in raw_children
        ]
        if len(node_children) != len(set(node_children)):
            raise ValueError("GLB node contains duplicate children")
        children.append(node_children)
        for child in node_children:
            parents[child] += 1
            if parents[child] > 1:
                raise ValueError("GLB node has more than one parent")
        if "mesh" in node:
            _index(node["mesh"], mesh_count, "node mesh")
        if "skin" in node:
            _index(node["skin"], skin_count, "node skin")
    state = [0] * len(nodes)

    def visit(index: int) -> None:
        if state[index] == 1:
            raise ValueError("GLB node graph contains a cycle")
        if state[index] == 2:
            return
        state[index] = 1
        for child in children[index]:
            visit(child)
        state[index] = 2

    for index in range(len(nodes)):
        visit(index)
    scenes = [
        _object(item, "scene") for item in _array(document, "scenes", required=True)
    ]
    scene_roots: list[list[int]] = []
    for scene in scenes:
        roots = scene.get("nodes", [])
        if not isinstance(roots, list):
            raise ValueError("GLB scene nodes must be an array")  # noqa: TRY004
        validated_roots = [_index(item, len(nodes), "scene node") for item in roots]
        if len(validated_roots) != len(set(validated_roots)) or any(
            parents[item] for item in validated_roots
        ):
            raise ValueError("GLB scene roots are invalid")
        scene_roots.append(validated_roots)
    scene_index = _index(document.get("scene", 0), len(scenes), "default scene")
    pending = scene_roots[scene_index]
    if not pending:
        raise ValueError("GLB default scene has no root nodes")
    reachable: set[int] = set()
    seen: set[int] = set()
    while pending:
        index = pending.pop()
        if index in seen:
            continue
        seen.add(index)
        node = nodes[index]
        if "mesh" in node:
            reachable.add(_index(node["mesh"], mesh_count, "node mesh"))
        pending.extend(children[index])
    if not reachable:
        raise ValueError("GLB default scene does not reach a mesh")
    return reachable, seen


def _triangle_primitive(
    primitive: dict[str, object], accessors: list[Accessor]
) -> tuple[Accessor, dict[str, object]]:
    if primitive.get("mode", 4) != 4:
        raise ValueError("GLB mesh primitive is not TRIANGLES")
    if "targets" in primitive:
        raise ValueError(
            "GLB morph targets are not supported by this artifact contract"
        )
    attributes = _object(primitive.get("attributes"), "primitive attributes")
    for name, value in attributes.items():
        attribute = accessors[_index(value, len(accessors), f"{name} accessor")]
        if attribute.start % 4 or attribute.stride % 4:
            raise ValueError("GLB vertex attributes must be four-byte aligned")
        if attribute.target not in {None, 34962}:
            raise ValueError(
                "GLB vertex attribute bufferView target must be ARRAY_BUFFER"
            )
        if attribute.component_type == 5125:
            raise ValueError(
                "GLB vertex attributes must not use UNSIGNED_INT components"
            )
        if attribute.component_type == 5126:
            _finite(attribute, name)
        if name == "NORMAL" and (
            attribute.component_type != 5126
            or attribute.kind != "VEC3"
            or attribute.normalized
        ):
            raise ValueError("GLB NORMAL accessor must be unnormalized FLOAT VEC3")
        if name == "TANGENT" and (
            attribute.component_type != 5126
            or attribute.kind != "VEC4"
            or attribute.normalized
        ):
            raise ValueError("GLB TANGENT accessor must be unnormalized FLOAT VEC4")
        if name.startswith("TEXCOORD_") and (
            attribute.kind != "VEC2"
            or attribute.component_type not in {5121, 5123, 5126}
            or (attribute.component_type != 5126 and not attribute.normalized)
        ):
            raise ValueError("GLB texture-coordinate accessor type is invalid")
        if name.startswith("JOINTS_") and (
            attribute.kind != "VEC4"
            or attribute.component_type not in {5121, 5123}
            or attribute.normalized
        ):
            raise ValueError("GLB joint accessor type or normalization is invalid")
        if name.startswith("COLOR_") and (
            attribute.kind not in {"VEC3", "VEC4"}
            or attribute.component_type not in {5121, 5123, 5126}
            or (attribute.component_type != 5126 and not attribute.normalized)
        ):
            raise ValueError("GLB color accessor type or normalization is invalid")
        if name.startswith("WEIGHTS_") and (
            attribute.kind != "VEC4"
            or attribute.component_type not in {5121, 5123, 5126}
            or (attribute.component_type != 5126 and not attribute.normalized)
        ):
            raise ValueError("GLB weight accessor type or normalization is invalid")
        if not name.startswith("_") and name not in {
            "POSITION",
            "NORMAL",
            "TANGENT",
            "TEXCOORD_0",
            "TEXCOORD_1",
            "COLOR_0",
            "JOINTS_0",
            "WEIGHTS_0",
        }:
            raise ValueError(f"GLB vertex attribute semantic is unsupported: {name}")
    position = accessors[
        _index(attributes.get("POSITION"), len(accessors), "POSITION accessor")
    ]
    if position.component_type != 5126 or position.kind != "VEC3" or position.count < 3:
        raise ValueError(
            "GLB POSITION accessor must be FLOAT VEC3 with at least three vertices"
        )
    minimum = [math.inf, math.inf, math.inf]
    maximum = [-math.inf, -math.inf, -math.inf]
    for value in position.values():
        if any(not math.isfinite(float(component)) for component in value):
            raise ValueError("GLB POSITION accessor contains non-finite coordinates")
        for axis in range(3):
            minimum[axis] = min(minimum[axis], float(value[axis]))
            maximum[axis] = max(maximum[axis], float(value[axis]))
    if position.minimum is None or position.maximum is None:
        raise ValueError("GLB POSITION accessor must declare min and max bounds")
    for declared, actual in ((position.minimum, minimum), (position.maximum, maximum)):
        if any(
            not math.isclose(declared[i], actual[i], rel_tol=1e-6, abs_tol=1e-7)
            for i in range(3)
        ):
            raise ValueError(
                "GLB POSITION accessor bounds do not match its coordinates"
            )
    if max(maximum[axis] - minimum[axis] for axis in range(3)) <= 1e-8:
        raise ValueError("GLB mesh has a zero-size position extent")
    indices = accessors[
        _index(primitive.get("indices"), len(accessors), "indices accessor")
    ]
    if indices.target not in {None, 34963}:
        raise ValueError("GLB index bufferView target must be ELEMENT_ARRAY_BUFFER")
    if (
        indices.component_type not in {5121, 5123, 5125}
        or indices.kind != "SCALAR"
        or indices.count < 3
        or indices.count % 3
        or indices.interleaved
        or indices.normalized
    ):
        raise ValueError("GLB indices must be unsigned SCALAR triangle indices")
    for name, value in attributes.items():
        attribute = accessors[_index(value, len(accessors), f"{name} accessor")]
        if attribute.count != position.count:
            raise ValueError(
                "GLB primitive vertex attribute counts do not match POSITION"
            )
    nondegenerate = False
    triangle: list[int] = []
    for raw in indices.values():
        index = int(raw[0])
        if not 0 <= index < position.count:
            raise ValueError("GLB triangle index exceeds POSITION count")
        triangle.append(index)
        if len(triangle) == 3:
            if len(set(triangle)) == 3:
                a, b, c = (position.value(item) for item in triangle)
                ab = tuple(float(b[i]) - float(a[i]) for i in range(3))
                ac = tuple(float(c[i]) - float(a[i]) for i in range(3))
                cross = (
                    ab[1] * ac[2] - ab[2] * ac[1],
                    ab[2] * ac[0] - ab[0] * ac[2],
                    ab[0] * ac[1] - ab[1] * ac[0],
                )
                nondegenerate |= sum(value * value for value in cross) > 1e-20
            triangle.clear()
    if not nondegenerate:
        raise ValueError("GLB mesh contains no finite nondegenerate triangle")
    return position, attributes
