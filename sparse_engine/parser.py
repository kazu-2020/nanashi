"""Pigment 風の式の文字列を AST に変換する。to_formula で AST を文字列に戻す。

    Revenue   = Price * Volume
    DeptCost  = Cost[BY SUM: Employee.Department]
    Total     = DeptCost[REMOVE SUM: Department]
    Bonus     = Salary * Rate[BY: Employee.Department]
    Cash      = PREVIOUS(Month) + Funding - TotalCost
    Order     = IF(Stock[SELECT: Month - 1] < 5, 10, 0)

文法（優先順位の低い順）:

    expr     := or
    or       := and ("OR" and)*
    and      := not ("AND" not)*
    not      := "NOT" not | cmp
    cmp      := add (("=" | "<>" | "<" | "<=" | ">" | ">=") add)?
    add      := mul (("+" | "-") mul)*
    mul      := unary (("*" | "/") unary)*
    unary    := "-" unary | postfix
    postfix  := primary ("[" modifier "]")*
    primary  := NUMBER | "TRUE" | "FALSE" | name | "(" expr ")" | call
    modifier := "BY" [agg] ":" name "." name ("," name "." name)*
              | "REMOVE" [agg] ":" name ("," name)*
              | "FILTER" ":" expr
              | "SELECT" ":" name ("+" | "-") INTEGER
              | "EXPAND" ":" name ("," name)*
              | "ON" ":" expr
    call     := "IF" "(" expr "," expr ["," expr] ")"
              | "IFBLANK" "(" expr "," 定数 ")"
              | "ISBLANK" "(" expr ")"
              | "PREVIOUS" "(" name ["," INTEGER] ")"
    name     := 識別子 | "'" 任意の文字（' は '' と書く） "'"

キーワード・関数名・集計関数名は大文字小文字を区別しない。Metric 名と軸名は区別する。
PREVIOUS(Month) はその式を持つ Metric 自身の前の時点を指す（self_name が必要）。
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Callable, TypeVar

from .evaluate import FormulaError
from .expr import (AGGREGATORS, BinOp, By, Const, Expand, Expr, Filter, If, IfBlank, IsBlank,
                   Not, On, Ref, Remove, Shift)

KEYWORDS = {"AND", "OR", "NOT", "TRUE", "FALSE"}
FUNCTIONS = {"IF", "IFBLANK", "ISBLANK", "PREVIOUS"}
MODIFIERS = {"BY", "REMOVE", "FILTER", "SELECT", "EXPAND", "ON"}
COMPARE_OPS = ("=", "<>", "<", "<=", ">", ">=")

_TOKEN = re.compile(r"""
    (?P<ws>\s+)
  | (?P<num>\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)
  | (?P<ident>[^\W\d]\w*)
  | (?P<quoted>'(?:[^']|'')*')
  | (?P<op><>|<=|>=|[-+*/=<>()\[\],:.])
