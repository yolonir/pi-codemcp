import { expect, test } from "bun:test";
import { mkdtemp, readFile, realpath, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { SidecarClient, type SidecarToolName, sidecarToolTimeoutMs } from "../../src/mcp-client.js";

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

test("OAuth-capable calls outlive the interactive callback window", () => {
  const oauthCapable: SidecarToolName[] = [
    "search",
    "inspect",
    "discover",
    "execute",
    "save_chain",
    "execute_chain",
    "revalidate_chain",
  ];
  for (const name of oauthCapable) {
    expect(sidecarToolTimeoutMs(name, 30)).toBe(305_000);
  }
  expect(sidecarToolTimeoutMs("status", 30)).toBe(30_000);
});

test("stdio client runs typed search/chains, forwards cancellation, and cleans up", async () => {
  const traceId = "test:mcp-client";
  const temporary = await mkdtemp(join(tmpdir(), "pi-codemcp-ts-"));
  const workingDirectory = await realpath(temporary);
  const configPath = join(temporary, "mcp.json");
  const projectChainsPath = join(temporary, "project", ".pi", "pi-codemcp", "chains");
  const alphaPidPath = join(temporary, "alpha.pid");
  const alphaCwdPath = join(temporary, "alpha.cwd");
  const betaPidPath = join(temporary, "beta.pid");
  const fixture = join(root, "tests", "fixtures", "upstream_server.py");
  const sidecarProject = join(root, "sidecar");
  const packageVersion = (
    JSON.parse(await readFile(join(root, "package.json"), "utf8")) as { version: string }
  ).version;
  await writeFile(
    configPath,
    JSON.stringify({
      mcpServers: {
        alpha: {
          command: "uv",
          args: ["run", "--project", sidecarProject, "--frozen", fixture, "alpha"],
          env: { TEST_PID_FILE: alphaPidPath, TEST_CWD_FILE: alphaCwdPath },
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
    workingDirectory,
  });
  let sidecarPid: number | null = null;
  let upstreamPids: number[] = [];
  try {
    const [search, initialStatus] = await Promise.all([
      client.call("search", { query: "save number", limit: 5, trace_id: traceId }),
      client.call("status", {}),
    ]);
    const matches = search.results as Array<{ name: string; signature: string; stub?: string }>;
    expect(matches[0]?.name).toBe("beta_save_number");
    expect(matches[0]?.signature).toContain("BetaSaveNumberArgs");
    expect(matches[0]?.stub).toContain("BetaSaveNumberArgs");
    expect(search.prelude).toContain("JsonValue: TypeAlias");
    const inspected = await client.call("inspect", {
      calls: ["beta.save_number"],
      trace_id: traceId,
    });
    expect(inspected.prelude).toContain("JsonValue: TypeAlias");
    expect((inspected.results as Array<{ stub: string }>)[0]?.stub).toContain("BetaSaveNumberArgs");
    expect(initialStatus).toMatchObject({ connected: true, tool_count: 0 });
    expect(
      await readFile(join(temporary, "pi-codemcp", "runtime", "venv", "pyvenv.cfg"), "utf8"),
    ).toContain("version");

    const execution = await client.call("execute", {
      trace_id: traceId,
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
      version: 2,
      operations: {
        search: { count: 1, success: 1 },
        inspect: { count: 1, success: 1 },
        execute: { count: 1, success: 1, calls: 2 },
      },
    });
    expect(await readFile(alphaCwdPath, "utf8")).toBe(workingDirectory);

    const savedChain = await client.call("save_chain", {
      trace_id: traceId,
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
      trace_id: traceId,
      name: "increment",
      arguments: { seed: 4 },
    });
    expect(nativeChain).toMatchObject({ ok: true, result: { value: 5 }, calls_made: 1 });
    const composedChain = await client.call("execute", {
      trace_id: traceId,
      code: 'return await chains.increment({"seed": 7})',
    });
    expect(composedChain).toMatchObject({
      ok: true,
      result: { value: 8 },
      calls_made: 1,
      chain_calls: 1,
    });

    const oversized = await client.call("execute", {
      trace_id: traceId,
      code: 'return {"payload": "x" * 70000}',
    });
    expect(oversized).toMatchObject({
      ok: false,
      failure_stage: "result",
      expires_in_seconds: 300,
      calls_made: 0,
    });
    expect(typeof oversized.result_ref).toBe("string");
    const refined = await client.call("execute", {
      trace_id: traceId,
      input_ref: oversized.result_ref,
      code: `
        root = expect_object(input)
        payload = expect_string(root.get("payload"))
        return {"characters": len(payload)}
      `,
    });
    expect(refined).toMatchObject({
      ok: true,
      result: { characters: 70_000 },
      calls_made: 0,
    });

    const disabled = await client.call("set_chain_enabled", {
      trace_id: traceId,
      name: "increment",
      scope: "project",
      enabled: false,
    });
    expect(disabled).toMatchObject({ scope: "project", status: "disabled" });
    expect(
      await client.call("execute_chain", {
        trace_id: traceId,
        name: "increment",
        arguments: { seed: 1 },
      }),
    ).toMatchObject({ ok: false, failure_stage: "preflight" });
    await client.call("set_chain_enabled", {
      trace_id: traceId,
      name: "increment",
      scope: "project",
      enabled: true,
    });

    const controller = new AbortController();
    const cancelled = client.call(
      "execute",
      {
        trace_id: traceId,
        code: `return await alpha.slow_number({"delay_seconds": 5.0})`,
      },
      controller.signal,
    );
    setTimeout(() => controller.abort(), 150);
    await expect(cancelled).rejects.toThrow();

    const failureTraceId = "pi-tool-call-preflight";
    const rejected = await client.call("execute", {
      trace_id: failureTraceId,
      code: "return inspect_json([], samples=4)",
    });
    expect(rejected).toMatchObject({ ok: false, failure_stage: "preflight", calls_made: 0 });
    const finalStats = await client.call("stats", {});
    const recentFailures = finalStats.recent_failures as Array<Record<string, unknown>>;
    expect(recentFailures[0]).toMatchObject({
      trace_id: failureTraceId,
      operation: "execute",
      stage: "preflight",
      subtype: "preflight_typecheck",
      calls: 0,
      package_version: packageVersion,
    });
    expect(recentFailures[0]).not.toHaveProperty("code");
    expect(recentFailures[0]).not.toHaveProperty("error");

    const status = await client.call("status", {});
    expect(status).toMatchObject({ connected: true, tool_count: 5 });
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
