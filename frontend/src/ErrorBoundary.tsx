/** 错误边界：单个页面渲染崩溃时兜底，避免整屏白屏。 */

import { Component } from "react";
import type { ErrorInfo, ReactNode } from "react";
import { GhostButton, PrimaryButton } from "./ui";

interface Props {
  children: ReactNode;
  /** 出错时通过切换 key 复位，通常是当前路由路径。 */
  resetKey?: string;
}

interface State {
  error: Error | null;
}

export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidUpdate(prev: Props) {
    if (prev.resetKey !== this.props.resetKey && this.state.error) {
      this.setState({ error: null });
    }
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error("页面渲染出错：", error, info.componentStack);
  }

  render() {
    const { error } = this.state;
    if (!error) return this.props.children;

    return (
      <div className="flex min-h-[60vh] flex-col items-center justify-center gap-3 px-6 text-center">
        <span className="t-h3" style={{ color: "var(--ink)" }}>
          这个页面出了点问题
        </span>
        <p className="t-meta" style={{ color: "var(--muted)", maxWidth: "40ch" }}>
          界面渲染时遇到异常，重试一次通常就能恢复；若反复出现，请重启本地服务。
        </p>
        <code
          className="t-micro"
          style={{ color: "var(--faint)", maxWidth: "52ch", wordBreak: "break-all" }}
        >
          {error.message}
        </code>
        <div className="mt-1 flex items-center gap-2">
          <PrimaryButton onClick={() => this.setState({ error: null })}>重试</PrimaryButton>
          <GhostButton onClick={() => location.reload()}>重新加载</GhostButton>
        </div>
      </div>
    );
  }
}
