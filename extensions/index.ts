import { join } from "node:path";
import {
  CONFIG_DIR_NAME,
  type ExtensionAPI,
  type ExtensionCommandContext,
} from "@earendil-works/pi-coding-agent";
import { SavedChainManager } from "../src/chains.js";
import { setMcpServersEnabled } from "../src/config.js";
import { summarizeError } from "../src/errors.js";
import { CodeMcpLifecycle } from "../src/lifecycle.js";
import type { SidecarClientOptions } from "../src/mcp-client.js";
import {
  type ChainModalState,
  chainStatesFromViews,
  type ChainEnabledChange as ModalChainEnabledChange,
  type ServerEnabledChange,
  type ServerModalState,
  serverStatesFromStatus,
  showServerManagerModal,
  statsStateFromSnapshot,
} from "../src/modal.js";
import { type CodeMcpSettings, saveCodeMcpSettings } from "../src/settings.js";
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
            chains.list(),
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
            onDiscover: async (server) =>
              requireServerStatus(
                await lifecycle.request("discover", { server: server.name }),
                server.name,
              ),
            onSaveChanges: (updated, serverChanges, chainChanges) =>
              saveManagerChanges(lifecycle, chains, updated, serverChanges, chainChanges),
            onResolveUnsaved: async () => {
              const choice = await ctx.ui.select("Unsaved CodeMCP changes", [
                "Save",
                "Discard",
                "Cancel",
              ]);
              if (choice === "Save") return "save";
              if (choice === "Discard") return "discard";
              return "cancel";
            },
            onRevalidateChain: async (chain) => {
              await chains.revalidate(chain.name, chain.scope);
              return chainStatesFromViews(await chains.list());
            },
            onDeleteChain: async (chain) =>
              chainStatesFromViews(await chains.delete(chain.name, chain.scope)),
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

export async function saveManagerChanges(
  lifecycle: Pick<CodeMcpLifecycle, "configPath" | "settingsPath" | "loadSettings" | "reload">,
  chains: Pick<SavedChainManager, "applyEnabled">,
  updated: CodeMcpSettings,
  serverChanges: readonly ServerEnabledChange[],
  chainChanges: readonly ModalChainEnabledChange[],
): Promise<{
  settings: CodeMcpSettings;
  servers: ServerModalState[];
  chains: ChainModalState[];
}> {
  const previousSettings = lifecycle.loadSettings();
  let serverConfigChanged = false;
  try {
    saveCodeMcpSettings(lifecycle.settingsPath, updated);
    if (serverChanges.length > 0) {
      setMcpServersEnabled(
        lifecycle.configPath,
        serverChanges.map((change) => ({ name: change.name, enabled: change.enabled })),
      );
      serverConfigChanged = true;
      await lifecycle.reload();
    }
    const applied = await chains.applyEnabled(
      chainChanges.map((change) => ({
        name: change.name,
        scope: change.scope,
        enabled: change.enabled,
      })),
    );
    return {
      settings: lifecycle.loadSettings(),
      servers: serverStatesFromStatus(applied.status),
      chains: chainStatesFromViews(applied.chains),
    };
  } catch (error) {
    saveCodeMcpSettings(lifecycle.settingsPath, previousSettings);
    if (serverConfigChanged) {
      setMcpServersEnabled(
        lifecycle.configPath,
        serverChanges.map((change) => ({
          name: change.name,
          enabled: change.previousEnabled,
        })),
      );
    }
    try {
      await lifecycle.reload();
    } catch (rollbackError) {
      throw new AggregateError(
        [error, rollbackError],
        "CodeMCP save failed and runtime rollback also failed",
      );
    }
    throw error;
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