""", re.VERBOSE)
_IDENT = re.compile(r"[^\W\d]\w*\Z")


def _width(s: str) -> int:
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in s)


class ParseError(FormulaError):
    def __init__(self, message: str, text: str, pos: int):
        start = text.rfind("\n", 0, pos) + 1
        end = text.find("\n", pos)
        line = text[start:end if end != -1 else len(text)]
        caret = " " * _width(text[start:pos]) + "^"
        row = text.count("\n", 0, pos) + 1
        super().__init__(f"{row} 行 {pos - start + 1} 文字目: {message}\n  {line}\n  {caret}")
        self.pos = pos


@dataclass(frozen=True)
class Token:
    kind: str  # num / ident / quoted / op / eof
    text: str
    pos: int

    def describe(self) -> str:
        return "式の終わり" if self.kind == "eof" else f"'{self.text}'"


def tokenize(text: str) -> list[Token]:
    tokens, pos = [], 0
    while pos < len(text):
        m = _TOKEN.match(text, pos)
        if m is None:
            if text[pos] == "'":
                raise ParseError("' が閉じていない", text, pos)
            raise ParseError(f"使えない文字 {text[pos]!r}", text, pos)
        if m.lastgroup != "ws":
            tokens.append(Token(m.lastgroup, m.group(), pos))
        pos = m.end()
    tokens.append(Token("eof", "", len(text)))
    return tokens


def parse(text: str, *, self_name: str | None = None) -> Expr:
    return _Parser(text, self_name).parse()


T = TypeVar("T")


class _Parser:
    def __init__(self, text: str, self_name: str | None):
        self.text = text
        self.tokens = tokenize(text)
        self.i = 0
        self.self_name = self_name

    # ------------------------------------------------ トークン操作

    @property
    def tok(self) -> Token:
        return self.tokens[self.i]

    def advance(self) -> Token:
        t = self.tok
        self.i += 1
        return t

    def error(self, message: str, tok: Token | None = None):
        raise ParseError(message, self.text, (tok or self.tok).pos)

    def at_op(self, *ops: str) -> bool:
        return self.tok.kind == "op" and self.tok.text in ops

    def at_word(self, *words: str) -> bool:
        return self.tok.kind == "ident" and self.tok.text.upper() in words

    def expect_op(self, op: str) -> Token:
        if not self.at_op(op):
            self.error(f"'{op}' が必要だが {self.tok.describe()} がある")
        return self.advance()

    def comma_list(self, item: Callable[[], T]) -> list[T]:
        items = [item()]
        while self.at_op(","):
            self.advance()
            items.append(item())
        return items

    # ------------------------------------------------ 式

    def parse(self) -> Expr:
        e = self.or_()
        if self.tok.kind != "eof":
            self.error(f"{self.tok.describe()} は余分")
        return e

    def or_(self) -> Expr:
        e = self.and_()
        while self.at_word("OR"):
            self.advance()
            e = BinOp("or", e, self.and_())
        return e

    def and_(self) -> Expr:
        e = self.not_()
        while self.at_word("AND"):
            self.advance()
            e = BinOp("and", e, self.not_())
        return e

    def not_(self) -> Expr:
        if self.at_word("NOT"):
            self.advance()
            return Not(self.not_())
        return self.cmp()

    def cmp(self) -> Expr:
        e = self.add()
        if self.at_op(*COMPARE_OPS):
            op = self.advance().text
            e = BinOp(op, e, self.add())
            if self.at_op(*COMPARE_OPS):
                self.error("比較は連結できない。AND でつなぐ")
        return e

    def add(self) -> Expr:
        e = self.mul()
        while self.at_op("+", "-"):
            op = self.advance().text
            e = BinOp(op, e, self.mul())
        return e

    def mul(self) -> Expr:
        e = self.unary()
        while self.at_op("*", "/"):
            op = self.advance().text
            e = BinOp(op, e, self.unary())
        return e

    def unary(self) -> Expr:
        if self.at_op("-"):
            self.advance()
            operand = self.unary()
            if isinstance(operand, Const) and not isinstance(operand.value, bool):
                return Const(-operand.value)
            return -operand
        return self.postfix()

    def postfix(self) -> Expr:
        e = self.primary()
        while self.at_op("["):
            self.advance()
            e = self.modifier(e)
            self.expect_op("]")
        return e

    def primary(self) -> Expr:
        t = self.tok
        if t.kind == "num":
            self.advance()
            return Const(float(t.text))
        if self.at_op("("):
            self.advance()
            e = self.or_()
            self.expect_op(")")
            return e
        if t.kind == "ident":
            word = t.text.upper()
            if word in ("TRUE", "FALSE"):
                self.advance()
                return Const(word == "TRUE")
            if word in KEYWORDS:
                self.error(f"{t.describe()} の前に値が必要")
            nxt = self.tokens[self.i + 1]
            if nxt.kind == "op" and nxt.text == "(":
                return self.call()
        if t.kind in ("ident", "quoted"):
            return Ref(self.name())
        self.error(f"値が必要だが {t.describe()} がある")

    def name(self) -> str:
        t = self.tok
        if t.kind == "quoted":
            self.advance()
            return t.text[1:-1].replace("''", "'")
        if t.kind == "ident" and t.text.upper() not in KEYWORDS:
            self.advance()
            return t.text
        self.error(f"名前が必要だが {t.describe()} がある")

    def integer(self) -> int:
        t = self.tok
        if t.kind != "num" or not t.text.isdigit():
            self.error(f"整数が必要だが {t.describe()} がある")
        self.advance()
        return int(t.text)

    # ------------------------------------------------ 関数

    def call(self) -> Expr:
        t = self.advance()
        fn = t.text.upper()
        if fn not in FUNCTIONS:
            self.error(f"未知の関数 {t.text}（使えるのは {', '.join(sorted(FUNCTIONS))}）", t)
        self.expect_op("(")
        if fn == "IF":
            cond = self.or_()
            self.expect_op(",")
            then = self.or_()
            else_ = None
            if self.at_op(","):
                self.advance()
                else_ = self.or_()
            result: Expr = If(cond, then, else_)
        elif fn == "IFBLANK":
            x = self.or_()
            self.expect_op(",")
            at = self.tok
            v = self.or_()
            if not isinstance(v, Const):
                self.error("IFBLANK の 2 番目の引数は定数でなければならない", at)
            result = IfBlank(x, v.value)
        elif fn == "ISBLANK":
            result = IsBlank(self.or_())
        else:  # PREVIOUS
            if self.self_name is None:
                self.error("PREVIOUS は Metric の式の中でしか使えない", t)
            dim = self.name()
            n = 1
            if self.at_op(","):
                self.advance()
                n = self.integer()
            result = Shift(Ref(self.self_name), dim, n)
        self.expect_op(")")
        return result

    # ------------------------------------------------ 修飾子 [...]

    def modifier(self, e: Expr) -> Expr:
        if not self.at_word(*MODIFIERS):
            self.error(f"{', '.join(sorted(MODIFIERS))} のいずれかが必要だが {self.tok.describe()} がある")
        word = self.advance().text.upper()
        agg = None
        if word in ("BY", "REMOVE") and not self.at_op(":"):
            if self.tok.kind != "ident" or self.tok.text.lower() not in AGGREGATORS:
                names = ", ".join(a.upper() for a in AGGREGATORS)
                self.error(f"集計関数（{names}）か ':' が必要だが {self.tok.describe()} がある")
            agg = self.advance().text.lower()
        self.expect_op(":")

        if word == "BY":
            for dim, prop in self.comma_list(self.dim_prop):
                e = By(e, dim, prop, agg)
        elif word == "REMOVE":
            for dim in self.comma_list(self.name):
                e = Remove(e, dim, agg or "sum")
        elif word == "FILTER":
            e = Filter(e, self.or_())
        elif word == "ON":
            e = On(e, self.or_())
        elif word == "EXPAND":
            e = Expand(e, tuple(self.comma_list(self.name)))
        else:  # SELECT
            dim = self.name()
            if not self.at_op("+", "-"):
                self.error(f"SELECT には '{dim} - 1' のようなずらし量が必要")
            sign = self.advance().text
            n = self.integer()
            e = Shift(e, dim, n if sign == "-" else -n)
        return e

    def dim_prop(self) -> tuple[str, str]:
        dim = self.name()
        self.expect_op(".")
        return dim, self.name()


# ---------------------------------------------------------------- AST -> 文字列

_PREC = {"or": 1, "and": 2, "+": 5, "-": 5, "*": 6, "/": 6} | {op: 4 for op in COMPARE_OPS}
_NOT, _UNARY, _POSTFIX, _ATOM = 3, 7, 8, 9


def to_formula(expr: Expr) -> str:
    return _fmt(expr)[0]


def _name(name: str) -> str:
    if _IDENT.match(name) and name.upper() not in KEYWORDS | FUNCTIONS:
        return name
    return "'" + name.replace("'", "''") + "'"


def _number(v: float) -> str:
    return str(int(v)) if v.is_integer() and abs(v) < 1e15 else repr(v)


def _wrap(e: Expr, min_prec: int) -> str:
    text, prec = _fmt(e)
    return f"({text})" if prec < min_prec else text


def _fmt(e: Expr) -> tuple[str, int]:
    match e:
        case Ref(name):
            return _name(name), _ATOM
        case Const(value):
            if isinstance(value, bool):
                return ("TRUE" if value else "FALSE"), _ATOM
            return _number(value), (_UNARY if value < 0 else _ATOM)
        case BinOp("*", Const(-1.0), x) if not isinstance(x, Const):
            return "-" + _wrap(x, _UNARY), _UNARY
        case BinOp(op, left, right):
            p = _PREC[op]
            word = op.upper() if op in ("and", "or") else op
            left_min = p + 1 if p == 4 else p  # 比較は結合しない
            return f"{_wrap(left, left_min)} {word} {_wrap(right, p + 1)}", p
        case Not(x):
            return "NOT " + _wrap(x, _NOT), _NOT
        case If(cond, then, else_):
            args = [cond, then] + ([else_] if else_ is not None else [])
            return "IF(" + ", ".join(to_formula(a) for a in args) + ")", _ATOM
        case IfBlank(x, value):
            return f"IFBLANK({to_formula(x)}, {to_formula(Const(value))})", _ATOM
        case IsBlank(x):
            return f"ISBLANK({to_formula(x)})", _ATOM
        case By(x, dim, prop, agg):
            head = f"BY {agg.upper()}" if agg else "BY"
            return f"{_wrap(x, _POSTFIX)}[{head}: {_name(dim)}.{_name(prop)}]", _POSTFIX
        case Remove(x, dim, agg):
            return f"{_wrap(x, _POSTFIX)}[REMOVE {agg.upper()}: {_name(dim)}]", _POSTFIX
        case Filter(x, cond):
            return f"{_wrap(x, _POSTFIX)}[FILTER: {to_formula(cond)}]", _POSTFIX
        case On(x, other):
            return f"{_wrap(x, _POSTFIX)}[ON: {to_formula(other)}]", _POSTFIX
        case Expand(x, dims):
            return f"{_wrap(x, _POSTFIX)}[EXPAND: {', '.join(_name(d) for d in dims)}]", _POSTFIX
        case Shift(x, dim, n):
            sign = "-" if n >= 0 else "+"
            return f"{_wrap(x, _POSTFIX)}[SELECT: {_name(dim)} {sign} {abs(n)}]", _POSTFIX
    raise TypeError(e)
