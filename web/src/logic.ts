// Pure calculations. No I/O here.

// uuidv7 makes a UUIDv7 (RFC 9562) from a Unix time in milliseconds and 16 random bytes.
// The time fills the first 48 bits, so an id from a later millisecond sorts after an id from an earlier one.
export function uuidv7(ms: number, random: Uint8Array): string {
  const b = random.slice(0, 16);
  for (let i = 0; i < 6; i++) b[i] = Math.floor(ms / 2 ** (8 * (5 - i))) % 256;
  b[6] = (b[6] & 0x0f) | 0x70; // version 7
  b[8] = (b[8] & 0x3f) | 0x80; // variant 10
  const h = Array.from(b, (x) => x.toString(16).padStart(2, "0")).join("");
  return `${h.slice(0, 8)}-${h.slice(8, 12)}-${h.slice(12, 16)}-${h.slice(16, 20)}-${h.slice(20)}`;
}
