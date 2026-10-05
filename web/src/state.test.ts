import { Code, ConnectError } from "@connectrpc/connect";
import { expect, test } from "vite-plus/test";
import { createOrUpdate } from "./state";

const opId = "0192f3a4-0000-7000-8000-000000000001";
const fail = (code: Code, msg: string) => () => Promise.reject(new ConnectError(msg, code));

test("createOrUpdate updates an existing id with its own client_op_id", async () => {
  const sent: string[] = [];
  const update = (r: { clientOpId: string }) => (sent.push(r.clientOpId), Promise.resolve());
  await createOrUpdate({ clientOpId: opId }, fail(Code.AlreadyExists, "id"), update);
  await createOrUpdate({ clientOpId: opId }, fail(Code.AlreadyExists, "id"), update);
  expect(sent[0]).not.toBe(opId);
  // A retry of the same action resends the same update.
  expect(sent[1]).toBe(sent[0]);
});

test("createOrUpdate gives the create error when the id does not exist", async () => {
  const run = createOrUpdate(
    { clientOpId: opId },
    fail(Code.AlreadyExists, "Metric A の名前はすでにある"),
    fail(Code.NotFound, "Metric A がない"),
  );
  await expect(run).rejects.toThrow("名前はすでにある");
});
