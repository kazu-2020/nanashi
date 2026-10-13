import { useQuery } from "@connectrpc/connect-query";
import { Table } from "@heroui/react";
import { createFileRoute } from "@tanstack/react-router";
import { createColumnHelper, flexRender, tableFeatures, useTable } from "@tanstack/react-table";
import { errorText } from "../api";
import { type Application, PlanService } from "../gen/nanashi/v1/plan_pb";

// Keep the features, the columns and the empty data at module scope, so that the table keeps its row models.
const features = tableFeatures({});
const column = createColumnHelper<typeof features, Application>();
const columns = column.columns([column.accessor("name", { header: "名前" })]);
const none: Application[] = [];

export const Route = createFileRoute("/")({
  component: function Applications() {
    const { data, error } = useQuery(PlanService.method.listApplications);
    const table = useTable({ features, columns, data: data?.applications ?? none });

    if (error) return <p className="text-danger">{errorText(error)}</p>;
    return (
      <Table>
        <Table.ScrollContainer>
          <Table.Content aria-label="アプリケーション">
            <Table.Header>
              {table.getHeaderGroups()[0]?.headers.map((header) => (
                <Table.Column key={header.id} id={header.id} isRowHeader>
                  {flexRender(header.column.columnDef.header, header.getContext())}
                </Table.Column>
              ))}
            </Table.Header>
            <Table.Body>
              {table.getRowModel().rows.map((row) => (
                <Table.Row key={row.original.id} id={row.original.id}>
                  {row.getAllCells().map((cell) => (
                    <Table.Cell key={cell.id}>
                      {flexRender(cell.column.columnDef.cell, cell.getContext())}
                    </Table.Cell>
                  ))}
                </Table.Row>
              ))}
            </Table.Body>
          </Table.Content>
        </Table.ScrollContainer>
      </Table>
    );
  },
});
