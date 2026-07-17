import { join } from "node:path";
import {
  CONFIG_DIR_NAME,
  type ExtensionAPI,
  type ExtensionCommandContext,
} from "@earendil-works/pi-coding-agent";
import { SavedChainManager } from "../src/chains.js";
import { setMcpServerEnabled } from "../src/config.js";
import { summarizeError } from "../src/errors.js";
import { CodeMcpLifecycle } from "../src/lifecycle.js";
import type { SidecarClientOptions } from "../src/mcp-client.js";
import {
  chainStatesFromViews,
  type ServerModalState,
  serverStatesFromStatus,
  showServerManagerModal,
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
          const [status, savedChains, settings] = await Promise.all([
            lifecycle.request("status", {}),
            chains.list(),
            Promise.resolve(lifecycle.loadSettings()),
          ]);
          const servers = serverStatesFromStatus(status);
          if (ctx.mode !== "tui") {
            if (ctx.hasUI) ctx.ui.notify(formatStatusSummary(servers), "info");
            return;
          }

          await showServerManagerModal(ctx, {
            servers,
            chains: chainStatesFromViews(savedChains),
            settings,
            onSetServerEnabled: (server, enabled) =>
              setServerEnabledFromManager(lifecycle, server, enabled),
            onDiscover: async (server) =>
              requireServerStatus(
                await lifecycle.request("discover", { server: server.name }),
                server.name,
              ),
            onSaveSettings: async (updated) => {
              const previous = lifecycle.loadSettings();
              saveCodeMcpSettings(lifecycle.settingsPath, updated);
              try {
                const status = await lifecycle.request("reload_settings", {});
                return {
                  settings: lifecycle.loadSettings(),
                  servers: serverStatesFromStatus(status),
                };
              } catch (error) {
                saveCodeMcpSettings(lifecycle.settingsPath, previous);
                throw error;
              }
            },
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
            onSetChainEnabled: async (chain, enabled) => {
              await chains.setEnabled(chain.name, chain.scope, enabled);
              return chainStatesFromViews(await chains.list());
            },
            onRevalidateChain: async (chain) => {
              await chains.revalidate(chain.name, chain.scope);
              return chainStatesFromViews(await chains.list());
            },
            onDeleteChain: async (chain) =>
              chainStatesFromViews(await chains.delete(chain.name, chain.scope)),
          });
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

export default createCodeMcpExtension();

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
