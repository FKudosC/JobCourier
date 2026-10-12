/**
 * 查企业弹窗：工商信息 → 网上搜索 → AI 综合评估。
 *
 * 后端「没查过」时会返回真值但没有字段的 report，这里统一 normalize，
 * 否则访问 report.info.length 会抛错把整页打白。
 */

import { ExternalLink, Loader2, RefreshCw, Search, ShieldAlert, ShieldCheck } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { toast } from "sonner";
import { api } from "../api";
import { Badge, GhostButton, PrimaryButton, SlideOver, Spinner } from "../ui";

export interface CompanyPoint {
  text: string;
  href?: string;
}

export interface CompanyHit {
  title: string;
  href: string;
  body: string;
  query?: string;
}

export interface CompanyReport {
  brand_id?: string;
  name: string;
  full_name?: string;
  info: { label: string; value: string }[];
  hits: CompanyHit[];
  risk: string;
  risk_label: string;
  summary: string;
  points: CompanyPoint[];
  checked_at?: string;
}

const RISK_TONE: Record<string, "green" | "orange" | "red" | "gray"> = {
  low: "green",
  medium: "orange",
  high: "red",
  unknown: "gray",
};

/** 风险小标：没查过时返回 null，行内直接不渲染。 */
export function RiskBadgeMini({ risk, label }: { risk?: string; label?: string }) {
  if (!risk) return null;
  return <Badge color={RISK_TONE[risk] ?? "gray"}>{label || "已查企业"}</Badge>;
}

/** 后端可能返回 {}：没有 name 一律视为「没查过」。 */
function normalizeReport(raw: unknown): CompanyReport | null {
  const r = raw as Partial<CompanyReport> | null | undefined;
  if (!r || !r.name) return null;
  return {
    ...r,
    name: r.name,
    info: Array.isArray(r.info) ? r.info : [],
    hits: Array.isArray(r.hits) ? r.hits : [],
    points: Array.isArray(r.points) ? r.points : [],
    risk: r.risk || "unknown",
    risk_label: r.risk_label || "",
    summary: r.summary || "",
  };
}

