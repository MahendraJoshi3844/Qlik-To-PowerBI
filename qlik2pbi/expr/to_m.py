"""Qlik script expressions (LOAD field expressions, WHERE clauses) -> Power Query M.

Row-level only: these run once per record inside `Table.AddColumn(... each ...)`
or `Table.SelectRows(... each ...)`. The same trust contract as DAX: a function
without a faithful M equivalent raises `Untranslatable`, and the caller does not
guess - it reports the table instead.

Null semantics are the trap. Qlik treats a null in `&` as an empty string and
returns null from division by zero; M propagates null through `&` and raises
on nothing but returns infinity from `x / 0`. Both are written out explicitly.
"""

from __future__ import annotations

import re
from typing import Callable

from qlik2pbi.expr.ast import Binary, Call, Dollar, FieldRef, Node, Num, Str, Unary
from qlik2pbi.expr.parser import ParseError, parse


class Untranslatable(Exception):
    pass


_SIMPLE = re.compile(r"^[A-Za-z_][A-Za-z0-9_ ]*$")


def m_string(value: str) -> str:
    """An M text literal. Line breaks become #(cr)/#(lf) escapes, so re-indenting
    generated M can never change a literal's contents (SQL text, inline values)."""
    escaped = (
        value.replace("#(", "#(#)(")
        .replace('"', '""')
        .replace("\r", "#(cr)")
        .replace("\n", "#(lf)")
        .replace("\t", "#(tab)")
    )
    return '"' + escaped + '"'


def m_field(name: str) -> str:
    """Record access inside `each`. `[Name]` for plain names, else `Record.Field`."""
    if _SIMPLE.match(name) and not name.startswith(" ") and not name.endswith(" "):
        return f"[{name}]"
    return f"Record.Field(_, {m_string(name)})"


def m_identifier(name: str) -> str:
    return "#" + m_string(name)


