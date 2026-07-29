import { useState } from "react";
import { ChevronRight, Copy } from "lucide-react";

interface TechnicalOutputProps {
  label: string;
  value: unknown;
  defaultOpen?: boolean;
  className?: string;
}

export function TechnicalOutput({ label, value, defaultOpen = false, className = "" }: TechnicalOutputProps) {
  const text = outputText(value);
  const [copied, setCopied] = useState(false); const [open, setOpen] = useState(defaultOpen);
  const preview = outputPreview(text);
  const meta = outputMeta(text);
  const copy = async () => {
    try {
      await navigator.clipboard?.writeText(text);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1200);
    } catch { setCopied(false); }
  };
  return <details className={`technical-output ${className}`.trim()} data-technical-output open={open} onToggle={(event) => setOpen(event.currentTarget.open)}>
    <summary><span className="technical-output-icon"><ChevronRight aria-hidden="true" size={15} /></span><span className="technical-output-heading"><strong>{label}</strong><small>{meta}</small></span><span className="technical-output-preview">{preview}</span><span className="technical-output-action">展开</span></summary>
    <div className="technical-output-body"><div className="technical-output-toolbar"><span>{meta}</span><button type="button" onClick={() => void copy()}><Copy aria-hidden="true" size={12} />{copied ? "已复制" : "复制全部"}</button></div><pre>{text || "（空输出）"}</pre></div>
  </details>;
}

export function outputText(value: unknown): string {
  if (typeof value === "string") return value;
  try { return JSON.stringify(value ?? null, null, 2); }
  catch { return String(value ?? ""); }
}

export function outputMeta(text: string): string {
  const lines = text ? text.split("\n").length : 0;
  const size = new Blob([text]).size;
  const displaySize = size < 1024 ? `${size} B` : `${(size / 1024).toFixed(size < 10240 ? 1 : 0)} KB`;
  return `${lines} 行 · ${displaySize}`;
}

function outputPreview(text: string): string {
  const lines = text.split("\n").map((line) => line.trim()).filter(Boolean);
  const first = lines.find((line) => !["{", "}", "[", "]"].includes(line)) || lines[0] || "空输出";
  const clean = first.replace(/\s+/g, " ").replace(/,$/, "");
  return clean.length > 110 ? `${clean.slice(0, 107)}…` : clean;
}
