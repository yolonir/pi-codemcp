import { expect, test } from "bun:test";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { SidecarClient } from "../../src/mcp-client.js";

const root = dirname(dirname(dirname(fileURLToPath(import.meta.url))));

async function waitForExit(pid: number): Promise<void> {
  for (let attempt = 0; attempt < 250; attempt += 1) {
    try {
      process.kill(pid, 0);
    } catch (error) {
      if (isErrno(error) && error.code === "ESRCH") return;
      throw error;
    }
    await Bun.sleep(20);
  }
  throw new Error(`process ${pid} did not exit`);
}

test("stdio client runs typed search/chains, forwards cancellation, and cleans up", async () => {
  const temporary = await mkdtemp(join(tmpdir(), "pi-codemcp-ts-"));
  const configPath = join(temporary, "mcp.json");
  const projectChainsPath = join(temporary, "project", ".pi", "pi-codemcp", "chains");
  const alphaPidPath = join(temporary, "alpha.pid");
  const betaPidPath = join(temporary, "beta.pid");
  const fixture = join(root, "tests", "fixtures", "upstream_server.py");
  const sidecarProject = join(root, "sidecar");
  await writeFile(
    configPath,
    JSON.stringify({
      mcpServers: {
        alpha: {
          command: "uv",
          args: ["run", "--project", sidecarProject, "--frozen", fixture, "alpha"],
          env: { TEST_PID_FILE: alphaPidPath },
        },
        beta: {
          command: "uv",
          args: ["run", "--project", sidecarProject, "--frozen", fixture, "beta"],
          env: { TEST_PID_FILE: betaPidPath },
        },
      },
    }),
    "utf8",
  );

  const client = new SidecarClient({
    packageRoot: root,
    agentDir: temporary,
    projectChainsPath,
  });
  let sidecarPid: number | null = null;
  let upstreamPids: number[] = [];
  try {
    const [search, initialStatus] = await Promise.all([
      client.call("search", { query: "save number", limit: 5 }),
      client.call("status", {}),
    ]);
    const matches = search.results as Array<{ name: string; signature: string; stub?: string }>;
    expect(matches[0]?.name).toBe("beta_save_number");
    expect(matches[0]?.signature).toContain("BetaSaveNumberArgs");
    expect(matches[0]).not.toHaveProperty("stub");
    const inspected = await client.call("inspect", { calls: ["beta.save_number"] });
    expect(inspected.prelude).toContain("JsonValue: TypeAlias");
    expect((inspected.results as Array<{ stub: string }>)[0]?.stub).toContain("BetaSaveNumberArgs");
    expect(initialStatus).toMatchObject({ connected: true, tool_count: 0 });
    expect(
      await readFile(join(temporary, "pi-codemcp", "runtime", "venv", "pyvenv.cfg"), "utf8"),
    ).toContain("version");

    const execution = await client.call("execute", {
      code: `
          number = await alpha.get_number({"seed": 9})
          saved = await beta.save_number({"value": number["value"]})
          return {"identifier": saved["identifier"]}
        `,
    });
    expect(execution).toMatchObject({
      ok: true,
      result: { identifier: "N-10" },
      calls_made: 2,
    });
    expect(execution).not.toHaveProperty("stage");

    const stats = await client.call("stats", {});
    expect(stats).toMatchObject({
      version: 1,
      operations: {
        search: { count: 1, success: 1 },
        inspect: { count: 1, success: 1 },
        execute: { count: 1, success: 1, calls: 2 },
      },
    });

    const savedChain = await client.call("save_chain", {
      scope: "project",
      name: "increment",
      description: "Increment one input through the alpha MCP server.",
      code: 'return await alpha.get_number({"seed": input["seed"]})',
      input_schema: {
        type: "object",
        properties: { seed: { type: "integer" } },
        required: ["seed"],
        additionalProperties: false,
      },
      output_schema: {
        type: "object",
        properties: { value: { type: "integer" } },
        required: ["value"],
        additionalProperties: false,
      },
    });
    expect(savedChain).toMatchObject({
      created: true,
      chain: {
        status: "ready",
        scope: "project",
        chain: { name: "increment", enabled: true },
      },
    });
    expect(await readFile(join(projectChainsPath, "increment.json"), "utf8")).toContain(
      '"name": "increment"',
    );
    const nativeChain = await client.call("execute_chain", {
      name: "increment",
      arguments: { seed: 4 },
    });
    expect(nativeChain).toMatchObject({ ok: true, result: { value: 5 }, calls_made: 1 });
    const composedChain = await client.call("execute", {
      code: 'return await chains.increment({"seed": 7})',
    });
    expect(composedChain).toMatchObject({
      ok: true,
      result: { value: 8 },
      calls_made: 1,
      chain_calls: 1,
    });

    const disabled = await client.call("apply_manager_changes", {
      changes: [{ name: "increment", scope: "project", enabled: false }],
    });
    expect(disabled).toMatchObject({
      status: { connected: true, tool_count: 3 },
      chains: [{ scope: "project", status: "disabled" }],
    });
    expect(
      await client.call("execute_chain", {
        name: "increment",
        arguments: { seed: 1 },
      }),
    ).toMatchObject({ ok: false, failure_stage: "preflight" });
    await client.call("apply_manager_changes", {
      changes: [{ name: "increment", scope: "project", enabled: true }],
    });

    const controller = new AbortController();
    const cancelled = client.call(
      "execute",
      {
        code: `return await alpha.slow_number({"delay_seconds": 5.0})`,
      },
      controller.signal,
    );
    setTimeout(() => controller.abort(), 150);
    await expect(cancelled).rejects.toThrow();

    const status = await client.call("status", {});
    expect(status).toMatchObject({ connected: true, tool_count: 4 });
    sidecarPid = client.pid;
    expect(sidecarPid).not.toBeNull();
    upstreamPids = [
      Number(await readFile(alphaPidPath, "utf8")),
      Number(await readFile(betaPidPath, "utf8")),
    ];
  } finally {
    await client.close();
    if (sidecarPid !== null) await waitForExit(sidecarPid);
    for (const pid of upstreamPids) await waitForExit(pid);
    await rm(temporary, { recursive: true, force: true });
  }
}, 60_000);

function isErrno(value: unknown): value is NodeJS.ErrnoException {
  return value instanceof Error && "code" in value;
}
