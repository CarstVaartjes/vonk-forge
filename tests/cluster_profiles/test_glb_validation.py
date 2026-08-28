from __future__ import annotations

import base64
import hashlib
import json
import math
import struct
import tempfile
import unittest
import zlib
from pathlib import Path

from cluster_profiles import glb_validation
from cluster_profiles.qualification_fixtures import FixtureError, _glb_metadata

ROOT = Path(__file__).resolve().parents[2]
CONTEXTS = ("platform",)
VALIDATOR_SHA256 = "56ce07a1bea9b97a5fb5d73b574d53ec0f3dbf19dd563f2151e45042f759d08a"


class Glb:
    def __init__(self) -> None:
        self.blob = bytearray()
        self.views: list[dict[str, object]] = []
        self.accessors: list[dict[str, object]] = []

    def view(self, value: bytes, *, target: int | None = None) -> int:
        while len(self.blob) % 4:
            self.blob.append(0)
        offset = len(self.blob)
        self.blob.extend(value)
        item: dict[str, object] = {
            "buffer": 0,
            "byteOffset": offset,
            "byteLength": len(value),
        }
        if target is not None:
            item["target"] = target
        self.views.append(item)
        return len(self.views) - 1

    def accessor(
        self,
        values: bytes,
        *,
        component_type: int,
        kind: str,
        count: int,
        target: int | None = None,
        minimum: list[float] | None = None,
        maximum: list[float] | None = None,
    ) -> int:
        item: dict[str, object] = {
            "bufferView": self.view(values, target=target),
            "componentType": component_type,
            "count": count,
            "type": kind,
        }
        if minimum is not None:
            item["min"] = minimum
        if maximum is not None:
            item["max"] = maximum
        self.accessors.append(item)
        return len(self.accessors) - 1

    def document(self, profile: str = "geometry") -> dict[str, object]:
        positions = ((0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0))
        position = self.accessor(
            struct.pack("<9f", *(item for point in positions for item in point)),
            component_type=5126,
            kind="VEC3",
            count=3,
            target=34962,
            minimum=[0.0, 0.0, 0.0],
            maximum=[1.0, 1.0, 0.0],
        )
        indices = self.accessor(
            struct.pack("<3I", 0, 1, 2),
            component_type=5125,
            kind="SCALAR",
            count=3,
            target=34963,
        )
        attributes: dict[str, int] = {"POSITION": position}
        primitive: dict[str, object] = {
            "attributes": attributes,
            "indices": indices,
            "mode": 4,
        }
        document: dict[str, object] = {
            "asset": {"version": "2.0"},
            "scene": 0,
            "scenes": [{"nodes": [0]}],
            "nodes": [{"mesh": 0}],
            "meshes": [{"primitives": [primitive]}],
        }
        if profile in {"textured", "textured-pbr"}:
            attributes["TEXCOORD_0"] = self.accessor(
                struct.pack("<6f", 0, 0, 1, 0, 0, 1),
                component_type=5126,
                kind="VEC2",
                count=3,
                target=34962,
            )

            def png_chunk(kind: bytes, payload: bytes) -> bytes:
                return (
                    struct.pack(">I", len(payload))
                    + kind
                    + payload
                    + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
                )

            ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
            image = (
                b"\x89PNG\r\n\x1a\n"
                + png_chunk(b"IHDR", ihdr)
                + png_chunk(b"IDAT", zlib.compress(b"\0\xff\0\0"))
                + png_chunk(b"IEND", b"")
            )
            image_views = [self.view(image)]
            if profile == "textured-pbr":
                image_views.append(self.view(image))
            document["images"] = [
                {"bufferView": view, "mimeType": "image/png"} for view in image_views
            ]
            document["textures"] = [
                {"source": index} for index in range(len(image_views))
            ]
            pbr: dict[str, object] = {"baseColorTexture": {"index": 0}}
            if profile == "textured-pbr":
                pbr["metallicRoughnessTexture"] = {"index": 1}
            document["materials"] = [{"pbrMetallicRoughness": pbr}]
            primitive["material"] = 0
        if profile == "skinned":
            normal = self.accessor(
                struct.pack("<9f", 0, 0, 1, 0, 0, 1, 0, 0, 1),
                component_type=5126,
                kind="VEC3",
                count=3,
                target=34962,
            )
            joints = self.accessor(
                struct.pack("<12H", *([0, 0, 0, 0] * 3)),
                component_type=5123,
                kind="VEC4",
                count=3,
                target=34962,
            )
            weights = self.accessor(
                struct.pack("<12f", *([1.0, 0.0, 0.0, 0.0] * 3)),
                component_type=5126,
                kind="VEC4",
                count=3,
                target=34962,
            )
            inverse = self.accessor(
                struct.pack("<16f", 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1),
                component_type=5126,
                kind="MAT4",
                count=1,
            )
            attributes.update(NORMAL=normal, JOINTS_0=joints, WEIGHTS_0=weights)
            document["nodes"] = [{"mesh": 0, "skin": 0}, {"name": "root"}]
            document["scenes"] = [{"nodes": [0, 1]}]
            document["skins"] = [
                {"inverseBindMatrices": inverse, "joints": [1], "skeleton": 1}
            ]
        return document

    def bytes(self, document: dict[str, object]) -> bytes:
        while len(self.blob) % 4:
            self.blob.append(0)
        document.setdefault("buffers", [{"byteLength": len(self.blob)}])
        document["bufferViews"] = self.views
        document["accessors"] = self.accessors
        encoded = json.dumps(document, separators=(",", ":"), allow_nan=False).encode()
        encoded += b" " * (-len(encoded) % 4)
        total = 12 + 8 + len(encoded) + 8 + len(self.blob)
        return (
            struct.pack("<4sII", b"glTF", 2, total)
            + struct.pack("<II", len(encoded), 0x4E4F534A)
            + encoded
            + struct.pack("<II", len(self.blob), 0x004E4942)
            + self.blob
        )


