"""Textures for glb validation."""

from __future__ import annotations

from .common import IMAGE_MIME_TYPES, Accessor, _array, _index, _object
from .images import _image_bytes


def _textures(
    document: dict[str, object],
    blob: bytes,
    views: list[tuple[int, int]],
    accessors: list[Accessor],
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    images = [_object(item, "image") for item in _array(document, "images")]
    image_payloads: list[tuple[int, str]] = []
    for image in images:
        if "uri" in image:
            raise ValueError("GLB images must be embedded")
        view = _index(image.get("bufferView"), len(views), "image bufferView")
        if any(accessor.view_index == view for accessor in accessors):
            raise ValueError("GLB image bufferView must not be shared with an accessor")
        raw_view = _object(
            _array(document, "bufferViews", required=True)[view], "bufferView"
        )
        if raw_view.get("byteStride") is not None or raw_view.get("target") is not None:
            raise ValueError(
                "GLB image bufferView must not declare byteStride or target"
            )
        mime_type = image.get("mimeType")
        if mime_type not in IMAGE_MIME_TYPES:
            raise ValueError("GLB embedded image MIME type is unsupported")
        image_payloads.append((view, str(mime_type)))
    textures = [_object(item, "texture") for item in _array(document, "textures")]
    samplers = [_object(item, "sampler") for item in _array(document, "samplers")]
    sampler_values = {
        "magFilter": {9728, 9729},
        "minFilter": {9728, 9729, 9984, 9985, 9986, 9987},
        "wrapS": {33071, 33648, 10497},
        "wrapT": {33071, 33648, 10497},
    }
    for sampler in samplers:
        for name, allowed in sampler_values.items():
            if name in sampler and sampler[name] not in allowed:
                raise ValueError(f"GLB sampler {name} is invalid")
    webp_used = False
    for texture in textures:
        source = texture.get("source")
        core_source: int | None = None
        if source is not None:
            core_source = _index(source, len(images), "texture source")
            if images[core_source].get("mimeType") not in {"image/jpeg", "image/png"}:
                raise ValueError("GLB core texture source must use a JPEG or PNG image")
        extension = texture.get("extensions")
        extension_source: int | None = None
        if isinstance(extension, dict) and "EXT_texture_webp" in extension:
            webp = extension.get("EXT_texture_webp")
            if not isinstance(webp, dict):
                raise ValueError("GLB EXT_texture_webp must be an object")
            extension_source = _index(
                webp.get("source"), len(images), "EXT_texture_webp source"
            )
            if images[extension_source].get("mimeType") != "image/webp":
                raise ValueError("GLB EXT_texture_webp source must use a WebP image")
            webp_used = True
        if core_source is None and extension_source is None:
            raise ValueError("GLB texture source index is invalid")
        if "sampler" in texture:
            _index(texture["sampler"], len(samplers), "texture sampler")
    if webp_used:
        used = document.get("extensionsUsed")
        required = document.get("extensionsRequired")
        if (
            not isinstance(used, list)
            or "EXT_texture_webp" not in used
            or not isinstance(required, list)
            or "EXT_texture_webp" not in required
        ):
            raise ValueError(
                "GLB WebP textures must declare EXT_texture_webp as used and required"
            )
    for view, mime_type in image_payloads:
        _image_bytes(blob, views[view], mime_type)
    return images, textures


def _texture_index(value: object, textures: list[dict[str, object]], name: str) -> int:
    info = _object(value, name)
    tex_coord = info.get("texCoord", 0)
    if isinstance(tex_coord, bool) or not isinstance(tex_coord, int) or tex_coord != 0:
        raise ValueError(f"GLB {name} must use TEXCOORD_0")
    return _index(info.get("index"), len(textures), name)


def _texture_source(texture: dict[str, object], image_count: int) -> int:
    source: object | None = None
    extension = texture.get("extensions")
    if isinstance(extension, dict):
        webp = extension.get("EXT_texture_webp")
        if isinstance(webp, dict):
            source = webp.get("source")
    if source is None:
        source = texture.get("source")
    return _index(source, image_count, "texture source")


def _validate_materials(
    document: dict[str, object], textures: list[dict[str, object]]
) -> list[dict[str, object]]:
    materials = [_object(item, "material") for item in _array(document, "materials")]
    for material in materials:
        pbr = material.get("pbrMetallicRoughness")
        if pbr is not None:
            pbr = _object(pbr, "material pbrMetallicRoughness")
            for name in ("baseColorTexture", "metallicRoughnessTexture"):
                if name in pbr:
                    _texture_index(pbr[name], textures, name)
        for name in ("normalTexture", "occlusionTexture", "emissiveTexture"):
            if name in material:
                _texture_index(material[name], textures, name)
    return materials
