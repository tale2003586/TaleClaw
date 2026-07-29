import { useState, type ReactNode } from "react";
import { Check, FolderCog, MonitorCog, Moon, RotateCcw, Sun, UserRound } from "lucide-react";
import type { LucideIcon } from "lucide-react";
import { useAppContext } from "../app/contexts";
import { useTheme } from "../hooks/useTheme";
import { Button, PageHeader } from "../components/ui";

export default function SettingsPage() {
  const { user, health, codingWorkspace, setCodingWorkspace } = useAppContext();
  const theme = useTheme();
  const serverWorkspace = health.coding_workspace || "";
  const [workspace, setWorkspace] = useState(codingWorkspace);
  const [notice, setNotice] = useState("");

  const saveWorkspace = () => {
    setCodingWorkspace(workspace);
    setWorkspace(workspace.trim());
    setNotice("工作区设置已保存在当前浏览器");
  };
  const resetWorkspace = () => {
    setCodingWorkspace("");
    setWorkspace(serverWorkspace);
    setNotice("已恢复服务端默认工作区");
  };

  return <div className="page settings-page">
    <PageHeader eyebrow="Preferences" title="设置" description="管理当前浏览器的界面主题和工作区偏好。" />
    <div className="page-body settings-body">
      {notice && <div className="settings-success" role="status"><Check aria-hidden="true" /><span>{notice}</span></div>}
      <div className="settings-grid">
        <SettingsCard icon={MonitorCog} title="外观" description="主题会立即应用，并保存在当前浏览器。">
          <div className="setting-row"><span><strong>界面主题</strong><small>当前为{theme.theme === "dark" ? "暗色" : "亮色"}主题</small></span><Button onClick={theme.toggleTheme}>{theme.theme === "dark" ? <Sun aria-hidden="true" size={15} /> : <Moon aria-hidden="true" size={15} />}{theme.theme === "dark" ? "切换到亮色" : "切换到暗色"}</Button></div>
        </SettingsCard>

        <SettingsCard icon={UserRound} title="当前账号" description="此信息来自当前登录会话。">
          <dl className="settings-facts"><div><dt>账号</dt><dd>{user.id}</dd></div><div><dt>权限</dt><dd>{user.role === "admin" ? "管理员" : "用户"}</dd></div><div><dt>Runtime</dt><dd>{health.runtime || "unknown"}</dd></div></dl>
        </SettingsCard>

        <SettingsCard className="settings-card-wide" icon={FolderCog} title="Coding 工作区" description="文件、分析等页面会使用这里设置的路径；只影响当前浏览器，不会改写服务端配置。">
          <form className="workspace-setting" onSubmit={(event) => { event.preventDefault(); saveWorkspace(); }}>
            <label className="field"><span>工作区路径</span><input value={workspace} onChange={(event) => { setWorkspace(event.target.value); setNotice(""); }} placeholder={serverWorkspace || "/path/to/workspace"} autoComplete="off" /></label>
            <div className="workspace-setting-meta"><span>服务端默认：<code>{serverWorkspace || "未配置"}</code></span><div className="button-row"><Button type="button" onClick={resetWorkspace}><RotateCcw aria-hidden="true" size={14} />恢复默认</Button><Button className="primary" type="submit" disabled={workspace.trim() === codingWorkspace.trim()}>保存工作区</Button></div></div>
          </form>
        </SettingsCard>
      </div>
    </div>
  </div>;
}

function SettingsCard({ icon: Icon, title, description, className = "", children }: { icon: LucideIcon; title: string; description: string; className?: string; children: ReactNode }) {
  return <section className={`settings-card ${className}`.trim()}><header><span><Icon aria-hidden="true" /></span><div><h3>{title}</h3><p>{description}</p></div></header>{children}</section>;
}
