"""メンバーの名前の変更と削除。"""
import random
import tempfile
import unittest

from examples.fpa import build
from sparse_engine import Model, Named, ref, to_formula
from sparse_engine.engine import ReferenceEngine

from .test_incremental import cells, check_full, mapping, same, snapshot
from .test_input_features import model as planning_model
from .test_member_values import model as version_model

try:
    from sparse_engine.rust_engine import RustEngine
except ImportError:  # nanashi_core をビルドしていない環境
    RustEngine = None

try:
    import nanashi_core  # noqa: F401  保存形式（Parquet）の読み書きに使う
except ImportError:
    nanashi_core = None


def model(engine=None) -> Model:
    m = Named(Model(engine=engine)) if engine is not None else Named(Model())
    m.add_dimension("Product", ["A", "B", "C"])
    m.add_dimension("Category", ["ハード", "ソフト"])
    m.add_dimension("Month", ["Jan", "Feb", "Mar", "Apr"], ordered=True)
    m.add_property("Product", "Category", "Category", {"A": "ハード", "B": "ハード", "C": "ソフト"})
    m.add_input("X", ["Product", "Month"], {("A", "Jan"): 1, ("A", "Feb"): 2, ("A", "Mar"): 4, ("B", "Feb"): 8,
                                            ("C", "Apr"): 16})
    m.add_input("Rate", ["Category"], {("ハード",): 10, ("ソフト",): 100})
    m.add_input("Cutoff", [], {(): "Mar"}, kind="member:Month")
    m.add_input("Launch", ["Product"], {("A",): "Feb", ("B",): "Jan", ("C",): "Apr"}, kind="member:Month")
    f = m.add_formula
    f("Prev", ["Product", "Month"], ref("X").prev("Month"))
    f("Total", ["Month"], "X[REMOVE SUM: Product]")
    f("Cum", ["Product", "Month"], "PREVIOUS(Month) + X")
    f("Past", ["Month"], "Month <= Cutoff", kind="boolean")
    f("Dense", [], "(X + 1)[REMOVE SUM: Month][REMOVE SUM: Product]")
    f("ByCategory", ["Category", "Month"], "X[BY SUM: Product.Category]")
    f("Priced", ["Product", "Month"], "X * Rate[BY: Product.Category]")
    f("Launched", ["Product", "Month"], "Month >= Launch", kind="boolean")
    f("Live", [], "Launched[FILTER: Launched][REMOVE COUNT: Month][REMOVE SUM: Product]")
    f("Jan", ["Product"], 'X[SELECT: Month."Jan"]')
    m.recalc()
    return m


