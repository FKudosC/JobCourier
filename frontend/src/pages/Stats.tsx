/** 数据：投递漏斗、每日投递、沟通状态分布与评分校准。 */

import {
  BarChart3,
  Gauge,
  Inbox,
  TrendingUp,
} from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { toast } from "sonner";
import { api } from "../api";
import { PageShell } from "../Shell";
import { useRuntime } from "../store";
import {
  Badge,
  Card,
  EmptyState,
  SectionTitle,
  Skeleton,
  Stat,
} from "../ui";

interface Funnel {
  total: number;
  suitable: number;
  applied: number;
  interviews: number;
  interview_done: number;
  passed: number;
  rejected: number;
  declined: number;
}

interface Daily {
  day: string;
  count: number;
  pct_str: string;
  max: boolean;
}

interface Dist {
  label: string;
  count: number;
  pct_str: string;
}

interface Bucket {
  bucket: string;
  jobs: number;
  applied: number;
  interviewed: number;
  rate: number | null;
  rate_label: string;
  has_rate: boolean;
}

interface StatsData {
  funnel: Funnel;
  applied_rate: string;
  interview_rate: string;
  daily: Daily[];
  daily_total: number;
  status_dist: Dist[];
  response: { median_label: string; note: string };
  buckets: Bucket[];
}