def load_validator(_context: str) -> object:
    return glb_validation


class ThreeDGlbValidationTests(unittest.TestCase):
    def test_qualification_wrapper_uses_the_byte_identical_validator(self) -> None:
        builder = Glb()
        metadata = _glb_metadata(builder.bytes(builder.document()), "triangle-mesh")
        self.assertEqual((metadata["mesh_count"], metadata["primitive_count"]), (1, 1))
        with self.assertRaisesRegex(FixtureError, "triangle-mesh structure is invalid"):
            _glb_metadata(b"not-a-glb", "triangle-mesh")

    def validate(self, document: dict[str, object], builder: Glb, profile: str) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "output.glb"
            path.write_bytes(builder.bytes(document))
            for context in CONTEXTS:
                with self.subTest(context=context, profile=profile):
                    load_validator(context).validate_mesh_glb(path, profile=profile)

    def rejected(
        self, document: dict[str, object], builder: Glb, profile: str, pattern: str
    ) -> None:
        self.raw_rejected(builder.bytes(document), profile, pattern)

    def raw_rejected(self, content: bytes, profile: str, pattern: str) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "output.glb"
            path.write_bytes(content)
            with self.assertRaisesRegex(ValueError, pattern):
                load_validator(CONTEXTS[0]).validate_mesh_glb(path, profile=profile)

    def test_all_copied_validators_accept_each_artifact_profile(self) -> None:
        for profile in ("geometry", "textured", "textured-pbr", "skinned"):
            with self.subTest(profile=profile):
                builder = Glb()
                self.validate(builder.document(profile), builder, profile)

    def test_accepts_a_decodable_extension_routed_webp(self) -> None:
        builder = Glb()
        document = builder.document("textured")
        webp = (
            b"RIFF\x1c\0\0\0WEBPVP8L\x0f\0\0\0/\x01@\0\0\x07\x10"
            b"\xfd\x8f\xfe\x07\x22\xa2\xff\x01\0"
        )
        document["images"][0] = {
            "bufferView": builder.view(webp),
            "mimeType": "image/webp",
        }
        document["textures"][0] = {"extensions": {"EXT_texture_webp": {"source": 0}}}
        document["extensionsUsed"] = ["EXT_texture_webp"]
        document["extensionsRequired"] = ["EXT_texture_webp"]
        self.validate(document, builder, "textured")

    def test_pbr_distinctness_uses_the_required_webp_sources(self) -> None:
        builder = Glb()
        document = builder.document("textured-pbr")
        webp = (
            b"RIFF\x1c\0\0\0WEBPVP8L\x0f\0\0\0/\x01@\0\0\x07\x10"
            b"\xfd\x8f\xfe\x07\x22\xa2\xff\x01\0"
        )
        document["images"].append(
            {"bufferView": builder.view(webp), "mimeType": "image/webp"}
        )
        for index, texture in enumerate(document["textures"]):
            texture["source"] = index
            texture["extensions"] = {"EXT_texture_webp": {"source": 2}}
        document["extensionsUsed"] = ["EXT_texture_webp"]
        document["extensionsRequired"] = ["EXT_texture_webp"]
        self.rejected(document, builder, "textured-pbr", "distinct embedded images")

    def test_validator_has_cross_repository_byte_identity(self) -> None:
        content = (ROOT / "src/cluster_profiles/glb_validation.py").read_bytes()
        self.assertEqual(hashlib.sha256(content).hexdigest(), VALIDATOR_SHA256)

    def test_rejects_out_of_range_indices_and_nonfinite_or_degenerate_positions(
        self,
    ) -> None:
        builder = Glb()
        document = builder.document()
        index_view = builder.views[builder.accessors[1]["bufferView"]]
        struct.pack_into("<I", builder.blob, int(index_view["byteOffset"]) + 8, 3)
        self.rejected(document, builder, "geometry", "exceeds POSITION")

        builder = Glb()
        document = builder.document()
        position_view = builder.views[builder.accessors[0]["bufferView"]]
        struct.pack_into("<f", builder.blob, int(position_view["byteOffset"]), math.nan)
        self.rejected(document, builder, "geometry", "non-finite")

        builder = Glb()
        document = builder.document()
        position_view = builder.views[builder.accessors[0]["bufferView"]]
        struct.pack_into(
            "<9f", builder.blob, int(position_view["byteOffset"]), *([0.0] * 9)
        )
        builder.accessors[0]["max"] = [0.0, 0.0, 0.0]
        self.rejected(document, builder, "geometry", "zero-size")

    def test_rejects_unreachable_mesh_and_dangling_attribute(self) -> None:
        builder = Glb()
        document = builder.document()
        document["nodes"] = [{"name": "empty"}]
        self.rejected(document, builder, "geometry", "does not reach")

        builder = Glb()
        document = builder.document()
        document["meshes"][0]["primitives"][0]["attributes"]["NORMAL"] = 99
        self.rejected(document, builder, "geometry", "NORMAL accessor")

    def test_profile_checks_reject_missing_pbr_texture_and_invalid_skin_weights(
        self,
    ) -> None:
        builder = Glb()
        document = builder.document("textured")
        self.rejected(document, builder, "textured-pbr", "metallicRoughnessTexture")

        builder = Glb()
        document = builder.document("skinned")
        weight_view = builder.views[builder.accessors[4]["bufferView"]]
        struct.pack_into("<f", builder.blob, int(weight_view["byteOffset"]), -1.0)
        self.rejected(document, builder, "skinned", "invalid weights")

    def test_rejects_duplicate_json_keys_and_nonzero_bin_padding(self) -> None:
        builder = Glb()
        content = builder.bytes(builder.document())
        json_length = struct.unpack_from("<I", content, 12)[0]
        json_body = content[20 : 20 + json_length].rstrip(b" ")
        duplicate = json_body[:-1] + b',"asset":{"version":"2.0"}}'
        duplicate += b" " * (-len(duplicate) % 4)
        binary = content[28 + json_length :]
        rebuilt = (
            struct.pack("<4sII", b"glTF", 2, 12 + 8 + len(duplicate) + 8 + len(binary))
            + struct.pack("<II", len(duplicate), 0x4E4F534A)
            + duplicate
            + struct.pack("<II", len(binary), 0x004E4942)
            + binary
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "output.glb"
            path.write_bytes(rebuilt)
            with self.assertRaisesRegex(ValueError, "duplicate key"):
                load_validator(CONTEXTS[0]).validate_mesh_glb(path)

        builder = Glb()
        document = builder.document()
        content = bytearray(builder.bytes(document))
        document["buffers"][0]["byteLength"] -= 1
        content = bytearray(builder.bytes(document))
        content[-1] = 1
        self.raw_rejected(bytes(content), "geometry", "padding bytes must be zero")

    def test_rejects_forbidden_json_padding_and_chunk_layouts(self) -> None:
        builder = Glb()
        content = builder.bytes(builder.document())
        json_length = struct.unpack_from("<I", content, 12)[0]
        body = content[20 : 20 + json_length].rstrip(b" ")
        remainder = content[20 + json_length :]
        overpadded = body + b" " * (4 + (-len(body) % 4))
        rebuilt = (
            struct.pack("<4sII", b"glTF", 2, 20 + len(overpadded) + len(remainder))
            + struct.pack("<II", len(overpadded), 0x4E4F534A)
            + overpadded
            + remainder
        )
        self.raw_rejected(rebuilt, "geometry", "padding must contain spaces only")

        nul = body + b"\0"
        nul += b" " * (-len(nul) % 4)
        rebuilt = (
            struct.pack("<4sII", b"glTF", 2, 20 + len(nul) + len(remainder))
            + struct.pack("<II", len(nul), 0x4E4F534A)
            + nul
            + remainder
        )
        self.raw_rejected(rebuilt, "geometry", "padding must contain spaces only")

        json_chunk = content[12 : 20 + json_length]
        bin_chunk = content[20 + json_length :]
        swapped = (
            struct.pack("<4sII", b"glTF", 2, len(content)) + bin_chunk + json_chunk
        )
        self.raw_rejected(swapped, "geometry", "first chunk is not JSON")

    def test_rejects_non_space_json_padding_and_finite_overflow(self) -> None:
        builder = Glb()
        content = builder.bytes(builder.document())
        json_length = struct.unpack_from("<I", content, 12)[0]
        body = content[20 : 20 + json_length].rstrip(b" ")
        remainder = content[20 + json_length :]

        newline_padded = body + b"\n"
        newline_padded += b" " * (-len(newline_padded) % 4)
        rebuilt = (
            struct.pack("<4sII", b"glTF", 2, 20 + len(newline_padded) + len(remainder))
            + struct.pack("<II", len(newline_padded), 0x4E4F534A)
            + newline_padded
            + remainder
        )
        self.raw_rejected(rebuilt, "geometry", "padding must contain spaces only")

        overflow = body[:-1] + b',"extras":{"overflow":1e999}}'
        overflow += b" " * (-len(overflow) % 4)
        rebuilt = (
            struct.pack("<4sII", b"glTF", 2, 20 + len(overflow) + len(remainder))
            + struct.pack("<II", len(overflow), 0x4E4F534A)
            + overflow
            + remainder
        )
        self.raw_rejected(rebuilt, "geometry", "non-finite number")

    def test_rejects_boolean_misaligned_strided_and_overrun_accessors(self) -> None:
        builder = Glb()
        document = builder.document()
        builder.views[0]["buffer"] = False
        self.rejected(document, builder, "geometry", "bufferView buffer index")

        builder = Glb()
        document = builder.document()
        builder.views[0]["byteOffset"] = 1
        self.rejected(document, builder, "geometry", "not aligned")

        builder = Glb()
        document = builder.document()
        builder.views[builder.accessors[1]["bufferView"]]["byteStride"] = 4
        self.rejected(document, builder, "geometry", "indices must be unsigned")

        builder = Glb()
        document = builder.document()
        builder.accessors[1]["count"] = 4
        self.rejected(document, builder, "geometry", "exceeds its bufferView")

        builder = Glb()
        document = builder.document()
        builder.accessors[0]["normalized"] = True
        self.rejected(document, builder, "geometry", "normalized accessor")

        builder = Glb()
        document = builder.document()
        builder.accessors[1]["componentType"] = 5123
        builder.accessors[1]["normalized"] = True
        self.rejected(document, builder, "geometry", "triangle indices")

    def test_rejects_auxiliary_attribute_count_and_scene_graph_corruption(self) -> None:
        builder = Glb()
        document = builder.document()
        normal = builder.accessor(
            struct.pack("<3f", 0, 0, 1),
            component_type=5126,
            kind="VEC3",
            count=1,
            target=34962,
        )
        document["meshes"][0]["primitives"][0]["attributes"]["NORMAL"] = normal
        self.rejected(document, builder, "geometry", "attribute counts")

        builder = Glb()
        document = builder.document()
        document["nodes"] = [
            {"mesh": 0, "children": [1]},
            {"children": [2]},
            {"children": [1]},
        ]
        document["scenes"] = [{"nodes": [0, 2]}]
        self.rejected(document, builder, "geometry", "more than one parent")

        builder = Glb()
        document = builder.document()
        document["scenes"] = [{"nodes": [0, 0]}]
        self.rejected(document, builder, "geometry", "roots are invalid")

        builder = Glb()
        document = builder.document()
        document["nodes"][0]["matrix"] = [1.0] * 16
        document["nodes"][0]["translation"] = [0.0, 0.0, 0.0]
        self.rejected(document, builder, "geometry", "combine matrix and TRS")

        builder = Glb()
        document = builder.document()
        document["nodes"][0]["scale"] = [1.0, 0.0, 1.0]
        self.rejected(document, builder, "geometry", "scale collapses")

        builder = Glb()
        document = builder.document()
        document["nodes"][0]["matrix"] = [0.0] * 16
        self.rejected(document, builder, "geometry", "matrix")

        builder = Glb()
        document = builder.document()
        document["nodes"][0]["matrix"] = [
            1.0,
            0.0,
            0.0,
            0.0,
            1.0,
            1.0,
            0.0,
            0.0,
            0.0,
            0.0,
            1.0,
            0.0,
            0.0,
            0.0,
            0.0,
            1.0,
        ]
        self.rejected(document, builder, "geometry", "unsupported shear")

        builder = Glb()
        document = builder.document()
        custom = builder.accessor(
            struct.pack("<6I", *range(6)),
            component_type=5125,
            kind="VEC2",
            count=3,
            target=34962,
        )
        document["meshes"][0]["primitives"][0]["attributes"]["_CUSTOM"] = custom
        self.rejected(document, builder, "geometry", "UNSIGNED_INT")

        builder = Glb()
        document = builder.document()
        document["meshes"][0]["primitives"][0]["attributes"]["NORMAL"] = len(
            builder.accessors
        )
        builder.accessors.append(
            {
                "bufferView": builder.accessors[0]["bufferView"],
                "componentType": 5126,
                "count": 3,
                "type": "VEC3",
            }
        )
        self.rejected(document, builder, "geometry", "must declare byteStride")

        builder = Glb()
        document = builder.document()
        document["extensionsUsed"] = ["VENDOR_unknown"]
        document["extensionsRequired"] = ["VENDOR_unknown"]
        self.rejected(
            document, builder, "geometry", "extensions are invalid or unsupported"
        )

    def test_rejects_invalid_roots_in_every_declared_scene_for_all_profiles(
        self,
    ) -> None:
        for profile in ("geometry", "textured", "textured-pbr", "skinned"):
            with self.subTest(profile=profile, corruption="invalid secondary root"):
                builder = Glb()
                document = builder.document(profile)
                document["scenes"].append({"nodes": [999]})
                self.rejected(document, builder, profile, "scene node index is invalid")

            with self.subTest(profile=profile, corruption="duplicate secondary roots"):
                builder = Glb()
                document = builder.document(profile)
                document["scenes"].append({"nodes": [0, 0]})
                self.rejected(document, builder, profile, "scene roots are invalid")

            with self.subTest(profile=profile, corruption="secondary root is a child"):
                builder = Glb()
                document = builder.document(profile)
                parent = len(document["nodes"])
                child = parent + 1
                document["nodes"].extend([{"children": [child]}, {"name": "child"}])
                document["scenes"].append({"nodes": [parent, child]})
                self.rejected(document, builder, profile, "scene roots are invalid")

    def test_rejects_unsupported_scene_features_and_material_booleans(self) -> None:
        for profile in ("geometry", "textured", "textured-pbr", "skinned"):
            with self.subTest(profile=profile, corruption="boolean camera"):
                builder = Glb()
                document = builder.document(profile)
                document["nodes"][0]["camera"] = False
                self.rejected(
                    document, builder, profile, "camera nodes are not supported"
                )

            with self.subTest(profile=profile, corruption="boolean material"):
                builder = Glb()
                document = builder.document(profile)
                document["meshes"][0]["primitives"][0]["material"] = False
                self.rejected(
                    document, builder, profile, "primitive material index is invalid"
                )

            with self.subTest(profile=profile, corruption="boolean morph accessor"):
                builder = Glb()
                document = builder.document(profile)
                document["meshes"][0]["primitives"][0]["targets"] = [
                    {"POSITION": False}
                ]
                self.rejected(
                    document, builder, profile, "morph targets are not supported"
                )

            with self.subTest(profile=profile, corruption="camera collection"):
                builder = Glb()
                document = builder.document(profile)
                document["cameras"] = [{}]
                self.rejected(document, builder, profile, "cameras are not supported")

            with self.subTest(profile=profile, corruption="animation collection"):
                builder = Glb()
                document = builder.document(profile)
                document["animations"] = [{}]
                self.rejected(
                    document, builder, profile, "animations are not supported"
                )

    def test_rejects_nonfinite_normal_for_all_profiles(self) -> None:
        for profile in ("geometry", "textured", "textured-pbr", "skinned"):
            with self.subTest(profile=profile):
                builder = Glb()
                document = builder.document(profile)
                attributes = document["meshes"][0]["primitives"][0]["attributes"]
                normal = attributes.get("NORMAL")
                if normal is None:
                    normal = builder.accessor(
                        struct.pack("<9f", *([0.0, 0.0, 1.0] * 3)),
                        component_type=5126,
                        kind="VEC3",
                        count=3,
                        target=34962,
                    )
                    attributes["NORMAL"] = normal
                normal_view = builder.views[builder.accessors[normal]["bufferView"]]
                struct.pack_into(
                    "<f", builder.blob, int(normal_view["byteOffset"]), math.nan
                )
                self.rejected(
                    document, builder, profile, "NORMAL accessor contains non-finite"
                )

    def test_rejects_texture_coordinate_image_and_extension_corruption(self) -> None:
        builder = Glb()
        document = builder.document("textured-pbr")
        document["materials"][0]["pbrMetallicRoughness"]["baseColorTexture"][
            "texCoord"
        ] = 1
        self.rejected(document, builder, "textured-pbr", "must use TEXCOORD_0")

        builder = Glb()
        document = builder.document("textured-pbr")
        document["images"][0]["mimeType"] = "image/jpeg"
        self.rejected(document, builder, "textured-pbr", "does not match its MIME")

        builder = Glb()
        document = builder.document("textured-pbr")
        document["extensionsUsed"] = ["EXT_texture_webp"]
        self.rejected(document, builder, "textured-pbr", "used and required")

        builder = Glb()
        document = builder.document("textured-pbr")
        document["materials"][0]["pbrMetallicRoughness"]["baseColorTexture"][
            "texCoord"
        ] = False
        self.rejected(document, builder, "textured-pbr", "must use TEXCOORD_0")

        builder = Glb()
        document = builder.document("textured-pbr")
        document["images"][0]["mimeType"] = "image/webp"
        document["textures"][0] = {"source": 0}
        self.rejected(document, builder, "textured-pbr", "core texture source")

        builder = Glb()
        document = builder.document("textured-pbr")
        image_view = document["images"][0]["bufferView"]
        builder.views[image_view]["byteStride"] = 4
        self.rejected(document, builder, "textured-pbr", "byteStride bufferView")

        builder = Glb()
        document = builder.document("textured-pbr")
        image_view = document["images"][0]["bufferView"]
        raw_view = builder.views[image_view]
        start = int(raw_view["byteOffset"])
        length = int(raw_view["byteLength"])
        bogus_jpeg = b"\xff\xd8\xff\xe0\x00\x04xx" + b"x" * (length - 10) + b"\xff\xd9"
        self.assertEqual(len(bogus_jpeg), length)
        builder.blob[start : start + length] = bogus_jpeg
        document["images"][0]["mimeType"] = "image/jpeg"
        document["textures"][0] = {"source": 0}
        self.rejected(document, builder, "textured-pbr", "does not match its MIME")

        builder = Glb()
        document = builder.document("textured-pbr")
        png_header = b"\x89PNG\r\n\x1a\n" + b"\0\0\0\rIHDR" + b"\0" * 8
        document["images"][0]["bufferView"] = builder.view(png_header)
        document["images"][0]["mimeType"] = "image/png"
        document["textures"][0] = {"source": 0}
        self.rejected(document, builder, "textured-pbr", "does not match its MIME")

        builder = Glb()
        document = builder.document("textured-pbr")
        webp_header = b"RIFF\x08\0\0\0WEBPVP8 "
        document["images"][0]["bufferView"] = builder.view(webp_header)
        document["images"][0]["mimeType"] = "image/webp"
        document["textures"][0] = {"extensions": {"EXT_texture_webp": {"source": 0}}}
        document["extensionsUsed"] = ["EXT_texture_webp"]
        document["extensionsRequired"] = ["EXT_texture_webp"]
        self.rejected(document, builder, "textured-pbr", "does not match its MIME")

        builder = Glb()
        document = builder.document("textured-pbr")
        valid_webp = (
            b"RIFF\x1c\0\0\0WEBPVP8L\x0f\0\0\0/\x01@\0\0\x07\x10"
            b"\xfd\x8f\xfe\x07\x22\xa2\xff\x01\0"
        )
        webp_chunks = valid_webp[12:]
        duplicate_webp = (
            b"RIFF"
            + struct.pack("<I", 4 + 2 * len(webp_chunks))
            + b"WEBP"
            + webp_chunks
            + webp_chunks
        )
        document["images"][0]["bufferView"] = builder.view(duplicate_webp)
        document["images"][0]["mimeType"] = "image/webp"
        document["textures"][0] = {"extensions": {"EXT_texture_webp": {"source": 0}}}
        document["extensionsUsed"] = ["EXT_texture_webp"]
        document["extensionsRequired"] = ["EXT_texture_webp"]
        self.rejected(document, builder, "textured-pbr", "does not match its MIME")

        builder = Glb()
        document = builder.document("textured-pbr")
        jpeg = base64.b64decode(
            "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8U"
            "HRofHh0aHBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/2wBDAQkJCQwLDBgN"
            "DRgyIRwhMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIy"
            "MjIyMjL/wAARCAABAAEDASIAAhEBAxEB/8QAFQABAQAAAAAAAAAAAAAAAAAAAAb/xAAU"
            "EAEAAAAAAAAAAAAAAAAAAAAA/8QAFQEBAQAAAAAAAAAAAAAAAAAABgf/xAAUEQEAAAAA"
            "AAAAAAAAAAAAAAAA/9oADAMBAAIRAxEAPwCLAGVxf//Z"
        )
        document["images"][0]["bufferView"] = builder.view(jpeg + jpeg)
        document["images"][0]["mimeType"] = "image/jpeg"
        document["textures"][0] = {"source": 0}
        self.rejected(document, builder, "textured-pbr", "does not match its MIME")

        builder = Glb()
        document = builder.document("textured-pbr")
        fake_webp = (
            b"RIFF\x18\0\0\0WEBPVP8 \x0b\0\0\0\x20\0\0\x9d\x01\x2a\x01\0\x01\0\0\0"
        )
        document["images"][0]["bufferView"] = builder.view(fake_webp)
        document["images"][0]["mimeType"] = "image/webp"
        document["textures"][0] = {"extensions": {"EXT_texture_webp": {"source": 0}}}
        document["extensionsUsed"] = ["EXT_texture_webp"]
        document["extensionsRequired"] = ["EXT_texture_webp"]
        self.rejected(document, builder, "textured-pbr", "does not match its MIME")

        def png_chunk(kind: bytes, payload: bytes) -> bytes:
            return (
                struct.pack(">I", len(payload))
                + kind
                + payload
                + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
            )

        builder = Glb()
        document = builder.document("textured-pbr")
        ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
        critical_png = (
            b"\x89PNG\r\n\x1a\n"
            + png_chunk(b"IHDR", ihdr)
            + png_chunk(b"ABCD", b"")
            + png_chunk(b"IDAT", zlib.compress(b"\0\0\0\0"))
            + png_chunk(b"IEND", b"")
        )
        document["images"][0]["bufferView"] = builder.view(critical_png)
        document["images"][0]["mimeType"] = "image/png"
        document["textures"][0] = {"source": 0}
        self.rejected(document, builder, "textured-pbr", "does not match its MIME")

        builder = Glb()
        document = builder.document("textured-pbr")
        jpeg = (
            b"\xff\xd8\xff\xdb\x00\x43\x00"
            + b"\0" * 64
            + b"\xff\xc4\x00\x04\x00\x00"
            + b"\xff\xc0\x00\x0b\x08\x00\x01\x00\x01\x01\x01\x11\x00"
            + b"\xff\xda\x00\x08\x01\x01\x00\x00\x3f\x00\x00\xff\xd9"
        )
        document["images"][0]["bufferView"] = builder.view(jpeg)
        document["images"][0]["mimeType"] = "image/jpeg"
        document["textures"][0] = {"source": 0}
        self.rejected(document, builder, "textured-pbr", "does not match its MIME")

        builder = Glb()
        document = builder.document("textured-pbr")
        image_view = document["images"][0]["bufferView"]
        document["meshes"][0]["primitives"][0]["attributes"]["_IMAGE_ALIAS"] = len(
            builder.accessors
        )
        builder.accessors.append(
            {
                "bufferView": image_view,
                "componentType": 5126,
                "count": 3,
                "type": "VEC2",
            }
        )
        self.rejected(document, builder, "textured-pbr", "must not be shared")

        builder = Glb()
        document = builder.document("textured-pbr")
        image_view = document["images"][0]["bufferView"]
        builder.views[image_view]["target"] = 34962
        self.rejected(document, builder, "textured-pbr", "byteStride or target")

    def test_rejects_nonunit_rotation_and_swapped_buffer_targets(self) -> None:
        builder = Glb()
        document = builder.document()
        document["nodes"][0]["rotation"] = [0.0, 0.0, 0.0, 2.0]
        self.rejected(document, builder, "geometry", "quaternion is not normalized")

        builder = Glb()
        document = builder.document()
        position_view = builder.accessors[0]["bufferView"]
        index_view = builder.accessors[1]["bufferView"]
        builder.views[position_view]["target"] = 34963
        builder.views[index_view]["target"] = 34962
        self.rejected(document, builder, "geometry", "target must be ARRAY_BUFFER")

    def test_rejects_skin_binding_joint_and_inverse_bind_corruption(self) -> None:
        builder = Glb()
        document = builder.document("skinned")
        document["nodes"][0].pop("skin")
        self.rejected(document, builder, "skinned", "must bind a skin")

        builder = Glb()
        document = builder.document("skinned")
        document["skins"].append(dict(document["skins"][0]))
        self.rejected(document, builder, "skinned", "exactly one skin")

        builder = Glb()
        document = builder.document("skinned")
        document["skins"][0]["joints"] = [1, 1]
        self.rejected(document, builder, "skinned", "duplicate joints")

        builder = Glb()
        document = builder.document("skinned")
        builder.accessors[5]["type"] = "VEC4"
        self.rejected(document, builder, "skinned", "inverseBindMatrices")

        builder = Glb()
        document = builder.document("skinned")
        joint_view = builder.views[builder.accessors[3]["bufferView"]]
        struct.pack_into("<H", builder.blob, int(joint_view["byteOffset"]), 1)
        self.rejected(document, builder, "skinned", "unknown joint")

        builder = Glb()
        document = builder.document("skinned")
        document["scenes"] = [{"nodes": [0]}]
        self.rejected(document, builder, "skinned", "skin joint is unreachable")

        builder = Glb()
        document = builder.document("skinned")
        builder.accessors[3]["normalized"] = True
        self.rejected(
            document, builder, "skinned", "joint accessor type or normalization"
        )


if __name__ == "__main__":
    unittest.main()
