"""Module layers and public-API imports (ADR 0012). Stdlib only, no app import."""

import ast
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "app"

# Top → bottom. Imports go only downward; same-layer imports only via PEERS.
LAYERS = [
    {"main"},
    {"call", "docs"},
    {"speech", "kernel", "router", "executor"},  # speech и agent-слой независимы
    {"context", "knowledge"},
    {"tracer", "config", "db"},
]
PEERS = {
    ("call", "docs"),
    ("db", "config"),
    *((a, b) for a in ("kernel", "router", "executor") for b in ("kernel", "router", "executor")),
}
# TODO(hack): existing violations, remove in follow-up PRs. (importer file, imported module)
ALLOWED_EXCEPTIONS: set[tuple[str, str]] = set()

LAYER = {m: i for i, layer in enumerate(LAYERS) for m in layer}


def owner(path: Path) -> str:
    return path.relative_to(APP).parts[0].removesuffix(".py")


def app_imports(tree: ast.AST):
    for node in ast.walk(tree):  # walk() also finds lazy imports inside functions
        if isinstance(node, ast.Import):
            yield from (a.name for a in node.names if a.name.split(".")[0] == "app")
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            if node.module == "app":
                yield from (f"app.{a.name}" for a in node.names)
            elif node.module.startswith("app."):
                yield node.module


def violations() -> list[str]:
    found = []
    for path in sorted(APP.rglob("*.py")):
        if path.parent == APP and path.name == "__init__.py":
            continue
        src, rel = owner(path), str(path.relative_to(APP.parent))
        for name in app_imports(ast.parse(path.read_text(), str(path))):
            dst = name.partition(".")[2].split(".")[0]
            if dst in ("", src) or (rel, name) in ALLOWED_EXCEPTIONS:
                continue
            if src not in LAYER or dst not in LAYER:
                found.append(f"{rel}: unknown module in `{name}`; add it to ADR 0012 and LAYERS")
            elif LAYER[dst] < LAYER[src] or (LAYER[dst] == LAYER[src] and (src, dst) not in PEERS):
                found.append(f"{rel}: `{name}` breaks layers ({src} -> {dst})")
            if (APP / dst).is_dir() and name != f"app.{dst}":
                found.append(f"{rel}: `{name}` bypasses public API, use `from app.{dst} import ...`")
    return found


def test_layer_map_covers_all_modules():
    existing = {p.name for p in APP.iterdir() if p.is_dir() and (p / "__init__.py").exists()}
    existing |= {p.stem for p in APP.glob("*.py") if p.stem != "__init__"}
    missing = existing - LAYER.keys()
    assert not missing, f"modules missing from ADR 0012 / LAYERS: {sorted(missing)}"


def test_module_imports_follow_layers_and_public_api():
    found = violations()
    assert not found, "module boundary violations:\n" + "\n".join(found)
