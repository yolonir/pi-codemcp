export const SEARCH_PROMPT_GUIDELINES = [
  "Use search when the exact current SDK signature is missing; reuse a signature already present in context.",
  "Use capability search for ranked discovery and inventory mode for enumeration; signatures search includes exact stubs for up to three top matches, so inspect only selected alternatives.",
] as const;

export const INSPECT_PROMPT_GUIDELINES = [
  "Batch-inspect selected alternatives from one search when their exact stubs are needed; do not repeat capability search for those same calls.",
] as const;

export const EXECUTE_PROMPT_GUIDELINES = [
  "Use programmatic execution for a bounded workflow when code can deterministically filter, join, aggregate, deduplicate, validate, or reduce intermediate results.",
  "Keep a model turn between calls when an intermediate result changes the semantic decision or user approval is required.",
  "Return the smallest result that answers the request; oversized results fail explicitly with bounded structural inspection data.",
  "SDK facades returned by search are prebound globals and must not be imported; use a normal import statement such as `import asyncio` before `asyncio.gather`, because `__import__` is unavailable.",
] as const;

export const SAVE_CHAIN_PROMPT_GUIDELINES = [
  "Save only after the user explicitly asks or accepts, and only after the same code has executed successfully.",
  "Use project scope when available unless the user explicitly requests global scope; make schemas describe the exact parameterized contract.",
] as const;

export const MANAGE_CHAIN_PROMPT_GUIDELINES = [
  "List saved chains freely, but enable, disable, revalidate, or delete only after the user explicitly requests that mutation.",
] as const;
