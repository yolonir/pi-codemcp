import { existsSync } from "node:fs";
import { readJsonObject, requireJsonObject, writeJsonObjectAtomically } from "./json-file.js";

export interface CodeMcpProfile {
  servers: string[];
  disabledTools: Record<string, string[]>;
}

export interface CodeMcpSettings {
  jevEnabled: boolean;
  backgroundWarmup: boolean;
  cacheTtlHours: number;
  executionTimeoutSeconds: number;
  toolTimeoutSeconds: number;
  maxCalls: number;
  resultLimitKiB: number;
  outputLimitKiB: number;
  disabledTools: Record<string, string[]>;
  profiles: Record<string, CodeMcpProfile>;
  activeProfile: string | null;
}

export type EditableSettingKey = Exclude<
  keyof CodeMcpSettings,
  "disabledTools" | "profiles" | "activeProfile"
>;
export type EditableSettingValue = boolean | number;

export const DEFAULT_CODEMCP_SETTINGS: Readonly<CodeMcpSettings> = {
  jevEnabled: false,
  backgroundWarmup: true,
  cacheTtlHours: 24,
  executionTimeoutSeconds: 30,
  toolTimeoutSeconds: 30,
  maxCalls: 50,
  resultLimitKiB: 16,
  outputLimitKiB: 50,
  disabledTools: {},
  profiles: {},
  activeProfile: null,
};

const ALLOWED_KEYS = new Set([
  "version",
  "jevEnabled",
  "backgroundWarmup",
  "cacheTtlHours",
  "executionTimeoutSeconds",
  "toolTimeoutSeconds",
  "maxCalls",
  "resultLimitKiB",
  "outputLimitKiB",
  "disabledTools",
  "profiles",
  "activeProfile",
]);

export function loadCodeMcpSettings(path: string): CodeMcpSettings {
  if (!existsSync(path)) return cloneDefaults();
  const root = readJsonObject(path, "CodeMCP settings");
  const version = root.version ?? 1;
  if (version !== 1 && version !== 2) {
    throw new Error(`Unsupported CodeMCP settings version: ${String(version)}`);
  }
  const versionMigrated =
    version === 1
      ? Object.fromEntries(Object.entries(root).filter(([key]) => key !== "outputLineLimit"))
      : root;
  const migrated = migrateDiscoveryMode(versionMigrated);
  const unknown = Object.keys(migrated).filter((key) => !ALLOWED_KEYS.has(key));
  if (unknown.length > 0) {
    throw new Error(`Unknown CodeMCP settings: ${unknown.join(", ")}`);
  }

  const profiles = profileSetting(migrated.profiles);
  const activeProfile = migrated.activeProfile ?? null;
  if (activeProfile !== null) {
    requireProfileName(activeProfile);
    if (!Object.hasOwn(profiles, activeProfile)) {
      throw new Error(`Unknown CodeMCP profile: ${activeProfile}`);
    }
  }

  return {
    profiles,
    activeProfile,
    jevEnabled: booleanSetting(migrated, "jevEnabled"),
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
    jevEnabled: settings.jevEnabled,
    backgroundWarmup: settings.backgroundWarmup,
    cacheTtlHours: settings.cacheTtlHours,
    executionTimeoutSeconds: settings.executionTimeoutSeconds,
    toolTimeoutSeconds: settings.toolTimeoutSeconds,
    maxCalls: settings.maxCalls,
    resultLimitKiB: settings.resultLimitKiB,
    outputLimitKiB: settings.outputLimitKiB,
    disabledTools: settings.disabledTools,
    profiles: settings.profiles,
    activeProfile: settings.activeProfile,
  });
}

