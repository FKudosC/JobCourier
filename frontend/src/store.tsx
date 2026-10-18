/** 运行时上下文：WebSocket 连接、快照与日志流；页面级事件用 window 自定义事件分发。 */

import { createContext, useContext, useEffect, useRef, useState } from "react";
import type { LogEntry, Runtime } from "./api";

/** 与本地服务的连接状态：首次连接前是 connecting，断开后进入 closed 并自动重连。 */
export type ConnState = "connecting" | "open" | "closed";

interface RuntimeCtx {
  rt: Runtime | null;
  logs: LogEntry[];
  conn: ConnState;
  /** 本次会话是否成功连上过；用来区分「还没启动完」和「中途掉线」。 */
  everConnected: boolean;
}

const Ctx = createContext<RuntimeCtx>({
  rt: null,
  logs: [],
  conn: "connecting",
  everConnected: false,
});

const RUNTIME_KEYS = [
  "boss_state",
  "busy",
  "reply_busy",
  "reply_state",
  "reply_count",
  "recv_count",
  "reply_skip_count",
  "browser_background",
  "stored_count",
  "session_count",
  "dup_count",
  "skip_count",
  "target_cities",
  "analyze",
  "company",
  "db_warnings",
] as const;

function pickRuntime(msg: Record<string, unknown>): Runtime {
  const out: Record<string, unknown> = {};
  for (const key of RUNTIME_KEYS) out[key] = msg[key];
  return out as unknown as Runtime;
}

export function RuntimeProvider({ children }: { children: React.ReactNode }) {
  const [rt, setRt] = useState<Runtime | null>(null);
  const [logs, setLogs] = useState<LogEntry[]>([]);
  const [conn, setConn] = useState<ConnState>("connecting");
  const everConnected = useRef(false);
  const wsRef = useRef<WebSocket | null>(null);

  useEffect(() => {
    let closed = false;
    let retry: ReturnType<typeof setTimeout> | undefined;
    // 断线重连的退避间隔：1s 起，每次翻倍，封顶 30s；连上后重置。
    let backoff = 1000;

    const connect = () => {
      const proto = location.protocol === "https:" ? "wss" : "ws";
      const ws = new WebSocket(`${proto}://${location.host}/ws`);
      wsRef.current = ws;
      ws.onopen = () => {
        everConnected.current = true;
        backoff = 1000;
        setConn("open");
      };
      ws.onmessage = (e) => {
        // 后台异常时可能推来非 JSON 文本，解析失败不能连累整条消息通道。
        let msg: Record<string, any>;
        try {
          msg = JSON.parse(e.data);
        } catch {
          return;
        }
        if (!msg || typeof msg !== "object") return;
        if (msg.type === "snapshot") {
          setRt(pickRuntime(msg));
          setLogs(msg.logs ?? []);
        } else if (msg.type === "log") {
          setLogs((prev) =>
            [{ time: msg.time, level: msg.level, text: msg.text }, ...prev].slice(0, 200),
          );
        } else if (msg.type === "status") {
          setRt(pickRuntime(msg));
        } else if (msg.type === "logs_cleared") {
          setLogs([]);
        } else if (
          msg.type === "analyze_done" ||
          msg.type === "apply_done" ||
          msg.type === "company_step" ||
          msg.type === "company_done" ||
          msg.type === "company_batch_done"
        ) {
          // 页面级事件：岗位页 / 查企业弹窗监听
          window.dispatchEvent(new CustomEvent("gj:event", { detail: msg }));
        }
      };
      ws.onclose = () => {
        setConn("closed");
        if (!closed) {
          const wait = backoff;
          backoff = Math.min(backoff * 2, 30000);
          retry = setTimeout(() => {
            setConn("connecting");
            connect();
          }, wait);
        }
      };
    };
    connect();
    return () => {
      closed = true;
      if (retry) clearTimeout(retry);
      wsRef.current?.close();
    };
  }, []);

  return (
    <Ctx.Provider value={{ rt, logs, conn, everConnected: everConnected.current }}>
      {children}
    </Ctx.Provider>
  );
}

export const useRuntime = () => useContext(Ctx);
