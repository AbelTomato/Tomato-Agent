import ast
import math
import operator

from pydantic import BaseModel, Field

from app.agent.models import ToolDefinition
from .base import ToolContext, ToolResult


class CalculatorInput(BaseModel):
    expression: str = Field(min_length=1, max_length=500)


class Calculator:
    name = "calculator"
    description = "Safely calculate a basic arithmetic expression."
    input_model = CalculatorInput
    _ops = {
        ast.Add: operator.add,
        ast.Sub: operator.sub,
        ast.Mult: operator.mul,
        ast.Div: operator.truediv,
        ast.FloorDiv: operator.floordiv,
        ast.Mod: operator.mod,
        ast.Pow: operator.pow,
    }

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description=self.description,
            parameters=CalculatorInput.model_json_schema(),
        )

    def _eval(self, node: ast.AST) -> float:
        if isinstance(node, ast.Expression):
            return self._eval(node.body)
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, (int, float))
            and not isinstance(node.value, bool)
        ):
            return node.value
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            value = self._eval(node.operand)
            return value if isinstance(node.op, ast.UAdd) else -value
        if isinstance(node, ast.BinOp) and type(node.op) in self._ops:
            left, right = self._eval(node.left), self._eval(node.right)
            if isinstance(node.op, ast.Pow) and abs(right) > 100:
                raise ValueError("Exponent is too large")
            value = self._ops[type(node.op)](left, right)
            if not math.isfinite(value):
                raise ValueError("Result is not finite")
            return value
        raise ValueError("Unsupported expression")

    async def execute(self, arguments: dict, context: ToolContext) -> ToolResult:
        data = self.input_model.model_validate(arguments)
        try:
            tree = ast.parse(data.expression, mode="eval")
            value = self._eval(tree)
            return ToolResult(
                success=True, data={"expression": data.expression, "value": value}
            )
        except (SyntaxError, ValueError, ZeroDivisionError) as exc:
            return ToolResult(success=False, error=str(exc))
