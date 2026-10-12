/** 网申助手：BOSS 上没有 HR 的岗位，去企业官网的表单里补一份。 */

import {
  CheckCircle2,
  ExternalLink,
  FileInput,
  Plus,
  RefreshCw,
  Search,
  Trash2,
} from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { toast } from "sonner";
import { api } from "../api";
import { ApplyDialog, type ApplyTask, statusTone } from "../components/ApplyDialog";
import { PageShell } from "../Shell";
import {
  Badge,
  Card,
  Dropdown,
  EmptyState,
  GhostButton,
  Modal,
  PrimaryButton,
  SectionTitle,
  SoftButton,
  Spinner,
  useDebouncedValue,
} from "../ui";

/** 筛选下拉的显示文字 → 后端状态值。 */
const STATUS_FILTERS: Record<string, string> = {
  全部状态: "",
  待网申: "pending",
  已填表: "filled",
  已提交: "submitted",
  需人工: "failed",
};

interface JobOption {
  uid: string;
  title: string;
  company: string;
  salary: string;
  location: string;
}

export default function Apply() {
  const [tasks, setTasks] = useState<ApplyTask[]>([]);
  const [loading, setLoading] = useState(true);
  const [filter, setFilter] = useState("全部状态");
  const [forUid, setForUid] = useState("");
  const [picking, setPicking] = useState(false);
  const [busy, setBusy] = useState("");

  const load = useCallback(async () => {
    setLoading(true);
    try {
      setTasks(await api.get<ApplyTask[]>("/api/apply/tasks"));
    } catch (err) {
      toast.error((err as Error).message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const status = STATUS_FILTERS[filter] ?? "";
  const rows = status ? tasks.filter((t) => t.status === status) : tasks;

  async function submit(task: ApplyTask) {
    setBusy(task.uid);
    try {
      await api.post(`/api/apply/task/${encodeURIComponent(task.uid)}/submit`);
      toast.success("已记为已提交");
      void load();
    } catch (err) {
      toast.error((err as Error).message);
    } finally {
      setBusy("");
    }
  }

  async function remove(task: ApplyTask) {
    setBusy(task.uid);
    try {
      await api.del(`/api/apply/task/${encodeURIComponent(task.uid)}`);
      toast.success("已删除网申任务");
      void load();
    } catch (err) {
      toast.error((err as Error).message);
    } finally {
      setBusy("");
    }
  }

  return (
    <PageShell active="/apply" fill>
      <div className="flex min-h-0 flex-1 flex-col gap-4 pb-6 pt-6">
        <div className="flex flex-wrap items-center gap-2.5">
          <Dropdown
            value={filter}
            options={Object.keys(STATUS_FILTERS)}
            onChange={setFilter}
            width={132}
          />
          <GhostButton onClick={() => void load()} disabled={loading}>
            <RefreshCw size={13} /> 刷新
          </GhostButton>
          <PrimaryButton onClick={() => setPicking(true)}>
            <Plus size={13} /> 新建网申任务
          </PrimaryButton>
          <span className="ml-auto t-meta" style={{ color: "var(--muted)" }}>
            共 <span className="t-num" style={{ color: "var(--ink)", fontWeight: 600 }}>{tasks.length}</span>{" "}
            条任务
          </span>
        </div>

        <Card pad="1.1rem 1.3rem">
          <SectionTitle
            title="网申助手"
            hint="不少公司在 BOSS 上不挂 HR，简历发不过去，只能去企业官网的投递表单填一遍。这里负责把表单扫成对照表、代填内容，提交按钮永远留给你自己点。"
          />
        </Card>

        <Card pad="0" className="flex min-h-0 flex-1 flex-col overflow-hidden">
          <div className="g-scroll min-h-0 flex-1 overflow-y-auto">
            {loading && rows.length === 0 && (
              <div className="flex items-center justify-center py-20">
                <Spinner size={18} />
              </div>
            )}
            {!loading && rows.length === 0 && (
              <EmptyState
                icon={<FileInput size={20} />}
                title={tasks.length === 0 ? "还没有网申任务" : "这个状态下没有任务"}
                hint="点「新建网申任务」挑一个岗位，把企业官网的表单页地址填进去即可。"
                action={
                  tasks.length === 0 ? (
                    <PrimaryButton onClick={() => setPicking(true)}>
                      <Plus size={13} /> 新建网申任务
                    </PrimaryButton>
                  ) : undefined
                }
              />
            )}
            {rows.map((task) => (
              <div
                key={task.uid}
                className="flex flex-wrap items-center gap-x-4 gap-y-2 px-4 py-3.5"
                style={{ borderBottom: "1px solid var(--line)" }}
              >
                <div className="min-w-0 flex-1">
                  <button
                    type="button"
                    className="t-label block max-w-full truncate text-left font-semibold"
                    style={{ color: "var(--ink)" }}
                    onClick={() => setForUid(task.uid)}
                  >
                    {task.title || "（无标题）"}
                  </button>
                  <div className="mt-1.5 flex min-w-0 flex-wrap items-center gap-2">
                    <span className="g-salary t-num">{task.salary || "薪资未知"}</span>
                    <span className="t-micro min-w-0 truncate" style={{ color: "var(--muted)" }}>
                      {[task.company, task.location].filter(Boolean).join(" · ") || "—"}
                    </span>
                  </div>
                  {task.url && (
                    <div className="t-micro mt-1 truncate" style={{ color: "var(--faint)" }} title={task.url}>
                      {task.url}
                    </div>
                  )}
                </div>

                <div className="flex shrink-0 items-center gap-2">
                  <Badge color={statusTone(task.status)}>{task.statusLabel}</Badge>
                  {task.note && (
                    <span
                      className="t-micro hidden max-w-[18ch] truncate lg:inline"
                      style={{ color: "var(--muted)" }}
                      title={task.note}
                    >
                      {task.note}
                    </span>
                  )}
                </div>

                <div className="flex shrink-0 items-center gap-1.5">
                  <SoftButton onClick={() => setForUid(task.uid)}>
                    <FileInput size={13} /> 填表
                  </SoftButton>
                  <GhostButton
                    disabled={busy === task.uid || task.status === "submitted"}
                    title="在浏览器里提交完之后，回来点这里记一笔"
                    onClick={() => submit(task)}
                  >
                    <CheckCircle2 size={13} /> 已提交
                  </GhostButton>
                  {task.url && (
                    <GhostButton
                      title="在浏览器里打开这个网申页"
                      onClick={() =>
                        api.post("/api/open-link", { url: task.url }).catch((err) => {
                          toast.error((err as Error).message);
                        })
                      }
                    >
                      <ExternalLink size={13} />
                    </GhostButton>
                  )}
                  <GhostButton danger title="删除这条网申任务" onClick={() => remove(task)}>
                    <Trash2 size={13} />
                  </GhostButton>
                </div>
              </div>
            ))}
          </div>
        </Card>
      </div>

      <JobPicker
        open={picking}
        onOpenChange={setPicking}
        onPick={(uid) => {
          setPicking(false);
          setForUid(uid);
        }}
      />

      <ApplyDialog
        open={!!forUid}
        onOpenChange={(v) => !v && setForUid("")}
        uid={forUid}
        onChanged={() => void load()}
      />
    </PageShell>
  );
}

/** 挑岗位：搜索已有的岗位库，选中后进网申对话框填地址。 */
function JobPicker({
  open,
  onOpenChange,
  onPick,
}: {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  onPick: (uid: string) => void;
}) {
  const [search, setSearch] = useState("");
  const debounced = useDebouncedValue(search);
  const [rows, setRows] = useState<JobOption[]>([]);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (!open) return;
    setLoading(true);
    const qs = new URLSearchParams({
      search: debounced,
      analysis: "全部分析状态",
      suitable: "全部",
      page: "1",
      page_size: "20",
    });
    api
      .get<{ rows: JobOption[] }>(`/api/jobs?${qs.toString()}`)
      .then((res) => setRows(res.rows ?? []))
      .catch((err) => toast.error((err as Error).message))
      .finally(() => setLoading(false));
  }, [open, debounced]);

  return (
    <Modal
      open={open}
      onOpenChange={onOpenChange}
      title="选择要网申的岗位"
      description="从岗位库里挑一个，下一步填企业官网的投递表单地址。"
      width={560}
    >
      <div className="flex flex-col gap-3">
        <div className="flex items-center gap-2">
          <Search size={14} style={{ color: "var(--faint)" }} />
          <input
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="搜索岗位或公司"
            className="g-field g-input w-full outline-none"
          />
        </div>
        <div className="g-scroll flex max-h-[46vh] flex-col overflow-y-auto">
          {loading && rows.length === 0 && (
            <div className="flex justify-center py-10">
              <Spinner size={16} />
            </div>
          )}
          {!loading && rows.length === 0 && (
            <span className="t-meta py-8 text-center" style={{ color: "var(--muted)" }}>
              没有匹配的岗位
            </span>
          )}
          {rows.map((job) => (
            <button
              key={job.uid}
              type="button"
              className="g-menu-item items-start"
              onClick={() => onPick(job.uid)}
            >
              <span className="flex min-w-0 flex-col text-left">
                <span className="t-label truncate font-semibold" style={{ color: "var(--ink)" }}>
                  {job.title || "（无标题）"}
                </span>
                <span className="t-micro mt-0.5 truncate" style={{ color: "var(--muted)" }}>
                  {[job.company, job.salary, job.location].filter(Boolean).join(" · ")}
                </span>
              </span>
            </button>
          ))}
        </div>
      </div>
    </Modal>
  );
}
