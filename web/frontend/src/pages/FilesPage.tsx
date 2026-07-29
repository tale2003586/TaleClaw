import { useCallback, useEffect, useRef, useState, type ChangeEvent } from "react";
import { Download, Eye, FileText, Folder, FolderPlus, Pencil, Trash2, Upload, ArrowUp } from "lucide-react";
import { getJson, postJson, uploadFormData } from "../api/client";
import type { FileEntry, FilePreviewResponse, FilesResponse } from "../api/types";
import { Button, EmptyState, ErrorState, Modal, PageHeader, Skeleton } from "../components/ui";

interface FilesEnvelope { files: FilesResponse }

export default function FilesPage() {
  const [path, setPath] = useState("");
  const [parent, setParent] = useState("");
  const [entries, setEntries] = useState<FileEntry[]>([]);
  const [status, setStatus] = useState<"loading" | "success" | "error">("loading");
  const [error, setError] = useState("");
  const [preview, setPreview] = useState<FilePreviewResponse | null>(null);
  const [previewMessage, setPreviewMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const uploadInput = useRef<HTMLInputElement>(null);

  const applyFiles = useCallback((data: FilesEnvelope) => {
    setPath(data.files?.path || "");
    setParent(data.files?.parent || "");
    setEntries(data.files?.entries || []);
    setStatus("success");
    setError("");
  }, []);

  const load = useCallback(async (target = "") => {
    setStatus("loading");
    try {
      applyFiles(await getJson<FilesEnvelope>(`/api/files?path=${encodeURIComponent(target)}`));
      setPreview(null);
    } catch (reason) {
      setStatus("error");
      setError(reason instanceof Error ? reason.message : String(reason));
    }
  }, [applyFiles]);

  useEffect(() => { void load(); }, [load]);

  const previewEntry = async (entry: FileEntry) => {
    setPreviewMessage("");
    if (!entry.previewable) {
      setPreview({ path: entry.path, name: entry.name, previewable: false });
      setPreviewMessage("该文件不能直接预览，请下载后查看。");
      return;
    }
    setPreview({ path: entry.path, name: entry.name, previewable: true });
    setPreviewMessage("正在加载预览…");
    try {
      const data = await getJson<FilePreviewResponse>(`/api/files/preview?path=${encodeURIComponent(entry.path)}`);
      setPreview({ ...data, path: data.path || entry.path, name: data.name || entry.name });
      setPreviewMessage("");
    } catch (reason) {
      setPreviewMessage(reason instanceof Error ? reason.message : String(reason));
    }
  };

  const mutate = async (endpoint: string, body: unknown) => {
    setBusy(true);
    try { applyFiles(await postJson<FilesEnvelope>(endpoint, body)); }
    catch (reason) { setError(reason instanceof Error ? reason.message : String(reason)); setStatus("error"); }
    finally { setBusy(false); }
  };

  const makeDirectory = () => {
    const name = window.prompt("文件夹名称");
    if (name?.trim()) void mutate("/api/files/mkdir", { path, name: name.trim() });
  };

  const rename = (entry: FileEntry) => {
    const name = window.prompt("新的名称", entry.name);
    if (name?.trim() && name.trim() !== entry.name) void mutate("/api/files/rename", { path: entry.path, name: name.trim() });
  };

  const remove = (entry: FileEntry) => {
    if (window.confirm(`确定删除 ${entry.name}？`)) void mutate("/api/files/delete", { path: entry.path });
  };

  const upload = async (event: ChangeEvent<HTMLInputElement>) => {
    const files = Array.from(event.target.files || []);
    if (!files.length) return;
    const form = new FormData();
    form.append("path", path);
    files.forEach((file) => form.append("file", file));
    setBusy(true);
    try { applyFiles(await uploadFormData<FilesEnvelope>("/api/files/upload", form)); }
    catch (reason) { setError(reason instanceof Error ? reason.message : String(reason)); setStatus("error"); }
    finally { setBusy(false); event.target.value = ""; }
  };

  const crumbs = path.split("/").filter(Boolean);
  return <div className="page files-page">
    <PageHeader eyebrow="Storage" title="文件" description="浏览用户存储，预览文本并管理运行时产物。" action={<div className="button-row"><Button disabled={!path || busy} onClick={() => void load(parent)}><ArrowUp aria-hidden="true" size={14} />上一级</Button><Button disabled={busy} onClick={makeDirectory}><FolderPlus aria-hidden="true" size={14} />新建目录</Button><Button className="primary" disabled={busy} onClick={() => uploadInput.current?.click()}><Upload aria-hidden="true" size={14} />{busy ? "处理中" : "上传"}</Button><input ref={uploadInput} hidden multiple type="file" onChange={(event) => void upload(event)} /></div>} />
    <div className="page-body">
      <section className="file-toolbar card"><nav className="breadcrumbs" aria-label="当前目录"><button onClick={() => void load("")}>storage</button>{crumbs.map((part, index) => <button key={`${part}-${index}`} onClick={() => void load(crumbs.slice(0, index + 1).join("/"))}>{part}</button>)}</nav><span>{entries.length} 项</span></section>
      {status === "loading" && <div className="card"><Skeleton lines={7} /></div>}
      {status === "error" && <ErrorState message={error} retry={() => void load(path)} />}
      {status === "success" && <section className="file-list card">{entries.length === 0 && <EmptyState title="目录为空" message="上传文件或创建一个新目录。" />}{entries.map((entry) => <article className="file-row" key={entry.path}>
        <button className="file-main" onClick={() => entry.is_dir ? void load(entry.path) : void previewEntry(entry)}><span className="file-glyph">{entry.is_dir ? <Folder aria-hidden="true" size={16} /> : <FileText aria-hidden="true" size={16} />}</span><span><strong>{entry.name}</strong><small>{entry.is_dir ? "文件夹" : `${formatBytes(entry.size)} · ${entry.mime || "file"}`} · {formatDate(entry.modified)}</small></span></button>
        <div className="file-actions"><Button onClick={() => entry.is_dir ? void load(entry.path) : void previewEntry(entry)}>{entry.is_dir ? <Folder aria-hidden="true" size={13} /> : <Eye aria-hidden="true" size={13} />}{entry.is_dir ? "打开" : "预览"}</Button>{!entry.is_dir && <a className="button" href={downloadUrl(entry.path)}><Download aria-hidden="true" size={13} />下载</a>}<Button onClick={() => rename(entry)}><Pencil aria-hidden="true" size={13} />重命名</Button><Button className="danger" onClick={() => remove(entry)}><Trash2 aria-hidden="true" size={13} />删除</Button></div>
      </article>)}</section>}
    </div>
    <Modal open={Boolean(preview)} title={preview?.name || "文件预览"} onClose={() => setPreview(null)}>{preview && <div className="file-preview"><div className="preview-actions"><span>{preview.path}</span><a className="button" href={downloadUrl(preview.path)}><Download aria-hidden="true" size={13} />下载</a></div>{previewMessage ? <p className="preview-empty">{previewMessage}</p> : <pre>{preview.content || "文件内容为空。"}</pre>}</div>}</Modal>
  </div>;
}

const downloadUrl = (path: string) => `/api/files/download?path=${encodeURIComponent(path)}`;
function formatBytes(value = 0) { if (!value) return "0 B"; const units = ["B", "KB", "MB", "GB"]; let size = value; let index = 0; while (size >= 1024 && index < units.length - 1) { size /= 1024; index += 1; } return `${size.toFixed(index ? 1 : 0)} ${units[index]}`; }
function formatDate(value?: string) { if (!value) return "未知时间"; const date = new Date(value); return Number.isNaN(date.getTime()) ? value : date.toLocaleString("zh-CN"); }
