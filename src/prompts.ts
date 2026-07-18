export const SEARCH_PROMPT_GUIDELINES = [
  "Use codemcp_search before codemcp_execute; every upstream or saved-chain match includes the complete typed SDK stub needed to write the execution.",
] as const;

export const EXECUTE_PROMPT_GUIDELINES = [
  "Use codemcp_execute if you know tool schemas; call the returned server.method facade and use top-level return for the compact final value.",
  "It is always better to execute multiple MCP calls in one codemcp_execute call rather than multiple single-call invocations.",
  "You can compose upstream SDK calls and saved chains.* calls, running independent work with asyncio.gather or dependent work sequentially.",
] as const;

export const SAVE_CHAIN_PROMPT_GUIDELINES = [
  "Save only after the user explicitly asks or accepts, and only after the same code has executed successfully.",
  "Use project scope when available unless the user explicitly requests global scope; make schemas describe the exact parameterized contract.",
] as const;

export const MANAGE_CHAIN_PROMPT_GUIDELINES = [
  "List saved chains freely, but enable, disable, revalidate, or delete only after the user explicitly requests that mutation.",
] as const;
