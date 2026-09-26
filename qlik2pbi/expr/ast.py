"""AST for Qlik expressions (chart expressions, master items, script field expressions)."""

from __future__ import annotations

from dataclasses import dataclass, field


class Node:
    pass


@dataclass
class Num(Node):
    value: str


@dataclass
class Str(Node):
    value: str  # a 'single-quoted' string literal


@dataclass
class FieldRef(Node):
    name: str
    quoted: bool = False  # written as [x] or "x"


@dataclass
class SetElement:
    """One item of an element set `{...}` in a set modifier."""

    kind: str  # number | string | search | dollar | field | other
    text: str


@dataclass
class SetModifier:
    field: str
    op: str  # = | += | -= | *= | /=
    elements: list[SetElement] = field(default_factory=list)
    clear: bool = False  # `Field=` with nothing after it
    raw: str = ""


@dataclass
class SetExpr:
    identifier: str = "$"  # $ | 1 | a bookmark id | an alternate state
    modifiers: list[SetModifier] = field(default_factory=list)
    raw: str = ""
    compound: bool = False  # set operators between sets: {A}+{B}


@dataclass
class Call(Node):
    name: str
    args: list[Node] = field(default_factory=list)
    set_expr: SetExpr | None = None
    distinct: bool = False
    nodistinct: bool = False
    total: bool = False
    total_fields: list[str] = field(default_factory=list)


@dataclass
class Binary(Node):
    op: str  # + - * / & = <> < > <= >= and or xor like
    left: Node
    right: Node


@dataclass
class Unary(Node):
    op: str  # - | not
    operand: Node


@dataclass
class Dollar(Node):
    """`$(=expr)` left after variable expansion - evaluated once, globally."""

    inner: str


def walk(node: Node):
    yield node
    if isinstance(node, Call):
        for a in node.args:
            yield from walk(a)
    elif isinstance(node, Binary):
        yield from walk(node.left)
        yield from walk(node.right)
    elif isinstance(node, Unary):
        yield from walk(node.operand)
