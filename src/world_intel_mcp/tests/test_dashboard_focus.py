"""Offline JS compile/runtime regressions; use installed Duktape or Node only.

No browser/DOM layout claim. The shared ES5 harness models focus loss on child
replacement and executes the real drawer renderer's collapse restoration loop.
"""

import ctypes
import ctypes.util
import json
from pathlib import Path
import re
import shutil
import subprocess
import unittest


HERE = Path(__file__).resolve().parent
INDEX = HERE.parent / "dashboard/index.html"
DUKTAPE = ctypes.util.find_library("duktape")
NODE = shutil.which("node")


class JavaScriptRuntime:
    """Small protected-eval adapter, with no JS-to-Python native callbacks."""
    def __init__(self):
        self.lib = ctypes.CDLL(DUKTAPE) if DUKTAPE else None
        if self.lib is None:
            return
        declarations = {
            "duk_create_heap": ([ctypes.c_void_p] * 5, ctypes.c_void_p),
            "duk_destroy_heap": ([ctypes.c_void_p], None),
            "duk_get_global_string": ([ctypes.c_void_p, ctypes.c_char_p], ctypes.c_int),
            "duk_push_lstring": ([ctypes.c_void_p, ctypes.c_char_p, ctypes.c_size_t], ctypes.c_void_p),
            "duk_pcall": ([ctypes.c_void_p, ctypes.c_int], ctypes.c_int),
            "duk_safe_to_lstring": ([ctypes.c_void_p, ctypes.c_int, ctypes.POINTER(ctypes.c_size_t)], ctypes.c_void_p),
        }
        for name, (args, result) in declarations.items():
            function = getattr(self.lib, name)
            function.argtypes, function.restype = args, result

    def evaluate(self, source):
        if self.lib is None:
            result = subprocess.run([NODE], input="console.log(eval(" + json.dumps(source) + "));",
                                    capture_output=True, text=True, timeout=30, check=True)
            return result.stdout.strip()
        context = self.lib.duk_create_heap(None, None, None, None, None)
        if not context:
            raise RuntimeError("Could not create Duktape heap")
        try:
            self.lib.duk_get_global_string(context, b"eval")
            encoded = source.encode("utf-8")
            self.lib.duk_push_lstring(context, encoded, len(encoded))
            failed = self.lib.duk_pcall(context, 1)
            length = ctypes.c_size_t()
            pointer = self.lib.duk_safe_to_lstring(context, -1, ctypes.byref(length))
            output = ctypes.string_at(pointer, length.value).decode("utf-8", "replace")
            if failed:
                raise AssertionError("Duktape JS execution failed: " + output)
            return output
        finally:
            self.lib.duk_destroy_heap(context)


@unittest.skipUnless(DUKTAPE or NODE, "No installed Duktape/Node runtime")
class DashboardFocusTests(unittest.TestCase):
    def setUp(self):
        html = INDEX.read_text()
        self.script = "\n".join(re.findall(r"<script\b[^>]*>([\s\S]*?)</script>", html))
        self.runtime = JavaScriptRuntime()

    def test_complete_inline_script_compiles(self):
        self.assertEqual(self.runtime.evaluate("new Function(" + json.dumps(self.script) + "); 'compiled';"), "compiled")

    def test_focus_and_persisted_cyber_state_runtime(self):
        functions = []
        for name in ("rememberControlFocus", "alertControlKey", "updateHudStats", "updateAlertBanner", "activateHudAction", "updateDrawer"):
            start = self.script.index("function " + name + "(")
            end = self.script.index("\n}\n", start) + 3
            functions.append(self.script[start:end])
        harness = HERE.joinpath("dashboard_focus.js").read_text()
        checks = self.runtime.evaluate("\n".join(functions) + "\n" + harness)
        self.assertGreaterEqual(int(checks), 20)


if __name__ == "__main__":
    unittest.main()
