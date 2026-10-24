import { existsSync, unlinkSync } from "node:fs";
import { dirname, join } from "node:path";
import {
  CONFIG_DIR_NAME,
  type ExtensionAPI,
  type ExtensionCommandContext,
} from "@earendil-works/pi-coding-agent";
import { TypeSafeClient } from "@typesafe-ai/sdk";
import { newCodeMcpTraceId, SavedChainManager } from "../src/chains.js";
import {
  addMcpServer,
  parseMcpServerDraft,
  removeMcpServer,
  setMcpServerEnabled,
} from "../src/config.js";
import { summarizeError } from "../src/errors.js";
import { JevRouter } from "../src/jev-router.js";
import { readJsonObject, writeJsonObjectAtomically } from "../src/json-file.js";
import { CodeMcpLifecycle } from "../src/lifecycle.js";
import type { SidecarClientOptions } from "../src/mcp-client.js";
import {
  type AddedServerManagerState,
  chainStatesFromViews,
  type ProfileAction,
  type ProfileManagerState,
  type ServerModalState,
  serverStatesFromStatus,
  showServerManagerModal,
  statsStateFromSnapshot,
} from "../src/modal.js";
import { searchMcpCatalog } from "../src/server-catalog.js";
import {
  activateProfile,
  type CodeMcpSettings,
  deleteProfile,
  saveCodeMcpSettings,
  saveProfile,
  setEditableSetting,
  setProfileServerEnabled,
  setToolEnabled,
} from "../src/settings.js";
import { registerCodeMcpTools, registerJevRouteTool } from "../src/tools.js";

export interface CodeMcpExtensionOptions extends SidecarClientOptions {
  jevClient?: TypeSafeClient;
}

