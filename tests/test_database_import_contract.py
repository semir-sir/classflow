import ast
import unittest
from pathlib import Path

import database


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class DatabaseImportContractTests(unittest.TestCase):
    def test_all_project_database_imports_are_defined(self):
        imported = set()
        for module_name in ("app.py", "makeup.py"):
            source = (PROJECT_ROOT / module_name).read_text(encoding="utf-8")
            tree = ast.parse(source, filename=module_name)
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module == "database":
                    imported.update(alias.name for alias in node.names)

        missing = sorted(
            symbol for symbol in imported
            if not callable(getattr(database, symbol, None))
        )
        self.assertEqual(missing, [], f"database.py is missing imported functions: {missing}")

    def test_database_does_not_import_bot_or_application_modules(self):
        tree = ast.parse((PROJECT_ROOT / "database.py").read_text(encoding="utf-8"))
        dependencies = {
            node.module for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        }
        dependencies.update(
            alias.name.split(".", 1)[0]
            for node in ast.walk(tree) if isinstance(node, ast.Import)
            for alias in node.names
        )
        self.assertFalse({"app", "bot", "makeup"} & dependencies)


if __name__ == "__main__":
    unittest.main()
