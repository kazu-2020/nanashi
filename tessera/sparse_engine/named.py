"""Named: the name facade on a Model.

The Model API takes ids (UUIDs) for a Metric, a dimension, a member and a property, and its reads give ids.
People (tests, examples, benchmarks, notebooks) think in names. Named wraps a Model: it turns names into ids
before each call and ids into names in the results (Cubes, rows, member-type values, logs, the cell history).
Everything that it does not need to translate goes to the Model as it is. The server does not use it: the HTTP
boundary passes ids through.

    m = Named(Model())
    m.add_dimension("Product", ["A", "B"])
    m.add_input("Price", ["Product"], {("A",): 10})
    m.get("Price", Product="A")  # 10.0
"""
from __future__ import annotations

from typing import Any, Callable, Iterable, Mapping

from .core import Cube, Dimension, Key
from .engine import Store
from .expr import Expr
from .model import Metric, Model


class Named:
    """A Model with names in place of ids in its public API. The Model is `.model`.

    A Metric or dimension argument is looked up as a name first, then as an id. So a script can also pass the
    ids that it got from the Model (`x.dims`, `x.id`). Named(Named(model)) is Named(model)."""

    def __init__(self, model: Model | Named):
        object.__setattr__(self, "model", model.model if isinstance(model, Named) else model)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.model, name)

    def __setattr__(self, name: str, value: Any) -> None:
        setattr(self.model, name, value)

    def __repr__(self) -> str:
        return f"Named({self.model!r})"

    # ------------------------------------------------ names -> ids

    def dimension(self, name: str) -> Dimension:
        """The dimension with this name (or else this id). ValueError if there is none."""
        d = self.model.dimensions.get(self._dim(name))
        if d is None:
            raise ValueError(f"軸 {name} がない")
        return d

    def metric(self, name: str) -> Metric:
        """The Metric with this name (or else this id). ValueError if there is none."""
        m = self.model.metrics.get(self.model._metric_ids.get(name, name))
        if m is None:
            raise ValueError(f"Metric {name} がない")
        return m

    def dimension_id(self, name: str) -> str:
        return self.dimension(name).id

    def member_id(self, dim: str, member: str) -> str:
        return self.dimension(dim).id_of(member)

    def property_id(self, dim: str, prop: str) -> str:
        return self.dimension(dim).prop_of(prop)

    def _dim(self, name: str) -> str:
        """The dimension id for a name. An unknown name stays as it is (the Model reports it)."""
        return self.model._dim_ids.get(name, name)

    def _kind(self, kind: str) -> str:
        return "member:" + self._dim(kind.removeprefix("member:")) if kind.startswith("member:") else kind

    def _value_in(self, m: Metric, value):
        """A member-type value (a name) as a member id. An unknown name stays as it is."""
        d = self._value_dim(m)
        return value if d is None else self._member(d, value)

    def _value_dim(self, m: Metric) -> Dimension | None:
        return self.model.dimensions[m.kind.removeprefix("member:")] if m.kind.startswith("member:") else None

    @staticmethod
    def _member(d: Dimension, member) -> str:
        """The member id for a name. An unknown name stays as it is (the Model reports it)."""
        return d.ids[d._index[member]] if isinstance(member, str) and member in d._index else member

    def _coords(self, m: Metric, coords: Mapping[str, Any]) -> dict[str, Any]:
        """{dimension name: member name or names} -> {dimension id: member id or ids}."""
        out = {}
        for name, ms in coords.items():
            d = self.model.dimensions.get(self._dim(name))
            if d is None:
                raise ValueError(f"{m.name}: 軸 {name} がない")
            out[d.id] = self._member(d, ms) if isinstance(ms, str) else [self._member(d, x) for x in ms]
        return out

    def _key(self, dims: tuple[str, ...], key: Key) -> Key:
        """A key of member names in the order of dims (ids) as member ids. A key of the wrong length stays."""
        if len(key) != len(dims):
            return key
        return tuple(self._member(self.model.dimensions[d], x) for d, x in zip(dims, key))

    # ------------------------------------------------ ids -> names

    def _names(self, dims: Iterable[str]) -> Callable[[Key], tuple[str, ...]]:
        """A function that gives the member names of a key of member ids (for many keys of the same dims)."""
        tables = [(self.model.dimensions[d]._by_id, self.model.dimensions[d].members) for d in dims]
        return lambda key: tuple(names[by_id[x]] for (by_id, names), x in zip(tables, key))

    def _value_out(self, m: Metric, value):
        d = self._value_dim(m)
        return d.member_of(value) if d is not None and value is not None else value

    def _cube(self, m: Metric, cube: Cube) -> Cube:
        names = self._names(cube.dims)
        dims = tuple(self.model.dimensions[d].name for d in cube.dims)
        if m.kind.startswith("member:"):
            d = self._value_dim(m)
            return Cube(dims, {names(k): d.member_of(v) for k, v in cube.cells.items()})
        return Cube(dims, {names(k): v for k, v in cube.cells.items()})

    # ------------------------------------------------ definitions

    def rename_dimension(self, dim: str, new: str) -> None:
        self.model.rename_dimension(self._dim(dim), new)

    def add_property(self, dim: str, prop: str, target: str, mapping: Mapping[str, str], *,
                     id: str | None = None) -> str:
        """Add the property ({member name of dim: member name of target}). Return the property id."""
        d, t = self.dimension(dim), self.dimension(target)
        return self.model.add_property(d.id, prop, t.id, {self._member(d, k): self._member(t, v)
                                                            for k, v in mapping.items()}, id=id)

    def rename_property(self, dim: str, prop: str, new: str) -> None:
        d = self.dimension(dim)
        self.model.rename_property(d.id, d.prop_of(prop), new)

    def set_property_values(self, dim: str, prop: str, values: Mapping[str, str | None]) -> None:
        d = self.dimension(dim)
        pid = d.prop_of(prop)
        t = self.model.dimensions[d.properties[pid][0]]
        self.model.set_property_values(d.id, pid, {self._member(d, k): None if v is None else self._member(t, v)
                                                   for k, v in values.items()})

    def add_input(self, name: str, dims: Iterable[str], cells: Mapping[Key, Any] | None = None, *,
                  kind: str = "number", storage: Any = None, partition: str | None = None,
                  id: str | None = None) -> str:
        """Add the input Metric. dims are dimension names and cells is {(member name, ...): value}."""
        dims = tuple(self._dim(d) for d in dims)
        kind = self._kind(kind)
        if cells:
            m = Metric(name, dims, kind)
            cells = {self._key(dims, tuple(k)): self._value_in(m, v) for k, v in cells.items()}
        return self.model.add_input(name, dims, cells, kind=kind, storage=storage,
                                    partition=None if partition is None else self._dim(partition), id=id)

    def add_formula(self, name: str, dims: Iterable[str], formula: Expr | str, *, kind: str = "number",
                    partition: str | None = None, overridable: bool = False, id: str | None = None) -> str:
        """Add the formula Metric. dims are dimension names. The formula is written with names, as always."""
        return self.model.add_formula(name, [self._dim(d) for d in dims], formula, kind=self._kind(kind),
                                      partition=None if partition is None else self._dim(partition),
                                      overridable=overridable, id=id)

    def remove_metric(self, name: str) -> None:
        self.model.remove_metric(self.metric(name).id)

    def rename_metric(self, old: str, new: str) -> None:
        self.model.rename_metric(self.metric(old).id, new)

    def add_member(self, dim: str, member: str, *, at: int | None = None, id: str | None = None,
                   **properties: str) -> str:
        """Add the member. properties sets the property values by name: m.add_member("Product", "p1", Category="c1")."""
        d = self.dimension(dim)
        props = {}
        for prop, value in properties.items():
            pid = d.prop_of(prop)
            props[pid] = self._member(self.model.dimensions[d.properties[pid][0]], value)
        return self.model.add_member(d.id, member, at=at, id=id, properties=props)

    def move_member(self, dim: str, member: str, at: int) -> None:
        d = self.dimension(dim)
        self.model.move_member(d.id, self._member(d, member), at)

    def rename_member(self, dim: str, old: str, new: str) -> None:
        d = self.dimension(dim)
        self.model.rename_member(d.id, self._member(d, old), new)

    def remove_member(self, dim: str, member: str) -> None:
        d = self.dimension(dim)
        self.model.remove_member(d.id, self._member(d, member))

    # ------------------------------------------------ inputs

    def set_cell(self, name: str, value, **coords: str) -> None:
        m = self.metric(name)
        self.model.set_cell(m.id, self._value_in(m, value), self._coords(m, coords))

    def spread(self, name: str, total: float, *, how: str = "proportional",
               where: Mapping[str, str] | None = None, **coords: str) -> int:
        """Spread total over a range: m.spread("Budget", 12000, Month="m01", where={"Product.Category": "ハード"})."""
        m = self.metric(name)
        by_id = None
        if where is not None:
            by_id = {}
            for path, value in where.items():
                dim, _, prop = path.partition(".")
                d = self.model.dimensions.get(self._dim(dim))
                if d is None or prop not in d._props:
                    by_id[path] = value  # the Model reports the bad path
                    continue
                pid = d._props[prop]
                by_id[f"{d.id}.{pid}"] = self._member(self.model.dimensions[d.properties[pid][0]], value)
        return self.model.spread(m.id, total, self._coords(m, coords), how=how, where=by_id)

    # ------------------------------------------------ reads

    def value(self, name: str) -> Cube:
        m = self.metric(name)
        return self._cube(m, self.model.value(m.id))

    def raw(self, name: str) -> Any:
        return self.model.raw(self.metric(name).id)

    def get(self, name: str, **coords: str):
        m = self.metric(name)
        return self._value_out(m, self.model.get(m.id, self._coords(m, coords)))

    def slice(self, name: str, **coords) -> Cube:
        m = self.metric(name)
        return self._cube(m, self.model.slice(m.id, self._coords(m, coords)))

    def rows(self, name: str, *, offset: int = 0, limit: int | None = None, **coords) -> tuple[list, int]:
        m = self.metric(name)
        rows, total = self.model.rows(m.id, self._coords(m, coords), offset=offset, limit=limit)
        names = self._names(m.dims)
        return [(names(k), self._value_out(m, v)) for k, v in rows], total

    def summarize(self, name: str, keep: Iterable[str] = (), agg: str = "sum", **coords) -> Cube:
        m = self.metric(name)
        cube = self.model.summarize(m.id, self._coords(m, coords), keep=[self._dim(d) for d in keep], agg=agg)
        return self._cube(m, cube)

    def memory(self) -> dict[str, dict[str, int]]:
        return {self.model.metrics[i].name: row for i, row in self.model.memory().items()}

    def cell_history(self, journal, name: str, **coords: str) -> list[dict]:
        """The change history of one cell from the journal. A member-type value shows the current name (a
        removed member stays an id)."""
        m = self.metric(name)
        history = journal.cell_history(self.model, m.id, self._coords(m, coords))
        d = self._value_dim(m)
        if d is not None:
            for h in history:
                for k in ("old", "new"):
                    if h[k] in d._by_id:
                        h[k] = d.member_of(h[k])
        return history

    def fork(self) -> Named:
        return Named(self.model.fork())

    @classmethod
    def load(cls, path, engine: Store | None = None) -> Named:
        return cls(Model.load(path, engine))

    # ------------------------------------------------ logs

    @property
    def eval_log(self) -> NamedLog:
        log, name = self.model.eval_log, self.model._name_of
        return NamedLog(log.clear, lambda: [name(i) for i in log])

    @property
    def delta_log(self) -> NamedLog:
        log, name = self.model.delta_log, self.model._name_of
        return NamedLog(log.clear, lambda: [name(i) for i in log])

    @property
    def slice_log(self) -> NamedLog:
        """(Metric id, dimension id -> member ids) items with the current names. A removed member shows its id."""
        model = self.model
        name, dims = model._name_of, model.dimensions

        def members(d: str, ms: frozenset) -> frozenset:
            by_id, names = dims[d]._by_id, dims[d].members
            return frozenset(names[by_id[x]] if x in by_id else x for x in ms)
        return NamedLog(model.slice_log.clear,
                        lambda: [(name(i), {dims[d].name: members(d, ms) for d, ms in r.items()})
                                 for i, r in model.slice_log])


class NamedLog:
    """An observation log of the Model (ids) shown with the current names. shown gives the items."""

    def __init__(self, clear: Callable[[], None], shown: Callable[[], list]):
        self.clear, self._shown = clear, shown

    def count(self, item) -> int:
        return self._shown().count(item)

    def __iter__(self):
        return iter(self._shown())

    def __len__(self) -> int:
        return len(self._shown())

    def __getitem__(self, i):
        return self._shown()[i]

    def __contains__(self, item) -> bool:
        return item in self._shown()

    def __eq__(self, other) -> bool:
        return self._shown() == list(other)

    def __repr__(self) -> str:
        return repr(self._shown())