export function createCodeMcpExtension(options: CodeMcpExtensionOptions = {}) {
  return function codeMcpExtension(pi: ExtensionAPI): void {
    const { jevClient, ...sidecarOptions } = options;
    const lifecycle = new CodeMcpLifecycle(sidecarOptions);
    const chains = new SavedChainManager(pi, lifecycle);
    const routerClient = jevClient ?? createJevClient();
    const jevRouter = routerClient ? new JevRouter(lifecycle, routerClient) : undefined;
    registerCodeMcpTools(pi, lifecycle, chains);
    registerJevRouteTool(pi, () => jevRouter);

    pi.registerCommand("codemcp", {
      description: "Manage CodeMCP servers, saved chains, tools, settings, and profiles",
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
            onChangeProfile: (action) => changeProfileFromManager(lifecycle, action),
            onSearchRegistry: (query, signal) => searchMcpCatalog(query, { signal }),
            onAddServer: (config) => addServerFromManager(lifecycle, config),
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
              if (key !== "jevEnabled") await lifecycle.request("reload_settings", {});
              setDiscoveryTools(pi, updated.jevEnabled && jevRouter !== undefined);
              if (updated.jevEnabled && !jevRouter) {
                ctx.ui.notify("Jev mode requires TYPESAFE_API_KEY; using local search", "warning");
              }
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
      try {
        showChangelogOnce(ctx, join(dirname(lifecycle.settingsPath), "changelog.json"));
      } catch (error) {
        ctx.ui.notify(`CodeMCP changelog failed: ${summarizeError(error)}`, "warning");
      }
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
      const useJev = settings.jevEnabled && jevRouter !== undefined;
      setDiscoveryTools(pi, useJev);
      if (settings.jevEnabled && !jevRouter) {
        ctx.ui.notify("Jev mode requires TYPESAFE_API_KEY; using local search", "warning");
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

export async function addServerFromManager(
  lifecycle: Pick<CodeMcpLifecycle, "configPath" | "settingsPath" | "loadSettings" | "request">,
  config: string,
): Promise<AddedServerManagerState> {
  const draft = parseMcpServerDraft(config);
  const settings = lifecycle.loadSettings();
  const configExisted = existsSync(lifecycle.configPath);
  const updatedSettings =
    settings.activeProfile === null
      ? settings
      : setProfileServerEnabled(settings, draft.name, true);
  addMcpServer(lifecycle.configPath, draft);
  try {
    if (updatedSettings !== settings) saveCodeMcpSettings(lifecycle.settingsPath, updatedSettings);
    const servers = serverStatesFromStatus(await lifecycle.request("reload_settings", {}));
    if (!servers.some((server) => server.name === draft.name)) {
      throw new Error(`CodeMCP returned no status for ${draft.name}`);
    }
    return { name: draft.name, servers };
  } catch (error) {
    if (configExisted) removeMcpServer(lifecycle.configPath, draft.name);
    else unlinkSync(lifecycle.configPath);
    if (updatedSettings !== settings) saveCodeMcpSettings(lifecycle.settingsPath, settings);
    try {
      await lifecycle.request("reload_settings", {});
    } catch {
      // Preserve the add failure after best-effort rollback.
    }
    throw error;
  }
}

export async function discoverServerFromManager(
  lifecycle: Pick<CodeMcpLifecycle, "configPath" | "settingsPath" | "loadSettings" | "request">,
  server: ServerModalState,
): Promise<ServerModalState> {
  if (!server.enabled) return setServerEnabledFromManager(lifecycle, server, true);
  return requireServerStatus(
    await lifecycle.request("discover", { server: server.name }),
    server.name,
  );
}

export async function setServerEnabledFromManager(
  lifecycle: Pick<CodeMcpLifecycle, "configPath" | "settingsPath" | "loadSettings" | "request">,
  previous: ServerModalState,
  enabled: boolean,
): Promise<ServerModalState> {
  const settings = lifecycle.loadSettings();
  if (settings.activeProfile === null) {
    setMcpServerEnabled(lifecycle.configPath, previous.name, enabled);
  } else {
    saveCodeMcpSettings(
      lifecycle.settingsPath,
      setProfileServerEnabled(settings, previous.name, enabled),
    );
  }
  const reloaded = await lifecycle.request("reload_settings", {});
  try {
    const status = enabled
      ? await lifecycle.request("discover", { server: previous.name })
      : reloaded;
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

export async function changeProfileFromManager(
  lifecycle: Pick<CodeMcpLifecycle, "settingsPath" | "loadSettings" | "request">,
  action: ProfileAction,
): Promise<ProfileManagerState> {
  const settings = lifecycle.loadSettings();
  if (action.action === "activate" && action.name === settings.activeProfile) {
    return { settings, servers: serverStatesFromStatus(await lifecycle.request("status", {})) };
  }
  const updated =
    action.action === "activate"
      ? activateProfile(settings, action.name)
      : action.action === "delete"
        ? deleteProfile(settings, action.name)
        : saveProfile(
            settings,
            action.name,
            serverStatesFromStatus(await lifecycle.request("status", {})),
          );
  saveCodeMcpSettings(lifecycle.settingsPath, updated);
  return {
    settings: updated,
    servers: serverStatesFromStatus(await lifecycle.request("reload_settings", {})),
  };
}

export default createCodeMcpExtension();

const CHANGELOG_ID = "profiles-native-python-v1";
const CHANGELOG_MESSAGE =
  "Howdy coders! Big pi-codemcp updates:\n\n" +
  "- I'd say the biggest update is Monty runtime upgrade, they now support lot of cool new useful stuff so now sandbox is less castrated. Expect more efficient code generation by agents, faster runtime, less errors. Can't stop winning\n" +
  "- My favorite update is MCP profiles. I have like 20 servers, some of them are for work, some for personal stuff, I found myself constantly toggling specific lists on and off. Now you can create an MCP profile! Very useful, test it out.\n" +
  "- @scytem requested functionality to add MCP servers directly within pi-codemcp from a catalog or pasted config in /codemcp (A). @scytem you can add MCP servers dedup and stars sorting yourself, you are very much welcome!\n" +
  "- @fu5ha had issues with tool search in large catalogs. It was clanky, I agree. Now it's way simpler for agents to navigate large catalogs\n\n" +
  "I am pretty happy with pi-codemcp extension. I believe pi went the wrong way by doing native MCP support, I found it pretty shitty. As always, if there is something that you are not happy with, hit me with an issue (/codemcp -> R). Cheers!\n" +
  "@yolonir";

export function showChangelogOnce(
  ctx: Pick<ExtensionCommandContext, "mode" | "ui">,
  path: string,
): void {
  if (ctx.mode !== "tui") return;
  const state = existsSync(path) ? readJsonObject(path, "CodeMCP changelog state") : {};
  if (state.lastSeen === CHANGELOG_ID) return;
  ctx.ui.notify(CHANGELOG_MESSAGE, "info");
  writeJsonObjectAtomically(path, { lastSeen: CHANGELOG_ID });
}

function createJevClient(): TypeSafeClient | undefined {
  return process.env.TYPESAFE_API_KEY?.trim() ? new TypeSafeClient() : undefined;
}

function setDiscoveryTools(pi: ExtensionAPI, useJev: boolean): void {
  const selected = useJev ? "codemcp_route" : "codemcp_search";
  const current = pi.getActiveTools();
  if (
    current.includes(selected) &&
    !current.includes(useJev ? "codemcp_search" : "codemcp_route")
  ) {
    return;
  }
  const active = current.filter((name) => name !== "codemcp_search" && name !== "codemcp_route");
  pi.setActiveTools([...active, selected]);
}

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
