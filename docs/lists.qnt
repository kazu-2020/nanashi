// The formal specification of the list screen (docs/lists.md).
//
// The model state is the lists, and the objects that use a list (a Metric, a view, a board).
// The screen state is the dialog that is open. A delete of a list is safe only if nothing uses it.
//
// Check it:
//   quint typecheck docs/lists.qnt
//   quint run docs/lists.qnt --invariant=safety --max-steps=30 --max-samples=20000
module lists {
  // --- Data ---

  type ListId = str
  type MemberId = str
  type PropId = str
  // A Metric, a view or a board. Each one uses lists by id.
  type UserId = str

  type Kind = Dimension | Transaction

  // The type of a property. A Dim property points to a member of the target list.
  type PropType = Text | Number | Boolean | Dim(ListId)

  type Prop = { name: str, ptype: PropType }

  type ListDef = {
    name: str,
    kind: Kind,
    members: Set[MemberId],
    props: PropId -> Prop,
  }

  // The dialog of the list screen. Confirming holds the text that the user typed.
  type Dialog =
    | Closed
    | Creating
    | AddingProperty(ListId)
    | Settings(ListId)
    | DeleteBlocked(ListId)
    | DeleteConfirm({ list: ListId, typed: str })

  // --- Constants of the model checker ---

  pure val LIST_IDS: Set[ListId] = Set("L1", "L2", "L3")
  pure val NAMES: Set[str] = Set("部門", "製品", "")
  pure val MEMBER_IDS: Set[MemberId] = Set("m1", "m2")
  pure val PROP_IDS: Set[PropId] = Set("p1", "p2")
  pure val USER_IDS: Set[UserId] = Set("売上高", "部門別P/L")

  // --- State ---

  var lists: ListId -> ListDef
  // The pairs (user, list): the user uses the list.
  var uses: Set[(UserId, ListId)]
  var dialog: Dialog

  // --- Calculations ---

  pure def nameFree(ls: ListId -> ListDef, name: str, self: ListId): bool =
    ls.keys().forall(id => id == self or ls.get(id).name != name)

  // A valid name is not blank and no other list has it.
  pure def validName(ls: ListId -> ListDef, name: str, self: ListId): bool =
    name != "" and nameFree(ls, name, self)

  pure def targetOf(t: PropType): Set[ListId] =
    match t {
      | Dim(l) => Set(l)
      | _ => Set()
    }

  // The lists that a list points to with its Dim properties.
  pure def targets(l: ListDef): Set[ListId] =
    l.props.keys().fold(Set(), (acc, p) => acc.union(targetOf(l.props.get(p).ptype)))

  // The usages of a list: each object that uses it, and each other list that points to it.
  // The screen shows this set in the usage tab and in the blocked dialog.
  pure def usages(ls: ListId -> ListDef, us: Set[(UserId, ListId)], id: ListId): Set[str] =
    us.filter(u => u._2 == id).map(u => u._1)
      .union(ls.keys().filter(o => o != id and targets(ls.get(o)).contains(id)).map(o => ls.get(o).name))

  // The delete action of the menu opens one of two dialogs. It never deletes directly.
  pure def deleteDialog(ls: ListId -> ListDef, us: Set[(UserId, ListId)], id: ListId): Dialog =
    if (usages(ls, us, id) == Set()) DeleteConfirm({ list: id, typed: "" }) else DeleteBlocked(id)

  // The delete button is on only if the typed text is the list name.
  pure def canDelete(ls: ListId -> ListDef, d: { list: ListId, typed: str }): bool =
    ls.keys().contains(d.list) and d.typed == ls.get(d.list).name

  // --- Invariants ---

  // No two lists have the same name, and no name is blank.
  val uniqueNames: bool =
    lists.keys().forall(a => lists.get(a).name != "" and nameFree(lists, lists.get(a).name, a))

  // Each reference points to a list that exists.
  val noDangling: bool = and {
    uses.forall(u => lists.keys().contains(u._2)),
    lists.keys().forall(id => targets(lists.get(id)).subseteq(lists.keys())),
  }

