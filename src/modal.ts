import type { ExtensionCommandContext, Theme } from "@earendil-works/pi-coding-agent";
import {
  Box,
  type Component,
  type Focusable,
  fuzzyFilter,
  Input,
  Key,
  matchesKey,
  Text,
  truncateToWidth,
  visibleWidth,
} from "@earendil-works/pi-tui";
import type { ChainScope, SavedChainView } from "./chains.js";
import { summarizeError } from "./errors.js";
import {
  type CodeMcpSettings,
  type EditableSettingKey,
  type EditableSettingValue,
  setEditableSetting,
  setToolEnabled,
} from "./settings.js";

export interface ToolModalState {
  name: string;
  description?: string;
  enabled: boolean;
  busy?: boolean;
}

export interface ChainModalState {
  name: string;
  scope: ChainScope;
  description: string;
  nativeTool: string;
  code: string;
  enabled: boolean;
  status: "ready" | "disabled" | "stale" | "shadowed";
  inputSchema: Record<string, unknown>;
  outputSchema: Record<string, unknown>;
  dependencies: Array<{
    kind: "mcp_tool" | "saved_chain";
    call: string;
    server: string;
  }>;
  staleDependencies: string[];
  calledBy: string[];
  busy?: boolean;
  error?: string;
}

export interface StatsRollupState {
  count: number;
  success: number;
  failure: number;
  inputBytes: number;
  outputBytes: number;
  calls: number;
  chainCalls: number;
  averageMs: number;
  p50Ms: number;
  p95Ms: number;
  maxMs: number;
  p50OutputBytes: number;
  p95OutputBytes: number;
}

export interface StatsModalState {
  updatedAt: number;
  lifetime: StatsRollupState;
  recent: StatsRollupState;
  operations: Array<{ name: string; rollup: StatsRollupState }>;
  phases: Array<{
    name: string;
    count: number;
    averageMs: number;
    p50Ms: number;
    p95Ms: number;
    maxMs: number;
  }>;
  failures: Array<{ stage: string; count: number }>;
  upstreamOutputBytes: number;
  cacheHits: number;
  cacheMisses: number;
  serverCount: number;
  toolCount: number;
}

export interface ServerModalState {
  name: string;
  transport: string;
  auth?: string;
  enabled: boolean;
  connected: boolean;
  discovered: boolean;
  toolCount: number;
  totalToolCount: number;
  tools: ToolModalState[];
  busy?: boolean;
  error?: string;
}

interface Keybindings {
  matches(data: string, id: "tui.select.up" | "tui.select.down" | "tui.select.cancel"): boolean;
}

export interface ServerEnabledChange {
  name: string;
  previousEnabled: boolean;
  enabled: boolean;
}

export interface ChainEnabledChange {
  name: string;
  scope: ChainScope;
  previousEnabled: boolean;
  enabled: boolean;
}

interface ManagerSaveResult {
  settings: CodeMcpSettings;
  servers: ServerModalState[];
  chains: ChainModalState[];
}

type UnsavedAction = "save" | "discard" | "cancel";

interface ServerManagerOptions {
  servers: ServerModalState[];
  chains: ChainModalState[];
  settings: CodeMcpSettings;
  stats: StatsModalState;
  onDiscover(server: ServerModalState): Promise<ServerModalState>;
  onSaveChanges(
    settings: CodeMcpSettings,
    serverChanges: ServerEnabledChange[],
    chainChanges: ChainEnabledChange[],
  ): Promise<ManagerSaveResult>;
  onResolveUnsaved(): Promise<UnsavedAction>;
  onRevalidateChain(chain: ChainModalState): Promise<ChainModalState[]>;
  onDeleteChain(chain: ChainModalState): Promise<ChainModalState[]>;
}

interface SettingChoice {
  value: EditableSettingValue;
  label: string;
}

interface SettingDefinition {
  key: EditableSettingKey;
  label: string;
  description: string;
  choices: SettingChoice[];
}

const OVERLAY_OPTIONS = {
  width: "90%",
  minWidth: 72,
  maxHeight: "85%",
} as const;

const SETTING_DEFINITIONS: SettingDefinition[] = [
  {
    key: "backgroundWarmup",
    label: "Background warmup",
    description: "Start the Python sidecar in the background when a Pi session starts.",
    choices: [
      { value: true, label: "on" },
      { value: false, label: "off" },
    ],
  },
  {
    key: "cacheTtlHours",
    label: "Catalog cache TTL",
    description: "Maximum age of a cached upstream tools/list response.",
    choices: [0, 1, 6, 12, 24, 72, 168].map((value) => ({
      value,
      label: value === 0 ? "off" : `${value}h`,
    })),
  },
  {
    key: "executionTimeoutSeconds",
    label: "Execution timeout",
    description: "Maximum wall-clock duration of one sandboxed CodeMCP program.",
    choices: [10, 30, 60, 120, 300].map(secondsChoice),
  },
  {
    key: "toolTimeoutSeconds",
    label: "Per-tool timeout",
    description: "Maximum duration of one upstream MCP tool call.",
    choices: [10, 30, 60, 120, 300].map(secondsChoice),
  },
  {
    key: "maxCalls",
    label: "Maximum calls",
    description: "Maximum total upstream-tool and nested-chain calls in one execution graph.",
    choices: [10, 25, 50, 100, 200].map(numberChoice),
  },
  {
    key: "resultLimitKiB",
    label: "Final result limit",
    description:
      "Maximum serialized value returned by sandbox code before it fails with a shape summary.",
    choices: [4, 8, 16, 32, 64, 128].map(kibChoice),
  },
  {
    key: "outputLimitKiB",
    label: "Agent output limit",
    description: "Maximum CodeMCP tool-result text placed into the agent context.",
    choices: [10, 25, 50, 100, 200, 512].map(kibChoice),
  },
  {
    key: "outputLineLimit",
    label: "Agent line limit",
    description: "Maximum CodeMCP tool-result lines placed into the agent context.",
    choices: [500, 1_000, 2_000, 5_000, 10_000].map(numberChoice),
  },
];

export async function showServerManagerModal(
  ctx: ExtensionCommandContext,
  options: ServerManagerOptions,
): Promise<void> {
  if (ctx.mode !== "tui") {
    throw new Error("CodeMCP server manager requires interactive mode");
  }

  await ctx.ui.custom<void>(
    (tui, theme, keybindings, done) =>
      new ServerManagerModal(
        options,
        theme,
        keybindings,
        () => done(undefined),
        () => tui.requestRender(),
      ),
    { overlay: true, overlayOptions: OVERLAY_OPTIONS },
  );
}

