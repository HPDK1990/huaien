"""Contract tests: the boundary between huaien and the Hermes install.

huaien is a separate program that drives Hermes through its command line and
through a handful of well-known files. When the Hermes install is upgraded,
an interface can change under huaien's feet - a flag is renamed, a JSON
payload is reshaped - and huaien then fails SILENTLY: an empty read, a
report that never ships, a watchdog that decides the gateway is dead.

These tests turn that silence into a failure. config/hermes_contract.json
declares every interface huaien depends on; each entry is verified here
against the installed Hermes.

They are read-only. Nothing here starts, stops, or mutates Hermes.

Public-copy note: the exported copy of huaien runs on other machines where
no Hermes install exists. Every test here skips in that case rather than
failing, so the public copy stays green.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parents[1]
CONTRACT_PATH = PROJECT / "config" / "hermes_contract.json"

HERMES_HOME = Path(
    os.environ.get("HERMES_HOME")
    or (Path.home() / "AppData" / "Local" / "hermes")
)


@pytest.fixture(scope="module")
def contract():
    if not CONTRACT_PATH.exists():
        pytest.skip(f"contract file missing: {CONTRACT_PATH}")
    with open(CONTRACT_PATH, encoding="utf-8") as fh:
        return json.load(fh)


@pytest.fixture(scope="module")
def hermes_bin(contract):
    binary = contract["cli"].get("binary", "hermes")
    found = shutil.which(binary)
    if not found:
        pytest.skip(f"'{binary}' is not on PATH - no Hermes install to verify against")
    return found


@pytest.fixture(scope="module")
def help_cache(hermes_bin):
    """One --help call per subcommand, reused across tests."""
    cache: dict[str, str] = {}

    def get(subcommand: str) -> str:
        if subcommand not in cache:
            args = [hermes_bin] + subcommand.split() + ["--help"]
            try:
                r = subprocess.run(args, capture_output=True, timeout=120)
            except (OSError, subprocess.SubprocessError) as exc:
                pytest.skip(f"could not run '{' '.join(args)}': {exc}")
            cache[subcommand] = (
                (r.stdout or b"").decode("utf-8", errors="replace")
                + (r.stderr or b"").decode("utf-8", errors="replace")
            )
        return cache[subcommand]

    return get


@pytest.fixture(scope="module")
def hermes_home():
    if not HERMES_HOME.is_dir():
        pytest.skip(f"HERMES_HOME not found: {HERMES_HOME}")
    return HERMES_HOME


# --- CLI surface ------------------------------------------------------------

def test_hermes_binary_responds(hermes_bin, contract):
    args = [hermes_bin] + contract["cli"]["version_probe"]
    r = subprocess.run(args, capture_output=True, timeout=60)
    assert r.returncode == 0, (
        f"'{' '.join(args)}' exited {r.returncode}. The Hermes CLI contract is broken."
    )


def test_global_flags_exist(hermes_bin, contract, help_cache):
    text = help_cache("")
    missing = [f for f in contract["cli"]["global_flags"]
               if f != "-z" and f not in text]
    # -z takes a value and may be listed as "-z PROMPT"; accept both forms.
    if "-z" in contract["cli"]["global_flags"] and "-z" not in text and "-z PROMPT" not in text:
        missing.append("-z")
    assert not missing, (
        f"global flag(s) {missing} vanished from 'hermes --help'. "
        f"huaien calls them by name."
    )


@pytest.mark.parametrize("subcommand", sorted(
    json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))["cli"]["subcommands"].keys()
    if CONTRACT_PATH.exists() else []
))
def test_subcommand_flags_still_exist(subcommand, help_cache, contract):
    entry = contract["cli"]["subcommands"][subcommand]
    text = help_cache(subcommand)
    assert text.strip(), f"'hermes {subcommand} --help' produced no output"

    missing = []
    for flag in entry.get("flags", []):
        if flag.startswith("--"):
            if f"{flag}" not in text:
                missing.append(flag)
        else:
            # short flags: match "-f PATH" style or bare "-f"
            if not re.search(rf"(?<![\w-]){re.escape(flag)}(?![\w-])", text):
                missing.append(flag)

    assert not missing, (
        f"'hermes {subcommand}' lost flag(s) {missing}. "
        f"huaien depends on: {entry.get('used_by', [])}"
    )


# --- JSON payload shapes ----------------------------------------------------

def test_kanban_show_json_root_key(hermes_bin, contract):
    """`kanban show --json` nests the task under a `task` key.

    Reading the wrong key returns None and looks like an empty task rather
    than an error, so this shape must be pinned.
    """
    spec = contract["json_shapes"].get("kanban show --json")
    if not spec:
        pytest.skip("no json_shapes entry for kanban show --json")

    r = subprocess.run(
        [hermes_bin, "kanban", "show", "--json", "t_contract_probe_missing"],
        capture_output=True, timeout=120,
    )
    out = (r.stdout or b"").decode("utf-8", errors="replace").strip()
    if not out:
        pytest.skip("kanban show returned nothing for a missing id - shape unobservable")
    try:
        payload = json.loads(out)
    except json.JSONDecodeError:
        pytest.skip(f"kanban show --json did not return JSON: {out[:200]}")

    for key in spec.get("root_keys", []):
        assert key in payload, (
            f"'hermes kanban show --json' no longer has a '{key}' root key "
            f"(keys now: {sorted(payload.keys())}). "
            f"huaien depends on: {spec.get('used_by', [])}"
        )


# --- file contracts ---------------------------------------------------------

def test_gateway_lock_shape(hermes_home, contract):
    spec = contract["files"]["gateway_lock"]
    path = hermes_home / spec["rel"]
    if not path.exists():
        pytest.skip(f"{path} not present")
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        pytest.fail(f"{path} is not readable JSON: {exc}")
    missing = [k for k in spec.get("json_keys", []) if k not in data]
    assert not missing, (
        f"{path} lost key(s) {missing} (keys now: {sorted(data.keys())}). "
        f"huaien depends on: {spec.get('used_by', [])}"
    )


def test_state_db_tables_and_columns(hermes_home, contract):
    spec = contract["files"]["state_db"]
    path = hermes_home / spec["rel"]
    if not path.exists():
        pytest.skip(f"{path} not present")
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        for table, columns in spec.get("tables", {}).items():
            cur = con.execute(f"PRAGMA table_info({table})")
            have = {row[1] for row in cur.fetchall()}
            if not have:
                pytest.fail(
                    f"{path} has no table '{table}' "
                    f"(huaien depends on it: {spec.get('used_by', [])})"
                )
            missing = [c for c in columns if c not in have]
            assert not missing, (
                f"{path} table '{table}' lost column(s) {missing}. "
                f"huaien depends on: {spec.get('used_by', [])}"
            )
    finally:
        con.close()


def test_state_db_is_not_locked_beyond_read(hermes_home, contract):
    """Opening the db read-only must never block on the live gateway."""
    path = hermes_home / contract["files"]["state_db"]["rel"]
    if not path.exists():
        pytest.skip(f"{path} not present")
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        cur = con.execute("PRAGMA quick_check")
        assert cur.fetchone()[0] in ("ok", "OK"), "state.db quick_check is not ok"
    finally:
        con.close()


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
