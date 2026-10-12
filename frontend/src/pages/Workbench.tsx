/** 工作台：运行状态、投递 / 回复控制、后台开关与实时日志。 */

import {
  Eraser,
  MonitorPlay,
  Play,
  Square,
} from "lucide-react";
import { useState } from "react";
import { toast } from "sonner";
import { api } from "../api";
import { PageShell } from "../Shell";
import { useRuntime } from "../store";
import {
  Badge,
  Card,
  PrimaryButton,
  SectionTitle,
  SoftButton,
  Skeleton,
} from "../ui";

/** 日志级别 → 标签与配色（与后端 push_log 的 level 对应）。 */
const LEVELS: Record<string, { label: string; tone: "gray" | "green" | "red" | "blue" | "orange" | "teal" }> = {
  ok: { label: "投递", tone: "green" },
  dup: { label: "重复", tone: "gray" },
  skip: { label: "跳过", tone: "orange" },
  warn: { label: "注意", tone: "red" },
  recv: { label: "收到", tone: "blue" },
  reply: { label: "回复", tone: "teal" },
  manual: { label: "待办", tone: "orange" },
  info: { label: "信息", tone: "gray" },
};

function Metric({ label, value, hint }: { label: string; value: number; hint?: string }) {
  return (
    <div className="flex min-w-0 flex-col px-5 py-4">
      <span className="t-micro font-semibold" style={{ color: "var(--faint)", letterSpacing: "0.06em" }}>
        {label}
      </span>
      <span className="t-num mt-1.5" style={{ fontSize: "1.6rem", fontWeight: 500, color: "var(--ink)", lineHeight: 1.1 }}>
        {value}
      </span>
      {hint && (
        <span className="t-micro mt-1" style={{ color: "var(--faint)" }}>
          {hint}
        </span>
      )}
    </div>
  );
}