export function chainStatesFromViews(views: SavedChainView[]): ChainModalState[] {
  return views.map((view) => ({
    name: view.chain.name,
    scope: view.scope,
    description: view.chain.description,
    nativeTool: `mcp_chain_${view.chain.name}`,
    code: view.chain.code,
    enabled: view.chain.enabled,
    status: view.status,
    inputSchema: view.chain.inputSchema,
    outputSchema: view.chain.outputSchema,
    dependencies: view.chain.dependencies.map((dependency) => ({
      kind: dependency.kind,
      call: dependency.call,
      server: dependency.server,
    })),
    staleDependencies: view.staleDependencies,
    calledBy: view.calledBy,
  }));
}

export function statsStateFromSnapshot(snapshot: Record<string, unknown>): StatsModalState {
  const lifetime = parseStatsRollup(snapshot.lifetime);
  const recent = Array.isArray(snapshot.recent)
    ? snapshot.recent.reduce<StatsRollupState>(
        (total, item) => addStatsRollups(total, parseStatsRollup(item)),
        emptyStatsRollup(),
      )
    : emptyStatsRollup();
  const operations = parseNamedStatsRollups(snapshot.operations);
  const rawPhases = isRecord(snapshot.phases) ? snapshot.phases : {};
  const phases = Object.entries(rawPhases).flatMap(([name, value]) => {
    if (!isRecord(value)) return [];
    return [
      {
        name,
        count: numberField(value, "count"),
        averageMs: numberField(value, "average"),
        p50Ms: histogramPercentile(value, 0.5),
        p95Ms: histogramPercentile(value, 0.95),
        maxMs: numberField(value, "max"),
      },
    ];
  });
  const rawFailures = isRecord(snapshot.failures) ? snapshot.failures : {};
  const failures = Object.entries(rawFailures)
    .flatMap(([stage, count]) =>
      typeof count === "number" && count >= 0 ? [{ stage, count }] : [],
    )
    .sort((left, right) => right.count - left.count || left.stage.localeCompare(right.stage));
  const rawServers = isRecord(snapshot.servers) ? snapshot.servers : {};
  const upstreamOutputBytes = Object.values(rawServers).reduce<number>(
    (total, value) => total + (isRecord(value) ? numberField(value, "output_bytes") : 0),
    0,
  );
  const cache = isRecord(snapshot.cache) ? snapshot.cache : {};
  const servers = Object.keys(rawServers).length;
  const tools = isRecord(snapshot.tools) ? Object.keys(snapshot.tools).length : 0;
  return {
    updatedAt: numberField(snapshot, "updated_at"),
    lifetime,
    recent,
    operations,
    phases,
    failures,
    upstreamOutputBytes,
    cacheHits: numberField(cache, "hits"),
    cacheMisses: numberField(cache, "misses"),
    serverCount: servers,
    toolCount: tools,
  };
}

export function serverStatesFromStatus(status: Record<string, unknown>): ServerModalState[] {
  if (!Array.isArray(status.upstreams)) return [];
  return status.upstreams.flatMap((value) => {
    if (!isRecord(value) || typeof value.name !== "string") return [];
    const tools = parseTools(value.tools);
    const toolCount =
      typeof value.tool_count === "number"
        ? value.tool_count
        : tools.filter((tool) => tool.enabled).length;
    return [
      {
        name: value.name,
        transport: typeof value.transport === "string" ? value.transport : "unknown",
        ...(typeof value.auth === "string" ? { auth: value.auth } : {}),
        enabled: value.enabled !== false,
        connected: value.connected === true,
        discovered: value.discovered === true || tools.length > 0,
        toolCount,
        totalToolCount:
          typeof value.total_tool_count === "number" ? value.total_tool_count : tools.length,
        tools,
      },
    ];
  });
}

class ServerManagerModal implements Component, Focusable {
  private readonly search = new Input();
  private activeTab: "servers" | "chains" | "stats" | "settings" = "servers";
  private activePane: "servers" | "tools" = "servers";
  private selectedServerIndex = 0;
  private selectedToolIndex = 0;
  private selectedChainIndex = 0;
  private selectedSettingIndex = 0;
  private savedSettings: CodeMcpSettings;
  private draftSettings: CodeMcpSettings;
  private savedServerEnabled: Map<string, boolean>;
  private savedChainEnabled: Map<string, boolean>;
  private settingsBusy = false;
  private closePromptBusy = false;
  private settingsError: string | undefined;
  private _focused = false;

  constructor(
    private readonly options: ServerManagerOptions,
    private readonly theme: Theme,
    private readonly keybindings: Keybindings,
    private readonly close: () => void,
    private readonly requestRender: () => void,
  ) {
    this.savedSettings = cloneSettings(options.settings);
    this.draftSettings = cloneSettings(options.settings);
    this.savedServerEnabled = serverEnabledMap(options.servers);
    this.savedChainEnabled = chainEnabledMap(options.chains);
    this.applyDraftToolPolicy();
  }

  get focused(): boolean {
    return this._focused;
  }

  set focused(value: boolean) {
    this._focused = value;
    this.search.focused = value && !["stats", "settings"].includes(this.activeTab);
  }

  render(width: number): string[] {
    const content = new Box(2, 1);
    content.addChild({
      render: (contentWidth: number) => [this.renderHeader(contentWidth)],
      invalidate: () => {},
    });
    content.addChild({
      render: (bodyWidth: number) => {
        if (this.activeTab === "servers") return this.renderServers(bodyWidth);
        if (this.activeTab === "chains") return this.renderChains(bodyWidth);
        if (this.activeTab === "stats") return this.renderStats(bodyWidth);
        return this.renderSettings(bodyWidth);
      },
      invalidate: () => this.search.invalidate(),
    });
    content.addChild(new Text(this.theme.fg("dim", this.footer()), 0, 0));
    return renderRoundedFrame(content, width, this.theme);
  }

  invalidate(): void {
    this.search.invalidate();
  }

  handleInput(data: string): void {
    if (matchesKey(data, Key.ctrl("s"))) {
      this.saveDraft();
      return;
    }
    if (this.settingsBusy || this.closePromptBusy) return;
    if (this.keybindings.matches(data, "tui.select.cancel") || matchesKey(data, Key.escape)) {
      if (this.activeTab !== "settings" && this.search.getValue()) {
        this.search.setValue("");
        this.resetSelections();
        this.requestRender();
        return;
      }
      if (this.hasUnsavedChanges()) this.resolveUnsavedClose();
      else this.close();
      return;
    }
    if (matchesKey(data, Key.tab)) {
      this.activeTab =
        this.activeTab === "servers"
          ? "chains"
          : this.activeTab === "chains"
            ? "stats"
            : this.activeTab === "stats"
              ? "settings"
              : "servers";
      this.search.setValue("");
      this.search.focused = this._focused && !["stats", "settings"].includes(this.activeTab);
      this.requestRender();
      return;
    }
    if (this.activeTab === "settings") this.handleSettingsInput(data);
    else if (this.activeTab === "chains") this.handleChainInput(data);
    else if (this.activeTab === "servers") this.handleServerInput(data);
    this.requestRender();
  }

