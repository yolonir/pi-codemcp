import { existsSync } from "node:fs";
import { readJsonObject, requireJsonObject, writeJsonObjectAtomically } from "./json-file.js";

export interface CodeMcpSettings {
  backgroundWarmup: boolean;
  cacheTtlHours: number;
  executionTimeoutSeconds: number;
  toolTimeoutSeconds: number;
  maxCalls: number;
  resultLimitKiB: number;
  outputLimitKiB: number;
  disabledTools: Record<string, string[]>;
}

export type EditableSettingKey = Exclude<keyof CodeMcpSettings, "disabledTools">;
export type EditableSettingValue = boolean | number;

export const DEFAULT_CODEMCP_SETTINGS: Readonly<CodeMcpSettings> = {
  backgroundWarmup: true,
  cacheTtlHours: 24,
  executionTimeoutSeconds: 30,
  toolTimeoutSeconds: 30,
  maxCalls: 50,
  resultLimitKiB: 16,
  outputLimitKiB: 50,
  disabledTools: {},
};

const ALLOWED_KEYS = new Set([
  "version",
  "backgroundWarmup",
  "cacheTtlHours",
  "executionTimeoutSeconds",
  "toolTimeoutSeconds",
  "maxCalls",
  "resultLimitKiB",
  "outputLimitKiB",
  "disabledTools",
]);

export function loadCodeMcpSettings(path: string): CodeMcpSettings {
  if (!existsSync(path)) return cloneDefaults();
  const root = readJsonObject(path, "CodeMCP settings");
  const version = root.version ?? 1;
  if (version !== 1 && version !== 2) {
    throw new Error(`Unsupported CodeMCP settings version: ${String(version)}`);
  }
  const migrated =
    version === 1
      ? Object.fromEntries(Object.entries(root).filter(([key]) => key !== "outputLineLimit"))
      : root;
  const unknown = Object.keys(migrated).filter((key) => !ALLOWED_KEYS.has(key));
  if (unknown.length > 0) {
    throw new Error(`Unknown CodeMCP settings: ${unknown.join(", ")}`);
  }

  return {
    backgroundWarmup: booleanSetting(migrated, "backgroundWarmup"),
    cacheTtlHours: integerSetting(migrated, "cacheTtlHours", 0, 720),
    executionTimeoutSeconds: integerSetting(migrated, "executionTimeoutSeconds", 1, 300),
    toolTimeoutSeconds: integerSetting(migrated, "toolTimeoutSeconds", 1, 300),
    maxCalls: integerSetting(migrated, "maxCalls", 1, 200),
    resultLimitKiB: integerSetting(migrated, "resultLimitKiB", 1, 1_024),
    outputLimitKiB: integerSetting(migrated, "outputLimitKiB", 1, 1_024),
    disabledTools: disabledToolSetting(migrated.disabledTools),
  };
}

export function saveCodeMcpSettings(path: string, settings: CodeMcpSettings): void {
  writeJsonObjectAtomically(path, {
    version: 2,
    backgroundWarmup: settings.backgroundWarmup,
    cacheTtlHours: settings.cacheTtlHours,
    executionTimeoutSeconds: settings.executionTimeoutSeconds,
    toolTimeoutSeconds: settings.toolTimeoutSeconds,
    maxCalls: settings.maxCalls,
    resultLimitKiB: settings.resultLimitKiB,
    outputLimitKiB: settings.outputLimitKiB,
    disabledTools: settings.disabledTools,
  });
}

export function setEditableSetting(
  settings: CodeMcpSettings,
  key: EditableSettingKey,
  value: EditableSettingValue,
): CodeMcpSettings {
  if (key === "backgroundWarmup") {
    if (typeof value !== "boolean") throw new TypeError(`${key} must be a boolean`);
    return { ...settings, [key]: value };
  }
  if (typeof value !== "number") throw new TypeError(`${key} must be a number`);
  return { ...settings, [key]: value };
}

export function setToolEnabled(
  settings: CodeMcpSettings,
  server: string,
  tool: string,
  enabled: boolean,
): CodeMcpSettings {
  const disabled = new Set(settings.disabledTools[server] ?? []);
  if (enabled) disabled.delete(tool);
  else disabled.add(tool);
  const disabledTools = { ...settings.disabledTools };
  if (disabled.size === 0) delete disabledTools[server];
  else disabledTools[server] = [...disabled].sort();
  return { ...settings, disabledTools };
}

function cloneDefaults(): CodeMcpSettings {
  return { ...DEFAULT_CODEMCP_SETTINGS, disabledTools: {} };
}

function booleanSetting(root: Record<string, unknown>, key: "backgroundWarmup"): boolean {
  const value = root[key] ?? DEFAULT_CODEMCP_SETTINGS[key];
  if (typeof value !== "boolean") throw new TypeError(`${key} must be a boolean`);
  return value;
}

function integerSetting(
  root: Record<string, unknown>,
  key: Exclude<EditableSettingKey, "backgroundWarmup">,
  minimum: number,
  maximum: number,
): number {
  const value = root[key] ?? DEFAULT_CODEMCP_SETTINGS[key];
  if (typeof value !== "number" || !Number.isInteger(value) || value < minimum || value > maximum) {
    throw new TypeError(`${key} must be an integer from ${minimum} to ${maximum}`);
  }
  return value;
}

function disabledToolSetting(value: unknown): Record<string, string[]> {
  if (value === undefined) return {};
  const root = requireJsonObject(value, "disabledTools");
  return Object.fromEntries(
    Object.entries(root).map(([server, tools]) => {
      if (!Array.isArray(tools) || tools.some((tool) => typeof tool !== "string")) {
        throw new TypeError(`disabledTools.${server} must be an array of tool names`);
      }
      return [server, [...new Set(tools)].sort()];
    }),
  );
}
