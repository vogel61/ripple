"""Python 3.9 really runs this code — the parts that no import checks.

`from __future__ import annotations` turns every annotation into a string, so an
interpreter that merely imports a module never evaluates it. FastAPI does
evaluate the annotations of a *route handler* when it builds the app. On 3.9 a
`float | None` there raises

    TypeError: unsupported operand type(s) for |: 'type' and 'NoneType'

and the app does not build at all — which is how the first cold start on the
target machine died, after every module had imported cleanly.

The suite runs on 3.13, where the same annotation is fine, so this is checked
statically: a route handler must not use PEP 604 in an annotation.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

PACKAGE = pathlib.Path(__file__).resolve().parent.parent / "ripple"

#: Decorators that make FastAPI evaluate the annotations: the ones this package
#: uses. `@property`, `@staticmethod` and `@dataclass` do not evaluate them.
ROUTE_DECORATORS = {"get", "post", "put", "patch", "delete", "head", "options",
                    "api_route", "websocket"}


def _is_route(node: ast.AST) -> bool:
    for dec in getattr(node, "decorator_list", []):
        target = dec.func if isinstance(dec, ast.Call) else dec
        if isinstance(target, ast.Attribute) and target.attr in ROUTE_DECORATORS:
            return True
    return False


def _route_handlers():
    for path in sorted(PACKAGE.glob("*.py")):
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and _is_route(node):
                yield path, source, node


def test_a_route_annotation_never_uses_pep604():
    offenders = []
    for path, source, node in _route_handlers():
        annotations = [a.annotation for a in node.args.args + node.args.kwonlyargs
                       if a.annotation is not None]
        if node.returns is not None:
            annotations.append(node.returns)
        for annotation in annotations:
            text = ast.get_source_segment(source, annotation) or ""
            if "|" in text:
                offenders.append(f"{path.name}:{node.lineno} {node.name}: {text}")
    assert not offenders, (
        "PEP 604 in a route annotation breaks the app on Python 3.9 — "
        "use Optional[...]/Union[...] instead:\n" + "\n".join(offenders))


def test_there_are_routes_to_check():
    # A guard that silently stops guarding is worse than no guard: this fails if
    # the decorator detection stops matching the code.
    assert len(list(_route_handlers())) >= 10


@pytest.mark.parametrize("module", ["ripple", "ripple.config", "ripple.proxy",
                                    "ripple.init", "ripple.discover",
                                    "ripple.doctor", "ripple.keys"])
def test_the_public_modules_import(module):
    __import__(module)
