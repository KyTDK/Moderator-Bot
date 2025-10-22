from __future__ import annotations

import ast
import os
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modules.i18n.config import resolve_locales_root  # noqa: E402
from modules.i18n.locales import LocaleRepository  # noqa: E402

_KEY_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+$")
_EXCLUDED_DIR_PARTS = {"tests", "test", "scripts", ".git", "__pycache__", "venv"}
_TEMPLATE_EXTENSIONS = {".jinja", ".j2", ".html"}


def _list_available_locales(locales_root: Path) -> set[str]:
    locales: set[str] = set()
    if not locales_root.exists():
        return locales
    for path in locales_root.iterdir():
        name = path.name
        if name.startswith('.'):
            continue
        if path.is_dir():
            locales.add(name)
        elif path.suffix == ".json":
            locales.add(path.stem)
    return locales


def _match_available_locale(candidate: str, available: Iterable[str]) -> str | None:
    if candidate in available:
        return candidate
    lowered = candidate.lower()
    for option in available:
        if option.lower() == lowered:
            return option
    return None


def _extract_source_language(config_path: Path) -> str | None:
    try:
        contents = config_path.read_text(encoding="utf-8")
    except OSError:
        return None
    for raw_line in contents.splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if ":" not in stripped:
            continue
        key, _, value = stripped.partition(":")
        key = key.strip()
        if key not in {"source_language", "default_locale", "defaultLanguage"}:
            continue
        candidate = value.strip().strip('"\'')
        if candidate:
            return candidate
    return None


def _detect_default_locale(repo_root: Path, locales_root: Path, available_locales: set[str]) -> str:
    env_default = (os.getenv("I18N_DEFAULT_LOCALE") or os.getenv("I18N_FALLBACK_LOCALE"))
    if env_default:
        matched = _match_available_locale(env_default, available_locales)
        if matched:
            return matched
    for config_name in ("weblate.yaml", "crowdin.yml", "crowdin.yaml"):
        config_path = repo_root / config_name
        if not config_path.exists():
            continue
        candidate = _extract_source_language(config_path)
        if not candidate:
            continue
        matched = _match_available_locale(candidate, available_locales)
        if matched:
            return matched
        # fall back to candidate even if not currently present; later check will fail loudly
        env_default = candidate
        break
    if env_default:
        raise AssertionError(
            "Configured default locale %s is not available in %s" % (env_default, locales_root)
        )
    for preference in ("en", "en-US", "en-GB", "en_GB"):
        matched = _match_available_locale(preference, available_locales)
        if matched:
            return matched
    if available_locales:
        return sorted(available_locales)[0]
    raise AssertionError("No locale data found to determine default locale")


def _flatten_keys(data: dict[str, Any], prefix: str = "") -> set[str]:
    keys: set[str] = set()
    if prefix:
        keys.add(prefix)
    for key, value in data.items():
        if not isinstance(key, str):
            continue
        nested = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            keys.update(_flatten_keys(value, nested))
        else:
            keys.add(nested)
    return keys


def _should_skip(path: Path, repo_root: Path) -> bool:
    try:
        relative = path.relative_to(repo_root)
    except ValueError:
        return True
    return any(part in _EXCLUDED_DIR_PARTS for part in relative.parts)


def _normalise_candidate(raw: str, top_level: set[str]) -> str | None:
    candidate = raw.strip()
    if "." not in candidate or candidate.startswith(".") or candidate.endswith("."):
        return None
    if any(ch in candidate for ch in {" ", "\n", "\t"}):
        return None
    lowered = candidate.lower()
    if lowered.endswith(".json") or lowered.endswith(".py"):
        return None
    if not _KEY_PATTERN.fullmatch(candidate):
        return None
    first = candidate.split(".", 1)[0]
    if first not in top_level:
        return None
    return candidate


def _collect_python_candidates(repo_root: Path, top_level: set[str]) -> dict[Path, set[str]]:
    candidates: dict[Path, set[str]] = {}
    for path in repo_root.rglob("*.py"):
        if _should_skip(path, repo_root):
            continue
        try:
            source = path.read_text(encoding="utf-8-sig")
        except UnicodeDecodeError:
            source = path.read_text(encoding="utf-8", errors="ignore")
        try:
            tree = ast.parse(source, filename=str(path))
        except SyntaxError as exc:  # pragma: no cover - fail loudly for invalid source
            raise AssertionError(f"Unable to parse {path}: {exc}") from exc
        module_strings, module_dict_keys = _gather_module_info(tree)
        dynamic_keys = _collect_dynamic_localization_candidates(
            tree,
            module_strings,
            module_dict_keys,
            top_level,
        )
        if dynamic_keys:
            candidates.setdefault(path, set()).update(dynamic_keys)
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                candidate = _normalise_candidate(node.value, top_level)
                if candidate:
                    candidates.setdefault(path, set()).add(candidate)
    return candidates