  private handleServerInput(data: string): void {
    if (data === "d") {
      this.discoverSelected();
      return;
    }
    if (matchesKey(data, Key.left) || matchesKey(data, Key.right)) {
      this.activePane = this.activePane === "servers" ? "tools" : "servers";
      this.search.setValue("");
      this.selectedToolIndex = 0;
      return;
    }
    if (this.keybindings.matches(data, "tui.select.up") || matchesKey(data, Key.up)) {
      this.moveSelection(-1);
      return;
    }
    if (this.keybindings.matches(data, "tui.select.down") || matchesKey(data, Key.down)) {
      this.moveSelection(1);
      return;
    }
    if (matchesKey(data, Key.enter) || data === " ") {
      if (this.activePane === "servers") this.toggleSelectedServer();
      else this.toggleSelectedTool();
      return;
    }
    const sanitized = data.replace(/ /g, "");
    if (sanitized) {
      this.search.handleInput(sanitized);
      this.resetSelections();
    }
  }

  private handleChainInput(data: string): void {
    if (this.keybindings.matches(data, "tui.select.up") || matchesKey(data, Key.up)) {
      this.selectedChainIndex = cycleIndex(
        this.selectedChainIndex,
        -1,
        this.filteredChains().length,
      );
      return;
    }
    if (this.keybindings.matches(data, "tui.select.down") || matchesKey(data, Key.down)) {
      this.selectedChainIndex = cycleIndex(
        this.selectedChainIndex,
        1,
        this.filteredChains().length,
      );
      return;
    }
    if (data === "r") {
      this.revalidateSelectedChain();
      return;
    }
    if (matchesKey(data, Key.delete)) {
      this.deleteSelectedChain();
      return;
    }
    if (matchesKey(data, Key.enter) || data === " ") {
      this.toggleSelectedChain();
      return;
    }
    const sanitized = data.replace(/ /g, "");
    if (sanitized) {
      this.search.handleInput(sanitized);
      this.selectedChainIndex = 0;
    }
  }

  private handleSettingsInput(data: string): void {
    if (this.settingsBusy) return;
    if (this.keybindings.matches(data, "tui.select.up") || matchesKey(data, Key.up)) {
      this.selectedSettingIndex = cycleIndex(
        this.selectedSettingIndex,
        -1,
        SETTING_DEFINITIONS.length,
      );
      return;
    }
    if (this.keybindings.matches(data, "tui.select.down") || matchesKey(data, Key.down)) {
      this.selectedSettingIndex = cycleIndex(
        this.selectedSettingIndex,
        1,
        SETTING_DEFINITIONS.length,
      );
      return;
    }
    if (matchesKey(data, Key.left)) this.cycleSelectedSetting(-1);
    else if (matchesKey(data, Key.right) || matchesKey(data, Key.enter) || data === " ") {
      this.cycleSelectedSetting(1);
    }
  }

  private renderHeader(width: number): string {
    const servers =
      this.activeTab === "servers"
        ? this.theme.fg("accent", this.theme.bold("[Servers]"))
        : this.theme.fg("muted", "Servers");
    const chains =
      this.activeTab === "chains"
        ? this.theme.fg("accent", this.theme.bold("[Chains]"))
        : this.theme.fg("muted", "Chains");
    const stats =
      this.activeTab === "stats"
        ? this.theme.fg("accent", this.theme.bold("[Stats]"))
        : this.theme.fg("muted", "Stats");
    const settings =
      this.activeTab === "settings"
        ? this.theme.fg("accent", this.theme.bold("[Settings]"))
        : this.theme.fg("muted", "Settings");
    const tabs = `${servers}  ${chains}  ${stats}  ${settings}`;
    const title = this.theme.fg("dim", this.theme.bold("CodeMCP"));
    const gap = " ".repeat(Math.max(1, width - visibleWidth(tabs) - visibleWidth(title)));
    return truncateToWidth(`${tabs}${gap}${title}`, width);
  }

  private renderServers(width: number): string[] {
    const filterLabel = this.activePane === "servers" ? "Filter servers" : "Filter tools";
    const lines = [this.theme.fg("dim", filterLabel), ...this.search.render(width), ""];
    const splitHeight = Math.max(1, modalBodyRows() - lines.length);
    const leftWidth = Math.min(36, Math.max(24, Math.floor(width * 0.32)));
    const rightWidth = Math.max(1, width - leftWidth - 3);
    const left = this.renderServerList(leftWidth, splitHeight);
    const right = this.renderServerDetails(rightWidth, splitHeight);
    for (let index = 0; index < splitHeight; index += 1) {
      lines.push(
        `${padLine(left[index] ?? "", leftWidth)} ${this.theme.fg("dim", "│")} ${truncateToWidth(right[index] ?? "", rightWidth)}`,
      );
    }
    return lines;
  }

  private renderServerList(width: number, height: number): string[] {
    const servers = this.filteredServers();
    const lines = [this.theme.fg("dim", this.theme.bold("SERVERS"))];
    if (servers.length === 0) return [...lines, this.theme.fg("warning", "No matching servers")];
    this.selectedServerIndex = clampIndex(this.selectedServerIndex, servers.length);
    const visible = visibleWindow(servers, this.selectedServerIndex, height - 1);
    for (const server of visible) {
      const selected =
        this.activePane === "servers" && servers.indexOf(server) === this.selectedServerIndex;
      const prefix = selected ? this.theme.fg("accent", "→") : " ";
      const status = serverIcon(server, this.theme);
      const count = server.discovered ? `${server.toolCount}/${server.totalToolCount}` : "—";
      const reserved = visibleWidth(prefix) + visibleWidth(status) + visibleWidth(count) + 4;
      const name = truncateToWidth(server.name, Math.max(4, width - reserved), "…");
      const gap = " ".repeat(Math.max(1, width - reserved - visibleWidth(name) + 1));
      lines.push(
        truncateToWidth(
          `${prefix} ${status} ${selected ? this.theme.fg("accent", name) : name}${gap}${this.theme.fg("muted", count)}`,
          width,
        ),
      );
    }
    return lines;
  }