class Rename(unittest.TestCase):
    engine = staticmethod(ReferenceEngine)

    def setUp(self):
        self.m = model(self.engine())
        self.before = snapshot(self.m)

    def test_values_follow_the_new_name(self):
        self.m.rename_member("Month", "Mar", "March")
        self.assertEqual(self.m.dimension("Month").members, ["Jan", "Feb", "March", "Apr"])
        self.assertEqual(cells(self.m, "X")[("A", "March")], 4)
        self.assertEqual(self.m.get("Cutoff"), "March")
        self.assertEqual(self.m.get("Total", Month="March"), 4)
        self.assertIsNone(self.m.get("Total", Month="Mar"))

    def test_nothing_is_recalculated(self):
        self.m.eval_log.clear()
        self.m.rename_member("Product", "A", "Alpha")
        self.m.recalc()
        self.assertEqual(list(self.m.eval_log), [])

    def test_formulas_use_the_new_name(self):
        self.m.rename_member("Month", "Jan", "January")
        self.assertEqual(to_formula(self.m.metric("Jan").written, self.m), 'X[SELECT: Month."January"]')
        self.assertEqual(self.m.get("Jan", Product="A"), 1)
        self.m.set_cell("X", 5, Product="A", Month="January")
        self.assertEqual(self.m.get("Jan", Product="A"), 5)

    def test_properties_on_both_sides(self):
        self.m.rename_member("Category", "ハード", "HW")
        self.m.rename_member("Product", "C", "Cee")
        self.assertEqual(mapping(self.m, "Product", "Category"), {"A": "HW", "B": "HW", "Cee": "ソフト"})
        self.m.set_cell("Rate", 20, Category="HW")
        self.assertEqual(self.m.get("Priced", Product="A", Month="Jan"), 20)
        self.m.set_cell("X", 1, Product="Cee", Month="Jan")
        self.assertEqual(self.m.get("ByCategory", Category="ソフト", Month="Jan"), 1)

    def test_same_values_under_new_names(self):
        self.m.rename_member("Month", "Feb", "February")
        after = snapshot(self.m)
        for name, cs in self.before.items():
            renamed = {tuple("February" if x == "Feb" else x for x in k): v for k, v in cs.items()}
            if name in ("Cutoff", "Launch"):
                renamed = {k: "February" if v == "Feb" else v for k, v in renamed.items()}
            self.assertEqual(after[name], renamed, name)

    def test_errors(self):
        with self.assertRaisesRegex(ValueError, "がない"):
            self.m.rename_member("Month", "Dec", "December")
        with self.assertRaisesRegex(ValueError, "すでにある"):
            self.m.rename_member("Month", "Jan", "Feb")

    def test_fork_is_independent(self):
        fork = self.m.fork()
        fork.rename_member("Month", "Jan", "January")
        self.assertEqual(snapshot(self.m), self.before)
        self.assertEqual(to_formula(self.m.metric("Jan").written, self.m), 'X[SELECT: Month."Jan"]')
        self.assertEqual(fork.get("Jan", Product="A"), 1)

    def test_rename_members_changes_nothing_on_an_error(self):
        d = self.m.dimension("Month")
        jan, feb = d.id_of("Jan"), d.id_of("Feb")
        for names, error in (({jan: "Feb"}, "重複"), ({jan: "X", "nope": "Y"}, "メンバーがない")):
            with self.assertRaisesRegex(ValueError, error):
                d.rename_members(names)
            self.assertEqual(d.members, ["Jan", "Feb", "Mar", "Apr"])
            self.assertEqual(d.id_of("Jan"), jan)
        d.rename_members({jan: "Feb", feb: "Jan"})
        self.assertEqual((d.id_of("Feb"), d.id_of("Jan")), (jan, feb))


