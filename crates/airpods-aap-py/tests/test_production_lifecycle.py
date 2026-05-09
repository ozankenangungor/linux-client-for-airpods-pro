"""FFI checks against `cargo build -p airpods-aap-py` (no wheel installation)."""

import importlib.util
import types
import unittest
from pathlib import Path


LIBRARY = Path(__file__).resolve().parents[3] / "target/debug/lib_airpods_aap_core.so"


class ProductionLifecycleBindingsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("_airpods_aap_core", LIBRARY)
        assert spec is not None and spec.loader is not None
        cls.native = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.native)

    def test_operation_table(self):
        allowed = {(0, 0), (2, 1), (4, 2), (4, 3)}
        self.assertIsInstance(self.native.production_operation, types.BuiltinFunctionType)
        for state in range(7):
            for operation in range(5):
                with self.subTest(state=state, operation=operation):
                    if (state, operation) in allowed or operation == 4:
                        self.assertEqual(self.native.production_operation(state, operation), state)
                    else:
                        with self.assertRaises(ValueError) as caught:
                            self.native.production_operation(state, operation)
                        self.assertEqual(caught.exception.args, (9,))

    def test_transition_table(self):
        rules = {
            (0, 0): 1, (1, 1): 2, (1, 2): 6, (3, 2): 6, (5, 2): 6,
            (6, 2): 6,
            (2, 3): 3, (3, 4): 4, (4, 6): 5, (5, 7): 2,
        }
        self.assertIsInstance(self.native.production_transition, types.BuiltinFunctionType)
        for state in range(7):
            for event in range(9):
                for complete in (False, True):
                    with self.subTest(state=state, event=event, complete=complete):
                        if event == 5:
                            expected = 6
                        elif event == 8 and state != 0:
                            expected = 0 if complete else 6
                        else:
                            expected = rules.get((state, event))
                        if expected is None:
                            with self.assertRaises(ValueError) as caught:
                                self.native.production_transition(state, event, complete)
                            self.assertEqual(caught.exception.args, (10,))
                        else:
                            self.assertEqual(
                                self.native.production_transition(state, event, complete), expected
                            )

    def test_all_unknown_u8_ids_and_non_u8_types(self):
        for first_bad, invoke in (
            (7, lambda bad: self.native.production_operation(bad, 4)),
            (7, lambda bad: self.native.production_transition(bad, 8, True)),
            (5, lambda bad: self.native.production_operation(0, bad)),
            (9, lambda bad: self.native.production_transition(0, bad, True)),
        ):
            for bad in range(first_bad, 256):
                with self.subTest(first_bad=first_bad, bad=bad):
                    with self.assertRaises(ValueError) as caught:
                        invoke(bad)
                    self.assertEqual(caught.exception.args, (7,))
            for bad in (-1, 256, True, False, 1.5, "1", None):
                with self.subTest(first_bad=first_bad, bad=bad):
                    with self.assertRaises(ValueError) as caught:
                        invoke(bad)
                    self.assertEqual(caught.exception.args, ("invalid transition identity",))

    def test_cleanup_flag_requires_actual_bool(self):
        for flag in (0, 1, -1, 256, 1.5, "true", None):
            for state, event in ((0, 0), (6, 8)):
                with self.subTest(flag=flag, state=state):
                    with self.assertRaises(ValueError) as caught:
                        self.native.production_transition(state, event, flag)
                    self.assertEqual(caught.exception.args, ("invalid cleanup_complete flag",))
        self.assertEqual(self.native.production_transition(0, 0, False), 1)
        self.assertEqual(self.native.production_transition(0, 0, True), 1)


if __name__ == "__main__":
    unittest.main()