  // A Dim property points to a dimension list other than its own list.
  val dimTargets: bool =
    lists.keys().forall(id => targets(lists.get(id)).forall(t =>
      t != id and lists.get(t).kind == Dimension))

  // A dialog names a list that exists. A blocked dialog shows a list that is in use.
  val dialogConsistent: bool =
    match dialog {
      | Closed => true
      | Creating => true
      | AddingProperty(l) => lists.keys().contains(l)
      | Settings(l) => lists.keys().contains(l)
      | DeleteBlocked(l) => lists.keys().contains(l) and usages(lists, uses, l) != Set()
      | DeleteConfirm(d) => lists.keys().contains(d.list)
    }

  val safety: bool = uniqueNames and noDangling and dimTargets and dialogConsistent

  // --- Actions ---

  action init = all {
    lists' = Map(),
    uses' = Set(),
    dialog' = Closed,
  }

  action openCreate = all {
    dialog == Closed,
    dialog' = Creating,
    lists' = lists,
    uses' = uses,
  }

  action cancel = all {
    dialog != Closed,
    dialog' = Closed,
    lists' = lists,
    uses' = uses,
  }

  // The create dialog takes a name and a kind only. The new list is empty.
  action createList(id: ListId, name: str, kind: Kind): bool = all {
    dialog == Creating,
    not(lists.keys().contains(id)),
    validName(lists, name, id),
    lists' = lists.put(id, { name: name, kind: kind, members: Set(), props: Map() }),
    uses' = uses,
    dialog' = Closed,
  }

  action addMember(id: ListId, m: MemberId): bool = all {
    lists.keys().contains(id),
    lists' = lists.setBy(id, l => { ...l, members: l.members.union(Set(m)) }),
    uses' = uses,
    dialog' = dialog,
  }

  action removeMember(id: ListId, m: MemberId): bool = all {
    lists.keys().contains(id),
    lists' = lists.setBy(id, l => { ...l, members: l.members.exclude(Set(m)) }),
    uses' = uses,
    dialog' = dialog,
  }

  action openAddProperty(id: ListId): bool = all {
    dialog == Closed,
    lists.keys().contains(id),
    dialog' = AddingProperty(id),
    lists' = lists,
    uses' = uses,
  }

