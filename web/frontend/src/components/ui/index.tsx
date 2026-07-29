import { useEffect, useRef, useState, type ButtonHTMLAttributes, type HTMLAttributes, type ReactNode } from "react";
import { Copy, Inbox, RefreshCw, X } from "lucide-react";
import type { LucideIcon } from "lucide-react";
import { outputMeta, outputText } from "../chat/TechnicalOutput";

export function Button({ className = "", ...props }: ButtonHTMLAttributes<HTMLButtonElement>) {
  return <button className={`button ${className}`.trim()} {...props} />;
}

export function IconButton({ icon: Icon, label, size = "md", className = "", type = "button", ...props }: ButtonHTMLAttributes<HTMLButtonElement> & { icon: LucideIcon; label: string; size?: "sm" | "md" }) {
  return <button
    {...props}
    type={type}
    className={`icon-button icon-button-${size} ${className}`.trim()}
    aria-label={label}
    title={label}
  ><Icon aria-hidden="true" focusable="false" size={size === "sm" ? 14 : 16} strokeWidth={1.8} /></button>;
}

export function Badge({ tone = "neutral", children }: { tone?: string; children: ReactNode }) {
  return <span className={`badge badge-${tone}`}>{children}</span>;
}

export function Card({ className = "", ...props }: HTMLAttributes<HTMLElement>) {
  return <section className={`card ${className}`.trim()} {...props} />;
}

export function PageHeader({ eyebrow, title, description, action }: { eyebrow: string; title: string; description?: string; action?: ReactNode }) {
  return <header className="page-header">
    <div className="page-title"><span className="eyebrow">{eyebrow}</span><h2>{title}</h2>{description && <p>{description}</p>}</div>
    {action && <div className="page-actions">{action}</div>}
  </header>;
}

export function Skeleton({ lines = 4 }: { lines?: number }) {
  return <div className="skeleton" aria-label="正在加载">{Array.from({ length: lines }, (_, index) => <span key={index} />)}</div>;
}

export function EmptyState({ title, message }: { title: string; message: string }) {
  return <div className="empty-state"><span className="empty-icon"><Inbox aria-hidden="true" size={28} /></span><h3>{title}</h3><p>{message}</p></div>;
}

export function ErrorState({ message, retry }: { message: string; retry?: () => void }) {
  return <div className="error-state" role="alert"><strong>加载失败</strong><p>{message}</p>{retry && <Button onClick={retry}><RefreshCw aria-hidden="true" size={14} />重试</Button>}</div>;
}

export function JsonDisclosure({ label = "查看 Payload", value }: { label?: string; value: unknown }) {
  const [copied, setCopied] = useState(false);
  const text = outputText(value ?? {});
  return <details className="json-disclosure"><summary>{label}<small>{outputMeta(text)}</small></summary><div><button type="button" onClick={() => { void navigator.clipboard?.writeText(text); setCopied(true); window.setTimeout(() => setCopied(false), 1200); }}><Copy aria-hidden="true" size={12} />{copied ? "已复制" : "复制"}</button><pre>{text}</pre></div></details>;
}

export function Modal({ open, title, onClose, children }: { open: boolean; title: string; onClose: () => void; children: ReactNode }) {
  const previousFocus = useRef<HTMLElement | null>(null);
  useEffect(() => {
    if (!open) return;
    previousFocus.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const close = (event: KeyboardEvent) => { if (event.key === "Escape") onClose(); };
    window.addEventListener("keydown", close);
    return () => {
      window.removeEventListener("keydown", close);
      previousFocus.current?.focus();
    };
  }, [onClose, open]);
  if (!open) return null;
  return <div className="modal-backdrop" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose(); }}>
    <section className="modal" role="dialog" aria-modal="true" aria-label={title}>
      <header><h3>{title}</h3><IconButton autoFocus icon={X} label="关闭" onClick={onClose} /></header>{children}
    </section>
  </div>;
}
