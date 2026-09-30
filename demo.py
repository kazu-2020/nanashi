"""人件費と資金繰りの小さな計画モデル。"""
from sparse_engine import Model

m = Model()
m.add_dimension("Month", ["Jan", "Feb", "Mar", "Apr", "May", "Jun"], ordered=True)
m.add_dimension("Employee", ["alice", "bob", "carol", "dave"])
m.add_dimension("Department", ["Sales", "Eng"])
m.add_property("Employee", "Department", "Department",
               {"alice": "Sales", "bob": "Sales", "carol": "Eng", "dave": "Eng"})

# 入力 Metric
m.add_input("Salary", ["Employee"], {("alice",): 50, ("bob",): 40, ("carol",): 60, ("dave",): 55})
m.add_input("Raise", ["Department"], {("Eng",): 0.1})
# 在籍している月だけ値を持つ（dave は Apr 入社、bob は Mar 退職）
active = {(e, t): 1 for e in ["alice", "carol"] for t in ["Jan", "Feb", "Mar", "Apr", "May", "Jun"]}
active |= {("bob", t): 1 for t in ["Jan", "Feb", "Mar"]}
active |= {("dave", t): 1 for t in ["Apr", "May", "Jun"]}
m.add_input("Active", ["Employee", "Month"], active)
m.add_input("Funding", ["Month"], {("Jan",): 500, ("Apr",): 200})

# 計算 Metric（すべて Metric 全体に対する式）
m.add_formula("Cost", ["Employee", "Month"],
              "Active * Salary * (1 + Raise[BY: Employee.Department])")
m.add_formula("DeptCost", ["Department", "Month"], "Cost[BY SUM: Employee.Department]")
m.add_formula("TotalCost", ["Month"], "DeptCost[REMOVE SUM: Department]")
m.add_formula("Cash", ["Month"], "PREVIOUS(Month) + Funding - TotalCost")

for name in ["DeptCost", "Cash"]:
    print(f"--- {name}\n{m.value(name).format(m.dimensions)}\n")

print("--- 計算計画")
for step in m._plan:
    kind = f"scan over {step.scan_dim}" if step.scan_dim else "block"
    print(f"{', '.join(step.names):12s} {kind}")

print("\n--- 警告")
for name, ws in m.warnings.items():
    for w in ws:
        print(f"{name}: {w}")

def show(region):
    if not region:
        return "全体"
    return " × ".join(f"{d}={{{', '.join(x for x in m.dimensions[d].members if x in ms)}}}" for d, ms in region.items())


m.slice_log.clear()
m.set_cell("Salary", 45, Employee="bob")
m.recalc()
print("\n--- bob の給与を変更したときに再計算された範囲")
for name, region in m.slice_log:
    print(f"{name:10s} {show(region)}")

m.slice_log.clear()
m.set_cell("Funding", 100, Month="Apr")
m.recalc()
print("\n--- Apr の資金調達を変更したときに再計算された範囲")
for name, region in m.slice_log:
    print(f"{name:10s} {show(region)}")
print(m.value("Cash").format(m.dimensions))
