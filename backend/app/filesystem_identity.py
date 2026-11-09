"""Portable file-system identities for SQLite and JSON metadata."""

import stat


def _portable_number(value: int) -> int | str:
    if -(2**63) <= value < 2**63:
        return value
    return f"fs:{value}"


def directory_identity(path) -> tuple[int | str, int | str]:
    info = path.stat()
    if not stat.S_ISDIR(info.st_mode):
        raise ValueError("Path is not a directory")
    return _portable_number(info.st_dev), _portable_number(info.st_ino)


def identity_matches(path, device, inode) -> bool:
    actual_device, actual_inode = directory_identity(path)
    return (str(actual_device), str(actual_inode)) == (str(device), str(inode))
