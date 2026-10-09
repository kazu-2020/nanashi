// The creation of a dimension list (docs/glossary.md).
//
// Requirements:
// - The request has the id and the name of the list. It has no members.
// - The api normalizes the name: it changes each run of control characters to one space, and removes the white
//   space at the start and at the end. It refuses an empty name and a name of more than 200 code points.
// - The engine refuses a name that a block already has. The engine applies the operations one at a time.
// - A refused operation makes nothing. The api deletes the app_list row of a failed operation.
// - A resend with the same client_op_id changes nothing.
//
// This specification checks the states. A name is an opaque value: NORMALIZE and valid stand for the
// normalization and the checks. The Go tests check the characters.
module dimension_list {
  type Status = Pending | Done | Failed
  type Op = { list: str, name: str }

  // Raw name to normalized name. "_a_" is "a" with white space and control characters around it.
  pure val NORMALIZE = Map("a" -> "a", "_a_" -> "a", "b" -> "b", "blank" -> "", "long" -> "long")
  // "long" stands for a name of more than 200 code points.
  pure def valid(name: str): bool = name != "" and name != "long"

  pure val LIST_IDS = Set("l1", "l2", "l3")
  pure val OP_IDS = Set("o1", "o2", "o3", "o4")

  // Block id to name. It has the lists and the Metrics of the engine.
  var blocks: str -> str
  // The ids of the dimension lists in the engine.
  var dimensionLists: Set[str]
  // The rows of app_list.
  var appList: Set[str]
  // client_op_id to status and operation (app_operation).
  var ops: str -> { status: Status, op: Op }
  // The client_op_ids of the pending operations, in the order of the engine.
  var queue: List[str]

  action init = all {
    // A Metric with the name "b" exists.
    blocks' = Map("m1" -> "b"),
    dimensionLists' = Set(),
    appList' = Set(),
    ops' = Map(),
    queue' = List(),
  }

  // CreateList on the api: the plan and the first transaction. The api does not check the uniqueness.
  action create(opId: str, list: str, raw: str): bool =
    val name = NORMALIZE.get(raw)
    all {
      not(ops.keys().contains(opId)),
      not(appList.contains(list)),
      not(blocks.keys().contains(list)),
      valid(name),
      appList' = appList.union(Set(list)),
      ops' = ops.put(opId, { status: Pending, op: { list: list, name: name } }),
      queue' = queue.append(opId),
      blocks' = blocks,
      dimensionLists' = dimensionLists,
    }

  // A resend of a known client_op_id gives the stored result.
  action resend(opId: str): bool = all {
    ops.keys().contains(opId),
    blocks' = blocks,
    dimensionLists' = dimensionLists,
    appList' = appList,
    ops' = ops,
    queue' = queue,
  }

  // The engine applies the next pending operation. Then the second transaction of the api flips the row.
  action apply: bool = all {
    queue.length() > 0,
    val opId = queue.head()
    val o = ops.get(opId).op
    val taken = blocks.keys().exists(b => blocks.get(b) == o.name)
    all {
      queue' = queue.tail(),
      if (taken) all {
        ops' = ops.set(opId, { status: Failed, op: o }),
        appList' = appList.exclude(Set(o.list)),
        blocks' = blocks,
        dimensionLists' = dimensionLists,
      } else all {
        ops' = ops.set(opId, { status: Done, op: o }),
        appList' = appList,
        blocks' = blocks.put(o.list, o.name),
        dimensionLists' = dimensionLists.union(Set(o.list)),
      },
    }
  }

  action step = any {
    nondet opId = OP_IDS.oneOf()
    any {
      nondet list = LIST_IDS.oneOf()
      nondet raw = NORMALIZE.keys().oneOf()
      create(opId, list, raw),
      resend(opId),
    },
    apply,
  }

  val uniqueNames = blocks.keys().forall(a => blocks.keys().forall(b => a == b or blocks.get(a) != blocks.get(b)))

  val validNames = dimensionLists.forall(l => valid(blocks.get(l)))

  // Each dimension list in the engine has an app_list row. With no pending operation, the opposite is also true.
  val appListMatchesEngine =
    dimensionLists.subseteq(appList) and (queue.length() == 0 implies appList == dimensionLists)

  val inv = uniqueNames and validNames and appListMatchesEngine

  // "a" and "_a_" are the same name after the normalization, so the second one fails.
  run sameNameAfterNormalizationTest =
    init
      .then(create("o1", "l1", "a"))
      .then(create("o2", "l2", "_a_"))
      .then(apply)
      .then(apply)
      .then(all {
        assert(ops.get("o1").status == Done),
        assert(ops.get("o2").status == Failed),
        assert(appList == Set("l1")),
        assert(inv),
        resend("o2"),
      })
}
