import { join } from "node:path";
import {
  CONFIG_DIR_NAME,
  type ExtensionAPI,
  type ExtensionCommandContext,
} from "@earendil-works/pi-coding-agent";
import { newCodeMcpTraceId, SavedChainManager } from "../src/chains.js";
import { setMcpServerEnabled } from "../src/config.js";
import { summarizeError } from "../src/errors.js";
import { CodeMcpLifecycle } from "../src/lifecycle.js";
import type { SidecarClientOptions } from "../src/mcp-client.js";
import {
  chainStatesFromViews,
  type ServerModalState,
  serverStatesFromStatus,
  showServerManagerModal,
  statsStateFromSnapshot,
} from "../src/modal.js";
import {
  type CodeMcpSettings,
  saveCodeMcpSettings,
  setEditableSetting,
  setToolEnabled,
} from "../src/settings.js";
import { registerCodeMcpTools } from "../src/tools.js";

export function createCodeMcpExtension(options: SidecarClientOptions = {}) {
  return function codeMcpExtension(pi: ExtensionAPI): void {
    const lifecycle = new CodeMcpLifecycle(options);
    const chains = new SavedChainManager(pi, lifecycle);
    registerCodeMcpTools(pi, lifecycle, chains);

    pi.registerCommand("codemcp", {
      description: "Manage CodeMCP servers, saved chains, tools, and settings",
      handler: async (_args, ctx) => {
        try {
          bindProjectChainScope(ctx, lifecycle, chains);
          const [status, savedChains, settings, stats] = await Promise.all([
            lifecycle.request("status", {}),
            chains.list(newCodeMcpTraceId("manager")),
            Promise.resolve(lifecycle.loadSettings()),
            lifecycle.request("stats", {}),
          ]);
          const servers = serverStatesFromStatus(status);
          if (ctx.mode !== "tui") {
            if (ctx.hasUI) ctx.ui.notify(formatStatusSummary(servers), "info");
            return;
          }

          const managerResult = await showServerManagerModal(ctx, {
            servers,
            chains: chainStatesFromViews(savedChains),
            settings,
            stats: statsStateFromSnapshot(stats),
            onSetServerEnabled: (server, enabled) =>
              setServerEnabledFromManager(lifecycle, server, enabled),
            onDiscover: (server) => discoverServerFromManager(lifecycle, server),
            onSetToolEnabled: async (server, tool, enabled) => {
              const updated = setToolEnabled(
                lifecycle.loadSettings(),
                server.name,
                tool.name,
                enabled,
              );
              saveCodeMcpSettings(lifecycle.settingsPath, updated);
              return requireServerStatus(
                await lifecycle.request("reload_settings", {}),
                server.name,
              );
            },
            onSetSetting: async (key, value) => {
              const updated = setEditableSetting(lifecycle.loadSettings(), key, value);
              saveCodeMcpSettings(lifecycle.settingsPath, updated);
              await lifecycle.request("reload_settings", {});
              return updated;
            },
            onSetChainEnabled: async (chain, enabled) => {
              const traceId = newCodeMcpTraceId("manager");
              await chains.setEnabled(chain.name, chain.scope, enabled, traceId);
              return chainStatesFromViews(await chains.list(traceId));
            },
            onRevalidateChain: async (chain) => {
              const traceId = newCodeMcpTraceId("manager");
              await chains.revalidate(chain.name, chain.scope, traceId);
              return chainStatesFromViews(await chains.list(traceId));
            },
            onDeleteChain: async (chain) => {
              const traceId = newCodeMcpTraceId("manager");
              return chainStatesFromViews(await chains.delete(chain.name, chain.scope, traceId));
            },
          });
          if (managerResult === "report-problem") await promptForProblemReport(pi, ctx);
        } catch (error) {
          ctx.ui.notify(summarizeError(error), "error");
        }
      },
    });

    pi.on("session_start", (_event, ctx) => {
      bindProjectChainScope(ctx, lifecycle, chains);
      chains.activatePersisted();
      for (const error of chains.startupErrors) ctx.ui.notify(error, "warning");
      let settings: CodeMcpSettings;
      try {
        settings = lifecycle.loadSettings();
      } catch (error) {
        ctx.ui.notify(`CodeMCP settings failed: ${summarizeError(error)}`, "warning");
        return;
      }
      if (!settings.backgroundWarmup) return;
      void lifecycle.warmup().catch((error: unknown) => {
        ctx.ui.notify(`CodeMCP background warmup failed: ${summarizeError(error)}`, "warning");
      });
    });

    pi.on("session_shutdown", async () => {
      await lifecycle.shutdown();
    });
  };
}

export async function discoverServerFromManager(
  lifecycle: Pick<CodeMcpLifecycle, "configPath" | "reload" | "request">,
  server: ServerModalState,
): Promise<ServerModalState> {
  if (!server.enabled) return setServerEnabledFromManager(lifecycle, server, true);
  return requireServerStatus(
    await lifecycle.request("discover", { server: server.name }),
    server.name,
  );
}

export async function setServerEnabledFromManager(
  lifecycle: Pick<CodeMcpLifecycle, "configPath" | "reload" | "request">,
  previous: ServerModalState,
  enabled: boolean,
): Promise<ServerModalState> {
  setMcpServerEnabled(lifecycle.configPath, previous.name, enabled);
  await lifecycle.reload();
  try {
    const status = enabled
      ? await lifecycle.request("discover", { server: previous.name })
      : await lifecycle.request("status", {});
    return requireServerStatus(status, previous.name);
  } catch (error) {
    try {
      const current = requireServerStatus(await lifecycle.request("status", {}), previous.name);
      return { ...current, error: summarizeError(error) };
    } catch {
      throw error;
    }
  }
}

export default createCodeMcpExtension();

export async function promptForProblemReport(
  pi: Pick<ExtensionAPI, "sendUserMessage">,
  ctx: Pick<ExtensionCommandContext, "ui">,
): Promise<void> {
  const description = await ctx.ui.editor("What went wrong?", "");
  if (description?.trim()) pi.sendUserMessage(formatProblemReportPrompt(description.trim()));
}

export function formatProblemReportPrompt(description: string): string {
  return `Something went wrong with pi-codemcp.

User's description:
${description}

Investigate the problem in current pi setup. Inspect the available pi-codemcp configuration, environment, and installed package as needed. Determine the likely cause, then prepare a GitHub issue for https://github.com/yolonir/pi-codemcp. Do not include any personal or sensitive information in the issue. Do not autosumbit issue without clear approval.`;
}

function requireServerStatus(
  status: Record<string, unknown>,
  serverName: string,
): ServerModalState {
  const server = serverStatesFromStatus(status).find((candidate) => candidate.name === serverName);
  if (!server) throw new Error(`CodeMCP returned no status for ${serverName}`);
  return server;
}

function bindProjectChainScope(
  ctx: Pick<ExtensionCommandContext, "cwd" | "isProjectTrusted">,
  lifecycle: CodeMcpLifecycle,
  chains: SavedChainManager,
): void {
  const projectChainsPath = ctx.isProjectTrusted()
    ? join(ctx.cwd, CONFIG_DIR_NAME, "pi-codemcp", "chains")
    : undefined;
  lifecycle.configureProjectChains(projectChainsPath);
  chains.configureProject(projectChainsPath);
}

function formatStatusSummary(servers: ServerModalState[]): string {
  const enabled = servers.filter((server) => server.enabled).length;
  const tools = servers.reduce((total, server) => total + server.toolCount, 0);
  return `CodeMCP: ${enabled}/${servers.length} servers · ${tools} enabled tools`;
}
