"""Validate the current portable NAS setup distribution before publication."""

from __future__ import annotations

import configparser
import email.parser
import io
import re
import zipfile
from pathlib import PurePosixPath

WHEEL_NAME = re.compile(
    r"vonk_nas_setup-([0-9]+[.][0-9]+[.][0-9]+)-py3-none-any[.]whl\Z"
)


def validate_wheel(filename: str, content: bytes) -> None:
    match = WHEEL_NAME.fullmatch(filename)
    if match is None:
        raise ValueError("NAS setup wheel filename is invalid")
    metadata_root = f"vonk_nas_setup-{match.group(1)}.dist-info"
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            members = archive.infolist()
            if sum(member.file_size for member in members) > 64 * 1024 * 1024:
                raise ValueError("NAS setup wheel expanded contents exceed 64 MiB")
            if any(member.flag_bits & 1 for member in members):
                raise ValueError("NAS setup wheel must not contain encrypted members")
            names = archive.namelist()
            if len(names) != len(set(names)) or any(
                PurePosixPath(name).is_absolute() or ".." in PurePosixPath(name).parts
                for name in names
            ):
                raise ValueError("NAS setup wheel members are unsafe")
            metadata = email.parser.BytesParser().parsebytes(
                archive.read(metadata_root + "/METADATA")
            )
            wheel = email.parser.BytesParser().parsebytes(
                archive.read(metadata_root + "/WHEEL")
            )
            if (
                metadata["Name"] != "vonk-nas-setup"
                or metadata["Version"] != match.group(1)
                or wheel["Root-Is-Purelib"] != "true"
                or wheel.get_all("Tag") != ["py3-none-any"]
            ):
                raise ValueError("NAS setup wheel distribution is not portable")
            entrypoints = configparser.ConfigParser(interpolation=None)
            entrypoints.read_string(
                archive.read(metadata_root + "/entry_points.txt").decode()
            )
            if (
                entrypoints["console_scripts"]["vonk-nas-setup"]
                != "vonk_nas_setup.cli:main"
            ):
                raise ValueError("NAS setup wheel entry point is invalid")
            for required in (
                "vonk_nas_setup/cli.py",
                "vonk_agent_protocol/__init__.py",
                "vonk_agent_protocol/installer_setup.py",
            ):
                if not archive.read(required):
                    raise ValueError("NAS setup wheel has an empty runtime member")
    except (KeyError, UnicodeError, configparser.Error, zipfile.BadZipFile) as error:
        raise ValueError("NAS setup wheel is incomplete or malformed") from error
