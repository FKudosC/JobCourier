/** 岗位管理：筛选、分页、批量分析 / 导出 / 删除，以及岗位详情与定制简历入口。 */

import {
  ArrowDown,
  ArrowUp,
  ArrowUpDown,
  CheckCheck,
  ChevronLeft,
  ChevronRight,
  Download,
  ExternalLink,
  Eye,
  Inbox,
  ShieldAlert,
  Sparkles,
  Trash2,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { toast } from "sonner";
import { api } from "../api";
import { CompanyDialog, RiskBadgeMini } from "../components/CompanyDialog";
import { TailorDialog } from "../components/TailorDialog";
import { PageShell } from "../Shell";
import { useRuntime } from "../store";
import {
  Badge,
  Card,
  CheckBox,
  Dropdown,
  EmptyState,
  GhostButton,
  IconButton,
  Modal,
  PrimaryButton,
  SectionTitle,
  SlideOver,
  SoftButton,
  Spinner,
  useDebouncedValue,
} from "../ui";

/** 后端 /api/jobs 返回的一行。 */
interface JobRow {
  uid: string;
  title: string;
  salary: string;
  company: string;
  brandId: string;
  location: string;
  link: string;
  hrName: string;
  hrTitle: string;
  address: string;
  description: string;
  experience: string;
  education: string;
  matchStatus: string;
  matchHigh: boolean;
  scoreDetail: { label: string; score: number; max: number; evidence?: string }[];
  hardGaps: string[];
  result: string;
  suitable: boolean;
  applied: boolean;
  manualPending: boolean;
  reason: string;
  createdAt: string;
  risk: string;
  riskLabel: string;
}

interface JobsResponse {
  total: number;
  page: number;
  page_size: number;
  rows: JobRow[];
  analysis_options: string[];
  suitable_options: string[];
}

const PAGE_SIZES = ["10 / page", "20 / page", "50 / page", "100 / page"];

/** 岗位列表排序：评分与薪资各自升降序，同一时刻只生效一种。 */
type SortKey = "" | "score_desc" | "score_asc" | "salary_desc" | "salary_asc";

/** 可点击的表头：点一下从高到低，再点从低到高，再点取消。 */
function SortHeader({
  label,
  field,
  sort,
  onToggle,
}: {
  label: string;
  field: "salary" | "score";
  sort: SortKey;
  onToggle: (field: "salary" | "score") => void;
}) {
  const desc = sort === `${field}_desc`;
  const asc = sort === `${field}_asc`;
  const active = desc || asc;
  const Icon = desc ? ArrowDown : asc ? ArrowUp : ArrowUpDown;
  return (
    <button
      type="button"
      onClick={() => onToggle(field)}
      className="inline-flex items-center gap-1 text-left"
      title={
        desc
          ? "当前从高到低，点击改为从低到高"
          : asc
            ? "当前从低到高，点击取消排序"
            : "点击按此列排序（从高到低）"
      }
    >
      <span className="t-eyebrow" style={{ letterSpacing: "0.05em" }}>
        {label}
      </span>
      <Icon
        size={12}
        aria-hidden
        style={{ color: active ? "var(--accent)" : "var(--faint)", opacity: active ? 1 : 0.5 }}
      />
    </button>
  );
}

// 自动补全公司背景只在本页首次挂载时触发一次（后端会跳过查过的，重复调用也安全）
let autoBackfillStarted = false;

export default function Jobs() {
  const { rt } = useRuntime();
  const [params, setParams] = useSearchParams();
  const focusUid = params.get("job") ?? "";

  const [search, setSearch] = useState("");
  const debounced = useDebouncedValue(search);
  const [analysis, setAnalysis] = useState("全部分析状态");
  const [suitable, setSuitable] = useState("合适");
  const [pageSize, setPageSize] = useState(PAGE_SIZES[1]);
  const [page, setPage] = useState(1);
  const [sort, setSort] = useState<SortKey>("");

  const [data, setData] = useState<JobsResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [detail, setDetail] = useState<JobRow | null>(null);
  const [companyFor, setCompanyFor] = useState<JobRow | null>(null);
  const [tailorFor, setTailorFor] = useState<JobRow | null>(null);
  const [toDelete, setToDelete] = useState<JobRow[] | null>(null);

  const size = Number(pageSize.split(" ")[0]);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const qs = new URLSearchParams({
        analysis,
        suitable,
        search: debounced,
        page: String(page),
        page_size: String(size),
      });
      if (focusUid) qs.set("focus", focusUid);
      if (sort) qs.set("sort", sort);
      const res = await api.get<JobsResponse>(`/api/jobs?${qs.toString()}`);
      setData(res);
    } catch (err) {
      toast.error((err as Error).message);
    } finally {
      setLoading(false);
    }
  }, [analysis, suitable, debounced, page, size, focusUid, sort]);

  useEffect(() => {
    void load();
  }, [load]);

  // 打开岗位页时自动补全「从没查过」的公司背景，无需手动逐个点。
  useEffect(() => {
    if (autoBackfillStarted) return;
    autoBackfillStarted = true;
    void api
      .post<{ total: number }>("/api/jobs/company-scan", { uids: [], only_missing: true })
      .then((res) => {
        if (res.total > 0) toast.success(`正在自动补全 ${res.total} 家公司的背景查询`);
      })
      .catch(() => {
        // 已在查询中或都查过了：静默忽略
      });
  }, []);

  useEffect(() => {
    const onEvent = (e: Event) => {
      const msg = (e as CustomEvent).detail as {
        type: string;
        ok?: number;
        failed?: number;
        done?: number;
        total?: number;
      };
      if (msg.type === "analyze_done") {
        const failed = msg.failed ?? 0;
        const ok = msg.ok ?? 0;
        toast.success(failed > 0 ? `分析完成 ${ok} 个，失败 ${failed} 个` : `已完成 ${ok} 个岗位的 AI 分析`);
        void load();
      } else if (msg.type === "company_batch_done") {
        toast.success(`企业查询完成：${msg.done ?? 0}/${msg.total ?? 0} 家`);
        void load();
      }
    };
    window.addEventListener("gj:event", onEvent);
    return () => window.removeEventListener("gj:event", onEvent);
  }, [load]);

  function changeFilter(setter: (v: string) => void) {
    return (v: string) => {
      setter(v);
      setPage(1);
      if (focusUid) {
        params.delete("job");
        setParams(params, { replace: true });
      }
    };
  }

  function toggleSort(field: "salary" | "score") {
    setSort((prev) =>
      prev === `${field}_desc` ? `${field}_asc` : prev === `${field}_asc` ? "" : `${field}_desc`,
    );
    setPage(1);
  }

  const rows = data?.rows ?? [];
  const total = data?.total ?? 0;
  const totalPages = Math.max(1, Math.ceil(total / size));
  const pageUids = rows.map((r) => r.uid);
  const allChecked = pageUids.length > 0 && pageUids.every((u) => selected.has(u));
  const analyzing = !!rt?.analyze?.analyzing;
  const scanning = !!rt?.company?.running;

  const statusTone = useMemo(
    () => (row: JobRow) => (row.applied ? "green" : row.suitable ? "blue" : "gray"),
    [],
  );

  function toggleOne(uid: string) {
    setSelected((prev) => {
      const next = new Set(prev);
      next.has(uid) ? next.delete(uid) : next.add(uid);
      return next;
    });
  }
  function toggleAll() {
    setSelected((prev) => {
      const next = new Set(prev);
      if (allChecked) pageUids.forEach((u) => next.delete(u));
      else pageUids.forEach((u) => next.add(u));
      return next;
    });
  }

  async function analyze(targets: string[]) {
    if (targets.length === 0) {
      toast.error("请先选择岗位或确保当前页有数据");
      return;
    }
    try {
      await api.post("/api/jobs/analyze", { uids: targets });
      toast.success(`已开始分析 ${targets.length} 个岗位`);
    } catch (err) {
      toast.error((err as Error).message);
    }
  }

  async function scanCompanies(targets: string[]) {
    try {
      // 没有选中岗位时 uids 为空 → 补全全部还没查过的公司
      const res = await api.post<{ total: number }>("/api/jobs/company-scan", { uids: targets });
      toast.success(
        targets.length === 0
          ? `已开始补全 ${res.total} 家公司的背景`
          : `已开始查询 ${res.total} 家公司的背景`,
      );
    } catch (err) {
      toast.error((err as Error).message);
    }
  }

  async function markApplied(uid: string) {
    try {
      await api.post(`/api/jobs/${encodeURIComponent(uid)}/applied`);
      toast.success("已记录为已投递，会进入漏斗统计");
      void load();
    } catch (err) {
      toast.error((err as Error).message);
    }
  }

  async function openLink(url: string) {
    if (!url) {
      toast.error("该岗位没有链接");
      return;
    }
    try {
      await api.post("/api/open-link", { url });
    } catch (err) {
      toast.error((err as Error).message);
    }
  }

  async function confirmDelete() {
    if (!toDelete) return;
    try {
      if (toDelete.length === 1) {
        await api.del(`/api/jobs/${encodeURIComponent(toDelete[0].uid)}`);
        toast.success("已删除该岗位");
      } else {
        const res = await api.post<{ deleted: number }>("/api/jobs/delete-many", {
          uids: toDelete.map((r) => r.uid),
        });
        toast.success(`已删除 ${res.deleted} 个岗位`);
        setSelected(new Set());
      }
      setToDelete(null);
      void load();
    } catch (err) {
      toast.error((err as Error).message);
    }
  }

  function exportJobs(uids: string[]) {
    window.open(`/api/jobs/export${uids.length ? `?uids=${uids.join(",")}` : ""}`);
  }

  return (
    <PageShell
      active="/jobs"
      fill
    >
      <div className="flex min-h-0 flex-1 flex-col gap-4 pb-6 pt-6">
        {/* 筛选 + 批量操作（同一行，窄屏自动换行） */}
        <div className="flex flex-wrap items-center gap-2.5">
          <input
            value={search}
            onChange={(e) => changeFilter(setSearch)(e.target.value)}
            placeholder="搜索岗位或公司"
            className="g-field g-input w-64 outline-none"
          />
          <Dropdown
            value={analysis}
            options={data?.analysis_options ?? ["全部分析状态"]}
            onChange={changeFilter(setAnalysis)}
            width={160}
          />
          <Dropdown
            value={suitable}
            options={data?.suitable_options ?? ["全部", "合适", "不合适"]}
            onChange={changeFilter(setSuitable)}
            width={120}
          />
          <Dropdown
            value={pageSize}
            options={PAGE_SIZES}
            onChange={(v) => {
              setPageSize(v);
              setPage(1);
            }}
            width={124}
          />
          <PrimaryButton loading={analyzing} onClick={() => analyze(selected.size ? [...selected] : pageUids)}>
            {analyzing ? <Spinner size={13} /> : <Sparkles size={13} />}
            {analyzing ? `分析中 ${rt?.analyze?.analyzed ?? 0}/${rt?.analyze?.total ?? 0}` : "AI 分析匹配度"}
          </PrimaryButton>
          <SoftButton onClick={() => exportJobs(selected.size ? [...selected] : [])}>
            <Download size={13} /> {selected.size ? "导出选中" : "导出全部"}
          </SoftButton>
          <SoftButton
            disabled={scanning}
            title="查所选岗位所属公司的工商信息与网络口碑，7 天内查过的不重复查"
            onClick={() => scanCompanies(selected.size ? [...selected] : pageUids)}
          >
            {scanning ? <Spinner size={13} /> : <ShieldAlert size={13} />}
            {scanning
              ? `查企业中 ${rt?.company?.done ?? 0}/${rt?.company?.total ?? 0}`
              : "批量查企业"}
          </SoftButton>
          {selected.size > 0 && (
            <>
              <GhostButton onClick={() => setSelected(new Set())}>取消选择</GhostButton>
              <GhostButton danger onClick={() => setToDelete(rows.filter((r) => selected.has(r.uid)))}>
                <Trash2 size={13} /> 批量删除
              </GhostButton>
            </>
          )}

          <span className="ml-auto t-meta" style={{ color: "var(--muted)" }}>
            共 <span className="t-num" style={{ color: "var(--ink)", fontWeight: 600 }}>{total}</span> 个岗位
            {selected.size > 0 && (
              <>
                {" · "}已选{" "}
                <span className="t-num" style={{ color: "var(--accent-deep)", fontWeight: 600 }}>
                  {selected.size}
                </span>{" "}
                项
              </>
            )}
          </span>
        </div>

        {focusUid && (
          <div className="flex items-center gap-2">
            <Badge color="blue">聚焦单个岗位</Badge>
            <GhostButton
              variant="plain"
              onClick={() => {
                params.delete("job");
                setParams(params, { replace: true });
              }}
            >
              显示全部
            </GhostButton>
          </div>
        )}

        {/* 表格 */}
        <Card pad="0" className="flex min-h-0 flex-1 flex-col overflow-hidden">
          <div className="g-scroll min-h-0 flex-1 overflow-auto">
            <table className="w-full min-w-[1024px] table-fixed border-collapse text-left">
              <colgroup>
                <col style={{ width: "3.5rem" }} />
                <col style={{ width: "24%" }} />
                <col style={{ width: "16%" }} />
                <col style={{ width: "21%" }} />
                <col style={{ width: "11%" }} />
              </colgroup>
              <thead className="g-thead sticky top-0 z-10">
                <tr>
                  <th className="px-4 py-3">
                    <CheckBox checked={allChecked} onChange={toggleAll} disabled={rows.length === 0} />
                  </th>
                  <th className="px-3 py-3">
                    <SortHeader label="岗位 / 薪资" field="salary" sort={sort} onToggle={toggleSort} />
                  </th>
                  <th className="t-eyebrow px-3 py-3" style={{ letterSpacing: "0.05em" }}>公司</th>
                  <th className="t-eyebrow px-3 py-3" style={{ letterSpacing: "0.05em" }}>判断描述</th>
                  <th className="px-3 py-3">
                    <SortHeader label="匹配度" field="score" sort={sort} onToggle={toggleSort} />
                  </th>
                  <th className="t-eyebrow px-3 py-3 text-center" style={{ letterSpacing: "0.05em" }}>操作</th>
                </tr>
              </thead>
              <tbody>
                {loading && rows.length === 0 && (
                  <tr>
                    <td colSpan={6} className="px-4 py-20 text-center">
                      <Spinner size={18} />
                    </td>
                  </tr>
                )}
                {!loading && rows.length === 0 && (
                  <tr>
                    <td colSpan={6}>
                      <EmptyState
                        icon={<Inbox size={20} />}
                        title="暂无岗位数据"
                        hint="请先在工作台开始自动投递，采集到的岗位会出现在这里。"
                      />
                    </td>
                  </tr>
                )}
                {rows.map((row) => (
                  <tr key={row.uid} className="g-row align-top" data-selected={selected.has(row.uid)}>
                    <td className="px-4 py-3.5">
                      <CheckBox checked={selected.has(row.uid)} onChange={() => toggleOne(row.uid)} />
                    </td>
                    <td className="px-3 py-3.5">
                      <button
                        type="button"
                        className="t-label block max-w-full truncate text-left font-semibold"
                        style={{ color: "var(--ink)" }}
                        onClick={() => setDetail(row)}
                      >
                        {row.title || "（无标题）"}
                      </button>
                      <div className="mt-1.5 flex min-w-0 items-center gap-2">
                        <span className="g-salary t-num">{row.salary || "薪资未知"}</span>
                        {row.location && (
                          <span className="t-micro min-w-0 truncate" style={{ color: "var(--faint)" }}>
                            {row.location}
                          </span>
                        )}
                      </div>
                    </td>
                    <td className="px-3 py-3.5">
                      <div className="t-meta truncate" style={{ color: "var(--text)" }}>{row.company || "—"}</div>
                      {row.hrName && (
                        <div className="t-micro mt-1 truncate" style={{ color: "var(--faint)" }}>
                          {row.hrName}
                          {row.hrTitle ? ` · ${row.hrTitle}` : ""}
                        </div>
                      )}
                    </td>
                    <td className="px-3 py-3.5">
                      <div className="flex flex-wrap items-center gap-1.5">
                        <Badge color={statusTone(row)}>{row.result}</Badge>
                        <RiskBadgeMini risk={row.risk} label={row.riskLabel} />
                        {row.manualPending && <Badge color="orange">待手动投递</Badge>}
                      </div>
                      {row.reason && (
                        <div className="t-micro mt-1.5 line-clamp-2" style={{ color: "var(--muted)" }} title={row.reason}>
                          {row.reason}
                        </div>
                      )}
                    </td>
                    <td className="px-3 py-3.5">
                      {row.matchHigh ? (
                        <Badge color="green">{row.matchStatus}</Badge>
                      ) : row.matchStatus === "未分析" ? (
                        <span className="t-micro" style={{ color: "var(--faint)" }}>未分析</span>
                      ) : (
                        <span className="t-num t-meta" style={{ color: "var(--ink)" }}>{row.matchStatus}</span>
                      )}
                    </td>
                    <td className="px-3 py-3.5">
                      <div className="flex items-center justify-center gap-1">
                        <IconButton title="查看详情" onClick={() => setDetail(row)}>
                          <Eye size={15} />
                        </IconButton>
                        <IconButton title="查询企业风险" onClick={() => setCompanyFor(row)}>
                          <ShieldAlert size={15} />
                        </IconButton>
                        <IconButton title="定制简历" onClick={() => setTailorFor(row)}>
                          <Sparkles size={15} />
                        </IconButton>
                        <IconButton title="打开 BOSS 页面" onClick={() => openLink(row.link)} disabled={!row.link}>
                          <ExternalLink size={15} />
                        </IconButton>
                        <IconButton
                          title={row.applied ? "已记录为已投递" : "标记为已投递：手动投过的岗位点这里记一笔，会计入统计"}
                          onClick={() => markApplied(row.uid)}
                          disabled={row.applied}
                        >
                          <CheckCheck size={15} />
                        </IconButton>
                        <IconButton title="删除岗位" onClick={() => setToDelete([row])}>
                          <Trash2 size={15} />
                        </IconButton>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          {/* 分页 */}
          <div className="flex shrink-0 items-center justify-between gap-3 px-4 py-3" style={{ borderTop: "1px solid var(--line)" }}>
            <span className="t-meta" style={{ color: "var(--muted)" }}>
              第 <span className="t-num" style={{ color: "var(--ink)" }}>{data?.page ?? 1}</span> / {totalPages} 页
            </span>
            <div className="flex items-center gap-2">
              <GhostButton disabled={page <= 1} onClick={() => setPage((p) => Math.max(1, p - 1))}>
                <ChevronLeft size={14} /> 上一页
              </GhostButton>
              <GhostButton disabled={page >= totalPages} onClick={() => setPage((p) => Math.min(totalPages, p + 1))}>
                下一页 <ChevronRight size={14} />
              </GhostButton>
            </div>
          </div>
        </Card>
      </div>

      {/* 详情 */}
      <SlideOver
        open={!!detail}
        onOpenChange={(v) => !v && setDetail(null)}
        title={detail?.title || "岗位详情"}
        description={
          detail
            ? [detail.company, detail.salary, detail.location].filter(Boolean).join(" · ")
            : undefined
        }
        footer={
          detail && (
            <>
              <SoftButton onClick={() => setCompanyFor(detail)}>
                <ShieldAlert size={13} /> 查询企业风险
              </SoftButton>
              <SoftButton onClick={() => setTailorFor(detail)}>
                <Sparkles size={13} /> 定制简历
              </SoftButton>
              <SoftButton onClick={() => openLink(detail.link)} disabled={!detail.link}>
                <ExternalLink size={13} /> 打开 BOSS 页面
              </SoftButton>
              <GhostButton
                onClick={() => markApplied(detail.uid)}
                disabled={detail.applied}
              >
                <CheckCheck size={13} /> 标记为已投递
              </GhostButton>
              <GhostButton danger className="ml-auto" onClick={() => setToDelete([detail])}>
                <Trash2 size={13} /> 删除岗位
              </GhostButton>
            </>
          )
        }
      >
        {detail && (
          <div className="flex flex-col gap-8">
            <section className="grid gap-5 sm:grid-cols-3">
              <Field label="投递状态" value={detail.result} />
              <Field label="匹配度" value={detail.matchStatus} />
              <Field label="入库时间" value={detail.createdAt} />
            </section>

            {detail.scoreDetail.length > 0 && (
              <section>
                <span className="t-eyebrow">评分明细</span>
                <div className="mt-3 flex flex-col gap-3.5">
                  {detail.scoreDetail.map((d, i) => (
                    <div key={i}>
                      <div className="mb-1.5 flex items-center justify-between">
                        <span className="t-meta font-semibold" style={{ color: "var(--ink)" }}>{d.label}</span>
                        <span className="t-num t-meta" style={{ color: "var(--muted)" }}>
                          {d.score}
                          <span style={{ color: "var(--faint)" }}> / {d.max}</span>
                        </span>
                      </div>
                      <div className="g-track" style={{ height: 6 }}>
                        <div className="g-fill" style={{ width: `${Math.min(100, (d.score / (d.max || 100)) * 100)}%` }} />
                      </div>
                      {d.evidence && (
                        <p className="t-micro mt-1.5" style={{ color: "var(--muted)" }}>{d.evidence}</p>
                      )}
                    </div>
                  ))}
                </div>
              </section>
            )}

            {detail.hardGaps.length > 0 && (
              <section>
                <span className="t-eyebrow" style={{ color: "var(--bad)" }}>缺失要求</span>
                <ul className="mt-3 flex flex-col gap-2">
                  {detail.hardGaps.map((gap, i) => (
                    <li key={i} className="t-meta flex items-start gap-2" style={{ color: "var(--text)" }}>
                      <span className="mt-1.5 shrink-0" style={{ width: 5, height: 5, borderRadius: 999, background: "var(--bad)" }} />
                      {gap}
                    </li>
                  ))}
                </ul>
              </section>
            )}

            <section className="grid gap-5 sm:grid-cols-2">
              <Field label="工作经验" value={detail.experience || "不限"} />
              <Field label="学历要求" value={detail.education || "不限"} />
            </section>

            {detail.address && (
              <section>
                <span className="t-eyebrow">工作地址</span>
                <p className="t-meta mt-2" style={{ color: "var(--text)" }}>{detail.address}</p>
              </section>
            )}

            <section>
              <span className="t-eyebrow">岗位描述</span>
              <p className="t-body mt-2.5 whitespace-pre-wrap" style={{ color: "var(--text)" }}>
                {detail.description || "（没有抓取到岗位描述）"}
              </p>
            </section>
          </div>
        )}
      </SlideOver>

      <CompanyDialog
        open={!!companyFor}
        onOpenChange={(v) => !v && setCompanyFor(null)}
        uid={companyFor?.uid ?? ""}
        company={companyFor?.company ?? ""}
      />

      <TailorDialog
        open={!!tailorFor}
        onOpenChange={(v) => !v && setTailorFor(null)}
        uid={tailorFor?.uid ?? ""}
      />

      <Modal
        open={!!toDelete}
        onOpenChange={(v) => !v && setToDelete(null)}
        title="删除岗位"
        description={
          toDelete && toDelete.length === 1
            ? `确定删除「${toDelete[0].title}」吗？删除后无法恢复。`
            : `确定删除选中的 ${toDelete?.length ?? 0} 个岗位吗？删除后无法恢复。`
        }
      >
        <div className="flex justify-end gap-2.5">
          <GhostButton onClick={() => setToDelete(null)}>取消</GhostButton>
          <PrimaryButton tone="danger" onClick={confirmDelete}>
            <Trash2 size={13} /> 删除
          </PrimaryButton>
        </div>
      </Modal>
    </PageShell>
  );
}

function Field({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <span className="t-eyebrow">{label}</span>
      <p className="t-label mt-1.5 font-semibold" style={{ color: "var(--ink)" }}>{value}</p>
    </div>
  );
}