class ToM:
    """`lookup(map_name, key_m, default_m)` builds ApplyMap; supplied by the interpreter."""

    def __init__(self, fields: set[str] | None = None,
                 lookup: Callable[[str, str, str], str] | None = None):
        self.fields = {f.lower(): f for f in (fields or set())}
        self.lookup = lookup
        self.notes: list[str] = []

    def translate(self, text: str) -> str:
        try:
            node = parse(text)
        except ParseError as exc:
            raise Untranslatable(f"expression not understood ({exc})") from exc
        return self.node(node)

    def field(self, name: str) -> str:
        if self.fields and name.lower() not in self.fields:
            raise Untranslatable(f"'{name}' is not a field of the source at this point")
        return m_field(self.fields.get(name.lower(), name))

    def node(self, n: Node) -> str:
        if isinstance(n, Num):
            return n.value
        if isinstance(n, Str):
            return m_string(n.value)
        if isinstance(n, FieldRef):
            return self.field(n.name)
        if isinstance(n, Dollar):
            raise Untranslatable("$(=...) expansion inside the script")
        if isinstance(n, Unary):
            inner = self.node(n.operand)
            return f"(not {inner})" if n.op == "not" else f"(-{inner})"
        if isinstance(n, Binary):
            return self.binary(n)
        if isinstance(n, Call):
            return self.call(n)
        raise Untranslatable(f"unsupported construct {type(n).__name__}")

    def binary(self, n: Binary) -> str:
        a, b = self.node(n.left), self.node(n.right)
        if n.op == "&":
            # Text.Combine skips nulls, which is what Qlik's '&' does.
            return f"Text.Combine({{Text.From({a}), Text.From({b})}})"
        if n.op == "/":
            return f"(if {b} = 0 or {b} = null then null else {a} / {b})"
        if n.op in ("+", "-", "*"):
            return f"({a} {n.op} {b})"
        if n.op in ("=", "<>", "<", ">", "<=", ">="):
            return f"({a} {n.op} {b})"
        if n.op in ("and", "or"):
            return f"({a} {n.op} {b})"
        raise Untranslatable(f"operator '{n.op}'")

    def call(self, c: Call) -> str:
        name = c.name.lower()
        if c.set_expr is not None or c.total or c.distinct:
            raise Untranslatable(f"{c.name}() with set analysis or qualifiers in the script")
        a = c.args
        if name in ("null",):
            return "null"
        if name in ("true",):
            return "true"
        if name in ("false",):
            return "false"
        if name == "applymap":
            if not self.lookup or len(a) < 2 or not isinstance(a[0], Str):
                raise Untranslatable("ApplyMap() with a computed map name")
            default = self.node(a[2]) if len(a) > 2 else self.node(a[1])
            return self.lookup(a[0].value, self.node(a[1]), default)
        args = [self.node(x) for x in a]
        simple = {
            ("year", 1): "Date.Year({0})",
            ("month", 1): "Date.Month({0})",
            ("day", 1): "Date.Day({0})",
            ("upper", 1): "Text.Upper({0})",
            ("lower", 1): "Text.Lower({0})",
            ("trim", 1): "Text.Trim({0})",
            ("ltrim", 1): "Text.TrimStart({0})",
            ("rtrim", 1): "Text.TrimEnd({0})",
            ("len", 1): "Text.Length(Text.From({0}))",
            ("left", 2): "Text.Start(Text.From({0}), {1})",
            ("right", 2): "Text.End(Text.From({0}), {1})",
            ("mid", 3): "Text.Middle(Text.From({0}), {1} - 1, {2})",
            ("mid", 2): "Text.Middle(Text.From({0}), {1} - 1)",
            ("replace", 3): "Text.Replace(Text.From({0}), {1}, {2})",
            ("text", 1): "Text.From({0})",
            ("makedate", 3): "#date({0}, {1}, {2})",
            ("makedate", 2): "#date({0}, {1}, 1)",
            ("makedate", 1): "#date({0}, 1, 1)",
            ("fabs", 1): "Number.Abs({0})",
            ("sqrt", 1): "Number.Sqrt({0})",
            ("exp", 1): "Number.Exp({0})",
            ("log", 1): "Number.Ln({0})",
            ("log10", 1): "Number.Log10({0})",
            ("pow", 2): "Number.Power({0}, {1})",
            ("mod", 2): "Number.Mod({0}, {1})",
            ("div", 2): "Number.IntegerDivide({0}, {1})",
            ("alt", 2): "({0} ?? {1})",
            ("isnull", 1): "({0} = null)",
            ("if", 3): "(if {0} then {1} else {2})",
            ("if", 2): "(if {0} then {1} else null)",
            ("monthstart", 1): "Date.StartOfMonth({0})",
            ("yearstart", 1): "Date.StartOfYear({0})",
            ("quarterstart", 1): "Date.StartOfQuarter({0})",
            ("weekday", 1): "Date.DayOfWeek({0}, Day.Monday)",
            ("today", 0): "Date.From(DateTime.LocalNow())",
            ("now", 0): "DateTime.LocalNow()",
            ("num", 1): "{0}",
            ("date", 1): "{0}",
            ("floor", 1): "Number.RoundDown({0})",
            ("ceil", 1): "Number.RoundUp({0})",
        }
        key = (name, len(args))
        if key in simple:
            if name in ("num", "date"):
                self.notes.append(f"{c.name}() formatting is display-only; the value is kept and the format belongs on the column.")
            return simple[key].format(*args)
        if name == "round":
            if len(args) == 1:
                return f"Number.Round({args[0]}, 0, RoundingMode.AwayFromZero)"
            step = a[1]
            if isinstance(step, Num):
                digits = _digits_for_step(step.value)
                if digits is not None:
                    return f"Number.Round({args[0]}, {digits}, RoundingMode.AwayFromZero)"
            raise Untranslatable("Round() to a step that is not a power of ten")
        if name == "capitalize" and len(args) == 1:
            self.notes.append("Capitalize() became Text.Proper, which also lower-cases the rest of each word.")
            return f"Text.Proper({args[0]})"
        if name == "match" and len(args) >= 2:
            branches = " else ".join(f"if {args[0]} = {v} then {i}" for i, v in enumerate(args[1:], 1))
            return f"({branches} else 0)"
        if name == "pick" and len(args) >= 2:
            branches = " else ".join(f"if {args[0]} = {i} then {v}" for i, v in enumerate(args[1:], 1))
            return f"({branches} else null)"
        raise Untranslatable(f"{c.name}() has no verified Power Query equivalent")


def _digits_for_step(step: str) -> int | None:
    try:
        value = float(step)
    except ValueError:
        return None
    for digits in range(-6, 7):
        if abs(value - 10 ** (-digits)) < 1e-12:
            return digits
    return None
