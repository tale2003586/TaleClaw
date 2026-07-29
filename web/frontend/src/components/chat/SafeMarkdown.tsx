import { useState, type ReactElement, type ReactNode } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { TechnicalOutput } from "./TechnicalOutput";

const safeUrl = (url: string) => /^(https?:|mailto:|\/|#)/i.test(url) ? url : "";

export function SafeMarkdown({ children }: { children: string }) {
  return <div className="markdown"><ReactMarkdown remarkPlugins={[remarkGfm]} skipHtml urlTransform={safeUrl} components={{
    a: ({ children: label, href }) => <a href={href} target="_blank" rel="noopener noreferrer">{label}</a>,
    pre: ({ children: code }) => <CopyableCode>{code}</CopyableCode>,
  }}>{children}</ReactMarkdown></div>;
}

function CopyableCode({ children }: { children: ReactNode }) {
  const [copied, setCopied] = useState(false);
  const text = extractText(children);
  const lines = text.split("\n").length;
  if (lines > 20 || text.length > 2000) return <TechnicalOutput className="markdown-output" label="长代码块" value={text.replace(/\n$/, "")} />;
  return <div className="code-block"><button onClick={() => { void navigator.clipboard?.writeText(text); setCopied(true); window.setTimeout(() => setCopied(false), 1200); }}>{copied ? "已复制" : "复制"}</button><pre>{children}</pre></div>;
}

function extractText(node: ReactNode): string {
  if (typeof node === "string" || typeof node === "number") return String(node);
  if (Array.isArray(node)) return node.map(extractText).join("");
  if (node && typeof node === "object" && "props" in node) return extractText((node as ReactElement<{ children?: ReactNode }>).props.children);
  return "";
}