class Remove(unittest.TestCase):
    engine = staticmethod(ReferenceEngine)

    def setUp(self):
        self.m = model(self.engine())

    def check_full(self):
        check_full(self, self.m)

    def test_middle_month(self):
        self.m.remove_member("Month", "Feb")
        self.assertEqual(self.m.dimension("Month").members, ["Jan", "Mar", "Apr"])
        self.assertEqual(cells(self.m, "X"), {("A", "Jan"): 1, ("A", "Mar"): 4, ("C", "Apr"): 16})
        # Mar の前月は Jan になる
        self.assertEqual(cells(self.m, "Prev"), {("A", "Mar"): 1, ("A", "Apr"): 4})
        self.assertEqual(cells(self.m, "Cum"), {("A", "Jan"): 1, ("A", "Mar"): 5, ("A", "Apr"): 5, ("C", "Apr"): 16})
        self.assertEqual(self.m.get("Dense"), 1 + 4 + 16 + 3 * 3 + 0)  # 値 + 各セルの 1
        self.assertEqual(self.m.get("Past", Month="Mar"), True)  # 順序の比較は詰めたあとも保たれる
        self.assertEqual(self.m.get("Past", Month="Apr"), False)
        self.check_full()

    def test_previous_skips_blank_month(self):
        # Mar には値がひとつもないが、消すと Apr の前月が Mar（空）から Feb になる
        self.m.set_cell("X", None, Product="A", Month="Mar")
        self.m.recalc()
        self.assertEqual(self.m.get("Prev", Product="A", Month="Apr"), None)
        self.m.remove_member("Month", "Mar")
        self.assertEqual(self.m.get("Prev", Product="A", Month="Apr"), 2)
        self.assertEqual(self.m.get("Prev", Product="B", Month="Apr"), 8)
        self.check_full()

    def test_member_values_pointing_to_it_become_blank(self):
        self.m.remove_member("Month", "Feb")
        self.assertEqual(cells(self.m, "Launch"), {("B",): "Jan", ("C",): "Apr"})
        self.assertEqual(self.m.get("Launched", Product="A", Month="Mar"), None)
        self.assertEqual(self.m.get("Live"), 3 + 1)  # B は残る 3 か月すべて、C は Apr だけ
        self.m.remove_member("Month", "Mar")
        self.assertEqual(self.m.get("Cutoff"), None)
        self.assertEqual(cells(self.m, "Past"), {})
        self.check_full()

    def test_property_target(self):
        self.m.remove_member("Category", "ハード")
        self.assertEqual(mapping(self.m, "Product", "Category"), {"C": "ソフト"})
        self.assertEqual(cells(self.m, "Priced"), {("C", "Apr"): 1600})  # A と B は参照先がなくなった
        self.assertEqual(cells(self.m, "ByCategory"), {("ソフト", "Apr"): 16})
        self.check_full()

    def test_property_source(self):
        self.m.remove_member("Product", "B")
        self.assertEqual(self.m.get("ByCategory", Category="ハード", Month="Feb"), 2)
        self.assertEqual(self.m.get("Total", Month="Feb"), 2)
        self.check_full()

    def test_referenced_in_formula(self):
        with self.assertRaisesRegex(ValueError, 'Jan の式が Month."Jan" を参照している'):
            self.m.remove_member("Month", "Jan")
        self.assertIn("Jan", self.m.dimension("Month"))

    def test_last_and_first_members(self):
        self.m.remove_member("Month", "Apr")
        self.m.remove_member("Product", "A")
        self.assertEqual(cells(self.m, "Prev"), {("B", "Mar"): 8})
        self.check_full()

    def test_pending_edits_are_kept(self):
        self.m.set_cell("X", 7, Product="C", Month="Mar")
        self.m.add_member("Month", "May")
        self.m.set_cell("X", 3, Product="C", Month="May")
        self.m.remove_member("Month", "Feb")
        self.assertEqual(self.m.get("Prev", Product="C", Month="May"), 16)
        self.assertEqual(self.m.get("Prev", Product="C", Month="Apr"), 7)
        self.check_full()

    def test_add_after_remove(self):
        self.m.remove_member("Month", "Feb")
        self.m.add_member("Month", "May")
        self.m.set_cell("X", 32, Product="A", Month="May")
        self.m.add_member("Month", "Feb")  # 名前は使い回せる（末尾に入る）
        self.m.set_cell("X", 64, Product="A", Month="Feb")
        self.assertEqual(self.m.get("Prev", Product="A", Month="Feb"), 32)
        self.check_full()

    def test_fork_is_independent(self):
        before = snapshot(self.m)
        fork = self.m.fork()
        fork.remove_member("Month", "Feb")
        fork.set_cell("X", 100, Product="A", Month="Mar")
        self.assertEqual(snapshot(self.m), before)
        self.assertEqual(self.m.dimension("Month").members, ["Jan", "Feb", "Mar", "Apr"])
        self.assertEqual(fork.get("Total", Month="Mar"), 100)

    @unittest.skipIf(nanashi_core is None, "nanashi_core が必要")
    def test_matches_model_saved_and_loaded(self):
        self.m.remove_member("Month", "Feb")
        self.m.remove_member("Category", "ソフト")
        with tempfile.TemporaryDirectory() as tmp:
            self.m.save(tmp)
            loaded = Named.load(tmp, self.engine())
        a, b = snapshot(self.m), snapshot(loaded)
        for name in a:
            self.assertTrue(same(a[name], b[name]), name)


@unittest.skipIf(RustEngine is None, "nanashi_core のビルドが必要")
class RustRename(Rename):
    engine = staticmethod(RustEngine) if RustEngine is not None else None


@unittest.skipIf(RustEngine is None, "nanashi_core のビルドが必要")
class RustRemove(Remove):
    engine = staticmethod(RustEngine) if RustEngine is not None else None


# ---------------------------------------------------------------- ランダムな操作

def structural(rng: random.Random, models: list[Model], dims: list[str], counter: list[int]) -> None:
    """メンバーの追加、名前の変更、削除のどれかを、全モデルに同じように加える。"""
    m0 = models[0]
    d = rng.choice(dims)
    members = m0.dimension(d).members
    kind = rng.random()
    counter[0] += 1
    if kind < 0.3 or len(members) <= 1:
        for m in models:
            m.add_member(d, f"{d[0]}+{counter[0]}")
    elif kind < 0.6:
        old = rng.choice(members)
        for m in models:
            m.rename_member(d, old, f"{old}~{counter[0]}")
    else:
        victim = rng.choice(members)
        if any(f'{d}."{victim}"' in to_formula(x.written, m0) for x in m0.metrics.values() if x.written is not None):
            return  # 式が参照しているメンバーは消せない
        for m in models:
            m.remove_member(d, victim)


