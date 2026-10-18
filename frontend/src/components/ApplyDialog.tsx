/**
 * 网申助手：把企业官网的投递表单扫成一张字段对照表，确认后由程序代填。
 *
 * 一条硬规矩：**这里只填，不提交**。官网表单提交后撤不回来，还常带验证码、
 * 登录态这些只有人能处理的门槛，所以最后一步永远留给用户在浏览器里自己点。
 */

import {
  BookOpen,
  CheckCircle2,
  CornerDownRight,
  ListChecks,
  RefreshCw,
  RotateCcw,
  Sparkles,
  Trash2,
} from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
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
  /** 控件在页面里的位置 `[x, y, 宽, 高]`；取不到就是空数组。 */
  box: number[];
  key: string;
  filled: string;
  /** 填完之后从页面回读到的实际显示值；空串表示这一栏没回读出来。 */
  shown: string;
  /** 回读结论：页面实际值和准备填的值一致吗（没得比时为 true）。 */
  echo: boolean;
  source: string;
  confidence: number;
  ok: boolean;
  note: string;
  /** 级联下拉（省 / 市 / 区）的后续几级序号；空数组表示这一栏不是级联。 */
  group: number[];
  /** 级联的后续几级：只参与填写，不在对照表里单独占一行。 */
  hidden: boolean;
  /** 这一栏的值是模型按已有资料推出来的，不是资料原文；面板上单独一块请人核对。 */
  inferred: boolean;
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
  /** 填表跑完留下的交付清单：还差哪些栏，分三堆。 */
  handoff: ApplyHandoff;
  submittedAt: string;
  createdAt: string;
  updatedAt: string;
}

/** 交付清单里的一条：指向对照表里的某一行。 */
export interface HandoffItem {
  index: number;
  label: string;
  key: string;
  type: string;
  required: boolean;
}

/** 交付清单三堆：缺资料 / 待复核 / 待手动。 */
export interface ApplyHandoff {
  gaps: HandoffItem[];
  unsure: HandoffItem[];
  manual: HandoffItem[];
}

/** 学习到的一条资料：字段键 / 界面标签 / 学到的值。 */
export interface LearnedItem {
  key: string;
  label: string;
  value: string;
}

const NO_HANDOFF: ApplyHandoff = { gaps: [], unsure: [], manual: [] };

/** 三堆各自的说法：一句话讲清「这一堆该谁去办」。面板与本页汇总共用这一份。 */
export const HANDOFF_GROUPS = [
  {
    name: "gaps" as const,
    title: "缺资料",
    hint: "简历正文里没写到、AI 也推不出来；补进「简历配置」的简历，或在浏览器里填一次再点「学习手填内容」",
    color: "orange" as const,
  },
  {
    name: "manual" as const,
    title: "只能手填",
    hint: "AI 没认出这一栏在问什么，只能在浏览器里自己填；填完再点「学习手填内容」就记住了",
    color: "red" as const,
  },
  {
    name: "unsure" as const,
    title: "待复核",
    hint: "AI 拿不准、按已有资料填上的，扫一眼对不对",
    color: "blue" as const,
  },
];

/** 对照表每一行的锚点 id：交付清单点一条就滚到这儿。 */
function fieldAnchor(index: number): string {
  return `apply-field-${index}`;
}

