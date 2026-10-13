import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { CodingWorkspacePanel } from "../components/CodingWorkspacePanel";
import type { CodingDelivery, WorkspaceBinding } from "../api/types";
const mocks = vi.hoisted(() => ({ getJson: vi.fn(), postJson: vi.fn() }));
vi.mock("../api/client", () => mocks);
const workspace: WorkspaceBinding = { worktree_id: "wt_A", workspace_root: "/managed/A", branch: "task/A", base_commit: "base-sha", status: "ready", dirty_source_excluded: true };
const delivery: CodingDelivery = { id: "dl_A", source_worktree_id: "wt_A", candidate_sha: "candidate-sha", target_ref: "refs/heads/main", target_sha: "target-sha", status: "prepared", ref: "refs/heads/delivery/A", workspace_root: "/managed/integration" };
beforeEach(() => {
  vi.clearAllMocks();
  mocks.getJson.mockImplementation(async (path: string) => path.includes("/deliveries") ? { deliveries: [] } : { worktrees: [workspace] });
});
async function open(running = false, canManage = true) {
  const user = userEvent.setup();
  render(<CodingWorkspacePanel workspace={workspace} running={running} canManage={canManage} />);
  await user.click(screen.getByText("任务工作区 · task/A"));
  await act(async () => undefined);
  return user;
}
describe("Coding workspace delivery", () => {
  it("requires explicit checkpoint, preparation and test command; shows exact candidate evidence", async () => {
    const user = await open();
    expect(mocks.postJson).not.toHaveBeenCalled();
    mocks.postJson.mockResolvedValueOnce({ checkpoint: { commit: "task-commit" } });
    await user.click(screen.getByRole("button", { name: "保存任务提交" }));
    expect(await screen.findByText(/任务修改已保存到提交 task-commit/)).toBeInTheDocument();
    expect(mocks.postJson).toHaveBeenLastCalledWith("/api/coding/worktrees/wt_A/checkpoint", {});
    mocks.postJson.mockResolvedValueOnce({ delivery });
    await user.click(screen.getByRole("button", { name: "创建集成候选" }));
    expect(await screen.findByText("candidate-sha")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "运行验证" })).toBeDisabled();
    expect(mocks.postJson).toHaveBeenCalledTimes(2);
    await user.type(screen.getByLabelText("验证命令 dl_A"), "pytest -q");
    let finish: (value: unknown) => void = () => undefined;
    mocks.postJson.mockImplementationOnce(() => new Promise((resolve) => { finish = resolve; }));
    await user.click(screen.getByRole("button", { name: "运行验证" }));
    expect(screen.getByRole("button", { name: "验证中…" })).toBeDisabled();
    expect(screen.queryByText("验证通过")).not.toBeInTheDocument();
    await act(async () => finish({ delivery: { ...delivery, status: "verified", verification: { command: "pytest -q", exit_code: 0, status: "success", output: "2 passed" } } }));
    expect(await screen.findByText("验证通过")).toBeInTheDocument();
    expect(screen.getByText(/主分支尚未更新/)).toBeInTheDocument();
    expect(mocks.postJson).toHaveBeenLastCalledWith("/api/coding/deliveries/dl_A/verify", { command: "pytest -q", timeout_seconds: 300 });
    expect(mocks.postJson.mock.calls.some(([path]) => String(path).includes("publish"))).toBe(false);
    mocks.postJson.mockResolvedValueOnce({ delivery: { ...delivery, status: "published", published_ref: "refs/heads/taleclaw/deliveries/dl_A" } });
    await user.click(screen.getByRole("button", { name: "生成交付分支" }));
    expect(await screen.findByText("refs/heads/taleclaw/deliveries/dl_A")).toBeInTheDocument();
    expect(mocks.postJson).toHaveBeenLastCalledWith("/api/coding/deliveries/dl_A/publish", {});
  });
  it("shows conflicts and keeps verification unavailable", async () => {
    const user = await open();
    mocks.postJson.mockResolvedValueOnce({ delivery: { ...delivery, status: "conflict", candidate_sha: "", conflicts: ["api.py"] } });
    await user.click(screen.getByRole("button", { name: "创建集成候选" }));
    expect(await screen.findByText("冲突文件：api.py")).toBeInTheDocument();
    expect(screen.getByText(/在任务工作区合入目标分支/)).toBeInTheDocument();
    await user.type(screen.getByLabelText("验证命令 dl_A"), "pytest");
    expect(screen.getByRole("button", { name: "运行验证" })).toBeDisabled();
  });
  it("shows interrupted delivery evidence without allowing reverification or publication", async () => {
    mocks.getJson.mockImplementation(async (path: string) => path.includes("/deliveries")
      ? { deliveries: [{ ...delivery, status: "interrupted", error: "操作进程已退出，请重新创建候选。" }] }
      : { worktrees: [workspace] });
    const user = await open();
    expect(await screen.findByText("操作已中断")).toBeInTheDocument();
    expect(screen.getByText("操作进程已退出，请重新创建候选。")).toBeInTheDocument();
    await user.type(screen.getByLabelText("验证命令 dl_A"), "pytest");
    expect(screen.getByRole("button", { name: "运行验证" })).toBeDisabled();
    expect(screen.queryByRole("button", { name: "生成交付分支" })).not.toBeInTheDocument();
    expect(mocks.postJson).not.toHaveBeenCalled();
  });
  it("blocks checkpoint and preparation during active writing", async () => {
    await open(true);
    expect(screen.getByRole("button", { name: "保存任务提交" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "创建集成候选" })).toBeDisabled();
    expect(mocks.postJson).not.toHaveBeenCalled();
  });
  it("shows binding without fetching or exposing management for a non-admin", async () => {
    await open(false, false);
    expect(screen.getByText("/managed/A")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "保存任务提交" })).not.toBeInTheDocument();
    await waitFor(() => expect(mocks.getJson).not.toHaveBeenCalled());
  });
});