  private renderServerDetails(width: number, height: number): string[] {
    const server = this.selectedServer();
    if (!server) return [this.theme.fg("muted", "Select a server")];
    const lines = [
      this.theme.fg("accent", this.theme.bold(server.name)),
      this.theme.fg(
        "muted",
        `${serverStatus(server)} · ${server.toolCount}/${server.totalToolCount} tools enabled`,
      ),
      this.theme.fg("dim", `${server.transport}${server.auth ? ` · ${server.auth}` : ""}`),
      server.busy
        ? this.theme.fg("warning", "Working…")
        : `${this.theme.fg("accent", "[d]")} Discover tools  ${this.theme.fg("accent", "[space]")} ${server.enabled ? "Disable server" : "Enable server"}`,
      ...(server.error ? [this.theme.fg("warning", `Error: ${server.error}`)] : []),
      this.theme.fg("dim", "─".repeat(Math.max(1, width))),
      this.theme.fg("dim", this.theme.bold("TOOLS")),
    ];
    if (!server.discovered) {
      lines.push(this.theme.fg("muted", "No catalog yet. Press D to discover tools."));
      return lines;
    }
    const tools = this.filteredTools(server);
    if (tools.length === 0) {
      lines.push(this.theme.fg("warning", "No matching tools"));
      return lines;
    }
    this.selectedToolIndex = clampIndex(this.selectedToolIndex, tools.length);
    const selectedTool = tools[this.selectedToolIndex];
    const available = Math.max(1, height - lines.length);
    if (selectedTool && width >= 72 && available >= 6) {
      const cardWidth = Math.min(42, Math.max(28, Math.floor(width * 0.42)));
      const gapWidth = 2;
      const listWidth = Math.max(1, width - cardWidth - gapWidth);
      const list = this.renderToolList(tools, listWidth, available);
      const cardHeight = Math.min(available, 12);
      const card = renderToolCard(selectedTool, cardWidth, cardHeight, this.theme);
      for (let index = 0; index < available; index += 1) {
        lines.push(
          `${padLine(list[index] ?? "", listWidth)}${" ".repeat(gapWidth)}${card[index] ?? ""}`,
        );
      }
      return lines;
    }

    const cardHeight =
      selectedTool && available >= 5
        ? Math.min(9, available - 1, Math.max(4, Math.floor(available * 0.35)))
        : 0;
    const listHeight = available - cardHeight;
    const list = listHeight > 0 ? this.renderToolList(tools, width, listHeight) : [];
    lines.push(...list, ...Array.from({ length: listHeight - list.length }, () => ""));
    if (selectedTool && cardHeight >= 4) {
      lines.push(...renderToolCard(selectedTool, width, cardHeight, this.theme));
    }
    return lines;
  }

  private renderToolList(tools: ToolModalState[], width: number, height: number): string[] {
    const lines: string[] = [];
    for (const tool of visibleWindow(tools, this.selectedToolIndex, height)) {
      const selected =
        this.activePane === "tools" && tools.indexOf(tool) === this.selectedToolIndex;
      const prefix = selected ? this.theme.fg("accent", "→") : " ";
      const state = tool.busy
        ? this.theme.fg("warning", "…")
        : tool.enabled
          ? this.theme.fg("success", "✓")
          : this.theme.fg("dim", "○");
      lines.push(
        truncateToWidth(
          `${prefix} ${state} ${selected ? this.theme.fg("accent", tool.name) : tool.name}`,
          width,
        ),
      );
    }
    return lines;
  }

  private renderChains(width: number): string[] {
    const lines = [this.theme.fg("dim", "Filter chains"), ...this.search.render(width), ""];
    const splitHeight = Math.max(1, modalBodyRows() - lines.length);
    const leftWidth = Math.min(38, Math.max(26, Math.floor(width * 0.36)));
    const rightWidth = Math.max(1, width - leftWidth - 3);
    const chains = this.filteredChains();
    const left = [this.theme.fg("dim", this.theme.bold("SAVED CHAINS"))];
    if (chains.length === 0) {
      left.push(this.theme.fg("muted", "No saved chains"));
    } else {
      this.selectedChainIndex = clampIndex(this.selectedChainIndex, chains.length);
      for (const chain of visibleWindow(chains, this.selectedChainIndex, splitHeight - 1)) {
        const selected = chains.indexOf(chain) === this.selectedChainIndex;
        const prefix = selected ? this.theme.fg("accent", "→") : " ";
        const icon = chainIcon(chain, this.theme);
        const scope = this.theme.fg("dim", chain.scope === "project" ? "[P]" : "[G]");
        left.push(
          truncateToWidth(
            `${prefix} ${icon} ${scope} ${selected ? this.theme.fg("accent", chain.name) : chain.name}`,
            leftWidth,
          ),
        );
      }
    }

    const selected = this.selectedChain();
    const right = selected
      ? this.renderChainDetails(selected, rightWidth)
      : [this.theme.fg("muted", "Select a saved chain")];
    for (let index = 0; index < splitHeight; index += 1) {
      lines.push(
        `${padLine(left[index] ?? "", leftWidth)} ${this.theme.fg("dim", "│")} ${truncateToWidth(right[index] ?? "", rightWidth)}`,
      );
    }
    return lines;
  }

  private renderChainDetails(chain: ChainModalState, width: number): string[] {
    const servers = [
      ...new Set(
        chain.dependencies
          .filter((dependency) => dependency.kind === "mcp_tool")
          .map((dependency) => dependency.server),
      ),
    ];
    const lines = [
      this.theme.fg("accent", this.theme.bold(chain.name)),
      this.theme.fg("muted", `${chain.scope} · ${chain.status} · ${chain.nativeTool}`),
      chain.busy
        ? this.theme.fg("warning", "Working…")
        : `${this.theme.fg("accent", "[space]")} ${chain.enabled ? "Disable" : "Enable"}  ${this.theme.fg("accent", "[r]")} Revalidate  ${this.theme.fg("accent", "[del]")} Delete`,
      ...(chain.error ? [this.theme.fg("warning", `Error: ${chain.error}`)] : []),
      "",
      ...wrapPlainText(chain.description, width).map((line) => this.theme.fg("muted", line)),
      "",
      this.theme.fg("dim", this.theme.bold("INPUT")),
      ...schemaSummary(chain.inputSchema).map((line) => this.theme.fg("muted", line)),
      "",
      this.theme.fg("dim", this.theme.bold("OUTPUT")),
      ...schemaSummary(chain.outputSchema).map((line) => this.theme.fg("muted", line)),
      "",
      this.theme.fg("dim", this.theme.bold("SERVERS")),
      this.theme.fg("muted", servers.length > 0 ? servers.join(", ") : "none"),
      "",
      this.theme.fg("dim", this.theme.bold("DEPENDENCIES")),
      ...(chain.dependencies.length > 0
        ? chain.dependencies.map((dependency) => this.theme.fg("muted", dependency.call))
        : [this.theme.fg("muted", "none")]),
      ...(chain.calledBy.length > 0
        ? [
            "",
            this.theme.fg("dim", this.theme.bold("CALLED BY")),
            this.theme.fg("muted", chain.calledBy.join(", ")),
          ]
        : []),
      ...(chain.staleDependencies.length > 0
        ? [
            "",
            this.theme.fg("warning", this.theme.bold("STALE")),
            ...chain.staleDependencies.map((dependency) => this.theme.fg("warning", dependency)),
          ]
        : []),
    ];
    return lines;
  }