/** 交付清单里的一堆：标题、该谁去办、具体是哪几栏。 */
function HandoffCard({
  title,
  hint,
  color,
  items,
  onJump,
}: {
  title: string;
  hint: string;
  color: "orange" | "red" | "blue";
  items: HandoffItem[];
  onJump: (index: number) => void;
}) {
  const edge =
    color === "red" ? "var(--bad-line)" : color === "orange" ? "var(--warn-line)" : "var(--accent-line)";
  return (
    <div
      className="flex min-w-0 flex-col gap-2 px-3.5 py-3"
      style={{
        borderRadius: "var(--r-md)",
        border: `1px solid ${edge}`,
        background: "var(--surface-2)",
      }}
    >
      <div className="flex flex-wrap items-center gap-2">
        <Badge color={color}>{title}</Badge>
        <span className="t-num" style={{ fontWeight: 600, color: "var(--ink)" }}>
          {items.length}
        </span>
      </div>
      <span className="t-micro" style={{ color: "var(--muted)" }}>
        {hint}
      </span>
      {items.length > 0 && (
        <div className="flex flex-col gap-1">
          {items.slice(0, 12).map((item) => (
            <button
              key={`${item.index}-${item.label}`}
              type="button"
              className="flex min-w-0 items-center gap-1.5 text-left"
              title="跳到下面这一行"
              onClick={() => onJump(item.index)}
            >
              <CornerDownRight size={12} style={{ color: "var(--faint)" }} />
              <span className="t-micro min-w-0 truncate" style={{ color: "var(--text)" }}>
                {item.label || item.key || "（未命名字段）"}
              </span>
            </button>
          ))}
          {items.length > 12 && (
            <span className="t-micro" style={{ color: "var(--faint)" }}>
              还有 {items.length - 12} 项…
            </span>
          )}
        </div>
      )}
    </div>
  );
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
function sourceTone(source: string): "gray" | "green" | "blue" | "orange" | "teal" | "ink" {
  if (source === "资料" || source === "简历") return "green";
  // 沿用与记忆都是「本机记下来的」，同一色系；推算单列一色，一眼看出不是资料原文。
  if (source === "沿用" || source === "记忆") return "blue";
  if (source === "手动") return "teal";
  if (source === "AI") return "orange";
  if (source === "推算") return "ink";
  return "gray";
}

/** 前端改过的行：只把动过的 index 发回去，后端据此覆盖对照表。 */
interface FieldEdit {
  filled?: string;
}

interface FillOutcome {
  ok: boolean;
  filled: number;
  skipped: number;
  failed: number;
}

/** 填表结果统一提示：填了几项、几项要手动处理。同步填完与后台填完共用。 */
function toastFill(outcome: FillOutcome) {
  const { filled, skipped, failed } = outcome;
  if (failed > 0) {
    toast.warning(`已填 ${filled} 项，${failed} 项需要手动处理`);
  } else {
    toast.success(
      `已填 ${filled} 项${skipped ? `，跳过 ${skipped} 项` : ""}；请在浏览器里核对后自己点提交`,
    );
  }
}

/** 验证码识别的结果（后端 /api/apply/task* 返回的 captcha 字段）。 */
export interface CaptchaOutcome {
  ok: boolean;
  detected: boolean;
  code: string;
  filled: boolean;
  message: string;
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
  const [loading, setLoading] = useState(false);
  /** 后台还有活（快扫后的补全 / 往浏览器里填）：表先给用户看，跑完自动刷新。空串表示没在跑。 */
  const [pending, setPending] = useState("");
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [needUrl, setNeedUrl] = useState(false);
  const [urlDraft, setUrlDraft] = useState("");
  const [edits, setEdits] = useState<Record<number, FieldEdit>>({});
  /** 已被用户点开「修改」的行：默认只读，动过手才露出输入控件。 */
  const [unlocked, setUnlocked] = useState<Record<number, boolean>>({});
  /** 刚从交付清单跳过来的那一行：高亮一下，让人一眼找到落点。 */
  const [flash, setFlash] = useState(-1);
  /** 「AI 推理」里被用户删掉的栏：不填进页面，也不回流进资料库。 */
  const [rejected, setRejected] = useState<number[]>([]);

  /** 交付清单点一条 → 滚到对照表里对应的那一行并闪一下。 */
  function jump(index: number) {
    setFlash(index);
    const el = document.getElementById(fieldAnchor(index));
    el?.scrollIntoView({ behavior: "smooth", block: "center" });
    window.setTimeout(() => setFlash(-1), 1800);
  }

  const load = useCallback(
    async (target: string, forUid: string) => {
      setLoading(true);
      setError("");
      try {
        const res = await api.post<{
          task: ApplyTask;
          gate: string;
          pending?: boolean;
          captcha?: CaptchaOutcome;
        }>("/api/apply/task", {
          job_uid: forUid,
          url: target,
        });
        setTask(res.task);
        setEdits({});
        setUnlocked({});
        setRejected([]);
        setNeedUrl(false);
        setPending(res.pending ? "正在补全验证码与 AI 兜底…" : "");
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

  /** 已经装载过哪一条任务（任务 uid，或新建时那个官网地址）；误关重开时靠它跳过重扫。 */
  const loadedRef = useRef("");

  useEffect(() => {
    if (!open) return;
    const key = uid || initialUrl;
    if (!key) return;
    // 误点到遮罩关掉、再点「填表」回来：同一条任务还在面板里，接着看就行。
    // 重新开页重扫既慢，又会把用户刚在对照表上改过的行冲掉。
    if (loadedRef.current === key) return;
    loadedRef.current = key;
    setTask(null);
    setEdits({});
    setUnlocked({});
    setRejected([]);
    setUrlDraft("");
    setPending("");
    void load(initialUrl, uid);
  }, [open, uid, initialUrl, load]);

  /** 任务建好之后以任务自己的 uid 为准（新建时 uid 是从后端回来的）。 */
  const activeUid = task?.uid || uid;

  // 后台补全（验证码识图 / AI 兜底）跑完，后端会广播一条 apply_done：
  // 把最新对照表换上去，顺便撤掉「补全中」的提示。
  useEffect(() => {
    function onEvent(ev: Event) {
      const msg = (ev as CustomEvent).detail as {
        type?: string;
        task?: ApplyTask;
        learned?: LearnedItem[];
        outcome?: FillOutcome;
      };
      if (msg?.type !== "apply_done") return;
      const next = msg.task;
      if (!next || next.uid !== activeUid) return;
      setTask(next);
      setEdits({});
      setUnlocked({});
      setRejected([]);
      setPending("");
      // 后台填表跑完了：这一次的成果直接报给用户（同步那条路走的是返回值）
      if (msg.outcome && typeof msg.outcome.filled === "number") {
        toastFill(msg.outcome);
      }
      // 用户绕过面板、直接在官网页面上补的栏，助手已经记进本机资料库了
      const learned = msg.learned ?? [];
      if (learned.length) {
        const names = learned.map((item) => item.label).join("、");
        toast.success(`已记住 ${learned.length} 项新资料：${names}`);
      }
    }
    window.addEventListener("gj:event", onEvent);
    return () => window.removeEventListener("gj:event", onEvent);
  }, [activeUid]);

  function patchField(index: number, patch: FieldEdit) {
    setEdits((prev) => ({ ...prev, [index]: { ...prev[index], ...patch } }));
  }

  function unlockField(index: number) {
    setUnlocked((prev) => ({ ...prev, [index]: true }));
  }

  /** 丢掉这一行的改动并收回输入控件，回到「程序填什么就显示什么」。 */
  function resetField(index: number) {
    setEdits((prev) => {
      const next = { ...prev };
      delete next[index];
      return next;
    });
    setUnlocked((prev) => ({ ...prev, [index]: false }));
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
      const res = await api.post<{
        task: ApplyTask;
        gate: string;
        pending?: boolean;
        captcha?: CaptchaOutcome;
      }>(
        `/api/apply/task/${encodeURIComponent(activeUid)}/scan`,
      );
      setTask(res.task);
      setEdits({});
      setUnlocked({});
      setRejected([]);
      setPending(res.pending ? "正在补全验证码与 AI 兜底…" : "");
      toast.success(res.pending ? "已扫到当前页面，正在补全…" : "已按当前页面重扫一遍");
    });
  }

  async function solveCaptcha() {
    await call("识别验证码", "captcha", async () => {
      const res = await api.post<{ captcha: CaptchaOutcome; task: ApplyTask }>(
        `/api/apply/task/${encodeURIComponent(activeUid)}/captcha`,
      );
      if (res.task) {
        setTask(res.task);
        setEdits({});
        setUnlocked({});
        setRejected([]);
      }
      const out = res.captcha;
      if (out.filled) {
        toast.success(`已识别并填入验证码：${out.code}`);
      } else {
        toast.warning(out.message || "没能自动识别，请手动填一下");
      }
    });
  }

  /** 学习手填内容：把浏览器里现在的表单和本机资料库比一遍，学新的、纠错的。 */
  async function learn() {
    await call("学习", "learn", async () => {
      const res = await api.post<{
        added: LearnedItem[];
        fixed: LearnedItem[];
        disabled?: boolean;
      }>(`/api/apply/task/${encodeURIComponent(activeUid)}/learn`);
      if (res.disabled) {
        toast.warning("「学习我在浏览器里手填的内容」已关闭，先去系统设置里打开");
        return;
      }
      const added = res.added ?? [];
      const fixed = res.fixed ?? [];
      if (!added.length && !fixed.length) {
        toast.message("没发现新东西：浏览器里填的都已在资料库里");
        return;
      }
      const parts: string[] = [];
      if (added.length) {
        parts.push(`新学 ${added.length} 项：${added.map((item) => item.label).join("、")}`);
      }
      if (fixed.length) {
        parts.push(`纠正 ${fixed.length} 项：${fixed.map((item) => item.label).join("、")}`);
      }
      toast.success(parts.join("；"));
      onChanged?.();
    });
  }

  async function fill() {
    await call("填表", "fill", async () => {
      const rows = Object.entries(edits).map(([index, patch]) => ({
        index: Number(index),
        ...patch,
      }));
      const res = await api.post<{
        task: ApplyTask;
        outcome: FillOutcome;
        pending?: boolean;
      }>(
        `/api/apply/task/${encodeURIComponent(activeUid)}/fill`,
        { fields: rows, rejected },
      );
      setTask(res.task);
      setEdits({});
      setUnlocked({});
      setRejected([]);
      // 后端改成后台填了：这条请求只负责开跑，填完会广播 apply_done 把结果送回来。
      // 填表链里 AI 兜底要几十秒，同步等必然被前端 120s 超时砍掉。
      if (res.pending) {
        setPending("正在往浏览器里填，填完会自动刷新…");
        toast.message("已开始往浏览器里填，填完这里会自动刷新");
        return;
      }
      toastFill(res.outcome);
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

  async function reopen() {
    await call("撤销提交", "reopen", async () => {
      const saved = await api.post<ApplyTask>(
        `/api/apply/task/${encodeURIComponent(activeUid)}/reopen`,
      );
      setTask(saved);
      toast.success("已撤销提交，可以接着改");
      onChanged?.();
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
  // 级联下拉的后续几级（省 / 市 / 区里除了第一级）不单独占一行，只跟着第一级一起填
  const rows = fields.filter((f) => !f.hidden);
  // 填表留下的交付清单：还差哪几栏、分别该谁去办
  const handoff = task?.handoff ?? NO_HANDOFF;
  const handoffTotal = HANDOFF_GROUPS.reduce((n, g) => n + handoff[g.name].length, 0);
  // 三组清单：程序已经对上的、拿不准要你确认的、还没认出来的。
  // 用户只需要看「要你确认」那一组，其余默认折叠成一行行结论。
  // 模型推出来的那几行不算「已对上」：它们单独占一块，删掉就不填、也不入库。
  const settled = (f: ApplyField) => !f.inferred && Boolean(f.key && f.filled && f.ok !== false);
  const inferred = rows.filter((f) => f.inferred);
  const liveInferred = inferred.filter((f) => !rejected.includes(f.index));
  const matched = rows.filter(settled).length;
  const unsure = rows.filter(
    (f) => (!settled(f) && !f.inferred) || (f.inferred && rejected.includes(f.index)),
  );

  /** 删掉一条推算：不填进页面，也不记进资料库；这个网站的这个栏位下次不再推。 */
  function rejectInferred(index: number) {
    setRejected((prev) => (prev.includes(index) ? prev : [...prev, index]));
  }

  /**
   * 渲染一行，三栏并排：
   *   左  = 官网表单要求填的栏（只读，用户不该改这个）
   *   中  = 程序准备填进去的值（对上的默认锁定，点「修改」才可改）
   *   右  = 填完后从页面回读到的实际值；和中栏对不上就把整行标黄
   */
  function renderRow(field: ApplyField, i: number) {
    const patch = edits[field.index] ?? {};
    const key = field.key;
    const value = patch.filled ?? field.filled;
    const changed = patch.filled !== undefined;
    const tone = changed ? "teal" : sourceTone(field.source);
    // 程序已经填出值的栏默认只读，避免误改；没填上或填失败的才直接把控件摆出来
    // 自己动过手的行（changed）一律不再收回只读：否则刚敲下第一个字，值从空变成非空，
    // 这一行会当场翻成只读展示，输入框被撤掉——只能再点一次「修改」才能接着敲。
    const locked =
      !unlocked[field.index] && !changed && Boolean(value) && field.ok !== false;
    // 回读到的页面实际值；本地改过值之后这次回读就过期了，不再拿出来比
    const actual = changed ? "" : field.shown;
    const mismatch = !changed && Boolean(actual) && !field.echo;
    // 刚从交付清单跳过来的那一行：给个底色，免得滚过来了还找不到
    const hot = flash === field.index;
    return (
      <div
        key={field.index}
        id={fieldAnchor(field.index)}
        className="flex flex-col gap-2 py-3.5"
        style={{
          borderTop: i === 0 ? "none" : "1px solid var(--line)",
          ...(mismatch
            ? {
                background: "var(--warn-soft)",
                borderLeft: "3px solid var(--warn)",
                paddingLeft: "0.6rem",
                paddingRight: "0.6rem",
                borderRadius: "var(--r-sm)",
              }
            : {}),
          ...(hot
            ? {
                background: "var(--accent-tint)",
                borderLeft: "3px solid var(--accent)",
                paddingLeft: "0.6rem",
                paddingRight: "0.6rem",
                borderRadius: "var(--r-sm)",
              }
            : {}),
        }}
      >
        <div className="grid items-start gap-2.5 sm:grid-cols-[minmax(0,0.85fr)_minmax(0,1.05fr)_minmax(0,1.05fr)]">
          {/* 左：网站表单要求填的栏，只读展示，不给改 */}
          <div className="flex min-w-0 flex-col gap-1.5">
            <span className="t-label font-semibold" style={{ color: "var(--ink)" }}>
              {field.label || field.placeholder || field.name || "（未命名字段）"}
            </span>
            <span className="flex flex-wrap items-center gap-1.5">
              <Badge color="gray">{typeLabel(field)}</Badge>
              {field.required && <Badge color="orange">必填</Badge>}
              {key && (
                <Badge color={tone}>{changed ? "手动" : field.source || "已对上"}</Badge>
              )}
              {!changed && confidenceLabel(field.confidence) && (
                <span className="t-micro" style={{ color: "var(--faint)" }}>
                  可信度{confidenceLabel(field.confidence)}
                </span>
              )}
            </span>
          </div>

          {/* 中：要填进去的内容，按需修改 */}
          <div className="flex min-w-0 flex-col gap-1.5">
            <span className="t-micro" style={{ color: "var(--faint)" }}>
              准备填入
            </span>
            {field.type === "file" ? (
              <span className="t-micro" style={{ color: "var(--muted)" }}>
                {field.key === "photo"
                  ? "自动上传「简历配置」里传过的寸照"
                  : field.key === "resume"
                    ? "自动上传「简历配置」里传过的简历原件"
                    : "没识别到上传栏，请在浏览器里手动上传"}
              </span>
            ) : field.type === "radio" || field.type === "checkbox" ? (
              <span className="t-micro" style={{ color: "var(--muted)" }}>
                {key ? "识别到就自动勾上" : "没识别到，请在浏览器里手动勾选"}
              </span>
            ) : locked ? (
              <div className="flex min-w-0 items-center gap-2">
                <span
                  className="t-body min-w-0 flex-1 truncate"
                  title={value}
                  style={{
                    padding: "0.45rem 0.7rem",
                    borderRadius: "var(--r-sm)",
                    border: "1px solid var(--accent-line)",
                    background: "var(--accent-tint)",
                    color: "var(--accent-deep)",
                  }}
                >
                  {value}
                </span>
                <GhostButton
                  variant="plain"
                  className="shrink-0"
                  title="这一栏改成别的值"
                  onClick={() => unlockField(field.index)}
                >
                  修改
                </GhostButton>
              </div>
            ) : (
              <div className="flex min-w-0 items-center gap-2">
                <div className="min-w-0 flex-1">
                  {field.options.length > 0 ? (
                    <Dropdown
                      value={value}
                      options={
                        value && !field.options.includes(value)
                          ? [value, ...field.options]
                          : field.options
                      }
                      placeholder="选择这一栏要填的选项"
                      width="100%"
                      onChange={(v) => patchField(field.index, { filled: v })}
                    />
                  ) : (
                    <TextInput
                      value={value}
                      onChange={(v) => patchField(field.index, { filled: v })}
                      placeholder="这一栏要填的内容"
                      width="100%"
                    />
                  )}
                </div>
                {(changed || unlocked[field.index]) && (
                  <GhostButton
                    variant="plain"
                    className="shrink-0"
                    title="丢掉这一行的改动，回到程序填的值"
                    onClick={() => resetField(field.index)}
                  >
                    {changed ? "还原" : "取消"}
                  </GhostButton>
                )}
              </div>
            )}
          </div>

          {/* 右：页面里实际显示的值（填完后本地回读），对不上就标黄 */}
          <div className="flex min-w-0 flex-col gap-1.5">
            <span className="t-micro" style={{ color: "var(--faint)" }}>
              页面实际值
            </span>
            {actual ? (
              <span
                className="t-body min-w-0 truncate"
                title={actual}
                style={{
                  padding: "0.45rem 0.7rem",
                  borderRadius: "var(--r-sm)",
                  border: `1px solid ${mismatch ? "var(--warn-line)" : "var(--line)"}`,
                  background: mismatch ? "transparent" : "var(--surface-2)",
                  color: mismatch ? "var(--warn)" : "var(--text)",
                }}
              >
                {actual}
              </span>
            ) : (
              <span className="t-micro" style={{ color: "var(--faint)" }}>
                {changed ? "改过了，点「填入浏览器」才会写进去" : "还没填，或没回读到"}
              </span>
            )}
          </div>
        </div>

        {mismatch && (
          <span className="t-micro" style={{ color: "var(--warn)" }}>
            页面里显示的和准备填的不一样，请在浏览器里核对这一栏
          </span>
        )}

        {/* 出问题的提示用警示色；「推算 / 记忆」这类说明只是让人知道这条不是资料原文 */}
        {field.note && (
          <span
            className="t-micro"
            style={{ color: field.ok ? "var(--faint)" : "var(--warn)" }}
          >
            {field.note}
          </span>
        )}
      </div>
    );
  }

  return (
    <SlideOver
      open={open}
      onOpenChange={onOpenChange}
      dismissible={false}
      title={task?.title || "网申助手"}
      description={
        task
          ? [task.company, task.salary, task.location].filter(Boolean).join(" · ") ||
            task.url ||
            undefined
          : "把企业官网的投递表单扫成一张对照表，确认后由程序代填。"
      }
      width={940}
      footer={
        <>
          <PrimaryButton loading={busy === "fill"} onClick={fill} disabled={!fields.length}>
            <Sparkles size={13} /> 填入浏览器
          </PrimaryButton>
          <SoftButton loading={busy === "scan"} onClick={scan} disabled={!task}>
            <RefreshCw size={13} /> 重新扫描
          </SoftButton>
          <SoftButton loading={busy === "learn"} onClick={learn} disabled={!task}>
            <BookOpen size={13} /> 学习手填内容
          </SoftButton>
          <SoftButton loading={busy === "captcha"} onClick={solveCaptcha} disabled={!task}>
            <Sparkles size={13} /> 用 AI 识别验证码
          </SoftButton>
          {task?.status === "submitted" ? (
            <GhostButton onClick={reopen} disabled={!task || busy === "reopen"}>
              <RotateCcw size={13} /> 撤销提交
            </GhostButton>
          ) : (
            <GhostButton onClick={submit} disabled={!task}>
              <CheckCircle2 size={13} /> 标记已提交
            </GhostButton>
          )}
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
            正在打开企业官网…
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
              把企业官网投递表单页的网址粘进来，地址会跟着这条任务一起记住，下次不用再填。
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
            {rows.length > 0 && (
              <span className="t-micro" style={{ color: "var(--faint)" }}>
                表单 {rows.length} 项 · 已对上 {matched} 项
                {unsure.length > 0 ? ` · 待确认 ${unsure.length} 项` : ""}
                {liveInferred.length > 0 ? ` · AI 推理 ${liveInferred.length} 项` : ""}
              </span>
            )}
            {pending && (
              <span
                className="t-micro inline-flex items-center gap-1.5"
                style={{ color: "var(--muted)" }}
              >
                <Spinner size={11} /> {pending}
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

          {task.note && (
            <p className="t-micro" style={{ color: "var(--muted)" }}>
              上次：{task.note}
            </p>
          )}

          {handoffTotal > 0 && (
            <section className="flex flex-col gap-2.5">
              <div className="flex flex-wrap items-baseline gap-2">
                <span
                  className="t-label inline-flex items-center gap-1.5 font-semibold"
                  style={{ color: "var(--ink)" }}
                >
                  <ListChecks size={13} /> 交付清单
                </span>
                <span className="t-micro" style={{ color: "var(--muted)" }}>
                  自动跑完还差这些；点一条会跳到下面那一行
                </span>
              </div>
              <div className="grid gap-2.5 md:grid-cols-3">
                {HANDOFF_GROUPS.map((g) => (
                  <HandoffCard
                    key={g.name}
                    title={g.title}
                    hint={g.hint}
                    color={g.color}
                    items={handoff[g.name]}
                    onJump={jump}
                  />
                ))}
              </div>
            </section>
          )}

          {rows.length === 0 ? (
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
            <div className="flex flex-col gap-5">
              {unsure.length > 0 && (
                <section className="flex flex-col">
                  <div className="flex flex-wrap items-baseline gap-2 pb-1">
                    <span className="t-label font-semibold" style={{ color: "var(--ink)" }}>
                      要你确认的 {unsure.length} 项
                    </span>
                    <span className="t-micro" style={{ color: "var(--muted)" }}>
                      没认出来的直接填，认错了就改一下，再点「填入浏览器」
                    </span>
                  </div>
                  {unsure.map((field, i) => renderRow(field, i))}
                </section>
              )}

              {(liveInferred.length > 0 || rejected.length > 0) && (
                <section className="flex flex-col">
                  <div className="flex flex-wrap items-baseline gap-2 pb-1">
                    <span
                      className="t-label inline-flex items-center gap-1.5 font-semibold"
                      style={{ color: "var(--ink)" }}
                    >
                      <Sparkles size={13} /> AI 推理（请核对）
                      {liveInferred.length > 0 ? ` ${liveInferred.length} 项` : ""}
                    </span>
                    <span className="t-micro" style={{ color: "var(--muted)" }}>
                      这几栏不是资料原文，是模型按你已有的资料推出来的。看着不对就删掉；
                      留着的会跟其它栏一起填进去，并在点「填入浏览器」时记进本机资料库。
                    </span>
                  </div>
                  {liveInferred.length > 0 && (
                    <div className="g-divided-soft">
                      {liveInferred.map((field) => (
                        <div
                          key={field.index}
                          className="grid items-center gap-x-4 gap-y-2 py-3 sm:grid-cols-[minmax(0,1fr)_minmax(0,0.9fr)_minmax(0,1.3fr)_auto]"
                        >
                          {/* 左：官网表单要求填的栏 */}
                          <div className="flex min-w-0 flex-col gap-1">
                            <span
                              className="t-label truncate font-semibold"
                              style={{ color: "var(--ink)" }}
                            >
                              {field.label || field.placeholder || field.name || "（未命名字段）"}
                            </span>
                            <span className="flex flex-wrap items-center gap-1.5">
                              <Badge color="gray">{typeLabel(field)}</Badge>
                              {field.required && <Badge color="orange">必填</Badge>}
                            </span>
                          </div>
                          {/* 中：推出来的值 */}
                          <span
                            className="t-body min-w-0 truncate"
                            title={field.filled}
                            style={{
                              padding: "0.45rem 0.7rem",
                              borderRadius: "var(--r-sm)",
                              border: "1px solid var(--line)",
                              background: "var(--surface-2)",
                            }}
                          >
                            {field.filled}
                          </span>
                          {/* 右：推的依据 */}
                          <span className="t-micro min-w-0" style={{ color: "var(--faint)" }}>
                            {field.note || "模型按已有资料推的"}
                          </span>
                          <GhostButton danger onClick={() => rejectInferred(field.index)}>
                            <Trash2 size={13} /> 删除
                          </GhostButton>
                        </div>
                      ))}
                    </div>
                  )}
                  {rejected.length > 0 && (
                    <span className="t-micro pt-2" style={{ color: "var(--faint)" }}>
                      已删掉 {rejected.length} 条推算，它们不会被填进去；这个网站上的同一栏位
                      下次也不再推。
                    </span>
                  )}
                </section>
              )}

              {matched > 0 && (
                <section className="flex flex-col">
                  <div className="flex flex-wrap items-baseline gap-2 pb-1">
                    <span className="t-label font-semibold" style={{ color: "var(--ink)" }}>
                      程序已对上 {matched} 项
                    </span>
                    <span className="t-micro" style={{ color: "var(--muted)" }}>
                      不用管，要换别的值点「修改」
                    </span>
                  </div>
                  {rows.filter(settled).map((field, i) => renderRow(field, i))}
                </section>
              )}
            </div>
          )}

          <p className="t-micro" style={{ color: "var(--faint)" }}>
            左栏是官网表单要填的栏，中栏是准备填进去的值，右栏是填完后从页面回读到的实际值。
            程序只负责把值写进控件，不会点提交按钮；中栏与右栏不一致的行会标黄，
            请在浏览器里核对后自己点提交，再回来点「标记已提交」。
          </p>
        </div>
      )}
    </SlideOver>
  );
}
