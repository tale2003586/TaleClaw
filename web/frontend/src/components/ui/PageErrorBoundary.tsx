import { Component, type ErrorInfo, type ReactNode } from "react";
import { Button } from "./index";

interface Props { children: ReactNode; resetKey: string }
interface State { failed: boolean; message: string }

export class PageErrorBoundary extends Component<Props, State> {
  state: State = { failed: false, message: "" };

  static getDerivedStateFromError(error: unknown): State {
    return { failed: true, message: error instanceof Error ? error.message : String(error) };
  }

  componentDidCatch(error: unknown, info: ErrorInfo) {
    console.error("React page failed", error, info.componentStack);
  }

  componentDidUpdate(previous: Props) {
    if (previous.resetKey !== this.props.resetKey && this.state.failed) this.setState({ failed: false, message: "" });
  }

  render() {
    if (!this.state.failed) return this.props.children;
    return <main className="bootstrap-state"><div className="error-state" role="alert"><strong>页面暂时无法显示</strong><p>{this.state.message || "发生了未预期的前端错误。"}</p><Button onClick={() => this.setState({ failed: false, message: "" })}>重试页面</Button></div></main>;
  }
}
