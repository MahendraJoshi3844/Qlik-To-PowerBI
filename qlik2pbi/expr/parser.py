"""Parser for the Qlik expression language.

Qlik expressions are the same language in chart measures, master items,
calculated dimensions and load-script field expressions. Quirks handled here:

* `'single'` quotes are strings, `"double"` quotes and `[brackets]` are field names;
* aggregation calls take a leading set expression `{<Year={2024}>}`, and the
  qualifiers `DISTINCT`, `NODISTINCT`, `TOTAL [<f1, f2>]`;
* `$(var)` dollar-sign expansion is textual and happens *before* parsing
  (`expand`), exactly as the Qlik engine does it; `$(=expr)` is evaluated once in
  the selection state and is kept as a `Dollar` node for the translator.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from qlik2pbi.expr.ast import (
    Binary,
    Call,
    Dollar,
    FieldRef,
    Node,
    Num,
    SetElement,
    SetExpr,
    SetModifier,
    Str,
    Unary,
)


class ParseError(ValueError):
    pass


# --- dollar-sign expansion --------------------------------------------------------


_DOLLAR = re.compile(r"\$\(([^()=][^()]*)\)")


def expand(text: str, variables: dict[str, str], depth: int = 0) -> tuple[str, list[str]]:
    """Replace `$(name)` with the variable's text. Returns (text, names that were unknown).

    `$(=expr)` is left in place. A variable whose definition starts with `=` is
    itself an expression evaluated in the selection state; expanding it inserts
    the expression, as Qlik does when the variable is used inside `$()`.
    """
    unknown: list[str] = []
    if depth > 10:
        raise ParseError("Variable expansion nests more than 10 levels deep.")

    def repl(m: re.Match) -> str:
        name = m.group(1).strip()
        if "(" in name or "," in name:
            unknown.append(name)
            return m.group(0)
        key = next((k for k in variables if k.lower() == name.lower()), None)
        if key is None:
            unknown.append(name)
            return m.group(0)
        value = variables[key]
        if value.startswith("="):
            value = value[1:]
        return value

    out = _DOLLAR.sub(repl, text)
    if out != text and _DOLLAR.search(out):
        out, more = expand(out, variables, depth + 1)
        unknown += more
    return out, unknown


# --- lexer ----------------------------------------------------------------------


@dataclass
class Tok:
    kind: str  # num str field ident op set dollar eof
    value: str
    pos: int


_OPS2 = ("<=", ">=", "<>")
_OPS1 = "+-*/&=<>(),"
_WORD_OPS = {"and", "or", "xor", "not", "like"}


def _read_balanced(text: str, i: int, open_: str, close: str) -> int:
    """Index just past the `close` matching the `open_` at i (quotes respected)."""
    depth = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch in "'\"":
            j = text.find(ch, i + 1)
            if j < 0:
                raise ParseError(f"Unterminated quote at {i}")
            i = j + 1
            continue
        if ch == "[" and open_ != "[":
            j = text.find("]", i + 1)
            if j < 0:
                raise ParseError(f"Unterminated [ at {i}")
            i = j + 1
            continue
        if ch == open_:
            depth += 1
        elif ch == close:
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    raise ParseError(f"Unbalanced {open_}{close}")


def tokenize(text: str) -> list[Tok]:
    toks: list[Tok] = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch.isspace():
            i += 1
            continue
        if ch == "/" and text.startswith("//", i):
            j = text.find("\n", i)
            i = n if j < 0 else j
            continue
        if ch == "/" and text.startswith("/*", i):
            j = text.find("*/", i + 2)
            i = n if j < 0 else j + 2
            continue
        if ch.isdigit() or (ch == "." and i + 1 < n and text[i + 1].isdigit()):
            j = i
            while j < n and (text[j].isdigit() or text[j] == "."):
                j += 1
            if j < n and text[j] in "eE" and j + 1 < n and (text[j + 1].isdigit() or text[j + 1] in "+-"):
                j += 2
                while j < n and text[j].isdigit():
                    j += 1
            toks.append(Tok("num", text[i:j], i))
            i = j
            continue
        if ch == "'":
            j, buf = i + 1, []
            while j < n:
                if text[j] == "'":
                    if j + 1 < n and text[j + 1] == "'":
                        buf.append("'")
                        j += 2
                        continue
                    break
                buf.append(text[j])
                j += 1
            if j >= n:
                raise ParseError(f"Unterminated string at {i}")
            toks.append(Tok("str", "".join(buf), i))
            i = j + 1
            continue
        if ch in "\"[`":
            close = {"\"": "\"", "[": "]", "`": "`"}[ch]
            j = text.find(close, i + 1)
            if j < 0:
                raise ParseError(f"Unterminated field name at {i}")
            toks.append(Tok("field", text[i + 1 : j], i))
            i = j + 1
            continue
        if ch == "{":
            j = _read_balanced(text, i, "{", "}")
            # Set operators between whole sets ({1}-{$}, {A}+{B}) make one compound set.
            while True:
                k = j
                while k < n and text[k].isspace():
                    k += 1
                if k < n and text[k] in "+-*/":
                    m = k + 1
                    while m < n and text[m].isspace():
                        m += 1
                    if m < n and text[m] == "{":
                        j = _read_balanced(text, m, "{", "}")
                        continue
                break
            toks.append(Tok("set", text[i:j], i))
            i = j
            continue
        if ch == "$" and text.startswith("$(=", i):
            j = _read_balanced(text, i + 1, "(", ")")
            toks.append(Tok("dollar", text[i + 3 : j - 1], i))
            i = j
            continue
        if ch.isalpha() or ch in "_%#@$":
            j = i
            while j < n and (text[j].isalnum() or text[j] in "_%#@$."):
                j += 1
            word = text[i:j]
            # `Date#`, `Num#` and friends are function names including the '#'.
            toks.append(Tok("ident", word, i))
            i = j
            continue
        two = text[i : i + 2]
        if two in _OPS2:
            toks.append(Tok("op", two, i))
            i += 2
            continue
        if ch in _OPS1:
            toks.append(Tok("op", ch, i))
            i += 1
            continue
        raise ParseError(f"Unexpected character {ch!r} at {i}")
    toks.append(Tok("eof", "", n))
    return toks


# --- set expressions ------------------------------------------------------------


def _split_top(text: str, sep: str = ",") -> list[str]:
    parts, depth, buf, i = [], 0, [], 0
    while i < len(text):
        ch = text[i]
        if ch in "'\"":
            j = text.find(ch, i + 1)
            j = len(text) - 1 if j < 0 else j
            buf.append(text[i : j + 1])
            i = j + 1
            continue
        if ch == "[":
            j = text.find("]", i + 1)
            j = len(text) - 1 if j < 0 else j
            buf.append(text[i : j + 1])
            i = j + 1
            continue
        if ch in "({<":
            depth += 1
        elif ch in ")}>":
            depth -= 1
        if ch == sep and depth == 0:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
        i += 1
    if "".join(buf).strip():
        parts.append("".join(buf))
    return [p.strip() for p in parts]


def _element(text: str) -> SetElement:
    t = text.strip()
    if re.fullmatch(r"-?\d+(\.\d+)?", t):
        return SetElement("number", t)
    if len(t) >= 2 and t[0] == t[-1] == "'":
        return SetElement("string", t[1:-1].replace("''", "'"))
    if len(t) >= 2 and t[0] == t[-1] == '"':
        inner = t[1:-1]
        if inner.startswith("$(=") and inner.endswith(")"):
            return SetElement("dollar", inner[3:-1])
        return SetElement("search", inner)
    if t.startswith("$(=") and t.endswith(")"):
        return SetElement("dollar", t[3:-1])
    return SetElement("other", t)


_MOD = re.compile(r"^\s*(\[[^\]]+\]|\"[^\"]+\"|[^\s=+\-*/<>]+)\s*(\+=|-=|\*=|/=|=)\s*(.*)$", re.S)


def parse_set(raw: str) -> SetExpr:
    """`{<Year={2024}, Region=>}`, `{1<...>}`, `{$}`, `{BM01}` ..."""
    body = raw.strip()
    if not (body.startswith("{") and body.endswith("}")):
        raise ParseError(f"Not a set expression: {raw}")
    inner = body[1:-1].strip()
    s = SetExpr(raw=raw)
    # Set operators between whole sets: {A}+{B}, {1}-{$}
    if re.search(r"\}\s*[-+*/]\s*\{", raw) or re.search(r">\s*[-+*/]\s*[1$<]", inner):
        s.compound = True
        return s
    lt = inner.find("<")
    if lt < 0:
        s.identifier = inner or "$"
        return s
    s.identifier = inner[:lt].strip() or "$"
    if not inner.endswith(">"):
        raise ParseError(f"Unterminated set modifier in {raw}")
    for part in _split_top(inner[lt + 1 : -1]):
        if not part:
            continue
        m = _MOD.match(part)
        if not m:
            s.modifiers.append(SetModifier(field="", op="?", raw=part))
            continue
        name, op, rest = m.group(1), m.group(2), m.group(3).strip()
        name = name.strip("[]\"")
        mod = SetModifier(field=name, op=op, raw=part)
        if rest == "":
            mod.clear = True
        elif rest.startswith("{") and rest.endswith("}"):
            if re.search(r"\}\s*[-+*/]\s*\{", rest):
                mod.elements = [SetElement("other", rest)]
            else:
                mod.elements = [_element(e) for e in _split_top(rest[1:-1]) if e]
        else:
            mod.elements = [SetElement("field" if re.fullmatch(r"\[?[\w .]+\]?", rest) else "other", rest)]
        s.modifiers.append(mod)
    return s


# --- parser ---------------------------------------------------------------------


class _Parser:
    def __init__(self, text: str):
        self.toks = tokenize(text)
        self.i = 0

    def peek(self, k: int = 0) -> Tok:
        return self.toks[min(self.i + k, len(self.toks) - 1)]

    def next(self) -> Tok:
        t = self.toks[self.i]
        self.i += 1
        return t

    def at_op(self, v: str) -> bool:
        t = self.peek()
        return t.kind == "op" and t.value == v

    def at_word(self, w: str) -> bool:
        t = self.peek()
        return t.kind == "ident" and t.value.lower() == w

    def expect(self, v: str) -> None:
        if not self.at_op(v):
            t = self.peek()
            raise ParseError(f"Expected '{v}' at {t.pos}, found {t.value or t.kind!r}")
        self.next()

    def parse(self) -> Node:
        node = self.parse_or()
        if self.peek().kind != "eof":
            t = self.peek()
            raise ParseError(f"Unexpected {t.value!r} at {t.pos}")
        return node

    def parse_or(self) -> Node:
        node = self.parse_and()
        while self.at_word("or") or self.at_word("xor"):
            op = self.next().value.lower()
            node = Binary(op, node, self.parse_and())
        return node

    def parse_and(self) -> Node:
        node = self.parse_not()
        while self.at_word("and"):
            self.next()
            node = Binary("and", node, self.parse_not())
        return node

    def parse_not(self) -> Node:
        if self.at_word("not"):
            self.next()
            return Unary("not", self.parse_not())
        return self.parse_cmp()

    def parse_cmp(self) -> Node:
        node = self.parse_concat()
        t = self.peek()
        if t.kind == "op" and t.value in ("=", "<>", "<", ">", "<=", ">="):
            self.next()
            return Binary(t.value, node, self.parse_concat())
        if self.at_word("like"):
            self.next()
            return Binary("like", node, self.parse_concat())
        return node

    def parse_concat(self) -> Node:
        node = self.parse_add()
        while self.at_op("&"):
            self.next()
            node = Binary("&", node, self.parse_add())
        return node

    def parse_add(self) -> Node:
        node = self.parse_mul()
        while self.peek().kind == "op" and self.peek().value in "+-":
            op = self.next().value
            node = Binary(op, node, self.parse_mul())
        return node

    def parse_mul(self) -> Node:
        node = self.parse_unary()
        while self.peek().kind == "op" and self.peek().value in "*/":
            op = self.next().value
            node = Binary(op, node, self.parse_unary())
        return node

    def parse_unary(self) -> Node:
        if self.at_op("-"):
            self.next()
            return Unary("-", self.parse_unary())
        if self.at_op("+"):
            self.next()
            return self.parse_unary()
        return self.parse_primary()

    def parse_call(self, name: str) -> Call:
        self.expect("(")
        call = Call(name=name)
        if self.at_op(")"):
            self.next()
            return call
        # Qualifiers, in Qlik's order: set expression, DISTINCT/NODISTINCT/ALL, TOTAL <...>
        while True:
            t = self.peek()
            if t.kind == "set":
                self.next()
                call.set_expr = parse_set(t.value)
            elif self.at_word("distinct"):
                self.next()
                call.distinct = True
            elif self.at_word("nodistinct"):
                self.next()
                call.nodistinct = True
            elif self.at_word("all"):
                self.next()
                call.total = True
                call.set_expr = call.set_expr or SetExpr(identifier="1", raw="{1}")
            elif self.at_word("total"):
                self.next()
                call.total = True
                if self.at_op("<"):
                    self.next()
                    while not self.at_op(">"):
                        f = self.next()
                        if f.kind in ("ident", "field"):
                            call.total_fields.append(f.value)
                        elif f.kind == "eof":
                            raise ParseError("Unterminated TOTAL <...>")
                    self.next()
            else:
                break
        call.args.append(self.parse_or())
        while self.at_op(","):
            self.next()
            call.args.append(self.parse_or())
        self.expect(")")
        return call

    def parse_primary(self) -> Node:
        t = self.peek()
        if t.kind == "num":
            self.next()
            return Num(t.value)
        if t.kind == "str":
            self.next()
            return Str(t.value)
        if t.kind == "field":
            self.next()
            if self.at_op("("):  # [Some Function](...) is not a thing; treat as field
                raise ParseError(f"Bracketed name followed by '(' at {t.pos}")
            return FieldRef(t.value, quoted=True)
        if t.kind == "dollar":
            self.next()
            return Dollar(t.value)
        if t.kind == "set":
            raise ParseError(f"Set expression outside an aggregation at {t.pos}")
        if t.kind == "op" and t.value == "(":
            self.next()
            node = self.parse_or()
            self.expect(")")
            return node
        if t.kind == "ident":
            if t.value.lower() in _WORD_OPS:
                raise ParseError(f"Unexpected operator {t.value!r} at {t.pos}")
            self.next()
            if self.at_op("("):
                return self.parse_call(t.value)
            if t.value.lower() in ("null", "true", "false"):
                return Call(name=t.value.lower())
            return FieldRef(t.value)
        raise ParseError(f"Unexpected {t.value or t.kind!r} at {t.pos}")


def parse(text: str) -> Node:
    body = text.strip()
    if body.startswith("="):
        body = body[1:]
    if not body.strip():
        raise ParseError("Empty expression")
    if "$(" in body.replace("$(=", ""):
        raise ParseError("Unexpanded dollar-sign expansion (unknown variable or a parameterised variable).")
    return _Parser(body).parse()
