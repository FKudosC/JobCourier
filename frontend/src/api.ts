/** 类型化 API 客户端：错误统一抛 Error(detail)，调用方 toast。 */

export type Dict = Record<string, any>;

/** 普通请求 15s 超时；下面这些接口要跑采集 / 大模型，放宽到 120s。 */
const LONG_RUNNING_PREFIXES = ["/api/analyze", "/api/tailor", "/api/company", "/api/llm/", "/api/reply/"];

function timeoutFor(path: string): number {
  return LONG_RUNNING_PREFIXES.some((p) => path.startsWith(p)) ? 120000 : 15000;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  // 后端卡死或网络中断时，fetch 会一直挂着，界面看起来像「点了没反应」；加个超时兜底。
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutFor(path));
  let res: Response;
  try {
    res = await fetch(path, {
      headers: init?.body instanceof FormData ? undefined : { "Content-Type": "application/json" },
      ...init,
      signal: controller.signal,
    });
  } catch (err) {
    clearTimeout(timer);
    if ((err as Error).name === "AbortError") throw new Error("请求超时，请确认后台服务是否在运行");
    throw new Error("无法连接本地服务，请确认程序仍在运行");
  } finally {
    clearTimeout(timer);
  }
  if (!res.ok) {
    let detail = `请求失败（${res.status}）`;
    try {
      const data = await res.json();
      if (data?.detail) detail = String(data.detail);
    } catch {
      /* ignore */
    }
    throw new Error(detail);
  }
  const text = await res.text();
  return (text ? JSON.parse(text) : {}) as T;
}

export const api = {
  get: <T = Dict>(path: string) => request<T>(path),
  post: <T = Dict>(path: string, body?: unknown) =>
    request<T>(path, { method: "POST", body: body === undefined ? undefined : JSON.stringify(body) }),
  put: <T = Dict>(path: string, body?: unknown) =>
    request<T>(path, { method: "PUT", body: JSON.stringify(body ?? {}) }),
  del: <T = Dict>(path: string) => request<T>(path, { method: "DELETE" }),
  upload: <T = Dict>(path: string, file: File) => {
    const form = new FormData();
    form.append("file", file);
    return request<T>(path, { method: "POST", body: form });
  },
};

/** 运行时快照（与后端 runtime.snapshot() 对应） */
export interface Runtime {
  boss_state: string;
  busy: boolean;
  reply_busy: boolean;
  reply_state: string;
  reply_count: number;
  recv_count: number;
  reply_skip_count: number;
  browser_background: boolean;
  stored_count: number;
  session_count: number;
  dup_count: number;
  skip_count: number;
  target_cities: string;
  analyze: { analyzing: boolean; analyzed: number; total: number };
  /** 批量查企业进度 */
  company: { running: boolean; done: number; total: number; name: string };
  /** 数据库自动迁移留下的告警（如 NOT NULL 列缺失），非空时说明库需手动处理。 */
  db_warnings?: string[];
}

export interface LogEntry {
  time: string;
  level: string;
  text: string;
}

export const getRuntime = () => api.get<Runtime & { logs: LogEntry[] }>("/api/runtime");