def run_random(test, make, edit, dims, seed, rounds, engines=None):
    """編集と構造の変更をランダムに加え、毎回、差分の結果を全体の計算し直し（と参照実装）と比べる。"""
    rng = random.Random(seed)
    models = [make(e()) for e in (engines or [ReferenceEngine])]
    counter = [0]
    for round_ in range(rounds):
        for _ in range(rng.randint(1, 3)):
            if rng.random() < 0.35:
                structural(rng, models, dims, counter)
            else:
                state = rng.getstate()
                for m in models:
                    r = random.Random()
                    r.setstate(state)
                    edit(m, r)
                rng.random()
        if engines is None:
            m = models[0]
            incremental = snapshot(m)
            m._invalidate()
            full = snapshot(m)
            pairs = [(incremental, full, "差分", "全体")]
        else:
            pairs = [(snapshot(models[0]), snapshot(other), "参照", "Rust") for other in models[1:]]
        for a, b, la, lb in pairs:
            for name in a:
                with test.subTest(round=round_, metric=name):
                    test.assertTrue(same(a[name], b[name]), f"{name}\n{la}: {a[name]}\n{lb}: {b[name]}")


def edit_small(m: Model, r: random.Random) -> None:
    kind = r.random()
    if kind < 0.6:
        coords = {d: r.choice(m.dimension(d).members) for d in ("Product", "Month")}
        m.set_cell("X", None if r.random() < 0.3 else float(r.randint(-5, 50)), **coords)
    elif kind < 0.8:
        p = r.choice(m.dimension("Product").members)
        m.set_cell("Launch", None if r.random() < 0.2 else r.choice(m.dimension("Month").members), Product=p)
    else:
        c = r.choice(m.dimension("Category").members)
        m.set_cell("Rate", float(r.randint(1, 9)), Category=c)


def edit_versions(m: Model, r: random.Random) -> None:
    coords = {d: r.choice(m.dimension(d).members) for d in ("Product", "Version", "Month")}
    m.set_cell("Sales", None if r.random() < 0.3 else float(r.randint(-5, 60)), **coords)


def edit_planning(m: Model, r: random.Random) -> None:
    e, t = r.choice(m.dimension("Employee").members), r.choice(m.dimension("Month").members)
    kind = r.random()
    if kind < 0.4:
        m.set_cell("Salary", None if r.random() < 0.2 else float(r.randint(1, 500)), Employee=e, Month=t)
    elif kind < 0.8:
        m.set_cell("Bonus", None if r.random() < 0.4 else float(r.randint(1, 90)), Employee=e, Month=t)
    else:
        m.spread("Budget", float(r.randint(10, 100)), Month=t)


def small_fpa(engine):
    m = build(engine, employees=10, products=6, months=8, seed=5)
    m.recalc()
    return m


def edit_fpa(m: Model, r: random.Random) -> None:
    """給与、異動、販売数量のどれか（EDITS と同じだが、消したり名前を変えたりしたメンバーを選ばない）。"""
    pick = lambda d: r.choice(m.dimension(d).members)
    kind = r.random()
    if kind < 0.3:
        m.set_cell("Salary", float(r.randint(300, 900)), Employee=pick("Employee"), Version="予算")
    elif kind < 0.6:
        m.set_cell("DeptOf", pick("Department"), Employee=pick("Employee"), Month=pick("Month"))
    elif kind < 0.9:
        m.set_cell("Units", float(r.randint(10, 200)), Product=pick("Product"), Version="予算", Month=pick("Month"))
    else:
        m.set_cell("HireMonth", pick("Month"), Employee=pick("Employee"))


CASES = [
    ("small", model, edit_small, ["Product", "Category", "Month"]),
    ("versions", version_model, edit_versions, ["Product", "Month"]),
    ("planning", planning_model, edit_planning, ["Employee", "Department", "Month"]),
    ("fpa", small_fpa, edit_fpa, ["Employee", "Product", "Month", "Department"]),
]


class MatchesFullRecalc(unittest.TestCase):
    def test_random(self):
        for i, (label, make, edit, dims) in enumerate(CASES):
            with self.subTest(case=label):
                run_random(self, make, edit, dims, seed=100 + i, rounds=40)


@unittest.skipIf(RustEngine is None, "nanashi_core のビルドが必要")
class RustMatchesReference(unittest.TestCase):
    def test_random(self):
        for i, (label, make, edit, dims) in enumerate(CASES):
            with self.subTest(case=label):
                run_random(self, make, edit, dims, seed=200 + i, rounds=40, engines=[ReferenceEngine, RustEngine])


if __name__ == "__main__":
    unittest.main()
