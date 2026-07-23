export const SEARCH_PROMPT_GUIDELINES = [
  "Use search when the exact current SDK signature is missing; reuse a signature already present in context.",
  "Use capability search for ranked discovery and inventory mode for enumeration; signatures search includes exact stubs for up to three top matches, so inspect only selected alternatives.",
] as const;

export const INSPECT_PROMPT_GUIDELINES = [
  "Batch-inspect selected alternatives from one search when their exact stubs are needed; do not repeat capability search for those same calls.",
] as const;

export const EXECUTE_PROMPT_GUIDELINES = [
  "Use bounded execution to deterministically filter, aggregate, sample, join, or reduce upstream data in the sandbox; do not return raw payloads.",
  "Keep a model turn between calls when an intermediate result changes the semantic decision or user approval is required.",
  "Return the smallest answer. If an oversized result provides result_ref, refine it via inputRef instead of repeating upstream calls.",
  "SDK facades are prebound globals and must not be imported. Use `import asyncio` for `asyncio.gather`; other imports, classes, `asyncio.create_task`, and `__import__` are unsupported.",
] as const;

export const SAVE_CHAIN_PROMPT_GUIDELINES = [
  "Save only after the user explicitly asks or accepts, and only after the same code has executed successfully.",
  "Use generated result item types for nested schema collections; do not declare TypedDict classes.",
  "Use project scope when available unless the user explicitly requests global scope; make schemas describe the exact parameterized contract.",
] as const;

export const MANAGE_CHAIN_PROMPT_GUIDELINES = [
  "List saved chains freely, but enable, disable, revalidate, or delete only after the user explicitly requests that mutation.",
] as const;