export function CompanyDialog({
  open,
  onOpenChange,
  uid,
  company,
}: {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  uid: string;
  company: string;
}) {
  const [name, setName] = useState(company);
  const [report, setReport] = useState<CompanyReport | null>(null);
  const [blacklisted, setBlacklisted] = useState(false);
  const [checking, setChecking] = useState(false);
  const [step, setStep] = useState("");
  const uidRef = useRef(uid);
  uidRef.current = uid;

  const fetchView = useCallback(async () => {
    try {
      const view = await api.get<{
        company: string;
        report: unknown;
        blacklisted: boolean;
      }>(`/api/company/${encodeURIComponent(uid)}?company=${encodeURIComponent(company)}`);
      setName(view.company || company);
      setReport(normalizeReport(view.report));
      setBlacklisted(!!view.blacklisted);
    } catch (err) {
      toast.error((err as Error).message);
    }
  }, [uid, company]);

  useEffect(() => {
    if (!open) return;
    setStep("");
    void fetchView();
  }, [open, fetchView]);

  useEffect(() => {
    if (!open) return;
    const onEvent = (e: Event) => {
      const msg = (e as CustomEvent).detail as {
        type: string;
        job_uid?: string;
        step?: string;
        report?: unknown;
      };
      if (msg.job_uid !== uidRef.current) return;
      if (msg.type === "company_step") setStep(msg.step ?? "");
      if (msg.type === "company_done") {
        setReport(normalizeReport(msg.report));
        setChecking(false);
        setStep("");
      }
    };
    window.addEventListener("gj:event", onEvent);
    return () => window.removeEventListener("gj:event", onEvent);
  }, [open]);

  async function recheck() {
    setChecking(true);
    setStep("正在打开浏览器…");
    try {
      const view = await api.post<{ company: string; report: unknown; blacklisted: boolean }>(
        `/api/company/${encodeURIComponent(uid)}/check`,
      );
      setName(view.company || name);
      setReport(normalizeReport(view.report));
      setBlacklisted(!!view.blacklisted);
    } catch (err) {
      toast.error((err as Error).message || "查询失败，请稍后重试");
    } finally {
      setChecking(false);
      setStep("");
    }
  }

  async function toggleBlacklist() {
    try {
      const res = await api.post<{ blacklisted: boolean; message: string }>(
        "/api/company/toggle-blacklist",
        { brand_id: report?.brand_id ?? "", name: report?.name ?? name },
      );
      setBlacklisted(res.blacklisted);
      toast.success(res.message || "已更新黑名单");
    } catch (err) {
      toast.error((err as Error).message);
    }
  }

  const risk = report?.risk ?? "";
  const tone = RISK_TONE[risk] ?? "gray";

  return (
    <SlideOver
      open={open}
      onOpenChange={onOpenChange}
      title={name || "查企业"}
      description={report?.checked_at ? `查询于 ${report.checked_at}` : "工商信息与网络口碑的 AI 综合评估"}
      width={720}
      footer={
        <>
          <GhostButton onClick={recheck} disabled={checking}>
            {checking ? <Spinner size={13} /> : <RefreshCw size={13} />} 重新查询
          </GhostButton>
          {report && (
            <GhostButton onClick={toggleBlacklist} danger={!blacklisted}>
              <ShieldAlert size={13} /> {blacklisted ? "移出黑名单" : "加入黑名单"}
            </GhostButton>
          )}
          <span className="ml-auto t-micro" style={{ color: "var(--faint)" }}>
            工商信息来自 BOSS 公司主页，负面信息来自网上搜索，由 AI 综合评估
          </span>
        </>
      }
    >
      {checking && (
        <div
          className="mb-5 flex items-center gap-3 px-4 py-3"
          style={{ borderRadius: "var(--r-md)", background: "var(--accent-soft)", color: "var(--accent-deep)" }}
        >
          <Loader2 size={15} className="g-spin" />
          <span className="t-meta font-semibold">{step || "查询中…"}</span>
        </div>
      )}

      {!report ? (
        <div className="flex flex-col items-center gap-3 py-16 text-center">
          <span
            className="flex items-center justify-center"
            style={{ width: 44, height: 44, borderRadius: 12, background: "var(--soft)", color: "var(--muted)" }}
          >
            <Search size={20} />
          </span>
          <span className="t-body font-semibold" style={{ color: "var(--ink)" }}>
            还没有查询结果
          </span>
          <span className="t-meta max-w-md" style={{ color: "var(--muted)" }}>
            这个岗位还没查过企业背景。新岗位会在投递时自动查，
            历史岗位打开岗位页会自动补全；也可以点下面的按钮立即查询。
          </span>
          <PrimaryButton disabled={checking} onClick={recheck}>
            {checking ? <Spinner size={13} /> : <Search size={13} />} 立即查询
          </PrimaryButton>
        </div>
      ) : (
        <div className="flex flex-col gap-8">
          {/* AI 评估 */}
          <section>
            <div className="mb-3 flex items-center gap-2.5">
              <ShieldCheck size={16} style={{ color: "var(--accent)" }} />
              <span className="t-eyebrow">AI 评估</span>
              <Badge color={tone}>{report.risk_label || "信息不足"}</Badge>
            </div>
            <div className="g-well px-4 py-3.5">
              <p className="t-body" style={{ color: "var(--text)" }}>
                {report.summary || "还没有生成结论"}
              </p>
            </div>
            {report.points.length > 0 && (
              <ul className="mt-3 flex flex-col gap-2">
                {report.points.map((p, i) => (
                  <li key={i} className="t-meta flex items-start gap-2.5" style={{ color: "var(--text)" }}>
                    <span
                      className="mt-1.5 shrink-0"
                      style={{ width: 5, height: 5, borderRadius: 999, background: "var(--accent)" }}
                    />
                    <span className="min-w-0 flex-1">
                      {p.text}
                      {p.href && (
                        <a
                          href={p.href}
                          target="_blank"
                          rel="noreferrer"
                          className="ml-2 inline-flex items-center gap-0.5"
                          style={{ color: "var(--accent)" }}
                        >
                          来源 <ExternalLink size={11} />
                        </a>
                      )}
                    </span>
                  </li>
                ))}
              </ul>
            )}
          </section>

          {/* 工商信息 */}
          <section>
            <div className="mb-3 flex items-center gap-2.5">
              <span className="t-eyebrow">工商信息</span>
              <span className="g-rule" style={{ flex: 1 }} />
            </div>
            {report.info.length === 0 ? (
              <p className="t-meta" style={{ color: "var(--faint)" }}>
                BOSS 公司主页上没有工商信息
              </p>
            ) : (
              <div className="g-divided">
                {report.info.map((row, i) => (
                  <div key={i} className="flex gap-5 py-2.5">
                    <span className="t-meta w-28 shrink-0" style={{ color: "var(--muted)" }}>
                      {row.label}
                    </span>
                    <span className="t-meta min-w-0 flex-1" style={{ color: "var(--ink)" }}>
                      {row.value}
                    </span>
                  </div>
                ))}
              </div>
            )}
          </section>

          {/* 网上搜索 */}
          <section>
            <div className="mb-3 flex items-center gap-2.5">
              <span className="t-eyebrow">网上搜索</span>
              <Badge color="gray">{report.hits.length} 条</Badge>
              <span className="g-rule" style={{ flex: 1 }} />
            </div>
            {report.hits.length === 0 ? (
              <p className="t-meta" style={{ color: "var(--faint)" }}>
                没搜到相关报道
              </p>
            ) : (
              <div className="flex flex-col gap-3">
                {report.hits.map((hit, i) => (
                  <article key={i} className="g-well px-4 py-3.5">
                    <a
                      href={hit.href}
                      target="_blank"
                      rel="noreferrer"
                      className="t-body font-semibold"
                      style={{ color: "var(--accent-deep)" }}
                    >
                      {hit.title}
                    </a>
                    {hit.body && (
                      <p className="t-meta mt-1.5" style={{ color: "var(--muted)" }}>
                        {hit.body}
                      </p>
                    )}
                    {hit.query && (
                      <span className="t-micro mt-2 inline-block" style={{ color: "var(--faint)" }}>
                        搜索词：{hit.query}
                      </span>
                    )}
                  </article>
                ))}
              </div>
            )}
          </section>
        </div>
      )}
    </SlideOver>
  );
}
