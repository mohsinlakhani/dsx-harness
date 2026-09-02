"""Guard the product/experiment import boundary."""

from __future__ import annotations

import ast
from pathlib import Path

PRODUCT_PACKAGES = ("builders", "pipeline", "packet")


def test_product_packages_do_not_import_experiment_code() -> None:
    root = Path(__file__).resolve().parents[2] / "src" / "dsx"
    forbidden: list[str] = []
    for package in PRODUCT_PACKAGES:
        for path in (root / package).rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module is not None:
                    names = [node.module]
                else:
                    continue
                if any(
                    name == "dsx.experiments" or name.startswith("dsx.experiments.")
                    for name in names
                ):
                    forbidden.append(f"{path}:{node.lineno}")
    assert forbidden == []
