"""Automated architectural fitness tests (#108-#113).

Enforces the module boundaries between the framework-independent core,
session/transport layer, and Home Assistant adapters using stdlib AST
inspection so architectural drift is caught immediately in CI with clear
pointers to the intended seam.
"""

from __future__ import annotations

import ast
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent.parent / "custom_components" / "iletcomfort"
PROTOCOLS_DIR = PACKAGE_ROOT / "protocols"

PURE_CORE_FILES: tuple[Path, ...] = (
    PACKAGE_ROOT / "c3_basic.py",
    PACKAGE_ROOT / "c3_frame.py",
    PACKAGE_ROOT / "capabilities.py",
    PACKAGE_ROOT / "commands.py",
    PACKAGE_ROOT / "health.py",
    PACKAGE_ROOT / "mqtt_credentials.py",
    PACKAGE_ROOT / "session.py",
    *sorted(PROTOCOLS_DIR.glob("*.py")),
)

ENTITY_PLATFORM_FILES: tuple[Path, ...] = (
    PACKAGE_ROOT / "binary_sensor.py",
    PACKAGE_ROOT / "climate.py",
    PACKAGE_ROOT / "number.py",
    PACKAGE_ROOT / "select.py",
    PACKAGE_ROOT / "sensor.py",
    PACKAGE_ROOT / "switch.py",
    PACKAGE_ROOT / "water_heater.py",
)


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _imported_roots_and_names(tree: ast.Module) -> tuple[set[str], set[str]]:
    """Return (top-level/relative module names, imported symbol names)."""
    modules: set[str] = set()
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                modules.add(alias.name.split(".")[0])
                modules.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                modules.add(node.module.split(".")[0])
                modules.add(node.module)
            for alias in node.names:
                names.add(alias.name)
    return modules, names


def test_pure_core_has_no_framework_or_transport_imports() -> None:
    """Core protocol, health, session, command, and cert modules stay HA/HTTP/MQTT-free."""
    forbidden = {"homeassistant", "requests", "paho"}
    violations: list[str] = []
    for path in PURE_CORE_FILES:
        modules, _ = _imported_roots_and_names(_parse(path))
        bad = sorted(modules & forbidden)
        if bad:
            violations.append(f"{path.relative_to(PACKAGE_ROOT)}: imports {bad}")
    assert not violations, (
        "Pure core modules must remain framework- and transport-independent:\n"
        + "\n".join(violations)
    )


def test_protocols_do_not_import_runtime_adapters() -> None:
    """Dialect modules under protocols/ must not depend on HA coordinator, MQTT, or session."""
    forbidden_modules = {
        "coordinator",
        "mqtt",
        "mqtt_credentials",
        "session",
        "health",
        "diagnostics",
        "config_flow",
        "entity",
    }
    forbidden_symbols = {"DollinCloudClient", "ILetComfortClient", "ILetComfortCoordinator"}
    violations: list[str] = []
    for path in sorted(PROTOCOLS_DIR.glob("*.py")):
        modules, names = _imported_roots_and_names(_parse(path))
        bad_mods = sorted(modules & forbidden_modules)
        bad_syms = sorted(names & forbidden_symbols)
        if bad_mods or bad_syms:
            violations.append(
                f"protocols/{path.name}: modules={bad_mods} symbols={bad_syms}"
            )
    assert not violations, (
        "Dialect modules in protocols/ must stay pure and independent of runtime adapters:\n"
        + "\n".join(violations)
    )


def test_no_model_profile_branching_outside_protocols() -> None:
    """Callers outside protocols/ must ask ProtocolAdapter, never branch on ModelProfile (#110)."""
    allowed = {
        PACKAGE_ROOT / "model_profiles.py",  # backwards-compatible re-export facade
    }
    forbidden_symbols = {"ModelProfile", "resolve_profile"}
    violations: list[str] = []
    for path in sorted(PACKAGE_ROOT.glob("*.py")):
        if path in allowed:
            continue
        _, names = _imported_roots_and_names(_parse(path))
        bad = sorted(names & forbidden_symbols)
        if bad:
            violations.append(f"{path.name}: imports {bad}")
    assert not violations, (
        "Do not branch on ModelProfile outside protocols/ — add a behavior method "
        "or flag to ProtocolAdapter instead:\n" + "\n".join(violations)
    )


def test_entity_platforms_use_semantic_commands_only() -> None:
    """Entity platforms must route writes through HeatPumpCommand, never build raw hex (#113)."""
    forbidden_modules = {"c3_frame"}
    forbidden_calls = {
        "send_hex_command",
        "build_c3_frame",
        "build_c3_query",
        "build_c3_set",
        "build_basic_set",
        "build_feature_set",
    }
    violations: list[str] = []
    for path in ENTITY_PLATFORM_FILES:
        tree = _parse(path)
        modules, names = _imported_roots_and_names(tree)
        bad_mods = sorted(modules & forbidden_modules)
        bad_syms = sorted(names & forbidden_calls)
        bad_attrs: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in forbidden_calls:
                bad_attrs.append(node.attr)
        if bad_mods or bad_syms or bad_attrs:
            violations.append(
                f"{path.name}: modules={bad_mods} symbols={bad_syms} attrs={sorted(set(bad_attrs))}"
            )
    assert not violations, (
        "Entity platforms must send typed HeatPumpCommand objects via "
        "coordinator.async_send_command(), never build or send raw frames:\n"
        + "\n".join(violations)
    )


def test_diagnostics_uses_public_coordinator_projections() -> None:
    """Diagnostics must read public coordinator projections, never private _* fields (#112)."""
    diag_path = PACKAGE_ROOT / "diagnostics.py"
    tree = _parse(diag_path)
    private_reads: list[str] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "coordinator"
            and node.attr.startswith("_")
        ):
            private_reads.append(f"coordinator.{node.attr} (line {node.lineno})")
    assert not private_reads, (
        "diagnostics.py must consume public projections (e.g. coordinator.health_snapshot) "
        "instead of private coordinator attributes:\n" + "\n".join(private_reads)
    )


def test_protocol_adapter_is_sans_io() -> None:
    """ProtocolAdapter methods must never accept I/O callbacks (use ReadPlan / WritePlan)."""
    tree = _parse(PROTOCOLS_DIR / "base.py")
    adapter_cls = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "ProtocolAdapter"
    )
    violations: list[str] = []
    for item in adapter_cls.body:
        if isinstance(item, ast.AsyncFunctionDef):
            violations.append(f"ProtocolAdapter.{item.name} is async def")
            continue
        if not isinstance(item, ast.FunctionDef):
            continue
        for arg in (*item.args.posonlyargs, *item.args.args, *item.args.kwonlyargs):
            if arg.annotation is None:
                continue
            ann_src = ast.unparse(arg.annotation)
            if "Callable" in ann_src:
                violations.append(f"ProtocolAdapter.{item.name}({arg.arg}: {ann_src})")
    assert not violations, (
        "ProtocolAdapter dialects must remain pure / sans-I/O (return ReadPlan / WritePlan "
        "instead of taking transport Callable parameters):\n" + "\n".join(violations)
    )
