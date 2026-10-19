"""Read job scalars and evaluate their small, explicit Actions expression subset."""

from __future__ import annotations

import ast
import re


def _scalar(line, current):
    field = re.fullmatch(r"    (name|if): (.+)", line)
    if field and current is not None:
        value = field[2]
        if value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        current[field[1]] = value


def job_scalars(path):
    jobs = {}
    current = None
    inside = False
    for line in path.read_text().splitlines():
        if line == "jobs:":
            inside = True
        elif inside:
            match = re.fullmatch(r"  ([A-Za-z0-9_-]+):", line)
            if match:
                current = jobs.setdefault(match[1], {})
            else:
                _scalar(line, current)
    return jobs


class _Expression(ast.NodeVisitor):
    def __init__(self, context):
        self.context = context

    def generic_visit(self, node):
        message = "Unsupported workflow condition; extend its explicit evaluator"
        raise AssertionError(message)

    def visit_Constant(self, node):
        return node.value

    def visit_Name(self, node):
        return {**self.context, "true": True, "false": False}[node.id]

    def visit_Attribute(self, node):
        return self.visit(node.value)[node.attr]

    def visit_Call(self, node):
        if (
            isinstance(node.func, ast.Name)
            and node.func.id == "always"
            and not node.args
            and not node.keywords
        ):
            return True
        return self.generic_visit(node)

    def visit_UnaryOp(self, node):
        if isinstance(node.op, ast.Not):
            return not self.visit(node.operand)
        return self.generic_visit(node)

    def visit_BoolOp(self, node):
        result = self.visit(node.values[0])
        for item in node.values[1:]:
            if (isinstance(node.op, ast.And) and not result) or (
                isinstance(node.op, ast.Or) and result
            ):
                return result
            result = self.visit(item)
        return result

    def visit_Compare(self, node):
        if len(node.ops) != 1:
            return self.generic_visit(node)
        first, second = self.visit(node.left), self.visit(node.comparators[0])
        if isinstance(node.ops[0], ast.Eq):
            return first == second
        if isinstance(node.ops[0], ast.NotEq):
            return first != second
        return self.generic_visit(node)


def expression(text, *, event, mode=False, subject=""):
    text = text.strip()
    if text.startswith("${{") and text.endswith("}}"):
        text = text[3:-2].strip()
    text = text.replace("&&", " and ").replace("||", " or ").replace("!(", "not (")
    context = {
        "github": {"event_name": event},
        "inputs": {"source_validation": mode, "source_validation_subject": subject},
        "matrix": {"env_name": "matrix-profile"},
    }
    return _Expression(context).visit(ast.parse(text, mode="eval").body)
