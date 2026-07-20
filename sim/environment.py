"""Reproducibility metadata for scenario and lab reports."""

import hashlib
import importlib.metadata
import os
import platform
import shutil
import subprocess
import sys

from . import config


def _command(*args, cwd=None):
    try:
        return subprocess.check_output(
            list(args), cwd=cwd, text=True, stderr=subprocess.DEVNULL, timeout=5
        ).strip()
    except (OSError, subprocess.SubprocessError):
        return None


def git_revision(path):
    if not path or not os.path.isdir(path):
        return None
    return _command("git", "rev-parse", "HEAD", cwd=path)


def git_dirty(path):
    if not path or not os.path.isdir(path):
        return None
    value = _command("git", "status", "--porcelain", cwd=path)
    return bool(value) if value is not None else None


def file_sha256(path):
    if not path or not os.path.isfile(path):
        return None
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        return None
    return digest.hexdigest()


def distribution_version(name):
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None


def capture_environment(lxmf_rs_root=None):
    rust_binary = config.reticulumd_path()
    if not os.path.isabs(rust_binary):
        rust_binary = shutil.which(rust_binary) or rust_binary
    reticulated_root = config.BASE_DIR
    return {
        "python_version": platform.python_version(),
        "python_executable": sys.executable,
        "python_rns_version": distribution_version("rns"),
        "python_lxmf_version": distribution_version("lxmf"),
        "python_rns_revision": git_revision(config.PYTHON_RNS_REPO),
        "python_lxmf_revision": git_revision(config.PYTHON_LXMF_REPO),
        "python_rns_repo": config.PYTHON_RNS_REPO,
        "python_lxmf_repo": config.PYTHON_LXMF_REPO,
        "reticulated_revision": git_revision(reticulated_root),
        "reticulated_dirty": git_dirty(reticulated_root),
        "lxmf_rs_revision": git_revision(lxmf_rs_root),
        "lxmf_rs_dirty": git_dirty(lxmf_rs_root),
        "rust_binary": os.path.abspath(rust_binary) if os.path.exists(rust_binary) else rust_binary,
        "rust_binary_sha256": file_sha256(rust_binary),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "processor": platform.processor(),
    }