def _collect_template_candidates(repo_root: Path, top_level: set[str]) -> dict[Path, set[str]]:
    matches: dict[Path, set[str]] = {}
    for extension in _TEMPLATE_EXTENSIONS:
        for path in repo_root.rglob(f"*{extension}"):
            if _should_skip(path, repo_root):
                continue
            try:
                contents = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                contents = path.read_text(encoding="utf-8", errors="ignore")
            for match in re.findall(r"['\"]([A-Za-z0-9_.-]+)['\"]", contents):
                candidate = _normalise_candidate(match, top_level)
                if candidate:
                    matches.setdefault(path, set()).add(candidate)
    return matches


def _merge_candidates(*sources: dict[Path, set[str]]) -> dict[Path, set[str]]:
    merged: dict[Path, set[str]] = defaultdict(set)
    for source in sources:
        for path, values in source.items():
            merged[path].update(values)
    return merged


def _extract_constant_string(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _gather_module_info(tree: ast.AST) -> tuple[dict[str, str], dict[str, set[str]]]:
    string_constants: dict[str, str] = {}
    dict_keys: dict[str, set[str]] = {}

    for node in getattr(tree, "body", []):
        if isinstance(node, ast.Assign):
            if not node.targets:
                continue
            value = node.value
            constant = _extract_constant_string(value)
            if constant is not None:
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        string_constants[target.id] = constant
                continue
            if isinstance(value, ast.Dict):
                keys: set[str] = set()
                for key in value.keys:
                    constant_key = _extract_constant_string(key)
                    if constant_key is not None:
                        keys.add(constant_key)
                if keys:
                    for target in node.targets:
                        if isinstance(target, ast.Name):
                            dict_keys[target.id] = set(keys)
        elif isinstance(node, ast.AnnAssign):
            target = node.target
            value = node.value
            if value is None:
                continue
            constant = _extract_constant_string(value)
            if constant is not None and isinstance(target, ast.Name):
                string_constants[target.id] = constant
                continue
            if isinstance(value, ast.Dict) and isinstance(target, ast.Name):
                keys: set[str] = set()
                for key in value.keys:
                    constant_key = _extract_constant_string(key)
                    if constant_key is not None:
                        keys.add(constant_key)
                if keys:
                    dict_keys[target.id] = set(keys)

    return string_constants, dict_keys


def _collect_dynamic_localization_candidates(
    tree: ast.AST,
    string_constants: dict[str, str],
    dict_keys: dict[str, set[str]],
    top_level: set[str],
) -> set[str]:
    collector = _LocalizationCallCollector(string_constants, dict_keys, top_level)
    collector.collect(tree)
    return collector.candidates


class _LocalizationCallCollector:
    def __init__(
        self,
        string_constants: dict[str, str],
        dict_keys: dict[str, set[str]],
        top_level: set[str],
    ) -> None:
        self._string_constants = string_constants
        self._dict_keys = dict_keys
        self._top_level = top_level
        self.candidates: set[str] = set()

    def collect(self, tree: ast.AST) -> None:
        body = getattr(tree, "body", [])
        self._process_block(body, {})

    def _process_block(self, body: list[ast.stmt], context: dict[str, set[str]]) -> None:
        local_context = {name: set(values) for name, values in context.items()}
        for statement in body:
            if isinstance(statement, ast.Assign):
                value_options = self._evaluate_expression(statement.value, local_context)
                self._inspect_expression(statement.value, local_context)
                if value_options:
                    for target in statement.targets:
                        if isinstance(target, ast.Name):
                            local_context[target.id] = set(value_options)
            elif isinstance(statement, ast.AnnAssign):
                if statement.value is not None:
                    value_options = self._evaluate_expression(statement.value, local_context)
                    self._inspect_expression(statement.value, local_context)
                    if value_options and isinstance(statement.target, ast.Name):
                        local_context[statement.target.id] = set(value_options)
            elif isinstance(statement, ast.Expr):
                self._inspect_expression(statement.value, local_context)
            elif isinstance(statement, ast.Return):
                if statement.value:
                    self._inspect_expression(statement.value, local_context)
            elif isinstance(statement, ast.If):
                self._inspect_expression(statement.test, local_context)
                body_context = {name: set(values) for name, values in local_context.items()}
                self._process_block(statement.body, body_context)
                orelse_context = {name: set(values) for name, values in local_context.items()}
                self._process_block(statement.orelse, orelse_context)
                local_context = self._merge_contexts(body_context, orelse_context)
            elif isinstance(statement, (ast.For, ast.AsyncFor)):
                self._inspect_expression(statement.iter, local_context)
                loop_context = {name: set(values) for name, values in local_context.items()}
                self._process_block(statement.body, loop_context)
                self._process_block(statement.orelse, {name: set(values) for name, values in local_context.items()})
            elif isinstance(statement, (ast.With, ast.AsyncWith)):
                for item in statement.items:
                    self._inspect_expression(item.context_expr, local_context)
                    if item.optional_vars:
                        self._inspect_expression(item.optional_vars, local_context)
                self._process_block(statement.body, {name: set(values) for name, values in local_context.items()})
            elif isinstance(statement, ast.Try):
                self._process_block(statement.body, {name: set(values) for name, values in local_context.items()})
                for handler in statement.handlers:
                    handler_context = {name: set(values) for name, values in local_context.items()}
                    if handler.type:
                        self._inspect_expression(handler.type, handler_context)
                    self._process_block(handler.body, handler_context)
                self._process_block(statement.orelse, {name: set(values) for name, values in local_context.items()})
                self._process_block(statement.finalbody, {name: set(values) for name, values in local_context.items()})
            elif isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self._process_block(statement.body, {})
            elif isinstance(statement, ast.ClassDef):
                self._process_block(statement.body, {})
            else:  # pragma: no cover - best effort fall back
                for child in ast.iter_child_nodes(statement):
                    if isinstance(child, ast.stmt):
                        self._process_block([child], {name: set(values) for name, values in local_context.items()})
                    elif isinstance(child, ast.expr):
                        self._inspect_expression(child, local_context)

    def _merge_contexts(
        self,
        a: dict[str, set[str]],
        b: dict[str, set[str]],
    ) -> dict[str, set[str]]:
        merged: dict[str, set[str]] = {}
        for key in set(a) | set(b):
            values: set[str] = set()
            values.update(a.get(key, set()))
            values.update(b.get(key, set()))
            if values:
                merged[key] = values
        return merged

    def _inspect_expression(self, node: ast.AST, context: dict[str, set[str]]) -> None:
        if isinstance(node, ast.Call):
            self._handle_call(node, context)
            for arg in node.args:
                self._inspect_expression(arg, context)
            for keyword in node.keywords:
                if keyword.value:
                    self._inspect_expression(keyword.value, context)
        elif isinstance(node, ast.IfExp):
            self._inspect_expression(node.test, context)
            self._inspect_expression(node.body, context)
            self._inspect_expression(node.orelse, context)
        elif isinstance(node, ast.BinOp):
            self._inspect_expression(node.left, context)
            self._inspect_expression(node.right, context)
        elif isinstance(node, ast.UnaryOp):
            self._inspect_expression(node.operand, context)
        elif isinstance(node, ast.BoolOp):
            for value in node.values:
                self._inspect_expression(value, context)
        elif isinstance(node, ast.Compare):
            self._inspect_expression(node.left, context)
            for comparator in node.comparators:
                self._inspect_expression(comparator, context)
        elif isinstance(node, ast.JoinedStr):
            for value in node.values:
                if isinstance(value, ast.FormattedValue):
                    self._inspect_expression(value.value, context)
        elif isinstance(node, ast.FormattedValue):
            self._inspect_expression(node.value, context)
        elif isinstance(node, (ast.List, ast.Tuple, ast.Set)):
            for element in node.elts:
                self._inspect_expression(element, context)
        elif isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values):
                if key:
                    self._inspect_expression(key, context)
                if value:
                    self._inspect_expression(value, context)
        elif isinstance(node, ast.Attribute):
            self._inspect_expression(node.value, context)
        elif isinstance(node, ast.Subscript):
            self._inspect_expression(node.value, context)
            self._inspect_expression(node.slice, context)
        elif isinstance(node, ast.Await):  # pragma: no cover - structural inspection
            self._inspect_expression(node.value, context)
        elif isinstance(node, (ast.Yield, ast.YieldFrom)):
            if node.value:
                self._inspect_expression(node.value, context)

    def _handle_call(self, node: ast.Call, context: dict[str, set[str]]) -> None:
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "get":
            dict_name = self._name_from_node(func.value)
            if dict_name and dict_name in self._dict_keys and node.args:
                first = node.args[0]
                if isinstance(first, ast.Name):
                    context.setdefault(first.id, set()).update(self._dict_keys[dict_name])
        if self._is_localize_call(func):
            namespace_expr = self._resolve_argument(node, 1, "namespace")
            key_expr = self._resolve_argument(node, 2, "key")
            if namespace_expr is not None and key_expr is not None:
                namespaces = self._evaluate_expression(namespace_expr, context)
                keys = self._evaluate_expression(key_expr, context)
                if namespaces and keys:
                    for namespace in namespaces:
                        for key in keys:
                            candidate = f"{namespace}.{key}" if key else namespace
                            normalised = _normalise_candidate(candidate, self._top_level)
                            if normalised:
                                self.candidates.add(normalised)

    def _resolve_argument(self, node: ast.Call, position: int, keyword: str) -> ast.AST | None:
        if len(node.args) > position:
            return node.args[position]
        for kw in node.keywords:
            if kw.arg == keyword:
                return kw.value
        return None

    def _evaluate_expression(
        self,
        node: ast.AST,
        context: dict[str, set[str]],
    ) -> set[str] | None:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return {node.value}
        if isinstance(node, ast.Name):
            if node.id in context and context[node.id]:
                return set(context[node.id])
            if node.id in self._string_constants:
                return {self._string_constants[node.id]}
            return None
        if isinstance(node, ast.JoinedStr):
            parts: list[set[str]] = []
            for value in node.values:
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    parts.append({value.value})
                elif isinstance(value, ast.FormattedValue):
                    evaluated = self._evaluate_expression(value.value, context)
                    if evaluated is None:
                        return None
                    parts.append(evaluated)
                else:
                    return None
            combinations = {""}
            for options in parts:
                combinations = {prefix + suffix for prefix in combinations for suffix in options}
            return combinations
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            left = self._evaluate_expression(node.left, context)
            right = self._evaluate_expression(node.right, context)
            if left and right:
                return {l + r for l in left for r in right}
            return None
        if isinstance(node, ast.IfExp):
            body = self._evaluate_expression(node.body, context)
            orelse = self._evaluate_expression(node.orelse, context)
            result: set[str] = set()
            if body:
                result.update(body)
            if orelse:
                result.update(orelse)
            return result or None
        return None

    def _is_localize_call(self, func: ast.AST) -> bool:
        if isinstance(func, ast.Name):
            return func.id == "localize_message"
        if isinstance(func, ast.Attribute):
            return func.attr == "localize_message"
        return False

    def _name_from_node(self, node: ast.AST) -> str | None:
        if isinstance(node, ast.Name):
            return node.id
        return None


