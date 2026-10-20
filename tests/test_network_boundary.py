"""Architectural tests for the network boundary.

All network traffic must originate in the Ollama client. Every other module is
forbidden from importing a network library.
"""

from __future__ import annotations

import ast
from pathlib import Path

PACKAGE_ROOT = Path(__file__).parents[1] / "src" / "sloth_valet"
NETWORK_MODULES = frozenset(
    {
        "aiohttp",
        "http.client",
        "httpx",
        "requests",
        "socket",
        "urllib.error",
        "urllib.request",
        "urllib3",
    }
)


def _network_imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                first_module = alias.name.split(".", maxsplit=1)[0]
                is_network = first_module in {
                    "aiohttp",
                    "httpx",
                    "requests",
                    "socket",
                    "urllib3",
                } or alias.name.startswith(("http.client", "urllib.request", "urllib.error"))
                if is_network:
                    found.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            if node.module in NETWORK_MODULES or node.module.startswith(
                ("http.client", "urllib.request", "urllib.error")
            ):
                found.add(node.module)
            if node.module == "urllib":
                found.update(
                    f"urllib.{alias.name}"
                    for alias in node.names
                    if alias.name in {"request", "error"}
                )
    return found


def test_only_the_ollama_client_may_import_network_libraries() -> None:
    offenders: dict[str, set[str]] = {}
    for path in sorted(PACKAGE_ROOT.glob("*.py")):
        if path.name == "ollama.py":
            continue
        imports = _network_imports(path)
        if imports:
            offenders[path.name] = imports

    assert not offenders, f"network imports outside ollama.py: {offenders}"
