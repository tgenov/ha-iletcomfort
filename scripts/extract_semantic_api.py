#!/usr/bin/env python3
"""Extract a deterministic semantic API catalogue from an unpacked Weex plugin.

This development-only tool performs conservative static extraction.  It records
only literal facts and marks dynamic syntax unresolved; it never executes vendor
JavaScript and never includes source snippets in its output.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlsplit, urlunsplit

CATALOGUE_VERSION = 1
_SELECTORS = {"query": "query_type", "control": "control_type"}
_ENDPOINT_KEYS = {"encodeUrl": "encode", "decodeUrl": "decode"}
_SENSITIVE = re.compile(
    r"(?:token|password|secret|credential|packageurl|previewurl|x-amz-)", re.I
)


@dataclass(frozen=True)
class Token:
    kind: str
    value: str
    line: int


@dataclass(frozen=True)
class Expression:
    name: str
    args: tuple[Any, ...] = ()
    called: bool = False


@dataclass(frozen=True)
class JsObject:
    values: dict[str, Any]
    line: int
    computed_keys: int = 0


def _tokens(source: str) -> list[Token]:
    """Tokenize the small JavaScript subset needed for conservative extraction."""
    result: list[Token] = []
    i = 0
    line = 1
    while i < len(source):
        ch = source[i]
        if ch.isspace():
            line += ch == "\n"
            i += 1
            continue
        if source.startswith("//", i):
            end = source.find("\n", i)
            i = len(source) if end < 0 else end
            continue
        if source.startswith("/*", i):
            end = source.find("*/", i + 2)
            end = len(source) - 2 if end < 0 else end
            line += source[i : end + 2].count("\n")
            i = end + 2
            continue
        if ch in "'\"`":
            quote, start_line = ch, line
            i += 1
            chars: list[str] = []
            while i < len(source) and source[i] != quote:
                if source[i] == "\\" and i + 1 < len(source):
                    chars.append(source[i + 1])
                    i += 2
                    continue
                line += source[i] == "\n"
                chars.append(source[i])
                i += 1
            i += i < len(source)
            result.append(Token("string", "".join(chars), start_line))
            continue
        match = re.match(r"[A-Za-z_$][\w$]*", source[i:])
        if match:
            value = match.group(0)
            result.append(Token("identifier", value, line))
            i += len(value)
            continue
        match = re.match(r"-?(?:\d+(?:\.\d+)?|\.\d+)", source[i:])
        if match:
            value = match.group(0)
            result.append(Token("number", value, line))
            i += len(value)
            continue
        result.append(Token("punct", ch, line))
        i += 1
    return result


class _Parser:
    def __init__(self, tokens: list[Token], index: int = 0) -> None:
        self.tokens = tokens
        self.index = index

    def _accept(self, value: str) -> bool:
        if self.index < len(self.tokens) and self.tokens[self.index].value == value:
            self.index += 1
            return True
        return False

    def value(self) -> Any:
        if self.index >= len(self.tokens):
            return Expression("<missing>")
        token = self.tokens[self.index]
        if token.value == "{":
            return self.object()
        if token.value == "[":
            return self.array()
        self.index += 1
        if token.kind == "string":
            return token.value
        if token.kind == "number":
            number = float(token.value)
            return int(number) if number.is_integer() else number
        if token.value in ("true", "false", "null"):
            return {"true": True, "false": False, "null": None}[token.value]
        if token.kind != "identifier":
            return Expression("<dynamic>")
        name = token.value
        while self._accept(".") and self.index < len(self.tokens):
            name += "." + self.tokens[self.index].value
            self.index += 1
        if self._accept("("):
            args: list[Any] = []
            while self.index < len(self.tokens) and not self._accept(")"):
                args.append(self.value())
                if not self._accept(",") and not (
                    self.index < len(self.tokens) and self.tokens[self.index].value == ")"
                ):
                    self.index += 1
            return Expression(name, tuple(args), True)
        return Expression(name)

    def array(self) -> list[Any]:
        self._accept("[")
        values: list[Any] = []
        while self.index < len(self.tokens) and not self._accept("]"):
            values.append(self.value())
            if not self._accept(",") and not (
                self.index < len(self.tokens) and self.tokens[self.index].value == "]"
            ):
                self.index += 1
        return values

    def object(self) -> JsObject:
        line = self.tokens[self.index].line
        self._accept("{")
        values: dict[str, Any] = {}
        computed = 0
        while self.index < len(self.tokens) and not self._accept("}"):
            token = self.tokens[self.index]
            if token.value == "[":
                computed += 1
                while self.index < len(self.tokens) and not self._accept("]"):
                    self.index += 1
                key = "<computed>"
            else:
                key = token.value
                self.index += 1
            if self._accept(":"):
                values[key] = self.value()
            else:
                values[key] = Expression(key)
            if not self._accept(",") and not (
                self.index < len(self.tokens) and self.tokens[self.index].value == "}"
            ):
                self.index += 1
        return JsObject(values, line, computed)


def _source_files(source: Path) -> tuple[Path, list[Path]]:
    if source.is_file():
        return source.parent, [source]
    files = sorted(
        (path for path in source.rglob("*") if path.is_file() and path.suffix in {".js", ".vue"}),
        key=lambda path: path.relative_to(source).as_posix(),
    )
    return source, files


def _provenance(path: Path, root: Path, line: int) -> dict[str, Any]:
    relative = path.relative_to(root).as_posix()
    module_id = hashlib.sha256(relative.encode()).hexdigest()[:12]
    return {
        "component": f"module_{module_id}",
        "line": line,
        "source": f"module:{module_id}",
    }


def _walk(value: Any) -> Iterator[JsObject]:
    if isinstance(value, JsObject):
        yield value
        for child in value.values.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)
    elif isinstance(value, Expression):
        for child in value.args:
            yield from _walk(child)


def _safe_endpoint(value: str) -> str | None:
    """Strip every credential-capable URL part, regardless of vendor scheme."""
    parts = urlsplit(value)
    if _SENSITIVE.search(parts.path):
        return None
    if parts.scheme and parts.scheme not in {"http", "https"}:
        return None
    hostname = parts.hostname or ""
    if parts.scheme:
        if not hostname:
            return None
        netloc = hostname + (f":{parts.port}" if parts.port else "")
        return urlunsplit((parts.scheme, netloc, parts.path, "", ""))
    return urlunsplit(("", "", parts.path, "", ""))


def _literal(value: Any) -> bool:
    return isinstance(value, (str, int, float, bool)) or value is None


def _merge_params(value: Any) -> tuple[JsObject | None, dict[str, Any] | None]:
    if isinstance(value, JsObject):
        return value, None
    if isinstance(value, Expression) and value.called and value.name.endswith("Object.assign"):
        objects = [arg for arg in value.args if isinstance(arg, JsObject)]
        if not objects:
            return None, None
        merged: dict[str, Any] = {}
        for obj in objects:
            merged.update(obj.values)
        sources = []
        last_semantic_index = max(
            index for index, arg in enumerate(value.args) if isinstance(arg, JsObject)
        )
        last_state_index = -1
        ambiguous = False
        for index, arg in enumerate(value.args):
            if isinstance(arg, Expression) and not arg.called and arg.name not in ("Object",):
                sources.append(arg.name)
                last_state_index = index
            elif not isinstance(arg, JsObject):
                ambiguous = True
        merge = {
            "strategy": "object_assign",
            "sources": sources,
            "semantic_values_override_state": last_semantic_index > last_state_index,
        }
        if ambiguous:
            merge["unresolved"] = True
        return JsObject(merged, objects[-1].line, sum(obj.computed_keys for obj in objects)), merge
    return None, None


def extract_catalogue(source: Path, *, model: str, plugin_version: str) -> dict[str, Any]:
    """Extract literal semantic facts from ``source`` without executing it."""
    if not re.fullmatch(r"[A-Za-z0-9]{8}", model):
        raise ValueError("model must be an 8-character sn8 model code, not a device serial")
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", plugin_version):
        raise ValueError("plugin version contains unsupported characters")
    source = source.resolve()
    root, paths = _source_files(source)
    operations: dict[tuple[str, str], dict[str, Any]] = {}
    field_facts: list[dict[str, Any]] = []
    endpoints: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []

    for path in paths:
        tokens = _tokens(path.read_text(encoding="utf-8", errors="replace"))
        provenance_for = lambda line: _provenance(path, root, line)  # noqa: E731

        # Collect literal constraint/domain objects and endpoint configuration.
        for index, token in enumerate(tokens):
            if token.value != "{":
                continue
            previous = tokens[index - 1].value if index else ""
            if previous not in {"=", ":", "(", "[", ","}:
                # Function/class blocks are not object literals. Skipping them
                # avoids repeatedly parsing large minified modules as objects.
                continue
            parser = _Parser(tokens, index)
            try:
                obj = parser.object()
            except (IndexError, ValueError):
                continue
            for nested in _walk(obj):
                for key, value in nested.values.items():
                    endpoint = _safe_endpoint(value) if isinstance(value, str) else None
                    if key in _ENDPOINT_KEYS and endpoint:
                        endpoints.append(
                            {
                                "direction": _ENDPOINT_KEYS[key],
                                "path": endpoint,
                                "provenance": provenance_for(nested.line),
                            }
                        )
                    if isinstance(value, JsObject):
                        numeric = {
                            field: item
                            for field, item in value.values.items()
                            if field in {"min", "max", "step"}
                            and isinstance(item, (int, float))
                        }
                        if numeric:
                            field_facts.append(
                                {
                                    "field": key,
                                    "numeric_constraints": dict(sorted(numeric.items())),
                                    "provenance": provenance_for(nested.line),
                                    "scope": "unlinked_literal_declaration",
                                }
                            )
                    if (
                        key != "fields"
                        and isinstance(value, list)
                        and value
                        and all(_literal(item) for item in value)
                    ):
                        field_facts.append(
                            {
                                "field": key,
                                "literal_values": sorted(set(value), key=lambda item: str(item)),
                                "provenance": provenance_for(nested.line),
                                "scope": "unlinked_literal_declaration",
                            }
                        )

        for index, token in enumerate(tokens):
            if token.value not in ("luaQuery", "luaControl"):
                continue
            direction = "query" if token.value == "luaQuery" else "control"
            call_line = token.line
            cursor = index + 1
            if cursor >= len(tokens) or tokens[cursor].value != "(":
                continue
            parser = _Parser(tokens, cursor + 1)
            argument = parser.value()
            prov = provenance_for(call_line)
            if not isinstance(argument, JsObject) or "params" not in argument.values:
                unresolved.append({"field": "params", "kind": "missing_params", "provenance": prov})
                continue
            params, merge = _merge_params(argument.values["params"])
            if params is None:
                unresolved.append({"field": "params", "kind": "dynamic_params", "provenance": prov})
                continue
            selector_name = _SELECTORS[direction]
            selector = params.values.get(selector_name)
            if not _literal(selector):
                unresolved.append(
                    {"field": selector_name, "kind": "dynamic_selector", "provenance": prov}
                )
                if params.computed_keys:
                    unresolved.append(
                        {"field": "<computed>", "kind": "dynamic_parameter", "provenance": prov}
                    )
                continue
            key = (direction, str(selector))
            operation = operations.setdefault(
                key,
                {
                    "direction": direction,
                    "selector": {"name": selector_name, "value": selector},
                    "parameters": {},
                    "provenance": [],
                },
            )
            operation["provenance"].append(prov)
            if merge:
                if merge.pop("unresolved", False):
                    unresolved.append(
                        {"field": "state_merge", "kind": "dynamic_state_merge", "provenance": prov}
                    )
                else:
                    prior_merge = operation.get("state_merge")
                    if prior_merge is not None and prior_merge != merge:
                        operation.pop("state_merge")
                        operation["state_merge_conflict"] = True
                        unresolved.append(
                            {"field": "state_merge", "kind": "conflicting_state_merge", "provenance": prov}
                        )
                    elif not operation.get("state_merge_conflict"):
                        operation["state_merge"] = merge
            names = set(params.values) - {selector_name}
            fields = params.values.get("fields")
            if isinstance(fields, list):
                names.remove("fields")
                names.update(item for item in fields if isinstance(item, str))
            for name in names:
                if name == "<computed>":
                    unresolved.append(
                        {"field": name, "kind": "dynamic_parameter", "provenance": prov}
                    )
                    continue
                operation["parameters"].setdefault(name, {"name": name})

    rendered_operations: list[dict[str, Any]] = []
    for operation in operations.values():
        params = []
        for name, parameter in sorted(operation.pop("parameters").items()):
            params.append(parameter)
        operation["parameters"] = params
        operation.pop("state_merge_conflict", None)
        operation["provenance"] = sorted(
            {json.dumps(item, sort_keys=True): item for item in operation["provenance"]}.values(),
            key=lambda item: (item["source"], item["line"]),
        )
        rendered_operations.append(operation)

    return {
        "catalogue_version": CATALOGUE_VERSION,
        "provenance": {
            "model_sn8": model,
            "plugin_version": plugin_version,
            "source_kind": "unpacked_weex_plugin",
        },
        "operations": sorted(
            rendered_operations,
            key=lambda item: (item["direction"], str(item["selector"]["value"])),
        ),
        "field_facts": sorted(
            {json.dumps(item, sort_keys=True): item for item in field_facts}.values(),
            key=lambda item: (
                item["field"],
                item["provenance"]["source"],
                item["provenance"]["line"],
            ),
        ),
        "parser_endpoints": sorted(
            {json.dumps(item, sort_keys=True): item for item in endpoints}.values(),
            key=lambda item: (item["direction"], item["path"]),
        ),
        "unresolved": sorted(
            {json.dumps(item, sort_keys=True): item for item in unresolved}.values(),
            key=lambda item: (
                item["provenance"]["source"],
                item["provenance"]["line"],
                item["kind"],
                item["field"],
            ),
        ),
    }


def catalogue_json(catalogue: dict[str, Any]) -> str:
    """Return the canonical on-disk representation."""
    return json.dumps(catalogue, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Extract a conservative semantic API catalogue from an unpacked Weex plugin."
    )
    parser.add_argument("source", type=Path, help="unpacked plugin directory or JavaScript entry file")
    parser.add_argument("--model", required=True, help="sn8 model code (never a device serial)")
    parser.add_argument("--plugin-version", required=True, help="vendor plugin version")
    parser.add_argument("--output", required=True, type=Path, help="catalogue JSON destination")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.source.exists():
        print(f"Source does not exist: {args.source}", file=sys.stderr)
        return 2
    catalogue = extract_catalogue(
        args.source, model=args.model, plugin_version=args.plugin_version
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(catalogue_json(catalogue), encoding="utf-8")
    print(
        f"Wrote catalogue v{CATALOGUE_VERSION}: "
        f"{len(catalogue['operations'])} operations, "
        f"{len(catalogue['unresolved'])} unresolved findings"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