  private renderStats(width: number): string[] {
    const stats = this.options.stats;
    const lifetime = stats.lifetime;
    const recent = stats.recent;
    const cacheTotal = stats.cacheHits + stats.cacheMisses;
    const cacheRate = cacheTotal > 0 ? `${Math.round((100 * stats.cacheHits) / cacheTotal)}%` : "—";
    const lines = [
      this.theme.fg("accent", this.theme.bold("LOCAL TELEMETRY")),
      this.theme.fg(
        "muted",
        `Updated ${stats.updatedAt > 0 ? new Date(stats.updatedAt * 1_000).toLocaleString() : "never"} · bounded rollups`,
      ),
      "",
      `${this.theme.fg("dim", "Lifetime")}  ${lifetime.count.toLocaleString()} runs · ${lifetime.success.toLocaleString()} ok · ${lifetime.failure.toLocaleString()} failed`,
      `${this.theme.fg("dim", "Calls")}     ${lifetime.calls.toLocaleString()} MCP · ${lifetime.chainCalls.toLocaleString()} nested chains`,
      `${this.theme.fg("dim", "Bytes")}     ${formatBytes(lifetime.inputBytes)} in · ${formatBytes(lifetime.outputBytes)} out`,
      `${this.theme.fg("dim", "Latency")}   ${formatMilliseconds(lifetime.p50Ms)} p50 · ${formatMilliseconds(lifetime.p95Ms)} p95 · ${formatMilliseconds(lifetime.maxMs)} max`,
      `${this.theme.fg("dim", "Results")}   ${formatBytes(lifetime.p50OutputBytes)} p50 · ${formatBytes(lifetime.p95OutputBytes)} p95`,
      `${this.theme.fg("dim", "Withheld")}  ${formatBytes(Math.max(0, stats.upstreamOutputBytes - lifetime.outputBytes))} upstream bytes kept out of final results`,
      `${this.theme.fg("dim", "Recent")}    ${recent.count.toLocaleString()} runs in retained hourly buckets`,
      `${this.theme.fg("dim", "Observed")}  ${stats.serverCount} servers · ${stats.toolCount} tools · ${cacheRate} cache hit`,
      "",
      this.theme.fg("dim", this.theme.bold("OPERATIONS")),
    ];
    if (stats.operations.length === 0) lines.push(this.theme.fg("muted", "No operations yet"));
    for (const operation of stats.operations) {
      lines.push(
        truncateToWidth(
          `${operation.name.padEnd(18)} ${operation.rollup.count.toLocaleString().padStart(8)} · ${operation.rollup.failure.toLocaleString()} failed · ${formatMilliseconds(operation.rollup.averageMs)} avg`,
          width,
        ),
      );
    }
    lines.push("", this.theme.fg("dim", this.theme.bold("FAILURE STAGES")));
    if (stats.failures.length === 0) lines.push(this.theme.fg("muted", "No failures yet"));
    for (const failure of stats.failures) {
      lines.push(`${failure.stage.padEnd(18)} ${failure.count.toLocaleString().padStart(8)}`);
    }
    lines.push("", this.theme.fg("dim", this.theme.bold("PHASES")));
    if (stats.phases.length === 0) lines.push(this.theme.fg("muted", "No phase timings yet"));
    for (const phase of stats.phases) {
      lines.push(
        truncateToWidth(
          `${phase.name.padEnd(18)} ${phase.count.toLocaleString().padStart(8)} · ${formatMilliseconds(phase.p50Ms)} p50 · ${formatMilliseconds(phase.p95Ms)} p95 · ${formatMilliseconds(phase.maxMs)} max`,
          width,
        ),
      );
    }
    return lines.slice(0, Math.max(1, modalBodyRows()));
  }

  private renderSettings(width: number): string[] {
    const splitHeight = Math.max(1, modalBodyRows());
    const leftWidth = Math.min(38, Math.max(28, Math.floor(width * 0.42)));
    const rightWidth = Math.max(1, width - leftWidth - 3);
    const left = [this.theme.fg("dim", this.theme.bold("SETTINGS"))];
    for (const [index, definition] of SETTING_DEFINITIONS.entries()) {
      const selected = index === this.selectedSettingIndex;
      const prefix = selected ? this.theme.fg("accent", "→") : " ";
      const value = settingLabel(definition, this.draftSettings[definition.key]);
      const reserved = visibleWidth(prefix) + visibleWidth(value) + 3;
      const label = truncateToWidth(definition.label, Math.max(4, leftWidth - reserved), "…");
      const gap = " ".repeat(Math.max(1, leftWidth - reserved - visibleWidth(label) + 1));
      left.push(
        truncateToWidth(
          `${prefix} ${selected ? this.theme.fg("accent", label) : label}${gap}${this.theme.fg("muted", value)}`,
          leftWidth,
        ),
      );
    }
    const definition = SETTING_DEFINITIONS[this.selectedSettingIndex];
    const right = definition
      ? [
          this.theme.fg("accent", this.theme.bold(definition.label)),
          this.theme.fg("muted", settingLabel(definition, this.draftSettings[definition.key])),
          "",
          ...wrapPlainText(definition.description, rightWidth).map((line) =>
            this.theme.fg("muted", line),
          ),
          "",
          this.theme.fg("dim", "←/→ change · enter next · ctrl+s save"),
          ...(this.settingsBusy
            ? ["", this.theme.fg("warning", "Saving staged changes…")]
            : this.hasUnsavedChanges()
              ? ["", this.theme.fg("warning", "Unsaved changes")]
              : []),
          ...(this.settingsError
            ? ["", this.theme.fg("warning", `Error: ${this.settingsError}`)]
            : []),
        ]
      : [];
    const lines: string[] = [];
    for (let index = 0; index < splitHeight; index += 1) {
      lines.push(
        `${padLine(left[index] ?? "", leftWidth)} ${this.theme.fg("dim", "│")} ${truncateToWidth(right[index] ?? "", rightWidth)}`,
      );
    }
    return lines;
  }

  private footer(): string {
    const pending = this.hasUnsavedChanges() ? " · * unsaved · ctrl+s save" : "";
    if (this.activeTab === "settings") {
      return `tab servers · ↑/↓ navigate · ←/→/enter change · ctrl+s save · esc close${pending}`;
    }
    if (this.activeTab === "chains") {
      return `tab stats · ↑/↓ navigate · space toggle · r revalidate · del delete · esc close${pending}`;
    }
    if (this.activeTab === "stats") {
      return `tab settings · bounded local rollups · esc close${pending}`;
    }
    return `tab chains · ←/→ pane · ↑/↓ navigate · space toggle · d discover · esc close${pending}`;
  }

  private moveSelection(direction: -1 | 1): void {
    if (this.activePane === "servers") {
      this.selectedServerIndex = cycleIndex(
        this.selectedServerIndex,
        direction,
        this.filteredServers().length,
      );
      this.selectedToolIndex = 0;
      return;
    }
    const server = this.selectedServer();
    this.selectedToolIndex = cycleIndex(
      this.selectedToolIndex,
      direction,
      server ? this.filteredTools(server).length : 0,
    );
  }

