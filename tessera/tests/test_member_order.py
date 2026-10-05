"""メンバーの並び順（順位）。順序のない軸は、途中への挿入と並び替えで並び順だけを変え、エンジンの番号も
セルも変えない。順序付きの軸（時系列）は並び順を変えられない。"""
import unittest

from sparse_engine import Model
from sparse_engine.engine import ReferenceEngine
from sparse_engine.storage import dump, read

try:
    from sparse_engine.rust_engine import RustEngine
except ImportError:  # nanashi_core をビルドしていない環境
    RustEngine = None

try:
    import nanashi_core  # noqa: F401  保存形式（Parquet）の読み書きに使う
except ImportError:
    nanashi_core = None


def build(engine) -> Model:
    m = Model(engine=engine)
    m.add_dimension("Account", ["売上", "原価", "販管費"])
    m.add_dimension("Month", ["Jan", "Feb", "Mar"], ordered=True)
    m.add_input("Plan", ["Account", "Month"], {
        ("売上", "Jan"): 100.0, ("売上", "Feb"): 110.0, ("原価", "Jan"): 60.0, ("販管費", "Mar"): 20.0})
    m.add_formula("Plus1", ["Account", "Month"], "Plan + 1")  # 全メンバーへ広げる
    m.add_formula("Total", ["Month"], "Plan[REMOVE SUM: Account]")
    m.add_formula("Running", ["Account", "Month"], "PREVIOUS(Month) + IFBLANK(Plan, 0)")
    m.recalc()
    return m