def _format_missing(missing: dict[Path, set[str]], default_locale: str) -> str:
    lines = [
        f"Missing locale entries for default locale '{default_locale}':",
    ]
    for path in sorted(missing, key=lambda p: str(p)):
        keys = ", ".join(sorted(missing[path]))
        relative = path.relative_to(ROOT)
        lines.append(f"  {relative}: {keys}")
    return "\n".join(lines)


def test_all_locale_references_exist_in_default_locale() -> None:
    configured_root = os.getenv("I18N_LOCALES_DIR") or os.getenv("LOCALES_DIR")
    locales_root, _ = resolve_locales_root(configured_root, ROOT)
    available_locales = _list_available_locales(locales_root)
    default_locale = _detect_default_locale(ROOT, locales_root, available_locales)

    repository = LocaleRepository(locales_root, default_locale=default_locale)
    repository.ensure_loaded()
    snapshot = repository.get_locale_snapshot(default_locale)
    if not snapshot:
        pytest.fail(f"Locale '{default_locale}' has no entries loaded from {locales_root}")

    valid_keys = _flatten_keys(snapshot)
    top_level = set(snapshot.keys())

    python_candidates = _collect_python_candidates(ROOT, top_level)
    template_candidates = _collect_template_candidates(ROOT, top_level)
    candidates = _merge_candidates(python_candidates, template_candidates)

    missing: dict[Path, set[str]] = {}
    for path, keys in candidates.items():
        unresolved = {key for key in keys if key not in valid_keys}
        if unresolved:
            missing[path] = unresolved

    if missing:
        pytest.fail(_format_missing(missing, default_locale))