  private toggleSelectedServer(): void {
    const server = this.selectedServer();
    if (!server || server.busy || this.settingsBusy) return;
    server.enabled = !server.enabled;
    delete server.error;
  }

  private discoverSelected(): void {
    const server = this.selectedServer();
    if (!server || server.busy || this.settingsBusy) return;
    if (server.enabled !== this.savedServerEnabled.get(server.name)) {
      server.error = "Save this server change before discovering tools";
      return;
    }
    if (!server.enabled) {
      server.error = "Enable and save this server before discovering tools";
      return;
    }
    server.busy = true;
    delete server.error;
    this.requestRender();
    void this.options
      .onDiscover(server)
      .then((updated) => {
        applyServerUpdate(server, updated);
        this.applyDraftToolPolicy();
      })
      .catch((error: unknown) => {
        server.error = summarizeError(error);
      })
      .finally(() => {
        server.busy = false;
        this.requestRender();
      });
  }

  private toggleSelectedTool(): void {
    const server = this.selectedServer();
    if (!server || server.busy || this.settingsBusy) return;
    const tools = this.filteredTools(server);
    const tool = tools[this.selectedToolIndex];
    if (!tool || tool.busy) return;
    const enabled = !tool.enabled;
    this.draftSettings = setToolEnabled(this.draftSettings, server.name, tool.name, enabled);
    tool.enabled = enabled;
    server.toolCount = server.tools.filter((candidate) => candidate.enabled).length;
    this.settingsError = undefined;
  }

  private toggleSelectedChain(): void {
    const chain = this.selectedChain();
    if (!chain || chain.busy || this.settingsBusy) return;
    applyChainEnabled(chain, !chain.enabled);
    delete chain.error;
  }

  private revalidateSelectedChain(): void {
    const chain = this.selectedChain();
    if (!chain || chain.busy || this.settingsBusy) return;
    if (chain.enabled !== this.savedChainEnabled.get(chainKey(chain))) {
      chain.error = "Save this chain change before revalidation";
      return;
    }
    chain.busy = true;
    delete chain.error;
    void this.options
      .onRevalidateChain(chain)
      .then((updated) => this.replaceChains(updated, chain))
      .catch((error: unknown) => {
        chain.error = summarizeError(error);
      })
      .finally(() => {
        chain.busy = false;
        this.requestRender();
      });
  }

  private replaceChains(
    updated: ChainModalState[],
    selected?: ChainModalState,
    preserveDrafts = true,
  ): void {
    const drafts = preserveDrafts ? this.chainEnabledChanges() : [];
    this.options.chains.splice(0, this.options.chains.length, ...updated);
    this.savedChainEnabled = chainEnabledMap(updated);
    for (const draft of drafts) {
      const chain = this.options.chains.find(
        (candidate) => candidate.name === draft.name && candidate.scope === draft.scope,
      );
      if (chain) applyChainEnabled(chain, draft.enabled);
    }
    if (selected) {
      const index = this.filteredChains().findIndex(
        (chain) => chain.name === selected.name && chain.scope === selected.scope,
      );
      if (index >= 0) {
        this.selectedChainIndex = index;
        return;
      }
    }
    this.selectedChainIndex = clampIndex(this.selectedChainIndex, this.filteredChains().length);
  }

  private deleteSelectedChain(): void {
    const chain = this.selectedChain();
    if (!chain || chain.busy) return;
    chain.busy = true;
    delete chain.error;
    void this.options
      .onDeleteChain(chain)
      .then((updated) => this.replaceChains(updated))
      .catch((error: unknown) => {
        chain.error = summarizeError(error);
      })
      .finally(() => {
        chain.busy = false;
        this.requestRender();
      });
  }

  private cycleSelectedSetting(direction: -1 | 1): void {
    const definition = SETTING_DEFINITIONS[this.selectedSettingIndex];
    if (!definition || this.settingsBusy) return;
    const current = this.draftSettings[definition.key];
    const currentIndex = Math.max(
      0,
      definition.choices.findIndex((choice) => choice.value === current),
    );
    const choice =
      definition.choices[cycleIndex(currentIndex, direction, definition.choices.length)];
    if (!choice) return;
    this.settingsError = undefined;
    try {
      this.draftSettings = setEditableSetting(this.draftSettings, definition.key, choice.value);
    } catch (error) {
      this.settingsError = summarizeError(error);
    }
  }

  private saveDraft(): void {
    void this.persistDraft();
  }

  private async persistDraft(): Promise<boolean> {
    if (this.settingsBusy) return false;
    if (!this.hasUnsavedChanges()) return true;
    this.settingsBusy = true;
    this.settingsError = undefined;
    this.requestRender();
    try {
      const result = await this.options.onSaveChanges(
        cloneSettings(this.draftSettings),
        this.serverEnabledChanges(),
        this.chainEnabledChanges(),
      );
      this.savedSettings = cloneSettings(result.settings);
      this.draftSettings = cloneSettings(result.settings);
      this.options.servers.splice(0, this.options.servers.length, ...result.servers);
      this.savedServerEnabled = serverEnabledMap(result.servers);
      this.replaceChains(result.chains, undefined, false);
      this.applyDraftToolPolicy();
      return true;
    } catch (error) {
      this.settingsError = summarizeError(error);
      return false;
    } finally {
      this.settingsBusy = false;
      this.requestRender();
    }
  }

  private resolveUnsavedClose(): void {
    if (this.closePromptBusy || this.settingsBusy) return;
    this.closePromptBusy = true;
    void this.options
      .onResolveUnsaved()
      .then(async (action) => {
        if (action === "discard") {
          this.close();
          return;
        }
        if (action === "save" && (await this.persistDraft())) this.close();
      })
      .catch((error: unknown) => {
        this.settingsError = summarizeError(error);
      })
      .finally(() => {
        this.closePromptBusy = false;
        this.requestRender();
      });
  }

  private hasUnsavedChanges(): boolean {
    return (
      !settingsEqual(this.savedSettings, this.draftSettings) ||
      this.serverEnabledChanges().length > 0 ||
      this.chainEnabledChanges().length > 0
    );
  }

  private serverEnabledChanges(): ServerEnabledChange[] {
    return this.options.servers.flatMap((server) => {
      const previousEnabled = this.savedServerEnabled.get(server.name);
      return previousEnabled === undefined || previousEnabled === server.enabled
        ? []
        : [{ name: server.name, previousEnabled, enabled: server.enabled }];
    });
  }