class MemberOrder(unittest.TestCase):
    engine = staticmethod(ReferenceEngine)

    def setUp(self):
        self.m = build(self.engine())

    def rows(self, name: str = "Plan", **coords) -> list:
        return [k for k, _ in self.m.rows(name, **coords)[0]]

    def test_insert_in_the_middle_keeps_numbers_and_cells(self):
        m = self.m
        account = m.dimensions["Account"]
        m.add_member("Account", "粗利", at=2)
        self.assertEqual(account.in_order(), ["売上", "原価", "粗利", "販管費"])
        self.assertEqual(account.members, ["売上", "原価", "販管費", "粗利"])  # 番号は末尾に振る
        m.set_cell("Plan", 40.0, Account="粗利", Month="Jan")
        self.assertEqual(self.rows(Month="Jan"), [("売上", "Jan"), ("原価", "Jan"), ("粗利", "Jan")])
        self.assertEqual(m.get("Total", Month="Jan"), 200.0)
        self.assertEqual(m.get("Plus1", Account="粗利", Month="Feb"), 1.0)  # 足したメンバーにも広がる
        self.assertEqual(m.get("Running", Account="粗利", Month="Mar"), 40.0)

    def test_move_changes_only_the_order(self):
        m = self.m
        stores = dict(m._values)
        m.slice_log.clear()
        m.move_member("Account", "販管費", 0)
        m.move_member("Account", "売上", 2)
        m.recalc()
        self.assertEqual(m.dimensions["Account"].in_order(), ["販管費", "原価", "売上"])
        self.assertEqual(list(m.slice_log), [])  # 何も計算し直さない
        for name, store in stores.items():
            self.assertIs(m._values[m.metric(name).id], store, name)  # 格納データにも触れない
        self.assertEqual(self.rows(), [("販管費", "Mar"), ("原価", "Jan"), ("売上", "Jan"), ("売上", "Feb")])
        self.assertEqual([k for k, _ in m.rows("Plan", offset=1, limit=2)[0]], [("原価", "Jan"), ("売上", "Jan")])
        self.assertEqual(m.rows("Plan", offset=1, limit=2)[1], 4)

    def test_order_follows_removal_and_moving_back_restores_the_number_order(self):
        m = self.m
        account = m.dimensions["Account"]
        m.move_member("Account", "販管費", 0)
        m.remove_member("Account", "原価")  # 後ろの番号が詰まっても、並び順は保つ
        self.assertEqual(account.in_order(), ["販管費", "売上"])
        self.assertEqual(self.rows(), [("販管費", "Mar"), ("売上", "Jan"), ("売上", "Feb")])
        m.move_member("Account", "販管費", 1)
        self.assertIsNone(account.rank_table())  # 番号の順に戻れば、並び順の表を持たない
        m.add_member("Account", "原価", at=1)
        self.assertEqual(account.in_order(), ["売上", "原価", "販管費"])
        m.rename_member("Account", "原価", "売上原価")
        self.assertEqual(account.in_order(), ["売上", "売上原価", "販管費"])

    def test_rename_after_reading_in_order(self):
        m = self.m
        m.move_member("Account", "販管費", 0)
        self.assertEqual(self.rows(), [("販管費", "Mar"), ("売上", "Jan"), ("売上", "Feb"), ("原価", "Jan")])
        m.rename_member("Account", "販管費", "一般管理費")  # 名前 -> 順位の表を引いたあとで名前を変える
        self.assertEqual(self.rows()[0], ("一般管理費", "Mar"))
        self.assertEqual(m.dimensions["Account"].ranks()["一般管理費"], 0)

    def test_ordered_dimensions_cannot_be_reordered(self):
        m = self.m
        with self.assertRaisesRegex(ValueError, "順序付きの軸"):
            m.move_member("Month", "Mar", 0)
        with self.assertRaisesRegex(ValueError, "順序付きの軸"):
            m.add_member("Month", "Dec", at=0)
        self.assertNotIn("Dec", m.dimensions["Month"])
        m.add_member("Month", "Apr", at=3)  # 最後なら位置を渡してもよい
        self.assertEqual(m.dimensions["Month"].in_order(), ["Jan", "Feb", "Mar", "Apr"])
        self.assertEqual(m.get("Running", Account="売上", Month="Apr"), 210.0)

    def test_positions_are_checked(self):
        m = self.m
        for at in (-1, 4, 1.5, True, "1"):
            with self.subTest(at=at), self.assertRaisesRegex(ValueError, "並び順の位置"):
                m.add_member("Account", "x", at=at)
        self.assertNotIn("x", m.dimensions["Account"])
        with self.assertRaisesRegex(ValueError, "並び順の位置"):
            m.move_member("Account", "売上", 3)
        with self.assertRaisesRegex(ValueError, "メンバー 'x' がない"):
            m.move_member("Account", "x", 0)

    def test_rolled_back_transaction_restores_the_order(self):
        m = self.m
        with self.assertRaises(RuntimeError):
            with m.transaction():
                m.move_member("Account", "販管費", 0)
                m.add_member("Account", "粗利", at=1)
                raise RuntimeError
        self.assertEqual(m.dimensions["Account"].in_order(), ["売上", "原価", "販管費"])

    def test_fork_has_its_own_order(self):
        other = self.m.fork()
        other.move_member("Account", "販管費", 0)
        self.assertEqual(self.m.dimensions["Account"].in_order(), ["売上", "原価", "販管費"])
        self.assertEqual(other.dimensions["Account"].in_order(), ["販管費", "売上", "原価"])

    @unittest.skipIf(nanashi_core is None, "nanashi_core のビルドが必要")
    def test_save_and_load_keep_the_order(self):
        m = self.m
        m.add_member("Account", "粗利", at=2)
        m.set_cell("Plan", 40.0, Account="粗利", Month="Jan")
        m.move_member("Account", "販管費", 0)
        files = dump(m)
        back = read(files.__getitem__, self.engine())
        self.assertEqual(back.dimensions["Account"].in_order(), ["販管費", "売上", "原価", "粗利"])
        self.assertEqual(back.dimensions["Account"].members, m.dimensions["Account"].members)
        self.assertEqual(back.rows("Plan")[0], m.rows("Plan")[0])
        self.assertEqual(back.get("Total", Month="Jan"), 200.0)


@unittest.skipIf(RustEngine is None, "nanashi_core のビルドが必要")
class RustMemberOrder(MemberOrder):
    engine = staticmethod(RustEngine) if RustEngine is not None else None

    def test_rank_tables_are_checked(self):
        m = self.m
        store, core = m._values[m.metric("Plan").id], m.engine.core
        region = m.engine._region(m, None)
        for ranks in ([[0, 1]], [[0, 0, 1], None], [[0, 1, 3], None]):
            with self.subTest(ranks=ranks), self.assertRaisesRegex(ValueError, "並び順の表"):
                core.rows_in(store, region, ranks)


if __name__ == "__main__":
    unittest.main()
