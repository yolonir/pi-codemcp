import {
  chmodSync,
  copyFileSync,
  existsSync,
  mkdirSync,
  readFileSync,
  renameSync,
  unlinkSync,
} from "node:fs";
import { createRequire } from "node:module";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { getAgentDir } from "@earendil-works/pi-coding-agent";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StdioClientTransport } from "@modelcontextprotocol/sdk/client/stdio.js";
import { loadCodeMcpSettings } from "./settings.js";

type JsonObject = Record<string, unknown>;

export type SidecarToolName =
  | "search"
  | "discover"
  | "reload_settings"
  | "apply_manager_changes"
  | "execute"
  | "save_chain"
  | "list_chains"
  | "execute_chain"
  | "revalidate_chain"
  | "delete_chain"
  | "status";

const LONG_RUNNING_TOOLS = new Set<SidecarToolName>([
  "execute",
  "save_chain",
  "execute_chain",
  "revalidate_chain",
]);

export interface SidecarClientOptions {
  packageRoot?: string;
  agentDir?: string;
  environment?: Record<string, string>;
  projectChainsPath?: string;
}

export class SidecarClient {
  private readonly packageRoot: string;
  private readonly packageVersion: string;
  private readonly agentDir: string;
  private readonly environment: Record<string, string>;
  private client: Client | undefined;
  private transport: StdioClientTransport | undefined;
  private startPromise: Promise<void> | undefined;
  private closePromise: Promise<void> | undefined;
  private projectChainsDirectory: string | undefined;
  private stderrTail = "";

  constructor(options: SidecarClientOptions = {}) {
    this.packageRoot = resolve(
      options.packageRoot ?? fileURLToPath(new URL("..", import.meta.url)),
    );
    this.packageVersion = readPackageVersion(this.packageRoot);
    this.agentDir = resolve(options.agentDir ?? getAgentDir());
    this.projectChainsDirectory = options.projectChainsPath
      ? resolve(options.projectChainsPath)
      : undefined;
    this.environment = {
      ...definedProcessEnvironment(),
      ...(options.environment ?? {}),
      PI_CODEMCP_AGENT_DIR: this.agentDir,
      ...(this.projectChainsDirectory === undefined
        ? {}
        : { PI_CODEMCP_PROJECT_CHAINS_DIR: this.projectChainsDirectory }),
      UV_PROJECT_ENVIRONMENT: join(this.agentDir, "pi-codemcp", "runtime", "venv"),
    };
    if (this.projectChainsDirectory === undefined) {
      delete this.environment.PI_CODEMCP_PROJECT_CHAINS_DIR;
    }
  }

  get configPath(): string {
    return join(this.agentDir, "mcp.json");
  }

  get settingsPath(): string {
    return join(this.agentDir, "pi-codemcp", "settings.json");
  }

  get chainsPath(): string {
    return join(this.agentDir, "pi-codemcp", "chains");
  }

  get projectChainsPath(): string | undefined {
    return this.projectChainsDirectory;
  }

  configureProjectChains(path: string | undefined): void {
    const resolved = path === undefined ? undefined : resolve(path);
    if (resolved === this.projectChainsDirectory) return;
    if (this.client || this.transport || this.startPromise || this.closePromise) {
      throw new Error("Cannot change CodeMCP project chain scope after the sidecar has started");
    }
    this.projectChainsDirectory = resolved;
    if (resolved === undefined) delete this.environment.PI_CODEMCP_PROJECT_CHAINS_DIR;
    else this.environment.PI_CODEMCP_PROJECT_CHAINS_DIR = resolved;
  }

  get pid(): number | null {
    return this.transport?.pid ?? null;
  }

  get connected(): boolean {
    return this.client !== undefined && this.transport?.pid !== null;
  }

  async call(name: SidecarToolName, args: JsonObject, signal?: AbortSignal): Promise<JsonObject> {
    await this.ensureStarted(signal);
    const client = this.client;
    if (!client) throw new Error("Sidecar client failed to initialize");
    const timeout = LONG_RUNNING_TOOLS.has(name)
      ? loadCodeMcpSettings(this.settingsPath).executionTimeoutSeconds * 1_000 + 5_000
      : 30_000;
    const result = await client.callTool({ name, arguments: args }, undefined, {
      timeout,
      ...(signal === undefined ? {} : { signal }),
    });

    if (result.isError) {
      throw new Error(textContent(result.content) || `Sidecar tool ${name} failed`);
    }
    if (isJsonObject(result.structuredContent)) {
      return result.structuredContent;
    }

    const text = textContent(result.content);
    if (!text) {
      throw new Error(`Sidecar tool ${name} returned no structured result`);
    }
    const parsed: unknown = JSON.parse(text);
    if (!isJsonObject(parsed)) {
      throw new Error(`Sidecar tool ${name} returned a non-object result`);
    }
    return parsed;
  }

