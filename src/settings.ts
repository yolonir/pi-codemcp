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
  outputLineLimit: number;
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
  outputLineLimit: 2_000,
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
  "outputLineLimit",
  "disabledTools",
]);

export function loadCodeMcpSettings(path: string): CodeMcpSettings {
  if (!existsSync(path)) return cloneDefaults();
  const root = readJsonObject(path, "CodeMCP settings");
  const unknown = Object.keys(root).filter((key) => !ALLOWED_KEYS.has(key));
  if (unknown.length > 0) {
    throw new Error(`Unknown CodeMCP settings: ${unknown.join(", ")}`);
  }
  const version = root.version ?? 1;
  if (version !== 1) throw new Error(`Unsupported CodeMCP settings version: ${String(version)}`);

  return {
    backgroundWarmup: booleanSetting(root, "backgroundWarmup"),
    cacheTtlHours: integerSetting(root, "cacheTtlHours", 0, 720),
    executionTimeoutSeconds: integerSetting(root, "executionTimeoutSeconds", 1, 300),
    toolTimeoutSeconds: integerSetting(root, "toolTimeoutSeconds", 1, 300),
    maxCalls: integerSetting(root, "maxCalls", 1, 200),
    resultLimitKiB: integerSetting(root, "resultLimitKiB", 1, 1_024),
    outputLimitKiB: integerSetting(root, "outputLimitKiB", 1, 1_024),
    outputLineLimit: integerSetting(root, "outputLineLimit", 1, 10_000),
    disabledTools: disabledToolSetting(root.disabledTools),
  };
}

export function saveCodeMcpSettings(path: string, settings: CodeMcpSettings): void {
  writeJsonObjectAtomically(path, {
    version: 1,
    backgroundWarmup: settings.backgroundWarmup,
    cacheTtlHours: settings.cacheTtlHours,
    executionTimeoutSeconds: settings.executionTimeoutSeconds,
    toolTimeoutSeconds: settings.toolTimeoutSeconds,
    maxCalls: settings.maxCalls,
    resultLimitKiB: settings.resultLimitKiB,
    outputLimitKiB: settings.outputLimitKiB,
    outputLineLimit: settings.outputLineLimit,
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
