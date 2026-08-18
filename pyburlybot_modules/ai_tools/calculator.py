"""Exact local arithmetic for the bbm harness: a small AST walker, no eval()."""

import ast
from collections.abc import Callable
from operator import add, floordiv, mod, mul, neg, pos, sub, truediv
from typing import Any

from pyburlybot_modules.ai_tools import AITool, ToolContext
from util.types import BotLike


_BINARY: dict[type[ast.operator], Callable[[Any, Any], Any]] = {
    ast.Add: add,
    ast.Sub: sub,
    ast.Mult: mul,
    ast.Div: truediv,
    ast.FloorDiv: floordiv,
    ast.Mod: mod,
}
_UNARY: dict[type[ast.unaryop], Callable[[Any], Any]] = {ast.UAdd: pos, ast.USub: neg}
# bounds that keep hostile expressions (9**9**9, huge factorial-ish pows)
# from pinning a worker thread or producing megabyte answers
_MAX_EXPONENT = 512
_MAX_RESULT_BITS = 8192


def evaluate(expression: str) -> int | float:
    """Evaluate an arithmetic expression; raises ValueError on anything else."""
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as exc:
        raise ValueError("invalid expression (%s)" % exc.msg) from exc
    return _eval(tree.body)


def _eval(node: ast.expr) -> int | float:
    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
            raise ValueError("only numbers are allowed")
        return node.value
    if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY:
        return _UNARY[type(node.op)](_eval(node.operand))
    if isinstance(node, ast.BinOp):
        left = _eval(node.left)
        right = _eval(node.right)
        if isinstance(node.op, ast.Pow):
            result = _power(left, right)
        else:
            operation = _BINARY.get(type(node.op))
            if operation is None:
                raise ValueError(
                    "unsupported operator %s" % type(node.op).__name__.lower()
                )
            result = operation(left, right)
        if isinstance(result, int) and result.bit_length() > _MAX_RESULT_BITS:
            raise ValueError("result too large")
        return result
    raise ValueError("unsupported syntax (%s)" % type(node).__name__.lower())


def _power(left: int | float, right: int | float) -> int | float:
    if abs(right) > _MAX_EXPONENT:
        raise ValueError("exponent too large")
    if (
        isinstance(left, int)
        and isinstance(right, int)
        and right > 0
        and left.bit_length() * right > _MAX_RESULT_BITS
    ):
        raise ValueError("result too large")
    try:
        return left**right
    except OverflowError as exc:
        raise ValueError("result too large") from exc


def format_result(value: int | float) -> str:
    if isinstance(value, float):
        if value.is_integer() and abs(value) < 1e15:
            return str(int(value))
        return repr(value)
    return str(value)


def _calculate(ctx: ToolContext, args: dict[str, Any]) -> str:
    expression = str(args.get("expression") or "").strip()
    if not expression:
        return "Error: no expression given."
    try:
        value = evaluate(expression)
    except (ValueError, ZeroDivisionError) as exc:
        return "Error: %s." % exc
    return "%s = %s" % (expression, format_result(value))


def get_tools(bot: BotLike) -> tuple[AITool, ...]:
    return (
        AITool(
            name="calculate",
            description=(
                "Evaluate an arithmetic expression exactly. Supports "
                "+, -, *, /, //, %, ** and parentheses. Use this for any "
                "arithmetic instead of computing it yourself."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "expression": {
                        "type": "string",
                        "description": "The expression, e.g. '3475 * 786324 / 3'.",
                    }
                },
                "required": ["expression"],
            },
            func=_calculate,
        ),
    )
