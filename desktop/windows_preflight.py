"""Read-only Windows path checks before dependency downloads or installation."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PureWindowsPath
import re
import sys

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
from platform_support import read_file

SCHEMA = "lodestone-windows-path-budget/v1"
REFUSAL = "LODESTONE_WINDOWS_PATH_TOO_LONG"
SCOPES = ("root", "bootstrap_root", "cache", "payload")
FILE_LIMIT = 259
DIRECTORY_LIMIT = 247
ATOMIC_NAME_OVERHEAD = 10  # .<selected filename>.<eight tempfile characters>


def utf16_units(value):
    return len(str(value).encode("utf-16-le")) // 2


def long_path_policy():
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                r"SYSTEM\CurrentControlSet\Control\FileSystem", 0, winreg.KEY_READ) as key:
            value, kind = winreg.QueryValueEx(key, "LongPathsEnabled")
    except FileNotFoundError:
        return {"state": "ABSENT", "value": None, "enabled": False}
    except OSError as exc:
        return {"state": "UNREADABLE", "value": None, "enabled": False,
                "error": str(exc)}
    return {"state": "ENABLED" if kind == winreg.REG_DWORD and value == 1 else "DISABLED",
            "value": value, "kind": kind, "enabled": kind == winreg.REG_DWORD and value == 1}


def canonical_path(value):
    path = PureWindowsPath(value)
    if len(path.drive) != 2 or path.drive[1] != ":" or not path.root or ".." in path.parts:
        raise ValueError("Use absolute local-drive paths without parent traversal")
    for part in path.parts[1:]:
        if utf16_units(part) > 255:
            raise ValueError(REFUSAL + ": A path component exceeds 255 UTF-16 units: " + part)
    return str(path).rstrip("\\")


def validate_budget(document, lock_bytes):
    if (type(document) is not dict or document.get("schema") != SCHEMA
            or document.get("lock_sha256") != hashlib.sha256(lock_bytes).hexdigest()):
        raise ValueError("Windows path inventory does not match this download's dependency lock")
    scopes, widths = document.get("scopes"), document.get("token_widths", {})
    if type(scopes) is not dict or type(widths) is not dict:
        raise ValueError("Windows path inventory is incomplete")
    for name in SCOPES:
        entry = scopes.get(name)
        if type(entry) is not dict:
            raise ValueError("Missing Windows path inventory scope: " + name)
        for kind in ("file", "directory"):
            count = entry.get("max_" + kind + "_relative_utf16")
            template = entry.get("longest_" + kind + "_template")
            if type(count) is not int or not 0 <= count < 32767 or type(template) is not str:
                raise ValueError("Invalid Windows path inventory bound: " + name)
            def expand(match):
                width = widths.get(match.group(1))
                if type(width) is not int or not 0 < width < 32767:
                    raise ValueError("Invalid Windows path inventory token")
                return "x" * width
            relative = template.replace("/", "\\").lstrip("\\")
            rendered = re.sub(r"\{([a-zA-Z0-9_]+)\}", expand, relative)
            if utf16_units("\\" + rendered if rendered else "") > count:
                raise ValueError("Windows path inventory understates its example: " + name)
    return document


def evaluate(document, *, root, cache, payload, app, policy):
    bases = {"root": canonical_path(root), "bootstrap_root": canonical_path(root),
             "cache": canonical_path(cache), "payload": canonical_path(payload)}
    rows = []
    enabled = policy.get("enabled") is True
    for name, base in bases.items():
        entry = document["scopes"][name]
        for kind in ("file", "directory"):
            # Host cmd, curl, tar, PowerShell and desktop shell paths stay in
            # the legacy envelope even when the pinned Python can go deeper.
            limit = 32766 if name == "root" and enabled else (
                FILE_LIMIT if kind == "file" else DIRECTORY_LIMIT)
            relative = entry["max_" + kind + "_relative_utf16"]
            template = entry["longest_" + kind + "_template"].replace("/", "\\").lstrip("\\")
            rows.append({"scope": name, "kind": kind, "base": base,
                "required_utf16": utf16_units(base) + relative, "limit_utf16": limit,
                "remaining_utf16": limit - utf16_units(base) - relative,
                "deepest_member": template, "maximum_base_utf16": limit - relative})
    app_path = PureWindowsPath(canonical_path(app))
    if utf16_units(app_path.name) + ATOMIC_NAME_OVERHEAD > 255:
        raise ValueError(REFUSAL + ": Choose a launcher filename of at most 245 UTF-16 units")
    for kind, value, overhead, limit in (("file", str(app_path), ATOMIC_NAME_OVERHEAD, FILE_LIMIT),
                                        ("directory", str(app_path.parent).rstrip("\\"), 0, DIRECTORY_LIMIT)):
        required = utf16_units(value) + overhead
        rows.append({"scope": "app", "kind": kind, "base": value,
            "required_utf16": required, "limit_utf16": limit, "remaining_utf16": limit - required,
            "deepest_member": "." + app_path.name + ".xxxxxxxx" if kind == "file" else "",
            "maximum_base_utf16": limit - overhead})
    failed = [row for row in rows if row["remaining_utf16"] < 0]
    if failed:
        lines = [REFUSAL + ": Setup stopped before downloads or installation changes."]
        for row in failed:
            lines.append("{scope} {kind}: {required_utf16} UTF-16 units exceed the supported limit "
                "{limit_utf16} ({remaining_utf16} remaining); deepest member: {deepest_member}. "
                "Use a base path no longer than {maximum_base_utf16} UTF-16 units.".format(**row))
        if not enabled and any(row["scope"] == "root" for row in failed):
            lines.append('Choose a shorter directory you own with --root, or explicitly run "Enable Windows '
                'Long Paths.cmd" and approve its administrator request, then start the installer again. '
                'The installer never changes that setting.')
        if any(row["scope"] != "root" for row in failed):
            lines.append("Use a shorter extraction directory, --root, --cache or --app path as identified above; "
                         "the host bootstrap tools keep their supported path limits.")
        raise ValueError("\n".join(lines))
    return {"schema": "lodestone-windows-path-preflight/v1", "status": "PATHS_ACCEPTED",
            "long_paths_enabled": enabled, "policy": policy, "paths": rows,
            "lock_sha256": document["lock_sha256"], "registry_changed": False}


def preflight(payload, root, app, cache=None):
    payload = Path(payload).absolute()
    try:
        raw = read_file(payload / "windows-path-budget.json")
        lock = read_file(payload / "dependencies.windows-x86_64.lock.json")
    except OSError as exc:
        raise ValueError("Cannot read the Windows path inventory; re-extract the complete download into a "
                         "shorter local directory. " + str(exc)) from exc
    document = validate_budget(json.loads(raw), lock)
    result = evaluate(document, root=root, cache=cache or PureWindowsPath(root) / "downloads",
                      payload=payload, app=app, policy=long_path_policy())
    result["inventory_sha256"] = hashlib.sha256(raw).hexdigest()
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--payload", type=Path, required=True)
    parser.add_argument("--root", required=True)
    parser.add_argument("--app", required=True)
    parser.add_argument("--cache")
    args = parser.parse_args()
    try:
        print(json.dumps(preflight(args.payload, args.root, args.app, args.cache), indent=2))
        return 0
    except (OSError, ValueError, KeyError) as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
