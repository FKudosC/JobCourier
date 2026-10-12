/**
 * 网申助手：把企业官网的投递表单扫成一张字段对照表，确认后由程序代填。
 *
 * 一条硬规矩：**这里只填，不提交**。官网表单提交后撤不回来，还常带验证码、
 * 登录态这些只有人能处理的门槛，所以最后一步永远留给用户在浏览器里自己点。
 */

import { CheckCircle2, Eye, RefreshCw, Sparkles, Trash2 } from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
import { toast } from "sonner";
import { api } from "../api";
import {
  Badge,
  Dropdown,
  GhostButton,
  PrimaryButton,
  SlideOver,
  SoftButton,
  Spinner,
  TextInput,
} from "../ui";

/** 后端 /api/apply/task* 返回的字段对照表一行。 */
export interface ApplyField {
  index: number;
  frame: number;
  tag: string;
  type: string;
  label: string;
  name: string;
  placeholder: string;
  required: boolean;
  options: string[];
  value: string;
  selector: string;
  fallback: string;
  key: string;
  filled: string;
  source: string;
  confidence: number;
  ok: boolean;
  note: string;
}

/** 一条网申任务。 */
export interface ApplyTask {
  uid: string;
  jobUid: string;
  platform: string;
  title: string;
  company: string;
  salary: string;
  location: string;
  url: string;
  domain: string;
  status: string;
  statusLabel: string;
  note: string;
  fields: ApplyField[];
  submittedAt: string;
  createdAt: string;
  updatedAt: string;
}

/** 网申资料（配置中心「网申资料」页存的那份）。 */
export interface ApplyProfileBundle {
  labels: Record<string, string>;
  values: Record<string, string>;
  extra: Record<string, string>;
}

/** 状态 → 徽章配色。 */
export function statusTone(status: string): "gray" | "green" | "red" | "blue" | "orange" {
  if (status === "submitted") return "green";
  if (status === "filled") return "blue";
  if (status === "failed") return "red";
  return "orange";
}

/** 控件类型 → 人话。 */
function typeLabel(field: ApplyField): string {
  if (field.type === "file") return "上传文件";
  if (field.type === "select") return "下拉选择";
  if (field.type === "radio") return "单选";
  if (field.type === "checkbox") return "多选";
  if (field.type === "textarea") return "多行文本";
  return "文本";
}

/** 置信度 → 高 / 中 / 低。 */
function confidenceLabel(value: number): string {
  if (value >= 0.9) return "高";
  if (value >= 0.6) return "中";
  if (value > 0) return "低";
  return "";
}

/** 来源 → 徽章配色。 */
function sourceTone(source: string): "gray" | "green" | "blue" | "orange" | "teal" {
  if (source === "资料" || source === "简历") return "green";
  if (source === "沿用") return "blue";
  if (source === "手动") return "teal";
  if (source === "AI") return "orange";
  return "gray";
}

/** 前端改过的行：只把动过的 index 发回去，后端据此覆盖对照表。 */
interface FieldEdit {
  key?: string;
  filled?: string;
}

interface FillOutcome {
  ok: boolean;
  filled: number;
  skipped: number;
  failed: number;
}