  async close(): Promise<void> {
    if (this.closePromise) return this.closePromise;
    this.closePromise = this.closeInternal();
    try {
      await this.closePromise;
    } finally {
      this.closePromise = undefined;
    }
  }

  private async ensureStarted(signal?: AbortSignal): Promise<void> {
    if (this.client && this.transport && !this.startPromise) return;
    if (!this.startPromise) {
      this.startPromise = this.start(signal).catch(async (error: unknown) => {
        await this.closeInternal();
        const detail = this.stderrTail.trim();
        const message = error instanceof Error ? error.message : String(error);
        throw new Error(detail ? `${message}\n${detail}` : message, { cause: error });
      });
    }

    const startPromise = this.startPromise;
    try {
      await startPromise;
    } finally {
      if (this.startPromise === startPromise) this.startPromise = undefined;
    }
  }

  private async start(signal?: AbortSignal): Promise<void> {
    this.stderrTail = "";
    const transport = new StdioClientTransport({
      command: prepareBundledUv(this.packageRoot, this.agentDir),
      args: [
        "run",
        ...(isTruthy(process.env.PI_OFFLINE) ? ["--offline"] : []),
        "--project",
        "sidecar",
        "--frozen",
        "--no-dev",
        "-m",
        "sidecar.gateway",
      ],
      cwd: this.packageRoot,
      env: this.environment,
      stderr: "pipe",
    });
    transport.stderr?.on("data", (chunk: Buffer | string) => {
      this.stderrTail = `${this.stderrTail}${chunk.toString()}`.slice(-8192);
    });

    const client = new Client({
      name: "pi-codemcp",
      version: this.packageVersion,
    });
    this.transport = transport;
    this.client = client;
    await client.connect(transport, {
      timeout: 310_000,
      ...(signal === undefined ? {} : { signal }),
    });
  }

  private async closeInternal(): Promise<void> {
    const client = this.client;
    const transport = this.transport;
    this.client = undefined;
    this.transport = undefined;
    this.startPromise = undefined;

    if (client) {
      try {
        await client.close();
      } finally {
        if (transport) await transport.close();
      }
    } else if (transport) {
      await transport.close();
    }
  }
}

function readPackageVersion(packageRoot: string): string {
  const packageJson = join(packageRoot, "package.json");
  const metadata: unknown = JSON.parse(readFileSync(packageJson, "utf8"));
  if (!isJsonObject(metadata) || typeof metadata.version !== "string") {
    throw new Error(`pi-codemcp package has invalid metadata: ${packageJson}`);
  }
  return metadata.version;
}

function prepareBundledUv(packageRoot: string, agentDir: string): string {
  const packageRequire = createRequire(join(packageRoot, "package.json"));
  const platformPackage = `@manzt/uv-${process.platform}-${process.arch}`;
  let packageJson: string;
  try {
    packageJson = packageRequire.resolve(`${platformPackage}/package.json`);
  } catch (error) {
    throw new Error(
      `Bundled uv is unavailable for ${process.platform}/${process.arch}; reinstall pi-codemcp with optional dependencies enabled`,
      { cause: error },
    );
  }
  const metadata: unknown = JSON.parse(readFileSync(packageJson, "utf8"));
  if (!isJsonObject(metadata) || typeof metadata.version !== "string") {
    throw new Error(`Bundled uv package has invalid metadata: ${packageJson}`);
  }

  const binaryName = process.platform === "win32" ? "uv.exe" : "uv";
  const source = join(dirname(packageJson), "bin", binaryName);
  if (!existsSync(source)) {
    throw new Error(`Bundled uv executable is missing: ${source}`);
  }

  const runtimeDirectory = join(agentDir, "pi-codemcp", "runtime", "uv", metadata.version);
  const executable = join(runtimeDirectory, binaryName);
  if (existsSync(executable)) return executable;

  mkdirSync(runtimeDirectory, { recursive: true });
  const temporary = `${executable}.${process.pid}.tmp`;
  copyFileSync(source, temporary);
  if (process.platform !== "win32") chmodSync(temporary, 0o755);
  try {
    renameSync(temporary, executable);
  } catch (error) {
    if (!existsSync(executable)) throw error;
    unlinkSync(temporary);
  }
  return executable;
}

function definedProcessEnvironment(): Record<string, string> {
  return Object.fromEntries(
    Object.entries(process.env).filter(
      (entry): entry is [string, string] => entry[1] !== undefined,
    ),
  );
}

function isTruthy(value: string | undefined): boolean {
  return value !== undefined && ["1", "true", "yes"].includes(value.toLowerCase());
}

function isJsonObject(value: unknown): value is JsonObject {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function textContent(content: unknown): string {
  if (!Array.isArray(content)) return "";
  return content
    .filter(
      (item): item is { type: "text"; text: string } =>
        isJsonObject(item) && item.type === "text" && typeof item.text === "string",
    )
    .map((item) => item.text)
    .join("\n");
}
