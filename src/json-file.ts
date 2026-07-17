import {
  existsSync,
  mkdirSync,
  readFileSync,
  renameSync,
  statSync,
  unlinkSync,
  writeFileSync,
} from "node:fs";
import { dirname, join } from "node:path";

export type JsonRecord = Record<string, unknown>;

export function readJsonObject(path: string, label: string): JsonRecord {
  let parsed: unknown;
  try {
    parsed = JSON.parse(readFileSync(path, "utf8"));
  } catch (error) {
    throw new Error(`${label} is not valid JSON`, { cause: error });
  }
  return requireJsonObject(parsed, label);
}

export function requireJsonObject(value: unknown, label: string): JsonRecord {
  if (!isJsonRecord(value)) {
    throw new TypeError(`${label} must be an object`);
  }
  return value;
}

export function writeJsonObjectAtomically(path: string, value: JsonRecord): void {
  const directory = dirname(path);
  mkdirSync(directory, { recursive: true });
  const temporary = join(directory, `.${process.pid}.${Date.now()}.tmp`);
  const mode = existsSync(path) ? statSync(path).mode : 0o600;
  writeFileSync(temporary, `${JSON.stringify(value, null, 2)}\n`, {
    encoding: "utf8",
    mode,
  });
  try {
    renameSync(temporary, path);
  } catch (error) {
    try {
      unlinkSync(temporary);
    } catch {
      // Preserve the original write error.
    }
    throw error;
  }
}

function isJsonRecord(value: unknown): value is JsonRecord {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
