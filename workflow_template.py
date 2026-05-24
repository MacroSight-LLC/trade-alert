"""Safe template evaluation for workflow YAML ``{{ expr }}`` placeholders."""

from __future__ import annotations

import ast
import operator
import re
from typing import Any

_TEMPLATE_RE = re.compile(r"\{\{(.+?)\}\}", re.DOTALL)

_SAFE_NAMES: dict[str, Any] = {"True": True, "False": False, "None": None}

_SAFE_FUNC_MAP: dict[str, Any] = {
    "len": len,
    "str": str,
    "int": int,
    "float": float,
    "bool": bool,
    "list": list,
    "dict": dict,
    "tuple": tuple,
    "set": set,
    "min": min,
    "max": max,
    "abs": abs,
    "round": round,
    "isinstance": isinstance,
    "sorted": sorted,
    "enumerate": enumerate,
}

_CMP_OPS: dict[type, Any] = {
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
    ast.Is: operator.is_,
    ast.IsNot: operator.is_not,
    ast.In: lambda a, b: a in b,
    ast.NotIn: lambda a, b: a not in b,
}

_BIN_OPS: dict[type, Any] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
}

_UNARY_OPS: dict[type, Any] = {
    ast.Not: operator.not_,
    ast.USub: operator.neg,
    ast.UAdd: operator.pos,
}


def safe_eval(expr: str, ns: dict[str, Any]) -> Any:
    """Evaluate a template expression via AST walking — no exec/eval."""
    _max_depth = 40
    tree = ast.parse(expr.strip(), mode="eval")

    def _eval(node: ast.AST, depth: int = 0) -> Any:  # noqa: PLR0911
        if depth > _max_depth:
            raise ValueError(f"Expression nested too deeply (>{_max_depth} levels)")
        if isinstance(node, ast.Expression):
            return _eval(node.body, depth + 1)
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Name):
            if node.id in _SAFE_NAMES:
                return _SAFE_NAMES[node.id]
            if node.id in ns:
                return ns[node.id]
            if node.id in _SAFE_FUNC_MAP:
                return _SAFE_FUNC_MAP[node.id]
            raise ValueError(f"Name {node.id!r} is not allowed")
        if isinstance(node, ast.Subscript):
            return _eval(node.value, depth + 1)[_eval(node.slice, depth + 1)]
        if isinstance(node, ast.Slice):
            return slice(
                _eval(node.lower, depth + 1) if node.lower else None,
                _eval(node.upper, depth + 1) if node.upper else None,
                _eval(node.step, depth + 1) if node.step else None,
            )
        if isinstance(node, ast.Attribute):
            if node.attr.startswith("__"):
                raise ValueError(f"Dunder attribute access is forbidden: {node.attr}")
            return getattr(_eval(node.value, depth + 1), node.attr)
        if isinstance(node, ast.UnaryOp):
            op_fn = _UNARY_OPS.get(type(node.op))
            if op_fn is None:
                raise ValueError(f"Unary op {type(node.op).__name__} not allowed")
            return op_fn(_eval(node.operand, depth + 1))
        if isinstance(node, ast.BoolOp):
            if isinstance(node.op, ast.And):
                return all(_eval(v, depth + 1) for v in node.values)
            return any(_eval(v, depth + 1) for v in node.values)
        if isinstance(node, ast.BinOp):
            op_fn = _BIN_OPS.get(type(node.op))
            if op_fn is None:
                raise ValueError(f"BinOp {type(node.op).__name__} not allowed")
            return op_fn(_eval(node.left, depth + 1), _eval(node.right, depth + 1))
        if isinstance(node, ast.Compare):
            left = _eval(node.left, depth + 1)
            for op, comparator in zip(node.ops, node.comparators):
                op_fn = _CMP_OPS.get(type(op))
                if op_fn is None:
                    raise ValueError(f"Compare op {type(op).__name__} not allowed")
                right = _eval(comparator, depth + 1)
                if not op_fn(left, right):
                    return False
                left = right
            return True
        if isinstance(node, ast.Call):
            if not (isinstance(node.func, ast.Name) and node.func.id in _SAFE_FUNC_MAP):
                raise ValueError(f"Function call not allowed: {ast.dump(node.func)}")
            func = _SAFE_FUNC_MAP[node.func.id]
            args = [_eval(a, depth + 1) for a in node.args]
            kwargs = {kw.arg: _eval(kw.value, depth + 1) for kw in node.keywords if kw.arg is not None}
            return func(*args, **kwargs)
        if isinstance(node, ast.IfExp):
            return (
                _eval(node.body, depth + 1) if _eval(node.test, depth + 1) else _eval(node.orelse, depth + 1)
            )
        if isinstance(node, ast.List):
            return [_eval(e, depth + 1) for e in node.elts]
        if isinstance(node, ast.Tuple):
            return tuple(_eval(e, depth + 1) for e in node.elts)
        if isinstance(node, ast.Dict):
            out: dict[Any, Any] = {}
            for k_node, v_node in zip(node.keys, node.values, strict=False):
                if k_node is not None:
                    out[_eval(k_node, depth + 1)] = _eval(v_node, depth + 1)
            return out
        raise ValueError(f"AST node {type(node).__name__} is not allowed")

    return _eval(tree)


def render_template(template: str, steps: dict[str, Any], extra_vars: dict[str, Any] | None = None) -> Any:
    """Evaluate ``{{ expr }}`` template expressions safely."""
    if not isinstance(template, str):
        return template

    matches = list(_TEMPLATE_RE.finditer(template))
    if not matches:
        return template

    ns: dict[str, Any] = {"steps": steps}
    if extra_vars:
        ns.update(extra_vars)

    if len(matches) == 1 and matches[0].start() == 0 and matches[0].end() == len(template.strip()):
        return safe_eval(matches[0].group(1), ns)

    result = template
    for m in reversed(matches):
        val = safe_eval(m.group(1), ns)
        result = result[: m.start()] + str(val) + result[m.end() :]
    return result


def render_params(params: Any, steps: dict[str, Any], extra_vars: dict[str, Any] | None = None) -> Any:
    """Recursively render template expressions in params dicts/lists."""
    if isinstance(params, str):
        return render_template(params, steps, extra_vars)
    if isinstance(params, dict):
        return {k: render_params(v, steps, extra_vars) for k, v in params.items()}
    if isinstance(params, list):
        return [render_params(v, steps, extra_vars) for v in params]
    return params
