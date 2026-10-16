import importlib.util
from pathlib import Path
import unittest


SCRIPT = Path(__file__).with_name("verify.py")
SPEC = importlib.util.spec_from_file_location("verify", SCRIPT)
verify = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(verify)


class VerificationEntryTests(unittest.TestCase):
    def test_module_matrix_contains_expected_groups(self):
        self.assertEqual(set(verify.MODULES), {"assistant", "execution", "rag", "writing", "frontend"})

    def test_integration_includes_sqlite_worker_and_api_scenarios(self):
        self.assertIn("tests/test_execution_worker.py", verify.INTEGRATION_TESTS)
        self.assertIn("tests/test_application_lifecycle.py", verify.INTEGRATION_TESTS)
        self.assertIn("tests/test_unified_tasks_api.py", verify.INTEGRATION_TESTS)

    def test_external_requires_explicit_confirmation(self):
        with self.assertRaises(SystemExit):
            verify.build_parser().parse_args(["external", "docker"])

    def test_external_requires_budget_for_provider(self):
        args = verify.build_parser().parse_args([
            "external", "provider", "--confirm-real-dependencies"
        ])
        self.assertIsNone(args.provider_max_runs)
        self.assertIsNone(args.provider_max_requests)


if __name__ == "__main__":
    unittest.main()