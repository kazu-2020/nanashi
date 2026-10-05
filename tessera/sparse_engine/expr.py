"""式の AST。Metric 全体（ブロック）に対する演算だけを表現し、セル単位の式は持たない。"""
from __future__ import annotations

from dataclasses import dataclass, fields, replace
from typing import Any, Callable

@dataclass(frozen=True)
class Aggregation:
    """集計関数。集計関数の一覧はここだけに持つ（構文、型検査、評価、集計の読み出し、Rust の変換が使う）。"""
    name: str
    fn: Callable[[list], Any]
    public: bool   # 利用者が式（BY、REMOVE）と集計の読み出し（summarize）で使える
    numeric: bool  # number の値だけを集計できる（そうでなければ値の種類を問わない）
    kind: str | None = "number"  # 結果の値の種類（None なら集計元と同じ）


AGGREGATIONS: dict[str, Aggregation] = {a.name: a for a in [
    Aggregation("sum", sum, True, True),
    Aggregation("avg", lambda vs: sum(vs) / len(vs), True, True),
    Aggregation("min", min, True, True),
    Aggregation("max", max, True, True),
    Aggregation("count", lambda vs: float(len(vs)), True, False),
    # 値が 1 つしかないグループ用。Metric を使った BY の引き下ろしを書き換えた式の内部でだけ使う
    Aggregation("first", lambda vs: vs[0], False, False, None),
]}
PUBLIC_AGGREGATIONS = tuple(n for n, a in AGGREGATIONS.items() if a.public)
AGGREGATORS = {n: a.fn for n, a in AGGREGATIONS.items()}

ARITH = {"+", "-", "*", "/"}
COMPARE = {"=", "<>", "<", "<=", ">", ">="}
LOGIC = {"and", "or"}

Value = float | bool


class Expr:
    def __add__(self, other): return BinOp("+", self, lift(other))
    def __radd__(self, other): return BinOp("+", lift(other), self)
    def __sub__(self, other): return BinOp("-", self, lift(other))
    def __rsub__(self, other): return BinOp("-", lift(other), self)
    def __mul__(self, other): return BinOp("*", self, lift(other))
    def __rmul__(self, other): return BinOp("*", lift(other), self)
    def __truediv__(self, other): return BinOp("/", self, lift(other))
    def __rtruediv__(self, other): return BinOp("/", lift(other), self)
    def __neg__(self): return BinOp("*", Const(-1.0), self)  # 疎性を保つ

    # 比較。== と != は Python の同一性判定を壊すので eq() / ne() にする
    def __lt__(self, other): return BinOp("<", self, lift(other))
    def __le__(self, other): return BinOp("<=", self, lift(other))
    def __gt__(self, other): return BinOp(">", self, lift(other))
    def __ge__(self, other): return BinOp(">=", self, lift(other))
    def eq(self, other) -> BinOp: return BinOp("=", self, lift(other))
    def ne(self, other) -> BinOp: return BinOp("<>", self, lift(other))

    # 論理演算（三値論理）
    def __and__(self, other): return BinOp("and", self, lift(other))
    def __rand__(self, other): return BinOp("and", lift(other), self)
    def __or__(self, other): return BinOp("or", self, lift(other))
    def __ror__(self, other): return BinOp("or", lift(other), self)
    def __invert__(self): return Not(self)

    def __bool__(self):
        raise TypeError("式は真偽値として評価できない。`1 < x < 2` ではなく `(x > 1) & (x < 2)` と書く")

    def by(self, path: str, agg: str | None = None) -> By:
        """Pigment の `[BY agg: Dim.Prop]`。

        式が Dim を持つなら Prop の参照先へ集約する（agg 省略時は sum）。
        式が参照先の軸を持つなら Dim へ値を引き下ろす（lookup。agg は指定不可）。
        """
        dim, prop = path.split(".")
        return By(self, dim, prop, agg)

    def remove(self, dim: str, agg: str = "sum") -> Remove:
        return Remove(self, dim, agg)

    def prev(self, dim: str, n: int = 1) -> Shift:
        """result[t] = self[t - n]。自己参照に使うと時間方向の scan になる。"""
        return Shift(self, dim, n)

    def select(self, dim: str, member: str) -> Select:
        """Pigment の `[SELECT: Dim."member"]`。member の切り口を取り出し、dim を結果から外す。"""
        return Select(self, dim, member)

    def ifblank(self, value: Value) -> IfBlank:
        return IfBlank(self, _literal(value))

    def filter(self, cond: Expr) -> Filter:
        """cond が TRUE のセルだけ残す。FALSE と空のセルは空になる。"""
        return Filter(self, cond)

    def isblank(self) -> IsBlank:
        return IsBlank(self)

    def expand(self, *dims: str) -> Expand:
        """dims の全メンバーへ値を複製する（明示的な密化）。"""
        return Expand(self, dims)

    def on(self, other: Expr) -> On:
        """other に値があるセルにだけ、自分の値を配る。軸は両者の和になる。"""
        return On(self, other)


