// The creation of a dimension list (docs/glossary.md).
//
// Requirements:
// - The request has the id and the name of the list. It has no members.
// - Name normalization: change each run of control characters to one space. Then remove the white space at the
//   start and at the end.
// - The api refuses an empty name and a name of more than MAX_LEN code points (after the normalization).
// - The engine refuses a name that a block already has. The engine applies the operations one at a time.
// - A refused operation makes nothing. The api deletes the app_list row of a failed operation.
// - A resend with the same client_op_id gives the stored result.
//
// A name is a list of characters. Txt is a character that is not white space.
module dimension_list {
  type Char = Txt(str) | Sp | Ctrl

  type Status = Pending | Done | Failed
  type Op = { list: str, name: List[Char] }

  const MAX_LEN: int

  // The calculations.

  pure def replaceCtrl(s: List[Char]): List[Char] =
    s.foldl({ out: List(), prevCtrl: false }, (st, c) => match c {
      | Ctrl => if (st.prevCtrl) st else { out: st.out.append(Sp), prevCtrl: true }
      | _ => { out: st.out.append(c), prevCtrl: false }
    }).out

  pure def trim(s: List[Char]): List[Char] =
    val kept = s.indices().filter(i => s[i] != Sp)
    if (kept.size() == 0) List()
    else
      val first = kept.fold(s.length(), (a, i) => if (i < a) i else a)
      val last = kept.fold(0, (a, i) => if (i > a) i else a)
      s.slice(first, last + 1)

  pure def normalize(s: List[Char]): List[Char] = trim(replaceCtrl(s))

  // The api check. The api does not check the uniqueness: only the engine knows all block names.
  pure def nameError(name: List[Char]): str =
    if (name.length() == 0) "empty"
    else if (name.length() > MAX_LEN) "too_long"
    else ""

  // The state.

  // Block id to name. It has the lists and the Metrics of the engine.
  var blocks: str -> List[Char]
  // The ids of the dimension lists in the engine.
  var dimensionLists: Set[str]
  // The rows of app_list.
  var appList: Set[str]
  // client_op_id to status and operation (app_operation).
  var ops: str -> { status: Status, op: Op }
  // The client_op_ids of the pending operations, in the order of the engine.
  var queue: List[str]

  pure val IDS = Set("l1", "l2", "l3")
  pure val OP_IDS = Set("o1", "o2", "o3", "o4")
  pure val ALPHABET = Set(Txt("a"), Txt("b"), Sp, Ctrl)

  action init = all {
    // A Metric with the name "a" exists.
    blocks' = Map("m1" -> List(Txt("a"))),
    dimensionLists' = Set(),
    appList' = Set(),
    ops' = Map(),
    queue' = List(),
  }

  // The actions.

  // CreateList on the api: the plan and the first transaction.
  action create(opId: str, list: str, raw: List[Char]): bool =
    val name = normalize(raw)
    all {
      not(ops.keys().contains(opId)),        // A resend of opId changes nothing.
      not(appList.contains(list)),
      not(blocks.keys().contains(list)),
      nameError(name) == "",
      appList' = appList.union(Set(list)),
      ops' = ops.put(opId, { status: Pending, op: { list: list, name: name } }),
      queue' = queue.append(opId),
      blocks' = blocks,
      dimensionLists' = dimensionLists,
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

  action createAny = {
    nondet opId = OP_IDS.oneOf()
    nondet list = IDS.oneOf()
    // A raw name of up to 5 characters. With MAX_LEN = 3, some names are too long.
    nondet n = 0.to(5).oneOf()
    nondet c0 = ALPHABET.oneOf()
    nondet c1 = ALPHABET.oneOf()
    nondet c2 = ALPHABET.oneOf()
    nondet c3 = ALPHABET.oneOf()
    nondet c4 = ALPHABET.oneOf()
    create(opId, list, List(c0, c1, c2, c3, c4).slice(0, n))
  }

  action step = any { createAny, apply }

  // The invariants.

  val uniqueNames = blocks.keys().forall(a => blocks.keys().forall(b => a == b or blocks.get(a) != blocks.get(b)))

  val validNames = dimensionLists.forall(l =>
    val name = blocks.get(l)
    nameError(name) == "" and name == normalize(name))

  // Each dimension list in the engine has an app_list row. With no pending operation, the opposite is also true.
  val appListMatchesEngine =
    dimensionLists.subseteq(appList) and (queue.length() == 0 implies appList == dimensionLists)

  val inv = uniqueNames and validNames and appListMatchesEngine
}

module dimension_list_tests {
  import dimension_list(MAX_LEN = 200).*

  run normalizeTest = all {
    assert(normalize(List(Ctrl, Txt("a"), Ctrl, Ctrl, Txt("b"), Sp)) == List(Txt("a"), Sp, Txt("b"))),
    assert(normalize(List(Txt("a"), Sp, Sp, Txt("b"))) == List(Txt("a"), Sp, Sp, Txt("b"))),
    assert(normalize(List(Sp, Ctrl, Sp)) == List()),
    assert(nameError(List()) == "empty"),
    assert(nameError(range(0, 200).foldl(List(), (acc, i) => acc.append(Txt("a")))) == ""),
    assert(nameError(range(0, 201).foldl(List(), (acc, i) => acc.append(Txt("a")))) == "too_long"),
  }
}

// A small instance for the simulator.
module dimension_list_small {
  import dimension_list(MAX_LEN = 3).*
}