  private chainEnabledChanges(): ChainEnabledChange[] {
    return this.options.chains.flatMap((chain) => {
      const previousEnabled = this.savedChainEnabled.get(chainKey(chain));
      return previousEnabled === undefined || previousEnabled === chain.enabled
        ? []
        : [
            {
              name: chain.name,
              scope: chain.scope,
              previousEnabled,
              enabled: chain.enabled,
            },
          ];
    });
  }

  private applyDraftToolPolicy(): void {
    for (const server of this.options.servers) {
      const disabled = new Set(this.draftSettings.disabledTools[server.name] ?? []);
      for (const tool of server.tools) tool.enabled = !disabled.has(tool.name);
      server.toolCount = server.tools.filter((tool) => tool.enabled).length;
    }
  }

  private filteredServers(): ServerModalState[] {
    const query = this.activePane === "servers" ? this.search.getValue().trim() : "";
    return query
      ? fuzzyFilter(this.options.servers, query, (server) =>
          [server.name, serverStatus(server)].join(" "),
        )
      : this.options.servers;
  }

  private filteredTools(server: ServerModalState): ToolModalState[] {
    const query = this.activePane === "tools" ? this.search.getValue().trim() : "";
    return query
      ? fuzzyFilter(server.tools, query, (tool) =>
          [tool.name, tool.description].filter(Boolean).join(" "),
        )
      : server.tools;
  }

  private filteredChains(): ChainModalState[] {
    const query = this.search.getValue().trim();
    return query
      ? fuzzyFilter(this.options.chains, query, (chain) =>
          [
            chain.name,
            chain.description,
            chain.nativeTool,
            ...chain.dependencies.map((dependency) => dependency.call),
          ].join(" "),
        )
      : this.options.chains;
  }

  private selectedChain(): ChainModalState | undefined {
    const chains = this.filteredChains();
    this.selectedChainIndex = clampIndex(this.selectedChainIndex, chains.length);
    return chains[this.selectedChainIndex];
  }

  private selectedServer(): ServerModalState | undefined {
    const servers = this.filteredServers();
    this.selectedServerIndex = clampIndex(this.selectedServerIndex, servers.length);
    return servers[this.selectedServerIndex];
  }

  private resetSelections(): void {
    this.selectedServerIndex = 0;
    this.selectedToolIndex = 0;
    this.selectedChainIndex = 0;
  }
}

function emptyStatsRollup(): StatsRollupState {
  return {
    count: 0,
    success: 0,
    failure: 0,
    inputBytes: 0,
    outputBytes: 0,
    calls: 0,
    chainCalls: 0,
    averageMs: 0,
    p50Ms: 0,
    p95Ms: 0,
    maxMs: 0,
    p50OutputBytes: 0,
    p95OutputBytes: 0,
  };
}

function parseStatsRollup(value: unknown): StatsRollupState {
  if (!isRecord(value)) return emptyStatsRollup();
  const duration = isRecord(value.duration_ms) ? value.duration_ms : {};
  const outputSize = isRecord(value.output_size_bytes) ? value.output_size_bytes : {};
  return {
    count: numberField(value, "count"),
    success: numberField(value, "success"),
    failure: numberField(value, "failure"),
    inputBytes: numberField(value, "input_bytes"),
    outputBytes: numberField(value, "output_bytes"),
    calls: numberField(value, "calls"),
    chainCalls: numberField(value, "chain_calls"),
    averageMs: numberField(duration, "average"),
    p50Ms: histogramPercentile(duration, 0.5),
    p95Ms: histogramPercentile(duration, 0.95),
    maxMs: numberField(duration, "max"),
    p50OutputBytes: histogramPercentile(outputSize, 0.5),
    p95OutputBytes: histogramPercentile(outputSize, 0.95),
  };
}

function addStatsRollups(left: StatsRollupState, right: StatsRollupState): StatsRollupState {
  const count = left.count + right.count;
  return {
    count,
    success: left.success + right.success,
    failure: left.failure + right.failure,
    inputBytes: left.inputBytes + right.inputBytes,
    outputBytes: left.outputBytes + right.outputBytes,
    calls: left.calls + right.calls,
    chainCalls: left.chainCalls + right.chainCalls,
    averageMs:
      count > 0 ? (left.averageMs * left.count + right.averageMs * right.count) / count : 0,
    p50Ms: Math.max(left.p50Ms, right.p50Ms),
    p95Ms: Math.max(left.p95Ms, right.p95Ms),
    maxMs: Math.max(left.maxMs, right.maxMs),
    p50OutputBytes: Math.max(left.p50OutputBytes, right.p50OutputBytes),
    p95OutputBytes: Math.max(left.p95OutputBytes, right.p95OutputBytes),
  };
}

function parseNamedStatsRollups(value: unknown): Array<{ name: string; rollup: StatsRollupState }> {
  if (!isRecord(value)) return [];
  return Object.entries(value)
    .map(([name, item]) => ({ name, rollup: parseStatsRollup(item) }))
    .sort(
      (left, right) =>
        right.rollup.count - left.rollup.count || left.name.localeCompare(right.name),
    );
}

function histogramPercentile(value: Record<string, unknown>, percentile: number): number {
  const count = numberField(value, "count");
  if (count === 0 || !Array.isArray(value.buckets)) return 0;
  const target = Math.ceil(count * percentile);
  let cumulative = 0;
  for (const bucket of value.buckets) {
    if (!isRecord(bucket)) continue;
    cumulative += numberField(bucket, "count");
    if (cumulative < target) continue;
    const boundary = bucket.le;
    return typeof boundary === "number" ? boundary : numberField(value, "max");
  }
  return numberField(value, "max");
}

function numberField(value: Record<string, unknown>, key: string): number {
  const item = value[key];
  return typeof item === "number" && Number.isFinite(item) && item >= 0 ? item : 0;
}

function formatBytes(value: number): string {
  if (value < 1_024) return `${value} B`;
  if (value < 1_024 * 1_024) return `${(value / 1_024).toFixed(1)} KiB`;
  return `${(value / (1_024 * 1_024)).toFixed(1)} MiB`;
}

function formatMilliseconds(value: number): string {
  return value >= 1_000 ? `${(value / 1_000).toFixed(2)}s` : `${value.toFixed(1)}ms`;
}

function parseTools(value: unknown): ToolModalState[] {
  if (!Array.isArray(value)) return [];
  return value.flatMap((tool) => {
    if (!isRecord(tool) || typeof tool.name !== "string") return [];
    return [
      {
        name: tool.name,
        enabled: tool.enabled !== false,
        ...(typeof tool.description === "string" ? { description: tool.description } : {}),
      },
    ];
  });
}

function applyServerUpdate(target: ServerModalState, updated: ServerModalState): void {
  target.transport = updated.transport;
  if (updated.auth === undefined) delete target.auth;
  else target.auth = updated.auth;
  target.enabled = updated.enabled;
  target.connected = updated.connected;
  target.discovered = updated.discovered;
  target.toolCount = updated.toolCount;
  target.totalToolCount = updated.totalToolCount;
  target.tools = updated.tools;
  if (updated.error === undefined) delete target.error;
  else target.error = updated.error;
}

