import { useMemory } from "../hooks/useMemory";
import { RefreshCw } from "lucide-react";
import { Button, EmptyState, ErrorState, PageHeader, Skeleton } from "../components/ui";

export default function MemoryPage() {
  const memory = useMemory();
  return <div className="page"><PageHeader eyebrow="Runtime Memory" title="记忆" description="查看当前账号作用域内的记忆工件。" action={<Button className="primary" onClick={() => void memory.reload()}><RefreshCw aria-hidden="true" size={14} />刷新记忆</Button>} /><div className="memory-layout">{memory.status === "loading" && <Skeleton lines={7} />}{memory.status === "error" && <ErrorState message={memory.error} retry={() => void memory.reload()} />}{memory.status === "success" && memory.data?.length === 0 && <EmptyState title="暂无记忆" message="当前账号还没有可展示的记忆文件。" />}{Boolean(memory.data?.length) && <><nav className="memory-tabs" aria-label="记忆文件">{memory.data!.map((file) => <button className={memory.current?.name === file.name ? "active" : ""} onClick={() => memory.setActive(file.name)} key={file.name}>{file.name.replace(/\.md$/, "")}</button>)}</nav><pre className="memory-content">{memory.current?.content || ""}</pre></>}</div></div>;
}