  // The type of a property does not change after this action.
  action addProperty(p: PropId, name: str, t: PropType): bool = all {
    match dialog {
      | AddingProperty(id) => all {
          not(lists.get(id).props.keys().contains(p)),
          name != "",
          lists.get(id).props.keys().forall(q => lists.get(id).props.get(q).name != name),
          targetOf(t).forall(tl => tl != id and lists.keys().contains(tl) and lists.get(tl).kind == Dimension),
          lists' = lists.setBy(id, l => { ...l, props: l.props.put(p, { name: name, ptype: t }) }),
        }
      | _ => all { false, lists' = lists }
    },
    uses' = uses,
    dialog' = Closed,
  }

  action openSettings(id: ListId): bool = all {
    dialog == Closed,
    lists.keys().contains(id),
    dialog' = Settings(id),
    lists' = lists,
    uses' = uses,
  }

  // The settings dialog changes the name only. The kind does not change after the create.
  action rename(name: str): bool = all {
    match dialog {
      | Settings(id) => all {
          validName(lists, name, id),
          lists' = lists.setBy(id, l => { ...l, name: name }),
        }
      | _ => all { false, lists' = lists }
    },
    uses' = uses,
    dialog' = Closed,
  }

  // A Metric, a view or a board starts to use a list (the other screens do it).
  action use(u: UserId, id: ListId): bool = all {
    lists.keys().contains(id),
    uses' = uses.union(Set((u, id))),
    lists' = lists,
    dialog' = dialog,
  }

  action unuse(u: UserId, id: ListId): bool = all {
    uses' = uses.exclude(Set((u, id))),
    lists' = lists,
    // The blocked dialog stays only while the list is in use.
    dialog' = match dialog {
      | DeleteBlocked(l) => if (usages(lists, uses.exclude(Set((u, id))), l) == Set()) Closed else dialog
      | _ => dialog
    },
  }

  action openDelete(id: ListId): bool = all {
    dialog == Closed,
    lists.keys().contains(id),
    dialog' = deleteDialog(lists, uses, id),
    lists' = lists,
    uses' = uses,
  }

  action typeName(text: str): bool = all {
    match dialog {
      | DeleteConfirm(d) => dialog' = DeleteConfirm({ ...d, typed: text })
      | _ => all { false, dialog' = dialog }
    },
    lists' = lists,
    uses' = uses,
  }

  // The delete removes the list with its members and properties. The guard checks the usages again,
  // because another user can start to use the list while the dialog is open.
  action deleteList = all {
    match dialog {
      | DeleteConfirm(d) => all {
          canDelete(lists, d),
          usages(lists, uses, d.list) == Set(),
          lists' = lists.keys().exclude(Set(d.list)).mapBy(k => lists.get(k)),
        }
      | _ => all { false, lists' = lists }
    },
    uses' = uses,
    dialog' = Closed,
  }

  action step = any {
    openCreate,
    cancel,
    nondet id = oneOf(LIST_IDS)
    nondet name = oneOf(NAMES)
    nondet kind = oneOf(Set(Dimension, Transaction))
    createList(id, name, kind),
    nondet id = oneOf(LIST_IDS)
    nondet m = oneOf(MEMBER_IDS)
    any { addMember(id, m), removeMember(id, m) },
    nondet id = oneOf(LIST_IDS)
    any { openAddProperty(id), openSettings(id), openDelete(id) },
    nondet p = oneOf(PROP_IDS)
    nondet name = oneOf(NAMES)
    nondet tl = oneOf(LIST_IDS)
    nondet t = oneOf(Set(Text, Number, Boolean, Dim(tl)))
    addProperty(p, name, t),
    nondet name = oneOf(NAMES)
    rename(name),
    nondet u = oneOf(USER_IDS)
    nondet id = oneOf(LIST_IDS)
    any { use(u, id), unuse(u, id) },
    nondet text = oneOf(NAMES)
    typeName(text),
    deleteList,
  }

  // --- Tests: the flows of the design ---

  // Flow 1 to 9: create, add a member and a property, then delete the unused list.
  run createThenDeleteTest =
    init
      .then(openCreate)
      .then(createList("L1", "製品", Dimension))
      .then(addMember("L1", "m1"))
      .then(openDelete("L1"))
      .expect(dialog == DeleteConfirm({ list: "L1", typed: "" }))
      .then(typeName("部門"))
      .expect(not(canDelete(lists, { list: "L1", typed: "部門" })))
      .then(typeName("製品"))
      .then(deleteList)
      .expect(lists == Map())

  // Flow 7: a list in use opens the blocked dialog, and the delete is not possible.
  run blockedTest =
    init
      .then(openCreate)
      .then(createList("L1", "部門", Dimension))
      .then(use("売上高", "L1"))
      .then(openDelete("L1"))
      .expect(dialog == DeleteBlocked("L1"))
      .expect(usages(lists, uses, "L1") == Set("売上高"))
      .then(deleteList.fail())

  // A Dim property of another list is a usage too.
  run propertyTargetBlocksTest =
    init
      .then(openCreate)
      .then(createList("L1", "部門", Dimension))
      .then(openCreate)
      .then(createList("L2", "製品", Dimension))
      .then(openAddProperty("L2"))
      .then(addProperty("p1", "責任部門", Dim("L1")))
      .then(openDelete("L1"))
      .expect(dialog == DeleteBlocked("L1"))
      .expect(usages(lists, uses, "L1") == Set("製品"))

  // A duplicate name and a blank name are refused.
  run uniqueNameTest =
    init
      .then(openCreate)
      .then(createList("L1", "部門", Dimension))
      .then(openCreate)
      .expect(not(validName(lists, "", "L2")))
      .then(createList("L2", "部門", Dimension).fail())
}
