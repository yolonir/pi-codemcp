export function summarizeError(error: unknown): string {
  const lines = (error instanceof Error ? error.message : String(error))
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean);
  const first = lines[0] ?? "Unknown error";
  const last = lines.at(-1);
  return last && last !== first ? `${first} — ${last}` : first;
}
