/** 入口：挂载路由、运行时上下文与 toast。 */

import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import { Toaster } from "sonner";
import App from "./App";
import { ErrorBoundary } from "./ErrorBoundary";
import { RuntimeProvider } from "./store";
import { useTheme } from "./theme";
import "./theme.css";

/** Toaster 需要跟着主题走：日间浅色气泡、夜间深色气泡。 */
function AppToaster() {
  const theme = useTheme();
  return (
    <Toaster
      theme={theme}
      position="bottom-right"
      offset={20}
      gap={10}
      toastOptions={{
        style: {
          background: "var(--sheet)",
          border: "1px solid var(--line-2)",
          color: "var(--text)",
          borderRadius: "var(--r-md)",
          boxShadow: "var(--sh-3)",
          fontSize: "var(--fs-meta)",
        },
      }}
    />
  );
}

createRoot(document.getElementById("root") as HTMLElement).render(
  <StrictMode>
    <BrowserRouter>
      <RuntimeProvider>
        <ErrorBoundary>
          <App />
        </ErrorBoundary>
        <AppToaster />
      </RuntimeProvider>
    </BrowserRouter>
  </StrictMode>,
);