def _literal(x) -> Value:
    if isinstance(x, bool):
        return x
    if isinstance(x, (int, float)):
        return float(x)
    raise TypeError(f"定数にできない: {x!r}")


def lift(x) -> Expr:
    return x if isinstance(x, Expr) else Const(_literal(x))


def ref(name: str) -> Ref:
    return Ref(name)


def dim(name: str) -> DimRef:
    """軸そのもの。各セルで、そのセルのメンバーを値として持つ（`IF(Month <= Month."Mar", ...)`）。"""
    return DimRef(name)


def member(dim: str, name: str) -> Member:
    """軸のメンバーの定数（`Month."Mar"`）。"""
    return Member(dim, name)


def if_(cond, then, else_=None) -> If:
    """条件が TRUE なら then、FALSE なら else_、空なら空。else_ 省略時は FALSE も空。"""
    return If(lift(cond), lift(then), None if else_ is None else lift(else_))


def _children(e: Expr):
    for f in fields(e):
        v = getattr(e, f.name)
        if isinstance(v, Expr):
            yield f.name, v


def _names_member(e: Expr, dim: str, member: str) -> bool:
    return isinstance(e, (Member, Select)) and e.dim == dim and e.member == member


def mentions_member(e: Expr, dim: str, member: str) -> bool:
    """式が `dim."member"`（定数か SELECT）を書いているか。"""
    return _names_member(e, dim, member) or any(mentions_member(c, dim, member) for _, c in _children(e))


def references_metric(e: Expr, name: str) -> bool:
    """Tell if the formula refers to the Metric with this id (also through a Metric BY `[BY: D.<id>]`)."""
    here = (isinstance(e, Ref) and e.name == name) or (isinstance(e, By) and e.prop == name)
    return here or any(references_metric(c, name) for _, c in _children(e))


def uses_property(e: Expr, dim: str, prop: str) -> bool:
    """式が `[BY: dim.prop]` を書いているか。"""
    here = isinstance(e, By) and e.dim == dim and e.prop == prop
    return here or any(uses_property(c, dim, prop) for _, c in _children(e))


def rename_member(e: Expr, dim: str, old: str, new: str) -> Expr:
    """式の中の `dim."old"` を `dim."new"` にする。変わらなければ同じオブジェクトを返す。"""
    changes: dict = {n: r for n, c in _children(e) if (r := rename_member(c, dim, old, new)) is not c}
    if _names_member(e, dim, old):
        changes["member"] = new
    return replace(e, **changes) if changes else e


@dataclass(eq=False)
class Ref(Expr):
    name: str


@dataclass(eq=False)
class Const(Expr):
    value: Value


@dataclass(eq=False)
class DimRef(Expr):
    """軸そのもの。値は各セルのメンバー（式の中では軸の名前で書く）。"""
    dim: str


@dataclass(eq=False)
class Member(Expr):
    """軸のメンバーの定数。"""
    dim: str
    member: str


@dataclass(eq=False)
class BinOp(Expr):
    op: str
    left: Expr
    right: Expr


@dataclass(eq=False)
class Not(Expr):
    child: Expr


@dataclass(eq=False)
class If(Expr):
    cond: Expr
    then: Expr
    else_: Expr | None


@dataclass(eq=False)
class Filter(Expr):
    child: Expr
    cond: Expr


@dataclass(eq=False)
class IsBlank(Expr):
    child: Expr


@dataclass(eq=False)
class Expand(Expr):
    child: Expr
    dims: tuple[str, ...]


@dataclass(eq=False)
class On(Expr):
    child: Expr
    other: Expr


@dataclass(eq=False)
class By(Expr):
    child: Expr
    dim: str
    prop: str
    agg: str | None


@dataclass(eq=False)
class Remove(Expr):
    child: Expr
    dim: str
    agg: str


@dataclass(eq=False)
class Shift(Expr):
    child: Expr
    dim: str
    n: int


@dataclass(eq=False)
class Coalesce(Expr):
    """first に値があればそれ、なければ second（計算 Metric の手入力の上書きに使う）。"""
    first: Expr
    second: Expr


@dataclass(eq=False)
class AsAxis(Expr):
    """メンバー型の式を、そのメンバーを dim の座標に持つ表（値は 1）に変える。

    Metric を使った BY（`Salary[BY SUM: Employee.DeptOf]`）を既存の演算に書き換えるときに使う。
    """
    child: Expr
    dim: str


@dataclass(eq=False)
class Select(Expr):
    child: Expr
    dim: str
    member: str


@dataclass(eq=False)
class IfBlank(Expr):
    child: Expr
    value: Value
