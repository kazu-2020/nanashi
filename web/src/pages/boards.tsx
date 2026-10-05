import type { MessageInitShape } from "@bufbuild/protobuf";
import { Button, Input } from "@heroui/react";
import { useState } from "react";
import { api, newId } from "../api";
import { type BoardDef, ItemType, type WidgetSchema, Role } from "../gen/nanashi/v1/plan_pb";
import { label, toFilters } from "../logic";
import { PivotWidget } from "../Pivot";
import { createOrUpdate, listOptions, memberOptions, useApp, useMutate } from "../state";
import { Sel } from "../ui";

export function BoardsPage() {
  const { appId, model, names, can } = useApp();
  const mutate = useMutate();
  const [sel, setSel] = useState(model.boards[0]?.id ?? "");
  const [name, setName] = useState("");
  const [draftId, setDraftId] = useState(newId);
  const [page, setPage] = useState<Record<string, string>>({});
  const [text, setText] = useState("");
  const b = model.boards.find((x) => x.id === sel);
  // The page selector members of one board must not filter the next board.
  const choose = (id: string) => {
    setSel(id);
    setPage({});
  };
  const boardPage = Object.fromEntries((b?.pageSelectors ?? []).map((d) => [d, page[d] ?? ""]));
  const modeler = can(Role.MODELER);
  const save = (
    board: BoardDef,
    patch: { widgets?: MessageInitShape<typeof WidgetSchema>[]; pageSelectors?: string[] },
  ) =>
    mutate((clientOpId) =>
      api.updateBoard({
        appId,
        clientOpId,
        board: {
          id: board.id,
          name: board.name,
          widgets: board.widgets,
          pageSelectors: board.pageSelectors,
          ...patch,
        },
      }),
    );
  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap gap-2">
        <Sel
          label="ボード"
          value={sel}
          onChange={choose}
          empty=""
          options={model.boards.map((x): [string, string] => [x.id, x.name])}
        />
        {modeler && (
          <>
            <Input
              aria-label="ボード名"
              placeholder="ボード名"
              value={name}
              onChange={(e) => setName(e.target.value)}
            />
            <Button
              size="sm"
              onPress={() =>
                mutate(async (clientOpId) => {
                  const req = { appId, clientOpId, board: { id: draftId, name } };
                  await createOrUpdate(
                    req,
                    (r) => api.createBoard(r),
                    (r) => api.updateBoard(r),
                  );
                  choose(draftId);
                }).then((ok) => ok !== null && setDraftId(newId()))
              }
            >
              ボードを作成
            </Button>
          </>
        )}
      </div>
      {b && (
        <>
          <div className="flex flex-wrap items-center gap-2 rounded border p-2">
            {b.pageSelectors.map((d) => (
              <Sel
                key={d}
                label={label(names, d)}
                value={page[d] ?? ""}
                onChange={(m) => setPage({ ...page, [d]: m })}
                empty="すべて"
                options={memberOptions(model, d)}
              />
            ))}
            {modeler && (
              <Sel
                label="ページセレクターを追加"
                value=""
                onChange={(d) => d && save(b, { pageSelectors: [...b.pageSelectors, d] })}
                empty=""
                options={listOptions(model).filter(([id]) => !b.pageSelectors.includes(id))}
              />
            )}
          </div>
          {b.widgets.map((w, i) => {
            const v =
              w.content.case === "viewId"
                ? model.views.find((x) => x.id === w.content.value)
                : undefined;
            return (
              // A key from the content stops a widget from showing the grid of a deleted widget while it reads.
              <div key={`${i}:${JSON.stringify(w.content)}`} className="rounded border p-2">
                <div className="flex items-center justify-between">
                  <b>{v?.name ?? ""}</b>
                  {modeler && (
                    <Button
                      size="sm"
                      variant="ghost"
                      onPress={() => save(b, { widgets: b.widgets.filter((_, j) => j !== i) })}
                    >
                      ウィジェットを削除
                    </Button>
                  )}
                </div>
                {w.content.case === "text" && (
                  <p className="whitespace-pre-wrap">{w.content.value}</p>
                )}
                {v && (
                  <PivotWidget spec={{ ...v, filters: toFilters(v.filters) }} page={boardPage} />
                )}
              </div>
            );
          })}
          {modeler && (
            <div className="flex flex-wrap items-center gap-2">
              <Sel
                label="ビューのウィジェットを追加"
                value=""
                onChange={(id) =>
                  id &&
                  save(b, { widgets: [...b.widgets, { content: { case: "viewId", value: id } }] })
                }
                empty=""
                options={model.views.map((x): [string, string] => [x.id, x.name])}
              />
              <Input
                aria-label="テキスト"
                placeholder="テキスト"
                value={text}
                onChange={(e) => setText(e.target.value)}
              />
              <Button
                size="sm"
                onPress={() =>
                  save(b, { widgets: [...b.widgets, { content: { case: "text", value: text } }] })
                }
              >
                テキストを追加
              </Button>
              <Button
                size="sm"
                variant="danger"
                onPress={() =>
                  mutate((clientOpId) =>
                    api.deleteItem({ appId, clientOpId, type: ItemType.BOARD, id: b.id }),
                  )
                }
              >
                ボードを削除
              </Button>
            </div>
          )}
        </>
      )}
    </div>
  );
}
