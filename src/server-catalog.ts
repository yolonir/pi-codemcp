import type { JsonRecord } from "./json-file.js";

const REGISTRY_URL = "https://registry.modelcontextprotocol.io/v0.1/servers";
const SUPPORTED_PACKAGE_RUNTIMES: Record<string, string> = {
  npm: "npx",
  pypi: "uvx",
  nuget: "dnx",
  oci: "docker",
};

export interface RegistryServerCandidate {
  id: string;
  name: string;
  title: string;
  description: string;
  source: string;
  config: JsonRecord;
}

type RegistryFetch = (input: string | URL, init?: RequestInit) => Promise<Response>;

export async function searchMcpRegistry(
  query: string,
  fetcher: RegistryFetch = fetch,
): Promise<RegistryServerCandidate[]> {
  const trimmed = query.trim();
  if (!trimmed) throw new Error("Enter a catalog search term");
  const url = new URL(REGISTRY_URL);
  url.searchParams.set("limit", "30");
  url.searchParams.set("version", "latest");
  url.searchParams.set("search", trimmed);
  const response = await fetcher(url, { signal: AbortSignal.timeout(10_000) });
  if (!response.ok) throw new Error(`MCP Registry search failed (${response.status})`);
  const payload: unknown = await response.json();
  if (!isRecord(payload) || !Array.isArray(payload.servers)) {
    throw new Error("MCP Registry returned an invalid response");
  }
  return payload.servers.flatMap((entry, serverIndex) => registryCandidates(entry, serverIndex));
}

function registryCandidates(value: unknown, serverIndex: number): RegistryServerCandidate[] {
  if (!isRecord(value) || !isRecord(value.server)) return [];
  const server = value.server;
  if (typeof server.name !== "string" || typeof server.description !== "string") return [];
  const name = suggestedServerName(server.name);
  const title = typeof server.title === "string" ? server.title : server.name;
  const base = {
    name,
    title,
    description: server.description,
  };
  const candidates: RegistryServerCandidate[] = [];

  if (Array.isArray(server.remotes)) {
    for (const [index, remote] of server.remotes.entries()) {
      if (!isRecord(remote) || !["http", "streamable-http"].includes(String(remote.type))) {
        continue;
      }
      if (typeof remote.url !== "string") continue;
      const headers = keyValueRecord(remote.headers, "HEADER");
      let hostname: string;
      try {
        hostname = new URL(replaceVariables(remote.url)).hostname;
      } catch {
        hostname = remote.url;
      }
      candidates.push({
        ...base,
        id: `${serverIndex}:remote:${index}`,
        source: `HTTP ${hostname}`,
        config: compactRecord({
          url: replaceVariables(remote.url),
          ...(Object.keys(headers).length > 0 ? { headers } : {}),
          description: server.description,
        }),
      });
    }
  }

  if (Array.isArray(server.packages)) {
    for (const [index, item] of server.packages.entries()) {
      const mapped = packageConfig(item, server.description);
      if (!mapped) continue;
      candidates.push({
        ...base,
        id: `${serverIndex}:package:${index}`,
        source: mapped.source,
        config: mapped.config,
      });
    }
  }
  return candidates;
}

function packageConfig(
  value: unknown,
  description: string,
): { source: string; config: JsonRecord } | undefined {
  if (!isRecord(value) || !isRecord(value.transport) || value.transport.type !== "stdio") {
    return undefined;
  }
  if (typeof value.registryType !== "string" || typeof value.identifier !== "string") {
    return undefined;
  }
  const command =
    typeof value.runtimeHint === "string"
      ? value.runtimeHint
      : SUPPORTED_PACKAGE_RUNTIMES[value.registryType];
  if (!command) return undefined;
  const version = typeof value.version === "string" ? value.version : undefined;
  const runtimeArguments = argumentList(value.runtimeArguments);
  const packageArguments = argumentList(value.packageArguments);
  const specifier = packageSpecifier(value.registryType, value.identifier, version);
  const args =
    command === "docker"
      ? ["run", ...runtimeArguments, specifier, ...packageArguments]
      : command === "npx"
        ? [
            ...(!runtimeArguments.includes("-y") ? ["-y"] : []),
            ...runtimeArguments,
            specifier,
            ...packageArguments,
          ]
        : [...runtimeArguments, specifier, ...packageArguments];
  const env = keyValueRecord(value.environmentVariables, "VALUE");
  return {
    source: `${value.registryType} ${specifier}`,
    config: compactRecord({
      command,
      args,
      ...(Object.keys(env).length > 0 ? { env } : {}),
      description,
    }),
  };
}

function packageSpecifier(
  registryType: string,
  identifier: string,
  version: string | undefined,
): string {
  if (!version || version === "latest") return identifier;
  if (registryType === "pypi") return `${identifier}==${version}`;
  if (registryType === "npm" || registryType === "nuget") return `${identifier}@${version}`;
  return identifier;
}

function argumentList(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  const args: string[] = [];
  for (const item of value) {
    if (!isRecord(item)) continue;
    const fixed = stringValue(item.value) ?? stringValue(item.default);
    const placeholder = inputPlaceholder(item);
    if (item.type === "named" && typeof item.name === "string" && item.name) {
      if (item.format === "boolean") {
        if (fixed === "true" || (fixed === undefined && item.isRequired === true)) {
          args.push(item.name);
        }
      } else if (fixed !== undefined || item.isRequired === true) {
        args.push(item.name, fixed ?? placeholder);
      }
    } else if (fixed !== undefined || item.isRequired === true) {
      args.push(fixed ?? placeholder);
    }
  }
  return args;
}

function keyValueRecord(value: unknown, fallback: string): Record<string, string> {
  if (!Array.isArray(value)) return {};
  return Object.fromEntries(
    value.flatMap((item) => {
      if (!isRecord(item) || typeof item.name !== "string" || !item.name) return [];
      const raw = stringValue(item.value) ?? stringValue(item.default);
      return [
        [
          item.name,
          raw === undefined ? `\${${environmentName(item.name, fallback)}}` : replaceVariables(raw),
        ],
      ];
    }),
  );
}

function inputPlaceholder(value: Record<string, unknown>): string {
  const hint =
    stringValue(value.valueHint) ?? stringValue(value.placeholder) ?? stringValue(value.name);
  return `<${hint?.replace(/^--?/, "") || "value"}>`;
}

function replaceVariables(value: string): string {
  return value.replace(/\{([A-Za-z0-9_-]+)\}/g, (_match, name: string) => {
    return `\${${environmentName(name, "VALUE")}}`;
  });
}

function environmentName(value: string, fallback: string): string {
  return (
    value
      .replace(/^--?/, "")
      .replace(/[^A-Za-z0-9]+/g, "_")
      .replace(/^_+|_+$/g, "")
      .toUpperCase() || fallback
  );
}

function suggestedServerName(registryName: string): string {
  const tail = registryName.split("/").at(-1) ?? registryName;
  return tail.replace(/[^A-Za-z0-9_-]+/g, "-").replace(/^-+|-+$/g, "") || "server";
}

function compactRecord(value: JsonRecord): JsonRecord {
  return Object.fromEntries(Object.entries(value).filter((entry) => entry[1] !== undefined));
}

function stringValue(value: unknown): string | undefined {
  return typeof value === "string" && value ? value : undefined;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
