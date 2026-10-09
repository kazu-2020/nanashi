// The creation of a dimension list (docs/glossary.md).
//
// Requirements:
// - The request has the id and the name of the list. It has no members.
// - The api changes each run of control characters in the name to one space.
//   Then it removes the white space at the start and at the end.
// - The api refuses an empty name and a name of more than 200 code points.
// - The engine refuses a name or an id that a block already has. It applies the operations one at a time.
// - A refused operation makes nothing. The api deletes the app_list row of a refused operation.
//
// This specification checks the states. A name is an opaque value: NORMALIZE and valid stand for the
// normalization and the checks (listName in api/model.go). The resend of a client_op_id is out of scope.
module dimension_list {
  type Status = Pending | Done | Failed
  type Op = { list: str, name: str }

  // Raw name to normalized name. "_a_" is "a" with white space and control characters around it.
  pure val NORMALIZE = Map("a" -> "a", "_a_" -> "a", "b" -> "b", "blank" -> "", "long" -> "long")
  // "long" stands for a name of more than 200 code points.
  pure def valid(name: str): bool = name != "" and name != "long"

  // The Metrics. m1 is also in LIST_IDS, so a list id can collide with a block that is not a list.
  pure val METRICS = Map("m1" -> "b")
  pure val LIST_IDS = Set("l1", "l2", "m1")
  pure val OP_IDS = Set("o1", "o2", "o3", "o4")

  // Block id to name. It has the lists and the Metrics of the engine.
  var blocks: str -> str
  // The ids of the dimension lists in the engine.
  var dimensionLists: Set[str]
  // client_op_id to the result of the engine.
  var engineResults: str -> Status
  // The rows of app_list.
  var appList: Set[str]
  // client_op_id to status and operation (app_operation).
  var ops: str -> { status: Status, op: Op }
  // The client_op_ids that the engine did not apply yet, in the order of the engine.
  var queue: List[str]

  action init = all {
    blocks' = METRICS,
    dimensionLists' = Set(),
    engineResults' = Map(),
    appList' = Set(),
    ops' = Map(),
    queue' = List(),
  }

  // CreateList on the api: the plan and the first transaction. As in api/model.go, the api checks only the
  // dimension lists and app_list. It does not check the names or the Metric ids.
  action create(opId: str, list: str, raw: str): bool =
    val name = NORMALIZE.get(raw)
    all {
      not(ops.keys().contains(opId)),
      not(dimensionLists.contains(list)),
      not(appList.contains(list)),
      valid(name),
      appList' = appList.union(Set(list)),
      ops' = ops.put(opId, { status: Pending, op: { list: list, name: name } }),
      queue' = queue.append(opId),
      blocks' = blocks,
      dimensionLists' = dimensionLists,
      engineResults' = engineResults,
    }

  // The engine applies the next operation and keeps the result.
  action engineApply: bool = all {
    queue.length() > 0,
    val opId = queue.head()
    val o = ops.get(opId).op
    val refused = blocks.keys().contains(o.list) or blocks.keys().exists(b => blocks.get(b) == o.name)
    all {
      queue' = queue.tail(),
      engineResults' = engineResults.put(opId, if (refused) Failed else Done),
      blocks' = if (refused) blocks else blocks.put(o.list, o.name),
      dimensionLists' = if (refused) dimensionLists else dimensionLists.union(Set(o.list)),
      appList' = appList,
      ops' = ops,
    }
  }

  // The second transaction of the api. It can come at any time after the engine result, in any order.
  action apiFinish(opId: str): bool = all {
    engineResults.keys().contains(opId),
    ops.get(opId).status == Pending,
    val o = ops.get(opId).op
    val result = engineResults.get(opId)
    all {
      ops' = ops.set(opId, { status: result, op: o }),
      appList' = if (result == Failed) appList.exclude(Set(o.list)) else appList,
      blocks' = blocks,
      dimensionLists' = dimensionLists,
      engineResults' = engineResults,
      queue' = queue,
    }
  }

  action step = any {
    nondet opId = OP_IDS.oneOf()
    any {
      nondet list = LIST_IDS.oneOf()
      nondet raw = NORMALIZE.keys().oneOf()
      create(opId, list, raw),
      apiFinish(opId),
    },
    engineApply,
  }

  val uniqueNames = blocks.keys().forall(a => blocks.keys().forall(b => a == b or blocks.get(a) != blocks.get(b)))

  val metricsKept = METRICS.keys().forall(m => blocks.get(m) == METRICS.get(m) and not(dimensionLists.contains(m)))

  val noPending = ops.keys().forall(o => ops.get(o).status != Pending)

  val appListMatchesEngine = dimensionLists.subseteq(appList) and (noPending implies appList == dimensionLists)

  // The api keeps the result of the engine.
  val statusMatchesEngine =
    ops.keys().forall(o => ops.get(o).status == Pending or ops.get(o).status == engineResults.get(o))

  val inv = uniqueNames and metricsKept and appListMatchesEngine and statusMatchesEngine

  run sameNameAfterNormalizationTest =
    init
      .then(create("o1", "l1", "a"))
      .then(create("o2", "l2", "_a_"))
      .then(engineApply)
      .then(engineApply)
      .then(apiFinish("o2"))
      .then(apiFinish("o1"))
      .then(all {
        assert(ops.get("o1").status == Done),
        assert(ops.get("o2").status == Failed),
        assert(appList == Set("l1")),
        assert(inv),
        engineApply.fail(),
      })

  run metricIdTest =
    init
      .then(create("o1", "m1", "a"))
      .then(engineApply)
      .then(apiFinish("o1"))
      .then(all {
        assert(ops.get("o1").status == Failed),
        assert(appList == Set()),
        assert(blocks.get("m1") == "b"),
        engineApply.fail(),
      })
}
