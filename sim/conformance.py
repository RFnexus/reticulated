"""Adapter for the authoritative LXMF-rs Python/Rust conformance harness."""

import importlib.util
import json
import os
import signal
import subprocess
import sys
import time

from .environment import capture_environment, git_revision
from .reporting import utc_timestamp
from . import config

try:
    import tomllib
except ImportError:  # pragma: no cover - Python 3.10 compatibility
    import tomli as tomllib


def load_reference_lock(lxmf_rs_root):
    path = os.path.join(lxmf_rs_root, "tools", "benchmarks", "python_impl.toml")
    with open(path, "rb") as stream:
        data = tomllib.load(stream)
    references = data.get("references") or {}
    return {
        "reticulum": references.get("reticulum"),
        "lxmf": references.get("lxmf"),
        "source": path,
    }


def load_harness(lxmf_rs_root):
    path = os.path.join(lxmf_rs_root, "tools", "scripts", "python_compat_harness.py")
    spec = importlib.util.spec_from_file_location("reticulated_lxmf_conformance", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load LXMF-rs conformance harness: " + path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    live = sorted(str(value) for value in module.SMOKE_SCRIPT_CASES)
    local = sorted(str(value) for value in module.LOCAL_CARGO_TEST_CASES)
    supported = sorted(str(value) for value in module.SUPPORTED_CASES)
    if set(supported) != set(live) | set(local):
        raise RuntimeError("LXMF-rs conformance harness exported inconsistent case sets")
    return {"path": path, "live": live, "local": local, "supported": supported}


def smoke_cases(harness):
    selected = []
    for case in harness["live"]:
        if case.startswith(("direct_", "opportunistic_", "propagated_")):
            selected.append(case)
        elif case.startswith("rns_path_request_") and "transport_policy" not in case:
            selected.append(case)
        elif case == "resource_transfer":
            selected.append(case)
    return sorted(selected)


def _terminate(process):
    if process.poll() is not None:
        return
    try:
        if os.name == "nt":
            process.terminate()
        else:
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
        process.wait(timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        try:
            if os.name == "nt":
                process.kill()
            else:
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        except OSError:
            pass


def run_command(case_id, command, cwd, env, log_path, timeout):
    started = time.monotonic()
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    try:
        with open(log_path, "w", encoding="utf-8") as log:
            process = subprocess.Popen(
                command,
                cwd=cwd,
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
                text=True,
                start_new_session=os.name != "nt",
            )
            try:
                exit_code = process.wait(timeout=timeout)
                status = "pass" if exit_code == 0 else "fail"
                error = None
            except subprocess.TimeoutExpired:
                _terminate(process)
                exit_code = 124
                status = "fail"
                error = "timed out after %s seconds" % timeout
    except OSError as error_value:
        exit_code = None
        status = "fail"
        error = str(error_value)
    return {
        "case_id": case_id,
        "status": status,
        "command": list(command),
        "cwd": os.path.abspath(cwd),
        "exit_code": exit_code,
        "duration_seconds": round(time.monotonic() - started, 3),
        "log": os.path.abspath(log_path),
        "error": error,
    }


def skip_case(case_id, command, cwd, log_path, reason, selected=False):
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    with open(log_path, "w", encoding="utf-8") as log:
        log.write(reason + "\n")
    return {
        "case_id": case_id,
        "status": "skip",
        "selected": bool(selected),
        "command": list(command),
        "cwd": os.path.abspath(cwd) if cwd else None,
        "exit_code": None,
        "duration_seconds": 0.0,
        "log": os.path.abspath(log_path),
        "error": reason,
    }


def _full_commands(lxmf_rs_root, python_bin, reference_root):
    commands = [
        ("python_channel_interop", ["cargo", "test", "-p", "reticulumd", "--test",
                                    "python_channel_interop", "--", "--ignored", "--nocapture"]),
        ("python_paper_interop", ["cargo", "test", "-p", "reticulumd", "--test",
                                  "python_paper_interop", "--", "--ignored", "--nocapture"]),
        ("python_lxmd_remote_relay", ["cargo", "test", "-p", "lxmf-cli", "--test",
                                      "python_lxmd_remote_relay", "--", "--ignored", "--nocapture"]),
    ]
    result = [(name, command, lxmf_rs_root) for name, command in commands]
    if reference_root:
        result.append((
            "python_reference_conformance",
            [python_bin, "-m", "pytest", "tests/", "--reference-only", "-v", "--tb=short"],
            reference_root,
        ))
    return result


def run_conformance(lxmf_rs_root, profile, output_dir, allow_unpinned=False,
                    python_bin=None, timeout=420):
    started = time.time()
    started_at = utc_timestamp()
    root = os.path.abspath(lxmf_rs_root)
    output_dir = os.path.abspath(output_dir)
    logs_dir = os.path.join(output_dir, "logs")
    python_bin = python_bin or sys.executable
    os.makedirs(logs_dir, exist_ok=True)
    try:
        harness = load_harness(root)
        expected = load_reference_lock(root)
        rns_repo = os.path.join(root, "target", "python-references", "Reticulum")
        lxmf_repo = os.path.join(root, "target", "python-references", "LXMF")
        actual = {"reticulum": git_revision(rns_repo), "lxmf": git_revision(lxmf_repo)}
        mismatches = {
            key: {"expected": expected[key], "actual": actual[key]}
            for key in ("reticulum", "lxmf") if expected[key] != actual[key]
        }
        if mismatches and not allow_unpinned:
            raise RuntimeError("pinned Python reference mismatch: " + json.dumps(mismatches, sort_keys=True))

        config.PYTHON_RNS_REPO = rns_repo
        config.PYTHON_LXMF_REPO = lxmf_repo
        rust_binary = os.path.join(root, "target", "release", "reticulumd")
        if os.path.isfile(rust_binary):
            config.RUST_RETICULUMD = rust_binary

        env = dict(os.environ)
        env.update({
            "LXMF_PYTHON_BIN": python_bin,
            "RETICULUM_PY_REPO": rns_repo,
            "LXMF_PY_REPO": lxmf_repo,
            "LXMF_PY_COMPAT_HARNESS": harness["path"],
        })
        selected = set(smoke_cases(harness) if profile == "smoke" else harness["supported"])
        results = []
        for case_id in harness["supported"]:
            command = [python_bin, harness["path"], case_id]
            log_path = os.path.join(logs_dir, case_id + ".log")
            if case_id not in selected:
                results.append(skip_case(
                    case_id, command, root, log_path,
                    "not selected by the %s profile" % profile,
                ))
                continue
            result = run_command(case_id, command, root, env, log_path, timeout)
            result["selected"] = True
            results.append(result)

        if profile == "full":
            reference_root = os.environ.get("RETICULUM_CONFORMANCE_REPO")
            if not reference_root:
                candidate = os.path.join(os.path.dirname(root), "reticulum-conformance")
                reference_root = candidate if os.path.isdir(candidate) else None
            if not reference_root:
                results.append(skip_case(
                    "python_reference_conformance", [], root,
                    os.path.join(logs_dir, "python_reference_conformance.log"),
                    "RETICULUM_CONFORMANCE_REPO is not available", selected=True,
                ))
            for case_id, command, cwd in _full_commands(root, python_bin, reference_root):
                result = run_command(
                    case_id, command, cwd, env,
                    os.path.join(logs_dir, case_id + ".log"), timeout,
                )
                result["selected"] = True
                results.append(result)

        failures = sum(result["status"] == "fail" for result in results)
        skipped = sum(result["status"] == "skip" for result in results)
        selected_skips = sum(result["status"] == "skip" and result.get("selected")
                             for result in results)
        conforming = failures == 0 and selected_skips == 0 and not mismatches
        return {
            "schema_version": 3,
            "report_type": "conformance",
            "scenario": "lxmf-rs-conformance-" + profile,
            "profile": profile,
            "status": "pass" if failures == 0 else "fail",
            "conforming": conforming,
            "started_at": started_at,
            "generated_at": utc_timestamp(),
            "duration_seconds": round(time.time() - started, 3),
            "source": {
                "lxmf_rs_root": root,
                "lxmf_rs_revision": git_revision(root),
                "harness": harness["path"],
                "canonical_case_count": len(harness["supported"]),
            },
            "references": {"expected": expected, "actual": actual, "mismatches": mismatches},
            "cases": results,
            "results": [],
            "nodes": 0,
            "summary": {
                "conformance": {
                    "total": len(results),
                    "passed": sum(result["status"] == "pass" for result in results),
                    "failed": failures,
                    "skipped": skipped,
                    "selected": sum(bool(result.get("selected")) for result in results),
                    "profile_skipped": skipped - selected_skips,
                }
            },
            "environment": capture_environment(root),
        }
    except Exception as error:
        return {
            "schema_version": 3,
            "report_type": "conformance",
            "scenario": "lxmf-rs-conformance-" + profile,
            "profile": profile,
            "status": "fail",
            "conforming": False,
            "error": str(error),
            "started_at": started_at,
            "generated_at": utc_timestamp(),
            "duration_seconds": round(time.time() - started, 3),
            "cases": [],
            "results": [],
            "nodes": 0,
            "summary": {"conformance": {"total": 0, "passed": 0, "failed": 1, "skipped": 0}},
            "environment": capture_environment(root),
        }
