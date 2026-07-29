import { useState } from "react";
import { Download, Sparkles } from "lucide-react";
import { postJson } from "../api/client";
import type { AnalysisResponse } from "../api/types";
import { Button, PageHeader } from "../components/ui";

export default function AnalysisPage() {
  const [text, setText] = useState(""); const [reply, setReply] = useState(""); const [state, setState] = useState(""); const [busy, setBusy] = useState(false);
  const [download, setDownload] = useState("/api/files/download?path=records%2Fanalysis.txt");
  const submit = async () => { if (!text.trim() || busy) return; setBusy(true); setReply(""); setState("分析中…"); try { const data = await postJson<AnalysisResponse>("/api/analyze", { text: text.trim(), session_id: "analysis" }); setReply(data.reply || data.analysis || ""); setDownload(data.record_download_url || download); setState(`已保存到 ${data.record_path || "records/analysis.txt"}`); } catch (error) { setReply(error instanceof Error ? error.message : String(error)); setState("保存失败"); } finally { setBusy(false); } };
  return <div className="page"><PageHeader eyebrow="Runtime Tool" title="文本分析" description="分析输入并把原文与回复写入记录。" action={<a className="button" href={download}><Download aria-hidden="true" size={14} />下载记录</a>} /><div className="analysis-layout"><section className="analysis-input card"><textarea value={text} disabled={busy} onChange={(event) => setText(event.target.value)} placeholder="粘贴需要分析的文字" /><footer><span>{state}</span><Button className="primary" disabled={!text.trim() || busy} onClick={() => void submit()}><Sparkles aria-hidden="true" size={14} />{busy ? "分析中" : "分析并保存"}</Button></footer></section><section className="analysis-output card"><h3>回复</h3><pre>{reply || "分析结果将在这里显示。"}</pre></section></div></div>;
}
