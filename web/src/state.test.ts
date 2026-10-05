import { Code, ConnectError } from "@connectrpc/connect";
import { expect, test } from "vite-plus/test";
import { createOrUpdate, runner } from "./state";

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

test("after an ambiguous error, the next action resends the original request", async () => {
  const sent: [string, string][] = [];
  const run = runner(() => {}, { current: null });
  // The api is unavailable for the first 3 sends.
  const send = (data: string) => (clientOpId: string) => {
    sent.push([data, clientOpId]);
    return sent.length > 3
      ? Promise.resolve()
      : Promise.reject(new ConnectError("x", Code.Unavailable));
  };
  expect(await run(send("old"))).toBe(null);
  // The user edits the data and clicks again: the original request goes again with the same id.
  expect(await run(send("new"))).toBe(true);
  expect(sent.slice(3)).toEqual([["old", sent[0][1]]]);
  // After it settles, the next action runs its own request with a new id.
  expect(await run(send("new"))).toBe(true);
  expect(sent[4][0]).toBe("new");
  expect(sent[4][1]).not.toBe(sent[0][1]);
});
