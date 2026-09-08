"""Pin the six migrated modules' effect calls to the reviewed parent tree."""

from __future__ import annotations

import ast
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PARENT = "d8fb563d94fc12f39b0d662a43703311f77ea5a8"
MODULES = (
    "aap.py",
    "aap_channel.py",
    "authentication.py",
    "bluetooth.py",
    "discovery.py",
    "session_reopen.py",
)
EFFECT_CALLS = frozenset(
    {
        "write", "receive", "record", "wait_for", "sleep", "get_adapter",
        "set_powered", "acquire", "create_l2cap_channel", "disconnect",
        "authenticate", "encrypt", "connect", "power_on", "power_off",
        "get_managed_objects", "load_classic_credentials",
        "send_handshake_request", "open_transport", "preflight", "close",
        "open", "start", "stop", "snapshot", "read_text", "read_bytes",
        "write_text", "write_bytes", "mkdir", "unlink", "remove",
        "replace", "system", "Popen",
    }
)
EFFECT_IMPORT_ROOTS = frozenset(
    {"os", "pathlib", "socket", "subprocess", "shutil", "dbus_next"}
)


def effect_signature(source: str) -> dict[str, tuple[str, ...]]:
    tree = ast.parse(source)
    signature = {}
    for class_node in tree.body:
        if not isinstance(class_node, ast.ClassDef):
            continue
        for method in class_node.body:
            if not isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            calls = []
            for node in ast.walk(method):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                    if node.func.attr in EFFECT_CALLS:
                        calls.append(node.func.attr)
                elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                    if node.func.id in {"open", "Path", "socket", "Popen"}:
                        calls.append(node.func.id)
            if calls:
                signature[f"{class_node.name}.{method.name}"] = tuple(calls)
    return signature


def effect_imports(source: str) -> tuple[str, ...]:
    imports = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.append(node.module)
    return tuple(sorted(name for name in imports if name.split(".")[0] in EFFECT_IMPORT_ROOTS))


class RuntimeEffectSafetyTests(unittest.TestCase):
    def test_parent_effect_calls_remain_in_same_methods_and_order(self) -> None:
        parent_ref = PARENT
        res = subprocess.run(
            ["git", "rev-parse", "--verify", f"{parent_ref}^{{commit}}"],
            cwd=ROOT,
            capture_output=True,
        )
        if res.returncode != 0:
            log_res = subprocess.run(
                ["git", "log", "-1", "--grep=^Use Rust diagnostic policy in Python$", "--format=%H"],
                cwd=ROOT,
                capture_output=True,
                text=True,
            )
            if log_res.returncode == 0 and log_res.stdout.strip():
                parent_ref = log_res.stdout.strip()

        for filename in MODULES:
            path = f"src/airpods_hr/{filename}"
            parent = subprocess.check_output(
                ["git", "show", f"{parent_ref}:{path}"], cwd=ROOT, text=True
            )
            current = (ROOT / path).read_text(encoding="utf-8")
            with self.subTest(path=path):
                self.assertEqual(effect_imports(current), effect_imports(parent))
                # The new native accumulator's snapshot is a pure in-memory read.
                old = effect_signature(parent)
                new = effect_signature(current)
                new.pop("AAPHandshakeSession._snapshot", None)
                self.assertEqual(new, old)