export default function Stats() {
  const { rt, conn } = useRuntime();
  const [data, setData] = useState<StatsData | null>(null);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      setData(await api.get<StatsData>("/api/stats"));
    } catch (err) {
      toast.error((err as Error).message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (conn === "open") void load();
  }, [conn, load]);

  // 投递 / 回复计数变化后刷新，让看板跟着动
  const stamp = `${rt?.stored_count ?? 0}-${rt?.reply_count ?? 0}-${rt?.recv_count ?? 0}`;
  useEffect(() => {
    if (conn === "open") void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [stamp]);

  const funnel = data?.funnel;
  const peak = Math.max(1, ...(data?.daily ?? []).map((d) => d.count));

  const steps = funnel
    ? [
        { label: "看过岗位", value: funnel.total },
        { label: "判为合适", value: funnel.suitable },
        { label: "已投递", value: funnel.applied },
        { label: "有面试", value: funnel.interviews },
        { label: "已面试", value: funnel.interview_done },
        { label: "面试通过", value: funnel.passed },
      ]
    : [];
  const stepMax = Math.max(1, ...steps.map((s) => s.value));

  /* 每日投递与沟通状态并排（窄屏上下排列）时保持等高 */
  const CARD_H = 320;

  return (
    <PageShell
      active="/stats"
      fill
    >
      <div className="g-scroll flex min-h-0 flex-1 flex-col gap-7 overflow-y-auto pb-4 pt-7">
        {/* 关键比率 */}
        <div className="grid gap-4 sm:grid-cols-3">
          <Card className="g-in" style={{ animationDelay: "0ms" }}>
            <Stat
              label="投递率"
              value={data?.applied_rate ?? "—"}
              hint="看过岗位 → 已投递"
            />
          </Card>
          <Card className="g-in" style={{ animationDelay: "50ms" }}>
            <Stat
              label="面试率"
              value={data?.interview_rate ?? "—"}
              hint="已投递 → 有面试"
            />
          </Card>
          <Card className="g-in" style={{ animationDelay: "100ms" }}>
            <Stat
              label="HR 响应中位"
              value={data?.response.median_label ?? "—"}
              hint="首次回复等待时长"
            />
          </Card>
        </div>

        {loading && !data ? (
          <div className="grid gap-4">
            <Skeleton style={{ height: 240 }} />
            <Skeleton style={{ height: 200 }} />
          </div>
        ) : (
          <>
            {/* 投递漏斗 */}
            <section className="g-in" style={{ animationDelay: "150ms" }}>
              <div className="mb-3 flex items-center gap-3">
                <span className="g-sect t-h2" style={{ color: "var(--ink)" }}>
                  投递漏斗
                </span>
                <span className="g-rule" style={{ flex: 1 }} />
              </div>
              <Card>
                <div className="flex flex-col gap-4">
                  {steps.map((s, i) => (
                    <div key={s.label}>
                      <div className="mb-1.5 flex items-baseline justify-between">
                        <span className="t-meta font-semibold" style={{ color: "var(--text)" }}>{s.label}</span>
                        <span className="t-num t-meta" style={{ color: "var(--muted)" }}>
                          {s.value}
                          <span style={{ color: "var(--faint)" }}>
                            {" "}
                            · {stepMax > 0 ? Math.round((s.value / stepMax) * 100) : 0}%
                          </span>
                        </span>
                      </div>
                      <div className="g-track" style={{ height: 8 }}>
                        <div
                          className="g-fill"
                          style={{
                            width: `${stepMax > 0 ? (s.value / stepMax) * 100 : 0}%`,
                            background: i === 0 ? "var(--bar-strong)" : "var(--accent)",
                            opacity: 1 - i * 0.08,
                          }}
                        />
                      </div>
                    </div>
                  ))}
                </div>
              </Card>
            </section>

            {/* 每日投递 + 沟通状态分布 */}
            <div className="grid gap-5 lg:grid-cols-[1.5fr_1fr]">
              <section className="g-in" style={{ animationDelay: "200ms" }}>
                <div className="mb-3 flex items-center gap-3">
                  <span className="g-sect t-h2" style={{ color: "var(--ink)" }}>每日投递</span>
                  <span className="g-rule" style={{ flex: 1 }} />
                  <span className="t-micro" style={{ color: "var(--faint)" }}>
                    最近 14 天共投递 <span className="t-num" style={{ color: "var(--ink)" }}>{data?.daily_total ?? 0}</span> 条
                  </span>
                </div>
                <Card className="flex flex-col" style={{ height: CARD_H }}>
                  <BarChart3 size={14} style={{ color: "var(--faint)" }} />
                  <div className="mt-4 flex min-h-0 flex-1 items-end gap-1.5">
                    {(data?.daily ?? []).map((d) => (
                      <div key={d.day} className="flex min-w-0 flex-1 flex-col items-center justify-end gap-2">
                        <span
                          className="t-num t-micro"
                          style={{ color: d.count > 0 ? "var(--ink)" : "transparent", fontWeight: d.max ? 700 : 500 }}
                        >
                          {d.count || 0}
                        </span>
                        <div
                          className="g-col"
                          title={`${d.day} · ${d.count} 条`}
                          style={{
                            height: `${Math.max(d.count > 0 ? 6 : 2, (d.count / peak) * 165)}px`,
                            background: d.max ? "var(--bar-strong)" : "var(--accent)",
                            opacity: d.count > 0 ? 1 : 0.25,
                          }}
                        />
                        <span className="t-micro truncate" style={{ color: "var(--faint)", fontSize: "0.62rem" }}>
                          {d.day}
                        </span>
                      </div>
                    ))}
                  </div>
                </Card>
              </section>

              <section className="g-in" style={{ animationDelay: "250ms" }}>
                <div className="mb-3 flex items-center gap-3">
                  <span className="g-sect t-h2" style={{ color: "var(--ink)" }}>沟通状态</span>
                  <span className="g-rule" style={{ flex: 1 }} />
                </div>
                <Card className="flex flex-col" style={{ height: CARD_H }}>
                  <p className="t-micro" style={{ color: "var(--faint)" }}>
                    与 HR 的会话按沟通状态分布
                  </p>
                  {!data || data.status_dist.length === 0 ? (
                    <div className="mt-4 flex min-h-0 flex-1 flex-col items-center justify-center gap-2 text-center">
                      <Inbox size={18} style={{ color: "var(--faint)" }} />
                      <span className="t-body font-semibold" style={{ color: "var(--ink)" }}>还没有会话</span>
                      <span className="t-meta" style={{ color: "var(--muted)" }}>开始自动回复后这里会有数据</span>
                    </div>
                  ) : (
                    <div className="g-scroll mt-4 flex min-h-0 flex-1 flex-col justify-between gap-2 overflow-y-auto">
                      {data.status_dist.map((d) => {
                        const total = data.status_dist.reduce((sum, x) => sum + x.count, 0) || 1;
                        return (
                          <div key={d.label}>
                            <div className="mb-1.5 flex items-baseline justify-between">
                              <span className="t-meta" style={{ color: "var(--text)" }}>{d.label}</span>
                              <span className="t-num t-meta" style={{ color: "var(--muted)" }}>{d.count}</span>
                            </div>
                            <div className="g-track" style={{ height: 6 }}>
                              <div className="g-fill" style={{ width: `${(d.count / total) * 100}%` }} />
                            </div>
                          </div>
                        );
                      })}
                    </div>
                  )}
                </Card>
              </section>
            </div>

            {/* 评分校准 */}
            <section className="g-in" style={{ animationDelay: "300ms" }}>
              <SectionTitle
                title={
                  <span className="flex items-center gap-2">
                    <Gauge size={15} /> 评分校准
                  </span>
                }
                hint="各分数段已投递岗位的面试率（不足 5 个投递的分档不给结论）。"
              />
              <Card pad="0" className="mt-3 overflow-hidden">
                <table className="w-full border-collapse text-left">
                  <thead className="g-thead">
                    <tr>
                      <th className="t-eyebrow px-4 py-3">分数段</th>
                      <th className="t-eyebrow px-4 py-3 text-right">岗位</th>
                      <th className="t-eyebrow px-4 py-3 text-right">已投递</th>
                      <th className="t-eyebrow px-4 py-3 text-right">有面试</th>
                      <th className="t-eyebrow px-4 py-3 text-right">面试率</th>
                    </tr>
                  </thead>
                  <tbody>
                    {(data?.buckets ?? []).length === 0 && (
                      <tr>
                        <td colSpan={5} className="px-4 py-12 text-center">
                          <EmptyState icon={<TrendingUp size={20} />} title="暂无分档数据" hint="投递并产生面试进展后这里会显示面试率。" />
                        </td>
                      </tr>
                    )}
                    {(data?.buckets ?? []).map((b) => (
                      <tr key={b.bucket} className="g-row">
                        <td className="t-label px-4 py-3 font-semibold" style={{ color: "var(--ink)" }}>{b.bucket}</td>
                        <td className="t-num t-meta px-4 py-3 text-right" style={{ color: "var(--muted)" }}>{b.jobs}</td>
                        <td className="t-num t-meta px-4 py-3 text-right" style={{ color: "var(--muted)" }}>{b.applied}</td>
                        <td className="t-num t-meta px-4 py-3 text-right" style={{ color: "var(--muted)" }}>{b.interviewed}</td>
                        <td className="px-4 py-3 text-right">
                          {b.has_rate ? (
                            <Badge color="green">{b.rate_label}</Badge>
                          ) : (
                            <span className="t-micro" style={{ color: "var(--faint)" }}>{b.rate_label}</span>
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </Card>
            </section>
          </>
        )}
      </div>
    </PageShell>
  );
}