export default function Workbench() {
  const { rt, logs } = useRuntime();
  const [pending, setPending] = useState<string | null>(null);

  async function run(key: string, fn: () => Promise<unknown>, okMsg?: string) {
    setPending(key);
    try {
      await fn();
      if (okMsg) toast.success(okMsg);
    } catch (err) {
      toast.error((err as Error).message);
    } finally {
      setPending(null);
    }
  }

  const applying = !!rt?.busy;
  const replying = !!rt?.reply_busy;

  return (
    <PageShell
      active="/"
    >
      <div className="flex flex-col gap-6 pb-4 pt-7">
        {/* 指标：运行中最关心的数字，放在最上面 */}
        <section className="g-in" style={{ animationDelay: "0ms" }}>
          <div className="mb-3 flex items-center gap-3">
            <span className="t-eyebrow">实时计数</span>
            <span className="g-rule" style={{ flex: 1 }} />
          </div>
          <Card pad="0">
            <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-6">
              <Metric label="本次投递" value={rt?.session_count ?? 0} />
              <Metric label="累计投递" value={rt?.stored_count ?? 0} />
              <Metric label="重复岗位" value={rt?.dup_count ?? 0} hint="已沟通过，自动略过" />
              <Metric label="跳过岗位" value={rt?.skip_count ?? 0} hint="不符合筛选条件" />
              <Metric label="收到消息" value={rt?.recv_count ?? 0} hint={`已回复 ${rt?.reply_count ?? 0} 条`} />
              <Metric label="已回复" value={rt?.reply_count ?? 0} hint={`本轮跳过 ${rt?.reply_skip_count ?? 0} 条`} />
            </div>
          </Card>
        </section>

        {/* 控制区 */}
        <div className="grid gap-5 lg:grid-cols-2">
          <Card className="g-in flex flex-col" style={{ animationDelay: "60ms" }}>
            <SectionTitle
              title="自动投递"
              hint="按求职配置逐城搜索并筛选岗位，合适的自动打招呼。"
              right={
                <Badge color={applying ? "green" : "gray"}>
                  {rt ? (applying ? "进行中" : "空闲") : "—"}
                </Badge>
              }
            />
            <div className="mt-5 flex items-center gap-4" style={{ alignSelf: "start" }}>
              {applying ? (
                <PrimaryButton
                  tone="danger"
                  loading={pending === "apply"}
                  onClick={() => run("apply", () => api.post("/api/apply/stop"), "正在停止投递")}
                >
                  <Square size={13} /> 停止投递
                </PrimaryButton>
              ) : (
                <PrimaryButton
                  loading={pending === "apply"}
                  onClick={() => run("apply", () => api.post("/api/apply/start"))}
                >
                  <Play size={13} /> 开始自动投递
                </PrimaryButton>
              )}
              <span className="t-meta" style={{ color: "var(--muted)" }}>
                状态 · <span style={{ color: "var(--ink)", fontWeight: 600 }}>{rt?.boss_state ?? "—"}</span>
              </span>
            </div>
            <p className="t-micro mt-5 pt-4" style={{ color: "var(--faint)", borderTop: "1px solid var(--line)" }}>
              {rt?.target_cities ? `目标城市 · ${rt.target_cities}` : "首次使用需在浏览器里登录 BOSS 直聘，再开始投递"}
            </p>
          </Card>

          <Card className="g-in flex flex-col" style={{ animationDelay: "120ms" }}>
            <SectionTitle
              title="自动回复"
              hint="HR 发来消息时由 AI 结合简历回复；可设置发送前人工确认。"
              right={
                <Badge color={replying ? "green" : "gray"}>
                  {rt ? (replying ? "进行中" : rt.reply_state) : "—"}
                </Badge>
              }
            />
            <div className="mt-5 flex items-center gap-4" style={{ alignSelf: "start" }}>
              {replying ? (
                <PrimaryButton
                  tone="danger"
                  loading={pending === "reply"}
                  onClick={() => run("reply", () => api.post("/api/reply/stop"), "正在停止自动回复")}
                >
                  <Square size={13} /> 停止回复
                </PrimaryButton>
              ) : (
                <PrimaryButton
                  loading={pending === "reply"}
                  onClick={() => run("reply", () => api.post("/api/reply/start"))}
                >
                  <Play size={13} /> 开始自动回复
                </PrimaryButton>
              )}
              <span className="t-meta" style={{ color: "var(--muted)" }}>
                本轮回复 <span className="t-num" style={{ color: "var(--ink)", fontWeight: 600 }}>{rt?.reply_count ?? 0}</span> 条
              </span>
            </div>
            <p className="t-micro mt-5 pt-4" style={{ color: "var(--faint)", borderTop: "1px solid var(--line)" }}>
              投递和回复可以同时进行，运行中请保持浏览器打开
            </p>
          </Card>
        </div>

        {/* 日志 */}
        <section className="g-in flex min-h-0 flex-col" style={{ animationDelay: "180ms" }}>
          <div className="mb-3 flex items-center gap-3">
            <span className="t-eyebrow">运行日志</span>
            <span className="g-rule" style={{ flex: 1 }} />
            <SoftButton
              onClick={() => run("logs", () => api.del("/api/logs"), "日志已清空")}
            >
              <Eraser size={13} /> 清空日志
            </SoftButton>
          </div>
          <Card pad="0" className="overflow-hidden">
            <div className="g-scroll" style={{ maxHeight: 420, overflowY: "auto" }}>
              {logs.length === 0 ? (
                <div className="flex flex-col items-center gap-2 px-6 py-14 text-center">
                  <MonitorPlay size={20} style={{ color: "var(--faint)" }} />
                  <span className="t-body font-semibold" style={{ color: "var(--ink)" }}>
                    暂无日志
                  </span>
                  <span className="t-meta" style={{ color: "var(--muted)" }}>
                    开始投递或自动回复后，过程会实时显示在这里
                  </span>
                </div>
              ) : (
                <div className="g-divided-soft">
                  {logs.map((line, i) => {
                    const meta = LEVELS[line.level] ?? LEVELS.info;
                    return (
                      <div key={`${line.time}-${i}`} className="flex items-start gap-3 px-4 py-2.5">
                        <span className="t-mono t-micro shrink-0 pt-0.5" style={{ color: "var(--faint)" }}>
                          {line.time}
                        </span>
                        <Badge color={meta.tone}>{meta.label}</Badge>
                        <span className="t-meta min-w-0 flex-1" style={{ color: "var(--text)" }}>
                          {line.text}
                        </span>
                      </div>
                    );
                  })}
                </div>
              )}
            </div>
          </Card>
        </section>
      </div>
    </PageShell>
  );
}