export function setEditableSetting(
  settings: CodeMcpSettings,
  key: EditableSettingKey,
  value: EditableSettingValue,
): CodeMcpSettings {
  if (key === "jevEnabled" || key === "backgroundWarmup") {
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
  const policy = activeToolPolicy(settings);
  const disabled = new Set(policy[server] ?? []);
  if (enabled) disabled.delete(tool);
  else disabled.add(tool);
  const disabledTools = { ...policy };
  if (disabled.size === 0) delete disabledTools[server];
  else disabledTools[server] = [...disabled].sort();
  if (settings.activeProfile !== null) {
    const profile = settings.profiles[settings.activeProfile];
    if (!profile) throw new Error(`Unknown CodeMCP profile: ${settings.activeProfile}`);
    return {
      ...settings,
      profiles: { ...settings.profiles, [settings.activeProfile]: { ...profile, disabledTools } },
    };
  }
  return { ...settings, disabledTools };
}

export function activeToolPolicy(settings: CodeMcpSettings): Record<string, string[]> {
  if (settings.activeProfile === null) return settings.disabledTools;
  const profile = settings.profiles[settings.activeProfile];
  if (!profile) throw new Error(`Unknown CodeMCP profile: ${settings.activeProfile}`);
  return profile.disabledTools;
}

export function saveProfile(
  settings: CodeMcpSettings,
  name: string,
  servers: Array<{ name: string; enabled: boolean }>,
): CodeMcpSettings {
  requireProfileName(name);
  if (Object.hasOwn(settings.profiles, name)) {
    throw new Error(`CodeMCP profile already exists: ${name}`);
  }
  const profile: CodeMcpProfile = {
    servers: [
      ...new Set(servers.filter((server) => server.enabled).map((server) => server.name)),
    ].sort(),
    disabledTools: disabledToolSetting(activeToolPolicy(settings)),
  };
  return { ...settings, profiles: { ...settings.profiles, [name]: profile }, activeProfile: name };
}

export function activateProfile(settings: CodeMcpSettings, name: string | null): CodeMcpSettings {
  if (name !== null && !Object.hasOwn(settings.profiles, name)) {
    throw new Error(`Unknown CodeMCP profile: ${name}`);
  }
  return { ...settings, activeProfile: name };
}

export function deleteProfile(settings: CodeMcpSettings, name: string): CodeMcpSettings {
  if (!Object.hasOwn(settings.profiles, name)) throw new Error(`Unknown CodeMCP profile: ${name}`);
  const profiles = { ...settings.profiles };
  delete profiles[name];
  return {
    ...settings,
    profiles,
    activeProfile: settings.activeProfile === name ? null : settings.activeProfile,
  };
}

export function setProfileServerEnabled(
  settings: CodeMcpSettings,
  server: string,
  enabled: boolean,
): CodeMcpSettings {
  const name = settings.activeProfile;
  if (name === null) throw new Error("No active CodeMCP profile");
  const profile = settings.profiles[name];
  if (!profile) throw new Error(`Unknown CodeMCP profile: ${name}`);
  const servers = new Set(profile.servers);
  if (enabled) servers.add(server);
  else servers.delete(server);
  return {
    ...settings,
    profiles: { ...settings.profiles, [name]: { ...profile, servers: [...servers].sort() } },
  };
}

function cloneDefaults(): CodeMcpSettings {
  return { ...DEFAULT_CODEMCP_SETTINGS, disabledTools: {}, profiles: {} };
}

function requireProfileName(value: unknown): asserts value is string {
  if (
    typeof value !== "string" ||
    !value.trim() ||
    value !== value.trim() ||
    value.length > 80 ||
    Array.from(value).some((char) => char.charCodeAt(0) < 32 || char.charCodeAt(0) === 127)
  ) {
    throw new TypeError(
      "Profile names must be 1–80 characters without surrounding whitespace or control characters",
    );
  }
}

function profileSetting(value: unknown): Record<string, CodeMcpProfile> {
  if (value === undefined) return {};
  const root = requireJsonObject(value, "profiles");
  return Object.fromEntries(
    Object.entries(root).map(([name, value]) => {
      requireProfileName(name);
      const profile = requireJsonObject(value, `profiles.${name}`);
      if (Object.keys(profile).some((key) => key !== "servers" && key !== "disabledTools")) {
        throw new Error(`Unknown CodeMCP profile settings: ${name}`);
      }
      if (
        !Array.isArray(profile.servers) ||
        profile.servers.some((server) => typeof server !== "string" || !server)
      ) {
        throw new TypeError(`profiles.${name}.servers must be an array of server names`);
      }
      return [
        name,
        {
          servers: [...new Set(profile.servers)].sort(),
          disabledTools: disabledToolSetting(profile.disabledTools),
        },
      ];
    }),
  );
}

function migrateDiscoveryMode(root: Record<string, unknown>): Record<string, unknown> {
  if (root.discoveryMode === undefined) return root;
  if (root.discoveryMode !== "search" && root.discoveryMode !== "jev") {
    throw new TypeError("discoveryMode must be search or jev");
  }
  return {
    ...Object.fromEntries(Object.entries(root).filter(([key]) => key !== "discoveryMode")),
    jevEnabled: root.jevEnabled ?? root.discoveryMode === "jev",
  };
}

function booleanSetting(
  root: Record<string, unknown>,
  key: "jevEnabled" | "backgroundWarmup",
): boolean {
  const value = root[key] ?? DEFAULT_CODEMCP_SETTINGS[key];
  if (typeof value !== "boolean") throw new TypeError(`${key} must be a boolean`);
  return value;
}

function integerSetting(
  root: Record<string, unknown>,
  key: Exclude<EditableSettingKey, "jevEnabled" | "backgroundWarmup">,
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
