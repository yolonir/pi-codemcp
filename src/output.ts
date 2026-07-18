import { DEFAULT_MAX_BYTES, formatSize } from "@earendil-works/pi-coding-agent";

export interface CodeMcpOutputDetails {
  truncated: boolean;
  outputBytes: number;
  totalBytes: number;
  outputLines: number;
  totalLines: number;
  outputTokens: number;
}

export interface CodeMcpOutputLimits {
  maxBytes?: number;
  maxLines?: number;
}

export function formatCodeMcpOutput(
  value: unknown,
  limits: CodeMcpOutputLimits = {},
): {
  text: string;
  details: CodeMcpOutputDetails;
} {
  const serialized = JSON.stringify(value) ?? "null";
  const totalBytes = Buffer.byteLength(serialized);
  const maxBytes = limits.maxBytes ?? DEFAULT_MAX_BYTES;
  const truncated = totalBytes > maxBytes;
  const content = truncated ? truncateUtf8(serialized, maxBytes) : serialized;
  const outputBytes = Buffer.byteLength(content);
  let text = content;
  if (truncated) {
    text +=
      `\n\n[Output truncated: showing ${formatSize(outputBytes)} of ` +
      `${formatSize(totalBytes)}. The full result was not persisted.]`;
  }
  return {
    text,
    details: {
      truncated,
      outputBytes,
      totalBytes,
      outputLines: content ? 1 : 0,
      totalLines: 1,
      outputTokens: Math.ceil(text.length / 4),
    },
  };
}

function truncateUtf8(value: string, maxBytes: number): string {
  const encoded = Buffer.from(value);
  const decoder = new TextDecoder("utf-8", { fatal: true });
  for (let end = Math.min(maxBytes, encoded.length); end > 0; end -= 1) {
    try {
      return decoder.decode(encoded.subarray(0, end));
    } catch {
      // Back up to the previous complete UTF-8 code point.
    }
  }
  return "";
}