function chainIcon(chain: ChainModalState, theme: Theme): string {
  if (chain.busy) return theme.fg("warning", "…");
  if (chain.status === "shadowed") return theme.fg("dim", "◇");
  if (!chain.enabled) return theme.fg("dim", "○");
  if (chain.status === "stale") return theme.fg("warning", "◌");
  return theme.fg("success", "●");
}

function schemaSummary(schema: Record<string, unknown>): string[] {
  const properties = isRecord(schema.properties) ? schema.properties : undefined;
  if (!properties || Object.keys(properties).length === 0) {
    return [typeof schema.type === "string" ? schema.type : "any JSON value"];
  }
  const required = new Set(
    Array.isArray(schema.required)
      ? schema.required.filter((value): value is string => typeof value === "string")
      : [],
  );
  return Object.entries(properties).map(([name, value]) => {
    const property = isRecord(value) ? value : {};
    const type =
      typeof property.type === "string"
        ? property.type
        : Array.isArray(property.type)
          ? property.type.filter((item) => typeof item === "string").join(" | ")
          : "value";
    return `${name}${required.has(name) ? "" : "?"}: ${type}`;
  });
}

function serverStatus(server: ServerModalState): string {
  if (server.busy) return "working";
  if (!server.enabled) return "disabled";
  if (server.connected) return "connected";
  if (server.discovered) return "ready";
  return "not discovered";
}

function serverIcon(server: ServerModalState, theme: Theme): string {
  if (server.busy) return theme.fg("warning", "…");
  if (!server.enabled) return theme.fg("dim", "○");
  if (server.discovered) return theme.fg("success", "●");
  return theme.fg("warning", "◌");
}

function serverEnabledMap(servers: readonly ServerModalState[]): Map<string, boolean> {
  return new Map(servers.map((server) => [server.name, server.enabled]));
}

function chainEnabledMap(chains: readonly ChainModalState[]): Map<string, boolean> {
  return new Map(chains.map((chain) => [chainKey(chain), chain.enabled]));
}

function chainKey(chain: Pick<ChainModalState, "name" | "scope">): string {
  return `${chain.scope}:${chain.name}`;
}

function applyChainEnabled(chain: ChainModalState, enabled: boolean): void {
  const shadowed = chain.status === "shadowed";
  chain.enabled = enabled;
  if (shadowed) return;
  chain.status = enabled ? (chain.staleDependencies.length > 0 ? "stale" : "ready") : "disabled";
}

function cloneSettings(settings: CodeMcpSettings): CodeMcpSettings {
  return {
    ...settings,
    disabledTools: Object.fromEntries(
      Object.entries(settings.disabledTools).map(([server, tools]) => [server, [...tools]]),
    ),
  };
}

function settingsEqual(left: CodeMcpSettings, right: CodeMcpSettings): boolean {
  return JSON.stringify(left) === JSON.stringify(right);
}

function settingLabel(definition: SettingDefinition, value: EditableSettingValue): string {
  return definition.choices.find((choice) => choice.value === value)?.label ?? String(value);
}

function secondsChoice(value: number): SettingChoice {
  return { value, label: `${value}s` };
}

function numberChoice(value: number): SettingChoice {
  return { value, label: value.toLocaleString("en-US") };
}

function kibChoice(value: number): SettingChoice {
  return { value, label: `${value} KiB` };
}

function cycleIndex(index: number, direction: -1 | 1, length: number): number {
  return length === 0 ? 0 : (index + direction + length) % length;
}

function clampIndex(index: number, length: number): number {
  return Math.max(0, Math.min(index, Math.max(0, length - 1)));
}

function visibleWindow<T>(items: T[], selectedIndex: number, maximum: number): T[] {
  const size = Math.max(1, maximum);
  const start = Math.max(0, Math.min(selectedIndex - Math.floor(size / 2), items.length - size));
  return items.slice(start, start + size);
}

function wrapPlainText(text: string, width: number): string[] {
  const words = text.split(/\s+/);
  const lines: string[] = [];
  let line = "";
  for (const word of words) {
    if (!line) line = word;
    else if (line.length + word.length + 1 <= width) line += ` ${word}`;
    else {
      lines.push(line);
      line = word;
    }
  }
  if (line) lines.push(line);
  return lines;
}

function modalBodyRows(): number {
  const reportedRows = process.stdout.rows;
  const terminalRows = typeof reportedRows === "number" && reportedRows > 0 ? reportedRows : 24;
  const overlayRows = Math.floor(terminalRows * 0.85);
  // Frame, Box padding, header, and footer consume six rows; keep one row as
  // safety because Pi clips overlays at maxHeight before the final border.
  return Math.max(1, overlayRows - 7);
}

function renderToolCard(
  tool: ToolModalState,
  width: number,
  height: number,
  theme: Theme,
): string[] {
  const cardWidth = Math.max(8, width);
  const cardHeight = Math.max(3, height);
  const innerWidth = Math.max(1, cardWidth - 2);
  const title = truncateToWidth(tool.name, Math.max(1, cardWidth - 5), "…");
  const top = [
    theme.fg("dim", "╭─ "),
    theme.fg("accent", theme.bold(title)),
    theme.fg("dim", ` ${"─".repeat(Math.max(0, cardWidth - visibleWidth(title) - 5))}╮`),
  ].join("");
  const bottom = theme.fg("dim", `╰${"─".repeat(Math.max(0, cardWidth - 2))}╯`);
  const description = tool.description ?? "No description provided.";
  const content = wrapPlainText(description, innerWidth).map((line) => theme.fg("muted", line));
  const body: string[] = [];
  for (let index = 0; index < cardHeight - 2; index += 1) {
    body.push(
      `${theme.fg("dim", "│")}${padLine(content[index] ?? "", innerWidth)}${theme.fg("dim", "│")}`,
    );
  }
  return [top, ...body, bottom];
}

function padLine(line: string, width: number): string {
  const truncated = truncateToWidth(line, width, "", true);
  return truncated + " ".repeat(Math.max(0, width - visibleWidth(truncated)));
}

function renderRoundedFrame(content: Component, width: number, theme: Theme): string[] {
  const color = (text: string) => theme.fg("accent", text);
  const innerWidth = Math.max(1, width - 2);
  const body = content.render(innerWidth).map((line) => {
    const truncated = truncateToWidth(line, innerWidth, "", true);
    const padded = truncated + " ".repeat(Math.max(0, innerWidth - visibleWidth(truncated)));
    return `${color("│")}${padded}${color("│")}`;
  });
  return [
    color(`╭${"─".repeat(Math.max(0, width - 2))}╮`),
    ...body,
    color(`╰${"─".repeat(Math.max(0, width - 2))}╯`),
  ];
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