export function ApplyDialog({
  open,
  onOpenChange,
  uid,
  initialUrl = "",
  onChanged,
}: {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  /** 已有任务的 uid；为空表示新建一条（用 ``initialUrl`` 开页）。 */
  uid: string;
  /** 新建任务时要打开的企业官网地址。 */
  initialUrl?: string;
  /** 任务状态有变化时通知外面刷新列表。 */
  onChanged?: () => void;
}) {
  const [task, setTask] = useState<ApplyTask | null>(null);
  const [gate, setGate] = useState("");
  const [profile, setProfile] = useState<ApplyProfileBundle | null>(null);
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [needUrl, setNeedUrl] = useState(false);
  const [urlDraft, setUrlDraft] = useState("");
  const [edits, setEdits] = useState<Record<number, FieldEdit>>({});

  const load = useCallback(
    async (target: string, forUid: string) => {
      setLoading(true);
      setError("");
      try {
        const res = await api.post<{ task: ApplyTask; gate: string }>("/api/apply/task", {
          job_uid: forUid,
          url: target,
        });
        setTask(res.task);
        setGate(res.gate || "");
        setEdits({});
        setNeedUrl(false);
      } catch (err) {
        const message = (err as Error).message;
        setError(message);
        // 后端说「请先填写网申地址」时，直接把输入框亮出来
        setNeedUrl(message.includes("网申地址"));
      } finally {
        setLoading(false);
      }
    },
    [],
  );

  useEffect(() => {
    if (!open || (!uid && !initialUrl)) return;
    setTask(null);
    setGate("");
    setEdits({});
    setUrlDraft("");
    void load(initialUrl, uid);
    api
      .get<ApplyProfileBundle>("/api/config/apply-profile")
      .then(setProfile)
      .catch(() => setProfile(null));
  }, [open, uid, initialUrl, load]);

  const labels = profile?.labels ?? {};
  const labelOptions = useMemo(() => ["不填", ...Object.values(labels)], [labels]);
  const labelToKey = useMemo(
    () => Object.fromEntries(Object.entries(labels).map(([key, label]) => [label, key])),
    [labels],
  );

  /** 任务建好之后以任务自己的 uid 为准（新建时 uid 是从后端回来的）。 */
  const activeUid = task?.uid || uid;

  function patchField(index: number, patch: FieldEdit) {
    setEdits((prev) => ({ ...prev, [index]: { ...prev[index], ...patch } }));
  }

  function pickKey(field: ApplyField, label: string) {
    const key = label === "不填" ? "" : (labelToKey[label] ?? "");
    const value = key ? (profile?.values[key] ?? "") : "";
    // 换资料时同步换成那份资料的值，省得用户再手打一遍
    patchField(field.index, { key, filled: value });
  }

  async function call(what: string, key: string, fn: () => Promise<void>) {
    setBusy(key);
    try {
      await fn();
    } catch (err) {
      toast.error((err as Error).message || `${what}失败`);
    } finally {
      setBusy("");
    }
  }

  async function scan() {
    await call("重扫", "scan", async () => {
      const res = await api.post<{ task: ApplyTask; gate: string }>(
        `/api/apply/task/${encodeURIComponent(activeUid)}/scan`,
      );
      setTask(res.task);
      setGate(res.gate || "");
      setEdits({});
      toast.success(res.gate ? "已重扫：页面上有需要人处理的东西" : "已按当前页面重扫一遍");
    });
  }

  async function fill() {
    await call("填表", "fill", async () => {
      const rows = Object.entries(edits).map(([index, patch]) => ({
        index: Number(index),
        ...patch,
      }));
      const res = await api.post<{ task: ApplyTask; outcome: FillOutcome }>(
        `/api/apply/task/${encodeURIComponent(activeUid)}/fill`,
        { fields: rows },
      );
      setTask(res.task);
      setEdits({});
      const { filled, skipped, failed } = res.outcome;
      if (failed > 0) {
        toast.warning(`已填 ${filled} 项，${failed} 项需要手动处理`);
      } else {
        toast.success(
          `已填 ${filled} 项${skipped ? `，跳过 ${skipped} 项` : ""}；请在浏览器里核对后自己点提交`,
        );
      }
      onChanged?.();
    });
  }

  async function submit() {
    await call("标记提交", "submit", async () => {
      await api.post(`/api/apply/task/${encodeURIComponent(activeUid)}/submit`);
      toast.success("已记为已提交，这个岗位的标签页也关掉了");
      onChanged?.();
      onOpenChange(false);
    });
  }

  async function remove() {
    await call("删除", "delete", async () => {
      await api.del(`/api/apply/task/${encodeURIComponent(activeUid)}`);
      toast.success("已删除网申任务");
      onChanged?.();
      onOpenChange(false);
    });
  }

  const fields = task?.fields ?? [];
  const matched = fields.filter((f) => f.key && f.filled).length;

  return (
    <SlideOver
      open={open}
      onOpenChange={onOpenChange}
      title={task?.title || "网申助手"}
      description={
        task
          ? [task.company, task.salary, task.location].filter(Boolean).join(" · ") ||
            task.url ||
            undefined
          : "把企业官网的投递表单扫成一张对照表，确认后由程序代填。"
      }
      width={780}
      footer={
        <>
          <PrimaryButton loading={busy === "fill"} onClick={fill} disabled={!fields.length}>
            <Sparkles size={13} /> 填入浏览器
          </PrimaryButton>
          <SoftButton loading={busy === "scan"} onClick={scan} disabled={!task}>
            <RefreshCw size={13} /> 重新扫描
          </SoftButton>
          <GhostButton
            onClick={() =>
              call("调出浏览器", "show", async () => {
                await api.post("/api/browser/show");
              })
            }
          >
            <Eye size={13} /> 查看浏览器
          </GhostButton>
          <GhostButton onClick={submit} disabled={!task}>
            <CheckCircle2 size={13} /> 标记已提交
          </GhostButton>
          <GhostButton danger className="ml-auto" onClick={remove} disabled={!task}>
            <Trash2 size={13} /> 删除任务
          </GhostButton>
        </>
      }
    >
      {loading && (
        <div className="flex flex-col items-center gap-3 py-20">
          <Spinner size={18} />
          <span className="t-meta" style={{ color: "var(--muted)" }}>
            正在打开企业官网并扫描表单，慢的话要十几秒…
          </span>
        </div>
      )}

      {!loading && needUrl && (
        <div className="flex flex-col gap-4">
          <div
            className="px-4 py-3.5"
            style={{
              borderRadius: "var(--r-md)",
              background: "var(--accent-tint)",
              border: "1px solid var(--accent-line)",
            }}
          >
            <div className="t-body font-semibold" style={{ color: "var(--accent-deep)" }}>
              先告诉我这个岗位去哪儿网申
            </div>
            <p className="t-meta mt-1.5" style={{ color: "var(--text)" }}>
              很多公司在 BOSS 上不挂 HR，简历发不过去，只能去企业官网的招聘页填一遍。
              把那个表单页的网址粘进来，地址会跟这个岗位一起记住，下次不用再填。
            </p>
          </div>
          <TextInput
            value={urlDraft}
            onChange={setUrlDraft}
            placeholder="https://公司官网/招聘/投递表单"
          />
          <div className="flex items-center gap-2.5">
            <PrimaryButton loading={loading} onClick={() => load(urlDraft.trim(), uid)}>
              打开并扫描表单
            </PrimaryButton>
            <span className="t-micro" style={{ color: "var(--faint)" }}>
              只支持 http:// 或 https:// 开头的地址
            </span>
          </div>
        </div>
      )}

      {!loading && !needUrl && error && (
        <div
          className="px-4 py-3.5"
          style={{
            borderRadius: "var(--r-md)",
            background: "var(--bad-soft)",
            border: "1px solid var(--bad-line)",
          }}
        >
          <span className="t-meta" style={{ color: "var(--bad)" }}>
            {error}
          </span>
        </div>
      )}

      {!loading && task && (
        <div className="flex flex-col gap-5">
          <div className="flex flex-wrap items-center gap-2">
            <Badge color={statusTone(task.status)}>{task.statusLabel}</Badge>
            <Badge color="gray">{task.domain || "未填域名"}</Badge>
            {fields.length > 0 && (
              <span className="t-micro" style={{ color: "var(--faint)" }}>
                表单 {fields.length} 项 · 已对上 {matched} 项
              </span>
            )}
            <GhostButton
              variant="plain"
              className="ml-auto"
              onClick={() => {
                setUrlDraft(task.url);
                setNeedUrl(true);
              }}
            >
              改网申地址
            </GhostButton>
          </div>

          {gate && (
            <div
              className="px-4 py-3.5"
              style={{
                borderRadius: "var(--r-md)",
                background: "var(--warn-soft)",
                border: "1px solid var(--warn-line)",
              }}
            >
              <div className="t-body font-semibold" style={{ color: "var(--warn)" }}>
                {gate}
              </div>
              <p className="t-meta mt-1.5" style={{ color: "var(--text)" }}>
                浏览器窗口已经调到屏幕前，手动过一下验证码，再点「重新扫描」。
              </p>
            </div>
          )}

          {task.note && !gate && (
            <p className="t-micro" style={{ color: "var(--muted)" }}>
              上次：{task.note}
            </p>
          )}

          {fields.length === 0 ? (
            <div className="flex flex-col items-center gap-3 py-16 text-center">
              <span className="t-body font-semibold" style={{ color: "var(--ink)" }}>
                这一页没扫到表单控件
              </span>
              <span className="t-meta" style={{ color: "var(--muted)", maxWidth: "42ch" }}>
                可能是投递入口还没展开，或者页面要登录后才显示。在浏览器里点开投递页，
                再点「重新扫描」。
              </span>
            </div>
          ) : (
            <>
              <p className="t-micro" style={{ color: "var(--faint)" }}>
                左边选这栏对应哪条资料，右边是实际填进去的内容；改过的行会记成「手动」，下次优先用你的选择。
              </p>
              <div className="flex flex-col">
                {fields.map((field, i) => {
                  const patch = edits[field.index] ?? {};
                  const key = patch.key ?? field.key;
                  const value = patch.filled ?? field.filled;
                  const changed = patch.key !== undefined || patch.filled !== undefined;
                  const currentLabel = key ? (labels[key] ?? key) : "不填";
                  const tone = changed ? "teal" : sourceTone(field.source);
                  return (
                    <div
                      key={field.index}
                      className="flex flex-col gap-2 py-3.5"
                      style={{ borderTop: i === 0 ? "none" : "1px solid var(--line)" }}
                    >
                      <div className="flex flex-wrap items-center gap-2">
                        <span
                          className="t-label font-semibold"
                          style={{ color: "var(--ink)", maxWidth: "42ch" }}
                        >
                          {field.label || field.placeholder || field.name || "（未命名字段）"}
                        </span>
                        <Badge color="gray">{typeLabel(field)}</Badge>
                        {field.required && <Badge color="orange">必填</Badge>}
                        <span className="ml-auto flex items-center gap-1.5">
                          {key && <Badge color={tone}>{changed ? "手动" : field.source || "已对上"}</Badge>}
                          {!changed && confidenceLabel(field.confidence) && (
                            <span className="t-micro" style={{ color: "var(--faint)" }}>
                              可信度{confidenceLabel(field.confidence)}
                            </span>
                          )}
                        </span>
                      </div>

                      <div className="grid items-center gap-2.5 sm:grid-cols-[minmax(0,0.9fr)_minmax(0,1.1fr)]">
                        <Dropdown
                          value={currentLabel}
                          options={
                            labelOptions.includes(currentLabel)
                              ? labelOptions
                              : [currentLabel, ...labelOptions]
                          }
                          onChange={(v) => pickKey(field, v)}
                        />
                        {field.type === "file" ? (
                          <span className="t-micro" style={{ color: "var(--muted)" }}>
                            自动上传「简历配置」里传过的简历原件
                          </span>
                        ) : field.type === "radio" || field.type === "checkbox" ? (
                          <span className="t-micro" style={{ color: "var(--muted)" }}>
                            {key ? "选中这条资料时自动勾上" : "选一条资料才会勾"}
                          </span>
                        ) : field.options.length > 0 ? (
                          <Dropdown
                            value={value}
                            options={
                              value && !field.options.includes(value)
                                ? [value, ...field.options]
                                : field.options
                            }
                            placeholder="选择这一栏要填的选项"
                            onChange={(v) => patchField(field.index, { filled: v })}
                          />
                        ) : (
                          <TextInput
                            value={value}
                            onChange={(v) => patchField(field.index, { filled: v })}
                            placeholder="这一栏要填的内容"
                          />
                        )}
                      </div>

                      {!field.ok && field.note && (
                        <span className="t-micro" style={{ color: "var(--warn)" }}>
                          {field.note}
                        </span>
                      )}
                    </div>
                  );
                })}
              </div>
            </>
          )}

          <p className="t-micro" style={{ color: "var(--faint)" }}>
            程序只负责把值写进控件，不会点提交按钮。填完请在浏览器里核对一遍，
            自己点提交，再回来点「标记已提交」。
          </p>
        </div>
      )}
    </SlideOver>
  );
}
