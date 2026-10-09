# The list screen

This note gives the requirements of the list screen in `web/`. The formal specification is `lists.qnt` (Quint). The design is the "リスト" page of the nanashi Web Mock canvas (flows 1 to 9).

A list is a dimension of the engine (`ids.md`). A list has a kind (DIMENSION or TRANSACTION), members in an order, and properties.

## Check the specification

```bash
npx @informalsystems/quint typecheck docs/lists.qnt
npx @informalsystems/quint test --backend=typescript docs/lists.qnt
npx @informalsystems/quint run --backend=typescript docs/lists.qnt --invariant=safety --max-steps=30 --max-samples=5000
```

## Terms

The terms below have the same names in `lists.qnt`.

- `usages(list)`: the objects that use a list. These are the Metrics with the list in `dimensions` or `member_list`, the views with the list in `rows`, `columns` or `filters`, the boards with the list in `page_selectors`, and the other lists with a DIMENSION property that points to the list. The NUMBER and BOOLEAN Metrics `<list>.<property>` of the list itself are part of the list. They are not usages.
- `validName(name)`: the name is not blank, and no other list has the name.
- `deleteDialog(list)`: the dialog that the delete command opens. It is `DeleteBlocked` if `usages(list)` is not empty. Otherwise it is `DeleteConfirm`.
- `canDelete`: the text that the user typed is the same as the list name.

## Invariants

- `uniqueNames`: no two lists have the same name, and no name is blank.
- `noDangling`: each reference points to a list that exists. Thus, a delete of a list that has usages is not possible.
- `dimTargets`: a DIMENSION property points to a DIMENSION list. The target is not the list of the property.
- `dialogConsistent`: an open dialog names a list that exists. `DeleteBlocked` shows a list that has usages.

## Requirements

1. The left pane shows the lists in two groups, "ディメンション" and "トランザクション". Each row shows the mark (the first character), the name and the number of members. A search field filters the rows by name.
2. The detail pane shows the selected list: the name, the kind, and the line "メンバー N · プロパティ N · N か所で使用". If there are no usages, the line shows "使用なし".
3. The detail pane has three tabs: "メンバー", "プロパティ" and "使用先".
4. The member tab shows a table. The first column is the member name. Each property is one column. The header of a column shows the type mark: `Aa` (TEXT), `Dim` (DIMENSION), `#` (NUMBER), `✓/✕` (BOOLEAN). The user edits a cell in place.
5. The last row of the table adds a member. Enter commits the name. Esc cancels it.
6. If a list has no members, the member tab shows "メンバーはまだない" and the button "＋ メンバーを追加".
7. "＋ リストを作成" opens the create dialog. The dialog takes a name and a kind only. The new list is empty. If `validName` is false, the dialog shows the error and the create button is off. After the create, the screen selects the new list and shows the toast "リスト「X」を作成した。".
8. "＋ プロパティ" opens the property dialog. The dialog takes a name, a type, and a target list for the DIMENSION type. The target choices are the other DIMENSION lists. The type does not change after the create.
9. The menu "⋯" of a list has "名前と設定を編集" and "リストを削除".
10. "名前と設定を編集" opens the settings dialog. It changes the name. It shows the kind as read only.
11. "リストを削除" opens `deleteDialog(list)`. The screen never deletes a list without the confirm dialog.
12. The blocked dialog has the title "「X」は削除できない". It shows the number of usages for each type and the list of usages.
13. The confirm dialog has the title "「X」を削除する". It shows the members and the properties that the delete removes. The delete button is on only if `canDelete` is true. After the delete, the screen selects the first list and shows the toast "リスト「X」を削除した。".
14. The engine does the last check. If a formula uses the list by name, the engine refuses the delete, and the screen shows the error of the engine.

## Not in this change

The design shows these items. This change does not make them.

- The description of a list, and the property for the display name.
- The commands "複製", "CSV で書き出し", and the toast command "元に戻す".
- A change of the order, a change of the type, and a delete of a property.
- The default value of a DIMENSION property for a member without a value.
- The date and the user of the last change of a list.
