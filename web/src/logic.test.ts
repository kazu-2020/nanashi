import { expect, test } from "vite-plus/test";
import { uuidv7 } from "./logic";

test("uuidv7 has the format, the version, the variant and the time order", () => {
  const ones = new Uint8Array(16).fill(0xff);
  const id = uuidv7(0x0192_f3a4_5b6c, ones);
  expect(id).toBe("0192f3a4-5b6c-7fff-bfff-ffffffffffff");
  const zeros = uuidv7(1, new Uint8Array(16));
  expect(zeros).toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/);
  expect(zeros).toBe("00000000-0001-7000-8000-000000000000");
  // A later millisecond sorts after an earlier one, whatever the random bytes are.
  const ms = Date.UTC(2026, 9, 5);
  const ids = [0, 1, 255, 256, 65_536].map((d) =>
    uuidv7(ms + d, d % 2 ? ones : new Uint8Array(16)),
  );
  expect([...ids].sort()).toEqual(ids);
  expect(uuidv7(ms + 1, new Uint8Array(16)) > uuidv7(ms, ones)).toBe(true);
});
