import {
  DEFAULT_MAX_BYTES,
  DEFAULT_MAX_LINES,
  formatSize,
  truncateHead,
} from "@earendil-works/pi-coding-agent";

export interface CodeMcpOutputDetails {
  truncated: boolean;
  outputBytes: number;
  totalBytes: number;
  outputLines: number;
  totalLines: number;
}

export function formatCodeMcpOutput(value: unknown): {
  text: string;
  details: CodeMcpOutputDetails;
} {
  const serialized = JSON.stringify(value, null, 2);
  const truncation = truncateHead(serialized, {
    maxBytes: DEFAULT_MAX_BYTES,
    maxLines: DEFAULT_MAX_LINES,
  });
  let text = truncation.content;
  if (truncation.truncated) {
    text +=
      `\n\n[Output truncated: showing ${truncation.outputLines} of ` +
      `${truncation.totalLines} lines (${formatSize(truncation.outputBytes)} of ` +
      `${formatSize(truncation.totalBytes)}). The full result was not persisted.]`;
  }
  return {
    text,
    details: {
      truncated: truncation.truncated,
      outputBytes: truncation.outputBytes,
      totalBytes: truncation.totalBytes,
      outputLines: truncation.outputLines,
      totalLines: truncation.totalLines,
    },
  };
}
