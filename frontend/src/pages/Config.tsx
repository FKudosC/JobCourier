/** 配置中心：岗位筛选、简历配置、自动回复、黑名单与系统设置。 */

import {
  Ban,
  Check,
  ClipboardList,
  Eye,
  FileText,
  FileUp,
  GitBranch,
  ListFilter,
  MessagesSquare,
  Plus,
  RefreshCw,
  RotateCcw,
  Settings2,
  Sparkles,
  Trash2,
} from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { toast } from "sonner";
import { api } from "../api";
import { PageShell } from "../Shell";
import { useRuntime } from "../store";
import {
  Badge,
  Card,
  ComboMulti,
  Dropdown,
  GhostButton,
  PrimaryButton,
  SectionTitle,
  Skeleton,
  SoftButton,
  SwitchRow,
  TextArea,
  TextInput,
} from "../ui";

import type {
  ApplyLearnedRow,
  ApplyProfile,
  ApplyTreeNode,
  Auto,
  BlackRow,
  Bundle,
  Captcha,
  ListField,
  Llm,
  LlmForm,
  Locate,
  ReplyForm,
  SearchForm,
  Vision,
} from "./config/types";
import {
  DelayInput,
  Field,
  NumInput,
  RangeField,
  Row,
  Seg,
  SetGrid,
  TagEditor,
} from "./config/parts";
import { ProfileTree, sourceTone } from "./config/ProfileTree";

const RESUME_ACCEPT = ".pdf,.docx,.jpg,.jpeg,.png,.bmp,.webp";
const CUSTOM = "自定义";
const DEFAULT_BASE_URL = "https://api.openai.com/v1";

/** 网申填表的交付口径：界面上用中文标签，落到配置里是三个英文枚举。 */
const HANDOFF_OPTIONS = ["宽松", "中档", "严格"];
const HANDOFF_IDS: Record<string, string> = { 宽松: "loose", 中档: "balanced", 严格: "strict" };
const HANDOFF_LABELS: Record<string, string> = { loose: "宽松", balanced: "中档", strict: "严格" };

/** API Key 只回显掩码，明文留在本机；提交掩码或空串时后端沿用已存的 Key。 */
function toLlmForm(llm: Llm): LlmForm {
  return { base_url: llm.base_url, api_key: llm.api_key, key_set: llm.key_set, model: llm.model };
}

function toForm(b: Bundle): SearchForm {
  return {
    query: b.query,
    keywords: [...b.keywords],
    cities: [...b.cities],
    experience: [...b.experience],
    education: [...b.education],
    funding: [...b.funding],
    scale: [...b.scale],
    job_type_label: b.job_type_label,
    salary_label: b.salary_label,
    pace_label: b.pace.label,
    pace_params: { ...b.pace.params },
    scam_block: b.scam_block,
    resume_match: b.resume_match,
    score_filter: b.score_filter,
    min_score: b.min_score,
    salary_min: b.salary_min,
    salary_max: b.salary_max,
    resume_path: b.resume_path,
    resume_size: b.resume_size,
    resume_text: b.resume_text,
    photo_path: b.photo_path,
    photo_size: b.photo_size,
  };
}

function toReply(b: Bundle): ReplyForm {
  return {
    enabled: b.reply.enabled,
    prompt: b.reply.prompt,
    confirm_before_send: b.reply.confirm_before_send,
    follow_up_hours: b.reply.follow_up_hours,
    block_proactive_hr: b.reply.block_proactive_hr,
    pace_label: b.reply.pace.label,
    pace_params: { ...b.reply.pace.params },
  };
}

function fileName(path: string): string {
  if (!path) return "";
  return path.split(/[\\/]/).pop() || path;
}

/** 简历 / 寸照的本地预览地址：后端只放行数据目录下的那两个文件夹。 */
function previewUrl(path: string): string {
  return `/api/config/file?path=${encodeURIComponent(path)}`;
}

const IMAGE_SUFFIX_RE = /\.(jpe?g|png|bmp|webp|gif)$/i;

/** 文件大小：只标到 KB / MB 一位小数，够看就行。 */
function fileSize(bytes: number): string {
  if (!bytes) return "";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

/** 一个附件位：缩略图 + 名称 / 大小 + 上传 / 预览，整块是一张卡片。 */
function AttachRow({
  label,
  path,
  size,
  empty,
  loading,
  accept,
  inputRef,
  onPick,
}: {
  label: string;
  path: string;
  size: number;
  empty: string;
  loading: boolean;
  accept: string;
  inputRef: React.RefObject<HTMLInputElement | null>;
  onPick: (e: React.ChangeEvent<HTMLInputElement>) => void;
}) {
  const name = fileName(path);
  const url = path ? previewUrl(path) : "";
  const thumb = path ? (
    <a href={url} target="_blank" rel="noreferrer" className="g-attach-thumb" title="点击预览">
      {IMAGE_SUFFIX_RE.test(path) ? (
        <img src={url} alt={name} />
      ) : (
        <FileText size={24} style={{ color: "var(--accent)" }} />
      )}
    </a>
  ) : (
    <span className="g-attach-thumb is-empty">
      <FileText size={24} />
    </span>
  );
  const sizeText = fileSize(size);
  return (
    <div className="g-attach-tile">
      {thumb}
      <div className="g-attach-body">
        <div className="g-attach-title">
          <span className="t-label font-semibold" style={{ color: "var(--ink)" }}>
            {label}
          </span>
          <Badge color={path ? "gray" : "orange"}>{path ? sizeText || "已上传" : "未上传"}</Badge>
        </div>
        <div className="g-attach-name" title={name || undefined}>
          {name || empty}
        </div>
      </div>
      <div className="g-attach-actions">
        <SoftButton loading={loading} onClick={() => inputRef.current?.click()}>
          <FileUp size={13} /> {path ? `更换${label}` : `上传${label}`}
        </SoftButton>
        {path && (
          <GhostButton variant="plain" onClick={() => window.open(url, "_blank", "noreferrer")}>
            <Eye size={13} /> 预览
          </GhostButton>
        )}
      </div>
      <input ref={inputRef} type="file" accept={accept} onChange={onPick} className="hidden" />
    </div>
  );
}



/* ------------------------------------------------------------------ */
/* 页面                                                                */
/* ------------------------------------------------------------------ */

export default function Config() {
  const { conn, rt } = useRuntime();
  const [tab, setTab] = useState("filters");
  const [bundle, setBundle] = useState<Bundle | null>(null);
  const [form, setForm] = useState<SearchForm | null>(null);
  const [reply, setReply] = useState<ReplyForm | null>(null);
  const [llmForm, setLlmForm] = useState<LlmForm>({
    base_url: "",
    api_key: "",
    key_set: false,
    model: "",
  });
  const [captcha, setCaptcha] = useState<Captcha>({ enabled: false, model: "" });
  const [locate, setLocate] = useState<Locate>({ enabled: false, max_fields: 3, timeout: 15 });
  const [vision, setVision] = useState<Vision>({
    enabled: true,
    model: "",
    timeout: 40,
    max_tiles: 4,
    mask_sensitive: true,
  });
  const [auto, setAuto] = useState<Auto>({
    handoff: "balanced",
    upload_files: true,
    learn_web: true,
    learn_ai: true,
  });
  /** 网申资料两个视图：树形（默认，看来源与共指项）/ 表单（老样子，平铺着改）。 */
  const [applyView, setApplyView] = useState("tree");
  const [savedAt, setSavedAt] = useState("");

  const [genKw, setGenKw] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [photoUploading, setPhotoUploading] = useState(false);
  const [addingBlack, setAddingBlack] = useState(false);
  const [blackDraft, setBlackDraft] = useState("");
  const [fetching, setFetching] = useState(false);
  const [testing, setTesting] = useState(false);
  /* 后台静默运行：本地乐观状态，随后端快照回填 */
  const [background, setBackground] = useState(false);

  useEffect(() => {
    setBackground(!!rt?.browser_background);
  }, [rt?.browser_background]);

  const fileRef = useRef<HTMLInputElement | null>(null);
  const photoRef = useRef<HTMLInputElement | null>(null);
  const loaded = useRef(false);

  const markSaved = () => {
    setSavedAt(new Date().toLocaleTimeString("zh-CN", { hour12: false, hour: "2-digit", minute: "2-digit" }));
  };

  const reload = useCallback(async () => {
    try {
      const b = await api.get<Bundle>("/api/config");
      setBundle(b);
      setForm(toForm(b));
      setReply(toReply(b));
      setLlmForm(toLlmForm(b.llm));
      setCaptcha(b.captcha);
      setLocate(b.locate);
      setVision(b.vision);
      setAuto(b.auto);
    } catch (err) {
      toast.error((err as Error).message);
    }
  }, []);

  useEffect(() => {
    if (conn === "open" && !loaded.current) {
      loaded.current = true;
      void reload();
    }
  }, [conn, reload]);

  /* ---- 搜索配置 ---- */

  async function saveSearch(patch: Partial<SearchForm>, withReload = false) {
    if (!form) return;
    const next = { ...form, ...patch };
    setForm(next);
    try {
      await api.put("/api/config/search", {
        query: next.query,
        keywords: next.keywords,
        cities: next.cities,
        experience: next.experience,
        education: next.education,
        funding: next.funding,
        scale: next.scale,
        job_type_label: next.job_type_label,
        salary_label: next.salary_label,
        pace_label: next.pace_label,
        pace_params: next.pace_params,
        scam_block: next.scam_block,
        resume_match: next.resume_match,
        score_filter: next.score_filter,
        min_score: next.min_score,
        salary_min: next.salary_min,
        salary_max: next.salary_max,
        resume_path: next.resume_path,
        resume_text: next.resume_text,
        photo_path: next.photo_path,
      });
      markSaved();
      if (withReload) void reload();
    } catch (err) {
      toast.error((err as Error).message);
      void reload();
    }
  }

  /* 骗子岗位识别开关（后端有独立接口，这里单独保存）。 */
  async function saveScamBlock(enabled: boolean) {
    if (!form) return;
    setForm({ ...form, scam_block: enabled });
    try {
      await api.put("/api/config/scam-block", { enabled });
      markSaved();
      toast.success(enabled ? "已开启骗子岗位识别" : "已关闭骗子岗位识别");
    } catch (err) {
      toast.error((err as Error).message);
      void reload();
    }
  }

  async function mutateList(field: ListField, value: string, add: boolean) {
    try {
      const res = add
        ? await api.post<{ items: string[] }>("/api/config/list-item", { field, value })
        : await api.del<{ items: string[] }>(
            `/api/config/list-item/${field}/${encodeURIComponent(value)}`,
          );
      const items = res.items ?? [];
      setForm((f) => (f ? { ...f, [field]: items } : f));
      setBundle((b) => (b ? { ...b, [field]: items } : b));
      markSaved();
    } catch (err) {
      toast.error((err as Error).message);
    }
  }

  async function generateKeywords() {
    setGenKw(true);
    try {
      const res = await api.post<{ keywords: string[] }>("/api/config/keywords/generate");
      setForm((f) => (f ? { ...f, keywords: res.keywords } : f));
      setBundle((b) => (b ? { ...b, keywords: res.keywords } : b));
      toast.success(`已生成 ${res.keywords.length} 个关键词`);
    } catch (err) {
      toast.error((err as Error).message);
    } finally {
      setGenKw(false);
    }
  }

  async function onPickResume(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    e.target.value = "";
    if (!file) return;
    setUploading(true);
    try {
      const res = await api.upload<{ resume_path: string; resume_text: string; hint: string }>(
        "/api/config/resume/upload",
        file,
      );
      setForm((f) => (f ? { ...f, resume_path: res.resume_path, resume_text: res.resume_text } : f));
      setBundle((b) => (b ? { ...b, resume_path: res.resume_path, resume_text: res.resume_text } : b));
      toast.success(res.hint);
      markSaved();
    } catch (err) {
      toast.error((err as Error).message);
    } finally {
      setUploading(false);
    }
  }

  async function onPickPhoto(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    e.target.value = "";
    if (!file) return;
    setPhotoUploading(true);
    try {
      const res = await api.upload<{ photo_path: string }>("/api/config/photo/upload", file);
      setForm((f) => (f ? { ...f, photo_path: res.photo_path } : f));
      setBundle((b) => (b ? { ...b, photo_path: res.photo_path } : b));
      toast.success("寸照已保存");
      markSaved();
    } catch (err) {
      toast.error((err as Error).message);
    } finally {
      setPhotoUploading(false);
    }
  }

  /* ---- 自动回复 ---- */

  async function saveReply(patch: Partial<ReplyForm>, withReload = false) {
    if (!reply) return;
    const payload: Record<string, unknown> = {};
    if (patch.enabled !== undefined) payload.enabled = patch.enabled;
    if (patch.prompt !== undefined) payload.prompt = patch.prompt;
    if (patch.confirm_before_send !== undefined) payload.confirm_before_send = patch.confirm_before_send;
    if (patch.follow_up_hours !== undefined) payload.follow_up_hours = patch.follow_up_hours;
    if (patch.block_proactive_hr !== undefined) payload.block_proactive_hr = patch.block_proactive_hr;
    if (patch.pace_label !== undefined || patch.pace_params !== undefined) {
      payload.reply_pace_label = patch.pace_label ?? reply.pace_label;
      payload.reply_pace_params = patch.pace_params ?? reply.pace_params;
    }
    setReply({ ...reply, ...patch });
    try {
      await api.put("/api/config/reply", payload);
      markSaved();
      if (withReload) void reload();
    } catch (err) {
      toast.error((err as Error).message);
      void reload();
    }
  }

  function toggleReplyEnabled(v: boolean) {
    if (v && !bundle?.llm.ready) {
      toast.error("请先在「系统设置」里开通大模型");
      return;
    }
    if (v && !form?.resume_text.trim()) {
      toast.error("请先在「简历配置」里上传简历或填写简历内容");
      return;
    }
    void saveReply({ enabled: v });
  }

  async function resetPrompt() {
    try {
      const res = await api.post<{ prompt: string }>("/api/config/reply-prompt/reset");
      setReply((r) => (r ? { ...r, prompt: res.prompt } : r));
      toast.success("已恢复默认提示词");
      markSaved();
    } catch (err) {
      toast.error((err as Error).message);
    }
  }

  /* ---- 黑名单 ---- */

  async function addBlack() {
    const name = blackDraft.trim();
    if (!name) {
      toast.error("请输入公司名或岗位关键词");
      return;
    }
    setAddingBlack(true);
    try {
      const res = await api.post<BlackRow[]>("/api/blacklist", { name });
      setBundle((b) => (b ? { ...b, blacklist: res } : b));
      setBlackDraft("");
      toast.success("已加入黑名单");
    } catch (err) {
      toast.error((err as Error).message);
    } finally {
      setAddingBlack(false);
    }
  }

  async function removeBlack(key: string) {
    try {
      const res = await api.del<BlackRow[]>(`/api/blacklist/${encodeURIComponent(key)}`);
      setBundle((b) => (b ? { ...b, blacklist: res } : b));
      toast.success("已移出黑名单");
    } catch (err) {
      toast.error((err as Error).message);
    }
  }

  /* ---- AI 服务 ---- */

  async function saveLlm(patch: Partial<LlmForm>) {
    const next = { ...llmForm, ...patch };
    setLlmForm(next);
    try {
      const res = await api.put<Llm>("/api/llm", {
        base_url: next.base_url,
        api_key: next.api_key,
        model: next.model,
      });
      setLlmForm(toLlmForm(res));
      setBundle((b) => (b ? { ...b, llm: res } : b));
      markSaved();
    } catch (err) {
      toast.error((err as Error).message);
    }
  }

  async function fetchModels() {
    setFetching(true);
    try {
      const res = await api.post<{ models: string[] }>("/api/llm/fetch-models", {
        base_url: llmForm.base_url,
        api_key: llmForm.api_key,
        model: llmForm.model,
      });
      toast.success(
        res.models.length > 0 ? `已获取 ${res.models.length} 个可用模型，请选择` : "没有取到可用模型",
      );
      await reload();
    } catch (err) {
      toast.error((err as Error).message);
    } finally {
      setFetching(false);
    }
  }

  async function testLlm() {
    setTesting(true);
    try {
      const res = await api.post<{ preview: string }>("/api/llm/test", {
        base_url: llmForm.base_url,
        api_key: llmForm.api_key,
        model: llmForm.model,
      });
      toast.success(`连接成功，模型已回复：${res.preview}`);
    } catch (err) {
      toast.error((err as Error).message);
    } finally {
      setTesting(false);
    }
  }

  async function saveCaptcha(patch: Partial<Captcha>) {
    const next = { ...captcha, ...patch };
    setCaptcha(next);
    try {
      const res = await api.put<Captcha>("/api/config/captcha", next);
      setCaptcha(res);
      setBundle((b) => (b ? { ...b, captcha: res } : b));
      markSaved();
    } catch (err) {
      toast.error((err as Error).message);
    }
  }

  async function saveLocate(patch: Partial<Locate>) {
    const next = { ...locate, ...patch };
    setLocate(next);
    try {
      const res = await api.put<Locate>("/api/config/locate", next);
      setLocate(res);
      setBundle((b) => (b ? { ...b, locate: res } : b));
      markSaved();
    } catch (err) {
      toast.error((err as Error).message);
    }
  }

  async function saveVision(patch: Partial<Vision>) {
    const next = { ...vision, ...patch };
    setVision(next);
    try {
      const res = await api.put<Vision>("/api/config/vision", next);
      setVision(res);
      setBundle((b) => (b ? { ...b, vision: res } : b));
      markSaved();
    } catch (err) {
      toast.error((err as Error).message);
    }
  }

  async function saveAuto(patch: Partial<Auto>) {
    const next = { ...auto, ...patch };
    setAuto(next);
    try {
      const res = await api.put<Auto>("/api/config/auto", next);
      setAuto(res);
      setBundle((b) => (b ? { ...b, auto: res } : b));
      markSaved();
    } catch (err) {
      toast.error((err as Error).message);
    }
  }

  const clearedLegacyModels = useRef(false);
  useEffect(() => {
    if (tab !== "system" || !bundle || clearedLegacyModels.current) return;
    clearedLegacyModels.current = true;
    if (captcha.model) void saveCaptcha({ model: "" });
    if (vision.model) void saveVision({ model: "" });
  }, [tab, bundle, captcha.model, vision.model]);

  /* ---- 网申资料 ---- */

  /** 只提交动过的键：后端按 patch 合并，补充字段传空值即删除。 */
  async function saveApplyProfile(values: Record<string, string>) {
    try {
      const res = await api.put<ApplyProfile>("/api/config/apply-profile", { values });
      setBundle((b) => (b ? { ...b, apply_profile: res } : b));
      markSaved();
    } catch (err) {
      toast.error((err as Error).message);
      void reload();
    }
  }

  /** 忘掉本机学会的某一条字段（留空 key 表示整表清空）。 */
  async function forgetLearned(key = "") {
    try {
      const path = key
        ? `/api/config/apply-learned/${encodeURIComponent(key)}`
        : "/api/config/apply-learned";
      const res = await api.del<ApplyProfile>(path);
      setBundle((b) => (b ? { ...b, apply_profile: res } : b));
      markSaved();
    } catch (err) {
      toast.error((err as Error).message);
    }
  }

  /** 核对过一条 AI 推理：推得对的点「保留」，撤掉「待核对」。 */
  async function confirmLearned(key: string) {
    try {
      const res = await api.post<ApplyProfile>("/api/config/apply-learned/confirm", { key });
      setBundle((b) => (b ? { ...b, apply_profile: res } : b));
      markSaved();
      toast.success("已核对：这条以后算你自己认下的，不再标「待核对」");
    } catch (err) {
      toast.error((err as Error).message);
    }
  }

  /** 认下一条同义叫法：「alias 其实就是 key 那条资料」。 */
  async function mergeAlias(alias: string, key: string) {
    try {
      const res = await api.post<ApplyProfile>("/api/config/apply-alias", { alias, key });
      setBundle((b) => (b ? { ...b, apply_profile: res } : b));
      markSaved();
      toast.success(`以后「${alias}」就按「${key}」这条资料填`);
    } catch (err) {
      toast.error((err as Error).message);
    }
  }

  /** 撤销一条手动合并。 */
  async function unmergeAlias(alias: string) {
    try {
      const res = await api.del<ApplyProfile>(
        `/api/config/apply-alias/${encodeURIComponent(alias)}`,
      );
      setBundle((b) => (b ? { ...b, apply_profile: res } : b));
      markSaved();
    } catch (err) {
      toast.error((err as Error).message);
    }
  }

  /* ---- 浏览器 ---- */

  async function toggleBackground(value: boolean) {
    setBackground(value);
    try {
      await api.put("/api/browser/background", { enabled: value });
      markSaved();
    } catch (err) {
      setBackground(!value);
      toast.error((err as Error).message);
    }
  }

  /* ---- 渲染 ---- */

  const resumeReady = !!form?.resume_text.trim();
  const applyReady = !!bundle?.apply_profile.ready;
  const applyLabels = bundle?.apply_profile.labels ?? {};
  const applyValues = bundle?.apply_profile.values ?? {};
  const applyGroups = bundle?.apply_profile.groups ?? [];
  // 表单视图也要说清「这条不是资料里写的那份」：把树上的来源按字段键对过来，
  // 否则本机学到 / 模型推的值和资料里手写的值长得一模一样，用户分不出哪条要核对。
  const applyNodes = new Map<string, ApplyTreeNode>();
  for (const group of bundle?.apply_profile.tree.groups ?? []) {
    if (group.kind !== "group") continue;
    for (const field of group.fields) applyNodes.set(field.key, field);
  }
  const modelOptions =
    bundle && bundle.llm.model && !bundle.llm.models.includes(bundle.llm.model)
      ? [bundle.llm.model, ...bundle.llm.models]
      : (bundle?.llm.models ?? []);

  const tabs = [
    { id: "filters", label: "岗位筛选", icon: <ListFilter size={13} /> },
    { id: "resume", label: "简历配置", icon: <FileText size={13} /> },
    { id: "apply", label: "网申资料", icon: <ClipboardList size={13} /> },
    { id: "reply", label: "自动回复", icon: <MessagesSquare size={13} /> },
    { id: "blacklist", label: "黑名单", icon: <Ban size={13} /> },
    { id: "system", label: "系统设置", icon: <Settings2 size={13} /> },
  ];

  return (
    <PageShell
      active="/config"
      fill
    >
      <div className="g-scroll flex min-h-0 flex-1 flex-col gap-5 overflow-y-auto pb-6">
        {/* 子标签常驻顶部，只有下面的配置内容滚动 */}
        <div
          className="sticky top-0 z-10 pb-2 pt-6"
          style={{ background: "var(--paper)", boxShadow: "0 1px 0 var(--line)" }}
        >
          <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
            <Seg value={tab} onChange={setTab} tabs={tabs} />
            <div className="ml-auto flex flex-wrap items-center gap-2">
              {bundle?.llm.ready ? (
                <Badge color="green">已开通 AI 服务</Badge>
              ) : (
                <GhostButton onClick={() => setTab("system")}>去开通</GhostButton>
              )}
              {resumeReady ? (
                <Badge color="green">已配置简历</Badge>
              ) : (
                <GhostButton onClick={() => setTab("resume")}>去上传</GhostButton>
              )}
              {applyReady && <Badge color="green">已填网申资料</Badge>}
              {savedAt && (
                <span className="t-micro inline-flex items-center gap-1" style={{ color: "var(--faint)" }}>
                  <Check size={12} /> 已自动保存 {savedAt}
                </span>
              )}
            </div>
          </div>
        </div>

        {!bundle || !form || !reply ? (
          <div className="flex flex-col gap-4">
            <Skeleton style={{ height: 220 }} />
            <Skeleton style={{ height: 160 }} />
          </div>
        ) : (
          <>
            {/* ---------------- 岗位筛选 ---------------- */}
            {tab === "filters" && (
              <div className="g-in flex flex-col gap-5">
                <Card pad="0.5rem 1.35rem">
                  <div className="pt-3">
                    <SectionTitle title="基础条件" />
                  </div>
                  <div className="g-divided">
                    <Row
                      label="岗位关键词"
                      hint="每个关键词单独跑一轮搜索；点标签上的 × 可删除"
                      right={
                        <GhostButton onClick={generateKeywords} disabled={genKw}>
                          {genKw ? <RefreshCw size={13} className="g-spin" /> : <Sparkles size={13} />}
                          {genKw ? "AI 研判中…" : "AI 生成"}
                        </GhostButton>
                      }
                    >
                      <TagEditor
                        tags={form.keywords}
                        onAdd={(v) => mutateList("keywords", v, true)}
                        onRemove={(v) => mutateList("keywords", v, false)}
                        placeholder="输入岗位关键词后回车添加"
                      />
                    </Row>

                    <div className="py-4">
                      <ComboMulti
                        title="目标城市"
                        hint="多选，按投递优先级排序"
                        options={bundle.options.cities}
                        selected={form.cities}
                        onToggle={(v) => mutateList("cities", v, !form.cities.includes(v))}
                        placeholder="选择城市"
                      />
                    </div>

                    <SetGrid>
                      <Field label="求职类型">
                        <Dropdown
                          value={form.job_type_label}
                          options={bundle.options.job_type}
                          onChange={(v) => saveSearch({ job_type_label: v })}
                          width="100%"
                        />
                      </Field>
                      <Field label="薪资范围" hint="薪资对不上的岗位在预筛阶段直接跳过">
                        <Dropdown
                          value={form.salary_label}
                          options={bundle.options.salary}
                          onChange={(v) => saveSearch({ salary_label: v })}
                          width="100%"
                        />
                      </Field>
                      <Field
                        label="期望月薪"
                        hint="单位 K / 月，0 表示不限"
                        right={<span className="t-micro" style={{ color: "var(--faint)" }}>下限 / 上限</span>}
                      >
                        {/* 1fr auto 1fr auto：两个输入框平分整列宽度，与上方两列的下拉框等长 */}
                        <div
                          className="grid items-center gap-2.5"
                          style={{ gridTemplateColumns: "1fr auto 1fr auto" }}
                        >
                          <DelayInput
                            value={form.salary_min}
                            onCommit={(v) => saveSearch({ salary_min: v })}
                            width="100%"
                            type="number"
                            align="center"
                          />
                          <span className="t-micro" style={{ color: "var(--faint)" }}>–</span>
                          <DelayInput
                            value={form.salary_max}
                            onCommit={(v) => saveSearch({ salary_max: v })}
                            width="100%"
                            type="number"
                            align="center"
                          />
                          <span className="t-micro" style={{ color: "var(--faint)" }}>K</span>
                        </div>
                      </Field>
                    </SetGrid>
                  </div>
                </Card>

                <Card pad="0.5rem 1.35rem">
                  <div className="pt-3">
                    <SectionTitle title="经验 / 学历 / 公司" />
                  </div>
                  <div className="g-divided">
                    <SetGrid cols="sm:grid-cols-2">
                      <ComboMulti
                        title="工作经验要求"
                        options={bundle.options.experience}
                        selected={form.experience}
                        onToggle={(v) => mutateList("experience", v, !form.experience.includes(v))}
                        placeholder="不选即不限"
                      />
                      <ComboMulti
                        title="最低学历"
                        options={bundle.options.education}
                        selected={form.education}
                        onToggle={(v) => mutateList("education", v, !form.education.includes(v))}
                        placeholder="不选即不限"
                      />
                    </SetGrid>
                    <SetGrid cols="sm:grid-cols-2">
                      <ComboMulti
                        title="融资阶段"
                        options={bundle.options.funding}
                        selected={form.funding}
                        onToggle={(v) => mutateList("funding", v, !form.funding.includes(v))}
                        placeholder="不选即不限"
                      />
                      <ComboMulti
                        title="公司规模"
                        options={bundle.options.scale}
                        selected={form.scale}
                        onToggle={(v) => mutateList("scale", v, !form.scale.includes(v))}
                        placeholder="不选即不限"
                      />
                    </SetGrid>
                  </div>
                </Card>

                <Card pad="0.5rem 1.35rem">
                  <div className="pt-3">
                    <SectionTitle title="投递节奏" />
                  </div>
                  <div className="g-divided">
                    <Row hint={bundle.pace.hint} flow>
                      <Dropdown
                        value={form.pace_label}
                        options={bundle.options.pace}
                        onChange={(v) => saveSearch({ pace_label: v }, true)}
                        width={160}
                      />
                    </Row>
                    {form.pace_label === CUSTOM && (
                      <SetGrid>
                        <RangeField
                          label="看完一条停"
                          lo={form.pace_params.read_min ?? 3}
                          hi={form.pace_params.read_max ?? 8}
                          onLo={(v) => saveSearch({ pace_params: { ...form.pace_params, read_min: v } }, true)}
                          onHi={(v) => saveSearch({ pace_params: { ...form.pace_params, read_max: v } }, true)}
                          unit="秒"
                        />
                        <RangeField
                          label="每次翻页停"
                          lo={form.pace_params.scroll_min ?? 2}
                          hi={form.pace_params.scroll_max ?? 5}
                          onLo={(v) => saveSearch({ pace_params: { ...form.pace_params, scroll_min: v } }, true)}
                          onHi={(v) => saveSearch({ pace_params: { ...form.pace_params, scroll_max: v } }, true)}
                          unit="秒"
                        />
                        <div>
                          <div className="t-label font-semibold" style={{ color: "var(--ink)" }}>
                            每投几条歇一次
                          </div>
                          <div className="mt-2.5 flex flex-wrap items-center gap-2">
                            <NumInput
                              value={form.pace_params.rest_every ?? 15}
                              onCommit={(v) => saveSearch({ pace_params: { ...form.pace_params, rest_every: v } }, true)}
                              width={64}
                              min={0}
                              max={1000}
                            />
                            <span className="t-micro" style={{ color: "var(--faint)" }}>条歇</span>
                            <NumInput
                              value={form.pace_params.rest_min ?? 30}
                              onCommit={(v) => saveSearch({ pace_params: { ...form.pace_params, rest_min: v } }, true)}
                              width={64}
                              min={0}
                              max={600}
                            />
                            <span className="t-micro" style={{ color: "var(--faint)" }}>–</span>
                            <NumInput
                              value={form.pace_params.rest_max ?? 60}
                              onCommit={(v) => saveSearch({ pace_params: { ...form.pace_params, rest_max: v } }, true)}
                              width={64}
                              min={0}
                              max={600}
                            />
                            <span className="t-micro" style={{ color: "var(--faint)" }}>秒（0 条表示不歇）</span>
                          </div>
                        </div>
                      </SetGrid>
                    )}
                  </div>
                </Card>

                <Card pad="0.5rem 1.35rem">
                  <div className="pt-3">
                    <SectionTitle title="岗位质量把关" />
                  </div>
                  <div className="g-divided">
                    <SwitchRow
                      label="骗子岗位识别"
                      hint="AI 识别刷单、培训收费、拉人头、只收简历不招人等岗位，识别到直接跳过。"
                      checked={form.scam_block}
                      onChange={(v) => {
                        void saveScamBlock(v);
                      }}
                    />
                    <SwitchRow
                      label="简历技术匹配"
                      hint="投递前由 AI 对比岗位要求与你的技能，只投技术对口的岗位，并在岗位列表显示匹配度。"
                      checked={form.resume_match}
                      onChange={(v) => saveSearch({ resume_match: v })}
                    />
                    <SwitchRow
                      label="匹配度过滤"
                      hint="只投递匹配度达到设定分数的岗位。"
                      checked={form.score_filter}
                      onChange={(v) => saveSearch({ score_filter: v })}
                      inline
                    >
                      <div className="flex items-center gap-2.5">
                        {/* w-16 + 右对齐：两个开关行的标签同宽，分数框与下方「分数线」框对齐 */}
                        <span className="t-micro w-16 text-right" style={{ color: "var(--faint)" }}>最低匹配度</span>
                        <NumInput
                          value={form.min_score}
                          onCommit={(v) => saveSearch({ min_score: v })}
                          width={72}
                          min={0}
                          max={100}
                        />
                        {!form.resume_match && (
                          <span className="t-micro" style={{ color: "var(--warn)" }}>
                            请先开启上方的简历技术匹配
                          </span>
                        )}
                      </div>
                    </SwitchRow>
                    <SwitchRow
                      label="高分岗位自动定制简历"
                      hint="AI 评分达到分数线的岗位，自动按岗位生成定制简历（后台进行，在岗位详情里查看与导出）。"
                      checked={bundle.tailor.enabled}
                      onChange={(v) =>
                        void saveTailor(v, bundle.tailor.min_score, setBundle, markSaved)
                      }
                      inline
                    >
                      <div className="flex items-center gap-2.5">
                        <span className="t-micro w-16 text-right" style={{ color: "var(--faint)" }}>分数线</span>
                        <NumInput
                          value={bundle.tailor.min_score}
                          onCommit={(v) => saveTailor(bundle.tailor.enabled, v, setBundle, markSaved)}
                          width={72}
                          min={0}
                          max={100}
                        />
                      </div>
                    </SwitchRow>
                  </div>
                </Card>
              </div>
            )}

            {/* ---------------- 简历配置 ---------------- */}
            {tab === "resume" && (
              <div className="g-in flex flex-col gap-5">
                <Card pad="0.5rem 1.35rem">
                  <div className="pt-3">
                    <SectionTitle
                      title="简历附件"
                      hint="上传你的简历，用于智能匹配岗位。支持 PDF / Word / 图片。"
                    />
                  </div>
                  <div className="g-attach-grid py-4">
                    <AttachRow
                      label="简历"
                      path={form.resume_path}
                      size={form.resume_size}
                      empty="尚未上传简历"
                      loading={uploading}
                      accept={RESUME_ACCEPT}
                      inputRef={fileRef}
                      onPick={onPickResume}
                    />
                    <AttachRow
                      label="寸照"
                      path={form.photo_path}
                      size={form.photo_size}
                      empty="尚未上传寸照"
                      loading={photoUploading}
                      accept=".jpg,.jpeg,.png,.bmp,.webp"
                      inputRef={photoRef}
                      onPick={onPickPhoto}
                    />
                  </div>
                </Card>

                <Card pad="0.5rem 1.35rem">
                  <div className="pt-3">
                    <SectionTitle title="简历内容" hint="AI 匹配与自动回复都基于这里的文字。" />
                  </div>
                  <div className="py-4">
                    <TextArea
                      value={form.resume_text}
                      debounce={900}
                      minHeight={280}
                      placeholder="上传后自动识别简历内容，也可以直接粘贴或修改"
                      onChange={(v) => {
                        setForm((f) => (f ? { ...f, resume_text: v } : f));
                        void api
                          .put("/api/config/resume-text", { text: v })
                          .then(() => markSaved())
                          .catch((err) => toast.error((err as Error).message));
                      }}
                    />
                  </div>
                </Card>
              </div>
            )}

            {/* ---------------- 网申资料 ---------------- */}
            {tab === "apply" && (
              <div className="g-in flex flex-col gap-5">
                <div className="flex flex-wrap items-center justify-between gap-3">
                  <Seg
                    value={applyView}
                    onChange={setApplyView}
                    tabs={[
                      { id: "tree", label: "树形", icon: <GitBranch size={13} /> },
                      { id: "form", label: "表单", icon: <ClipboardList size={13} /> },
                    ]}
                  />
                  <span className="t-micro" style={{ color: "var(--faint)" }}>
                    {applyView === "form"
                      ? "按分组逐项填；填得越全，网申助手能替你填的字段越多。"
                      : "同一份资料的树形摆法：看每条打哪来、还认下过哪些叫法，别名可并、可撤。"}
                  </span>
                </div>

                {applyView === "tree" ? (
                  <ProfileTree
                    tree={bundle.apply_profile.tree}
                    onSave={saveApplyProfile}
                    onConfirm={(key) => confirmLearned(key)}
                    onForget={(key) => forgetLearned(key)}
                    onMerge={(alias, key) => mergeAlias(alias, key)}
                    onUnmerge={(alias) => unmergeAlias(alias)}
                  />
                ) : (
                  <>
                {applyGroups.map((group) => (
                  <Card key={group.title} pad="0.5rem 1.35rem">
                    <div className="pt-3">
                      <SectionTitle
                        title={group.title}
                        hint={
                          APPLY_GROUP_HINTS[group.title] ??
                          "企业官网的网申表常问这些。填得越全，网申助手能替你填的字段越多；内容只存在本机，不会上传。"
                        }
                      />
                    </div>
                    <div className="g-divided">
                      <SetGrid cols="sm:grid-cols-2 lg:grid-cols-3">
                        {group.keys
                          // 自我介绍在下面单独一张卡里编辑，这里别重复渲染；
                          // 补充字段也不进分组（分组只装内置字段）
                          .filter((key) => key !== "self_intro")
                          .map((key) => (
                            <Field
                              key={key}
                              label={
                                <span className="inline-flex flex-wrap items-center gap-1.5">
                                  {applyLabels[key] ?? key}
                                  {applyNodes.get(key)?.memory && (
                                    <Badge
                                      color={
                                        applyNodes.get(key)?.pending
                                          ? "ink"
                                          : sourceTone(applyNodes.get(key)?.source ?? "")
                                      }
                                    >
                                      {applyNodes.get(key)?.pending ? "待核对" : "记忆"}
                                    </Badge>
                                  )}
                                </span>
                              }
                            >
                              <DelayInput
                                value={applyValues[key] ?? ""}
                                onCommit={(v) => saveApplyProfile({ [key]: v })}
                                width="100%"
                                delay={600}
                              />
                            </Field>
                          ))}
                      </SetGrid>
                    </div>
                  </Card>
                ))}

                <Card pad="0.5rem 1.35rem">
                  <div className="pt-3">
                    <SectionTitle
                      title="自我介绍"
                      hint="网申表的「个人简介 / 自我评价」栏会填这段；留空时网申助手会从简历正文里挑。"
                    />
                  </div>
                  <div className="py-4">
                    <TextArea
                      value={bundle.apply_profile.values.self_intro ?? ""}
                      debounce={900}
                      minHeight={140}
                      placeholder="一两句话介绍自己，比如专业、方向、做过什么。"
                      onChange={(v) => saveApplyProfile({ self_intro: v })}
                    />
                  </div>
                </Card>

                <Card pad="0.5rem 1.35rem">
                  <div className="pt-3">
                    <SectionTitle
                      title="本机学会的字段"
                    />
                  </div>
                  <LearnedFields
                    rows={bundle.apply_profile.learned ?? []}
                    onConfirm={(key) => confirmLearned(key)}
                    onForget={(key) => forgetLearned(key)}
                    onClear={() => forgetLearned()}
                  />
                </Card>
                  </>
                )}
              </div>
            )}

            {/* ---------------- 自动回复 ---------------- */}
            {tab === "reply" && (
              <div className="g-in flex flex-col gap-5">
                <Card pad="0.5rem 1.35rem">
                  <div className="pt-3">
                    <SectionTitle
                      title="自动回复"
                      hint="HR 发来消息时，由 AI 结合你的简历自动回复。"
                    />
                  </div>
                  <div className="g-divided">
                    <SwitchRow
                      label="启用自动回复"
                      hint="开启后，AI 会按下方提示词和你的简历回复 HR 的消息。"
                      checked={reply.enabled}
                      onChange={toggleReplyEnabled}
                    >
                      {(!bundle.llm.ready || !resumeReady) && (
                        <div className="flex flex-wrap items-center gap-2">
                          {!bundle.llm.ready && (
                            <button
                              type="button"
                              className="t-micro"
                              style={{ color: "var(--accent)", background: "none", border: 0, padding: 0, cursor: "pointer" }}
                              onClick={() => setTab("system")}
                            >
                              AI 服务尚未开通，去开通
                            </button>
                          )}
                          {!resumeReady && (
                            <button
                              type="button"
                              className="t-micro"
                              style={{ color: "var(--accent)", background: "none", border: 0, padding: 0, cursor: "pointer" }}
                              onClick={() => setTab("resume")}
                            >
                              请先配置简历，去上传
                            </button>
                          )}
                        </div>
                      )}
                    </SwitchRow>
                  </div>
                </Card>

                <Card pad="0.5rem 1.35rem">
                  <div className="flex items-start justify-between gap-3 pt-3">
                    <SectionTitle
                      title="回复提示词"
                      hint="告诉 AI 用什么语气、按什么原则回复 HR。简历内容会自动附带给 AI，这里只需写语气和原则。"
                    />
                    <GhostButton onClick={resetPrompt}>
                      <RotateCcw size={13} /> 恢复默认
                    </GhostButton>
                  </div>
                  <div className="py-4">
                    <TextArea
                      value={reply.prompt}
                      debounce={900}
                      minHeight={240}
                      onChange={(v) => {
                        setReply((r) => (r ? { ...r, prompt: v } : r));
                        void saveReply({ prompt: v });
                      }}
                    />
                  </div>
                </Card>

                <Card pad="0.5rem 1.35rem">
                  <div className="pt-3">
                    <SectionTitle title="回复策略" hint="控制发送方式与跟进提醒。" />
                  </div>
                  <div className="g-divided">
                    <SwitchRow
                      label="发送前人工确认"
                      hint="AI 生成回复后先不发送，进「消息」页待你确认；同意后自动回复运行中会自动发出。"
                      checked={reply.confirm_before_send}
                      onChange={(v) => saveReply({ confirm_before_send: v })}
                    />
                    <SwitchRow
                      label="自动拉黑主动联系的 HR"
                      hint="岗位不在你投递库中的会话视为 HR 主动联系：自动拉黑该公司并禁止回复；关闭后这类会话恢复走 AI 评审流程。"
                      checked={reply.block_proactive_hr}
                      onChange={(v) => saveReply({ block_proactive_hr: v })}
                    />
                    <Row
                      label="跟进提醒"
                      hint="我发出消息后 HR 超过设定小时数没回，「消息」页会标「待跟进」。"
                      inline
                    >
                      <div className="flex items-center gap-2.5">
                        <DelayInput
                          value={reply.follow_up_hours}
                          onCommit={(v) => saveReply({ follow_up_hours: v })}
                          width={84}
                          type="number"
                          align="center"
                        />
                        <span className="t-meta" style={{ color: "var(--muted)" }}>小时（0 表示不提醒）</span>
                      </div>
                    </Row>
                  </div>
                </Card>

                <Card pad="0.5rem 1.35rem">
                  <div className="pt-3">
                    <SectionTitle title="回复节奏" />
                  </div>
                  <div className="g-divided">
                    <Row hint={bundle.reply.pace.hint} flow>
                      <Dropdown
                        value={reply.pace_label}
                        options={bundle.options.pace}
                        onChange={(v) => saveReply({ pace_label: v }, true)}
                        width={160}
                      />
                    </Row>
                    {reply.pace_label === CUSTOM && (
                      <SetGrid cols="sm:grid-cols-2">
                        <RangeField
                          label="每天回复时段"
                          lo={reply.pace_params.start_hour ?? 9}
                          hi={reply.pace_params.end_hour ?? 22}
                          onLo={(v) => saveReply({ pace_params: { ...reply.pace_params, start_hour: v } }, true)}
                          onHi={(v) => saveReply({ pace_params: { ...reply.pace_params, end_hour: v } }, true)}
                          unit="点之间回复"
                        />
                        <RangeField
                          label="收到消息后等"
                          lo={reply.pace_params.delay_min ?? 30}
                          hi={reply.pace_params.delay_max ?? 180}
                          onLo={(v) => saveReply({ pace_params: { ...reply.pace_params, delay_min: v } }, true)}
                          onHi={(v) => saveReply({ pace_params: { ...reply.pace_params, delay_max: v } }, true)}
                          unit="秒再回复"
                        />
                        <div>
                          <div className="t-label font-semibold" style={{ color: "var(--ink)" }}>
                            每回复几条歇一次
                          </div>
                          <div className="mt-2.5 flex flex-wrap items-center gap-2">
                            <NumInput
                              value={reply.pace_params.rest_every ?? 10}
                              onCommit={(v) => saveReply({ pace_params: { ...reply.pace_params, rest_every: v } }, true)}
                              width={64}
                              min={0}
                              max={1000}
                            />
                            <span className="t-micro" style={{ color: "var(--faint)" }}>条歇</span>
                            <NumInput
                              value={reply.pace_params.rest_min ?? 5}
                              onCommit={(v) => saveReply({ pace_params: { ...reply.pace_params, rest_min: v } }, true)}
                              width={64}
                              min={0}
                              max={600}
                            />
                            <span className="t-micro" style={{ color: "var(--faint)" }}>–</span>
                            <NumInput
                              value={reply.pace_params.rest_max ?? 15}
                              onCommit={(v) => saveReply({ pace_params: { ...reply.pace_params, rest_max: v } }, true)}
                              width={64}
                              min={0}
                              max={600}
                            />
                            <span className="t-micro" style={{ color: "var(--faint)" }}>分钟（0 条表示不歇）</span>
                          </div>
                        </div>
                      </SetGrid>
                    )}
                  </div>
                </Card>
              </div>
            )}

            {/* ---------------- 黑名单 ---------------- */}
            {tab === "blacklist" && (
              <div className="g-in flex flex-col gap-5">
                <Card>
                  <SectionTitle
                    title="公司 / 岗位黑名单"
                    hint="命中黑名单词条的公司与岗位一律跳过；词条同时匹配公司名与岗位名称，查企业高风险自动进榜，主动联系拉黑的 HR 也在里面。"
                  />
                  <div className="mt-4 flex flex-wrap items-center gap-2.5">
                    <input
                      value={blackDraft}
                      onChange={(e) => setBlackDraft(e.target.value)}
                      onKeyDown={(e) => e.key === "Enter" && addBlack()}
                      placeholder="输入公司名或岗位关键词后回车添加"
                      className="g-field g-input min-w-[240px] flex-1 outline-none"
                    />
                    <PrimaryButton loading={addingBlack} onClick={addBlack}>
                      <Plus size={13} /> 添加
                    </PrimaryButton>
                  </div>
                </Card>

                <Card pad="0">
                  {bundle.blacklist.length === 0 ? (
                    <div className="flex flex-col items-center gap-2 px-6 py-14 text-center">
                      <Ban size={20} style={{ color: "var(--faint)" }} />
                      <span className="t-body font-semibold" style={{ color: "var(--ink)" }}>
                        黑名单还是空的
                      </span>
                      <span className="t-meta" style={{ color: "var(--muted)" }}>
                        查企业判为高风险的公司会自动进榜，也可以在上方手动添加。
                      </span>
                    </div>
                  ) : (
                    <div className="g-divided-soft">
                      {bundle.blacklist.map((row) => (
                        <div key={row.brand_id} className="flex items-start gap-3 px-4 py-3.5">
                          <div className="min-w-0 flex-1">
                            <div className="flex flex-wrap items-center gap-2">
                              <span className="t-label font-semibold" style={{ color: "var(--ink)" }}>
                                {row.name || "未命名"}
                              </span>
                              <Badge color="gray">{row.source_label}</Badge>
                              <span className="t-micro" style={{ color: "var(--faint)" }}>{row.created_at}</span>
                            </div>
                            {row.reason && (
                              <p className="t-micro mt-1.5" style={{ color: "var(--muted)" }}>
                                {row.reason}
                              </p>
                            )}
                          </div>
                          <GhostButton danger onClick={() => removeBlack(row.brand_id)}>
                            <Trash2 size={13} /> 移出黑名单
                          </GhostButton>
                        </div>
                      ))}
                    </div>
                  )}
                </Card>
              </div>
            )}

            {/* ---------------- 系统设置 ---------------- */}
            {tab === "system" && (
              <div className="g-in flex flex-col gap-5">
                <Card pad="0.5rem 1.35rem">
                  <div className="flex flex-wrap items-center justify-between gap-3 pt-3">
                    <SectionTitle
                      title="大模型服务"
                      hint="开通后即可使用 AI 岗位筛选、简历匹配与匹配度分析。"
                    />
                    {bundle.llm.ready ? (
                      <Badge color="green">已开通 AI 服务</Badge>
                    ) : (
                      <Badge color="orange">尚未开通</Badge>
                    )}
                  </div>
                  <div className="g-divided">
                    <Row
                      label="1. 接口地址"
                      hint="支持任意 OpenAI 兼容接口，包括本地 Ollama。"
                    >
                      <DelayInput
                        value={llmForm.base_url}
                        onCommit={(v) => saveLlm({ base_url: v })}
                        placeholder={DEFAULT_BASE_URL}
                        width="100%"
                        delay={700}
                      />
                    </Row>

                    <Row
                      label="2. API Key"
                      hint="API Key 仅保存在本机，不会上传到任何服务器。"
                    >
                      <DelayInput
                        value={llmForm.api_key}
                        onCommit={(v) => saveLlm({ api_key: v })}
                        placeholder="粘贴该服务的 API Key"
                        width="100%"
                        type="password"
                        delay={700}
                      />
                    </Row>

                    <Row label="3. 模型">
                      <div className="flex flex-wrap items-center gap-2.5">
                        <Dropdown
                          value={llmForm.model}
                          options={modelOptions}
                          onChange={(v) => saveLlm({ model: v })}
                          disabled={modelOptions.length === 0}
                          placeholder="请先获取模型列表"
                          width={260}
                        />
                        <SoftButton loading={fetching} onClick={fetchModels}>
                          <RefreshCw size={13} className={fetching ? "g-spin" : undefined} />
                          {fetching ? "获取中…" : "获取模型列表"}
                        </SoftButton>
                        <SoftButton loading={testing} onClick={testLlm}>
                          {testing ? "测试中…" : "测试连接"}
                        </SoftButton>
                      </div>
                    </Row>
                  </div>
                </Card>

                <Card pad="0.5rem 1.35rem">
                  <div className="pt-3">
                    <SectionTitle
                      title="网申与浏览器"
                      hint="填表口径、文件代传、手填学习，以及浏览器在后台怎么跑；企业官网的提交按钮永远留给你自己点。"
                    />
                  </div>
                  <div className="g-divided">
                    <Row
                      inline
                      label="交付口径"
                      hint="决定哪些算「没填好」并交还给你：宽松只报必填空栏；中档连 AI 猜的也算待确认；严格连选填空栏也一并列出。"
                    >
                      <Dropdown
                        value={HANDOFF_LABELS[auto.handoff] ?? "中档"}
                        options={HANDOFF_OPTIONS}
                        onChange={(v) => saveAuto({ handoff: HANDOFF_IDS[v] ?? "balanced" })}
                        width={140}
                      />
                    </Row>
                    <SwitchRow
                      label="自动上传简历 / 寸照"
                      hint="关掉后文件栏留空，等你在浏览器里自己选文件。"
                      checked={auto.upload_files}
                      onChange={(v) => saveAuto({ upload_files: v })}
                    />
                    <SwitchRow
                      label="学习我在浏览器里手填的内容"
                      hint="你在企业官网页面上自己补的那几栏，回读后记进本机资料库，换个官网再遇到直接有值可填。学进去的条目会先由 AI 判一遍有没有效、并把同义叫法归并成一条。关掉只填不学。"
                      checked={auto.learn_web}
                      onChange={(v) => saveAuto({ learn_web: v })}
                    />
                    <SwitchRow
                      label="把认可的 AI 推算记进资料库"
                      hint="网申面板上「AI 推理」那一栏你留着没删的，点「填入浏览器」后就存进本机资料库，换个官网再遇到同类栏目直接有值。关掉只填不记。"
                      checked={auto.learn_ai}
                      onChange={(v) => saveAuto({ learn_ai: v })}
                    />
                    <SwitchRow
                      label="后台静默运行"
                      hint="浏览器窗口移出屏幕，不打扰工作；需要时用顶栏「查看浏览器」调出。"
                      checked={background}
                      onChange={toggleBackground}
                    />
                  </div>
                </Card>

                <Card pad="0.5rem 1.35rem">
                  <div className="pt-3">
                    <SectionTitle
                      title="AI 识图与核对"
                      hint="验证码、认不出的栏位、扫描清单和页面对不上时，交给多模态模型处理；需要先开通大模型服务，且模型支持读图。"
                    />
                  </div>
                  <div className="g-divided">
                    <div>
                      <SwitchRow
                        label="AI 自动识别验证码"
                        hint="使用「大模型服务」里选中的模型，模型需支持读图。"
                        checked={captcha.enabled}
                        onChange={(v) => saveCaptcha({ enabled: v })}
                      />
                    </div>

                    <div>
                      <SwitchRow
                        label="开启截图定位"
                        hint="截图会发给你配置的模型服务，画面里可能有个人资料，确认后再开。"
                        checked={locate.enabled}
                        onChange={(v) => saveLocate({ enabled: v })}
                      />
                      <Row
                        inline
                        label="护栏"
                        hint="认不出的栏位一律标「请手动填写」，不会硬塞一个猜的值。"
                      >
                        <div className="flex flex-wrap items-center gap-2.5">
                          <span className="t-micro" style={{ color: "var(--faint)" }}>
                            单次上限
                          </span>
                          <NumInput
                            value={locate.max_fields}
                            onCommit={(v) => saveLocate({ max_fields: v })}
                            width={72}
                            min={1}
                            max={10}
                          />
                          <span className="t-micro" style={{ color: "var(--faint)" }}>
                            超时（秒）
                          </span>
                          <NumInput
                            value={locate.timeout}
                            onCommit={(v) => saveLocate({ timeout: v })}
                            width={72}
                            min={5}
                            max={60}
                          />
                        </div>
                      </Row>
                    </div>

                    <div>
                      <SwitchRow
                        label="开启视觉核对"
                        hint="只补对照表的行，不会替你点页面上的任何东西；截图会发给你配置的模型服务。"
                        checked={vision.enabled}
                        onChange={(v) => saveVision({ enabled: v })}
                      />
                      <SwitchRow
                        label="给已填内容打码"
                        hint="登录后的招聘系统常把姓名、手机号预填在表里，涂黑之后再截图。"
                        checked={vision.mask_sensitive}
                        onChange={(v) => saveVision({ mask_sensitive: v })}
                      />
                      <Row
                        inline
                        label="护栏"
                        hint="超时或模型说不清楚就沿用普通扫描的结果，不会卡住流程。"
                      >
                        <div className="flex flex-wrap items-center gap-2.5">
                          <span className="t-micro" style={{ color: "var(--faint)" }}>
                            超时（秒）
                          </span>
                          <NumInput
                            value={vision.timeout}
                            onCommit={(v) => saveVision({ timeout: v })}
                            width={72}
                            min={5}
                            max={120}
                          />
                          <span className="t-micro" style={{ color: "var(--faint)" }}>
                            最多分片
                          </span>
                          <NumInput
                            value={vision.max_tiles}
                            onCommit={(v) => saveVision({ max_tiles: v })}
                            width={72}
                            min={1}
                            max={12}
                          />
                        </div>
                      </Row>
                    </div>
                  </div>
                </Card>
              </div>
            )}
          </>
        )}
      </div>
    </PageShell>
  );
}


/** 高分岗位自动定制简历设置。 */
async function saveTailor(
  enabled: boolean,
  minScore: number,
  setBundle: React.Dispatch<React.SetStateAction<Bundle | null>>,
  markSaved: () => void,
) {
  setBundle((b) => (b ? { ...b, tailor: { enabled, min_score: minScore } } : b));
  try {
    await api.put("/api/config/tailor", { enabled, min_score: minScore });
    markSaved();
  } catch (err) {
    toast.error((err as Error).message);
  }
}

/** 分组说明：按分组标题给一句人话，认不出的分组退回通用说明。 */
const APPLY_GROUP_HINTS: Record<string, string> = {
  基本信息: "企业官网的网申表常问这些；内容只存在本机，不会上传。",
  联系方式: "网申表里的手机号、邮箱、地址等栏目会填这里。",
  教育背景: "校招与登记表里的学历、成绩、证书信息。",
  求职意向: "期望薪资、到岗时间、实习安排这类意向信息。",
  经历与能力: "工作、实习、项目与技能的长文本，会整段填进对应栏。",
  紧急联系人: "部分企业的登记表要求填写紧急联系人。",
  其他信息: "内推码、作品集链接等零散信息。",
};

/** 本机学会的字段：只读展示 + 单条删除 / 整表清空（内容来自各个官网的手工补填）。 */
/** 一条学会的字段。手填的、模型推的分开摆，来源徽章与树形视图同一口径。 */
function LearnedRow({
  row,
  onConfirm,
  onForget,
}: {
  row: ApplyLearnedRow;
  onConfirm?: (key: string) => void;
  onForget: (key: string) => void;
}) {
  const shown = row.value;
  return (
    <div className="g-learned-row">
      {/* 左边一栏：栏目名一行、内容一行、依据一行，都齐着左边起头 */}
      <div className="g-learned-main">
        <div className="g-learned-head">
          <span className="g-learned-label" title={row.label}>
            {row.label}
          </span>
          <Badge color={sourceTone(row.source)}>{row.source}</Badge>
          {row.domain && <Badge color="gray">{row.domain}</Badge>}
          {row.updated_at && (
            <span className="g-learned-time" title={row.updated_at}>
              {row.updated_at}
            </span>
          )}
        </div>
        <p className="g-learned-value" title={shown}>
          {shown}
        </p>
        {row.basis && <p className="g-learned-basis">依据：{row.basis}</p>}
      </div>
      {/* 右边一栏：两个动作竖着排，永远贴着右边缘，不跟内容抢地方 */}
      <div className="g-learned-side">
        {onConfirm && (
          <SoftButton onClick={() => onConfirm(row.key)}>
            <Check size={13} /> 推得对，保留
          </SoftButton>
        )}
        <GhostButton danger onClick={() => onForget(row.key)}>
          <Trash2 size={13} /> 忘掉
        </GhostButton>
      </div>
    </div>
  );
}

function LearnedFields({
  rows,
  onConfirm,
  onForget,
  onClear,
}: {
  rows: ApplyLearnedRow[];
  onConfirm: (key: string) => void;
  onForget: (key: string) => void;
  onClear: () => void;
}) {
  if (rows.length === 0) {
    return (
      <div className="py-4 t-micro" style={{ color: "var(--faint)" }}>
        还没学到新字段。网申时手工补了资料库里没有的栏目，这里就会自动多出一条。
      </div>
    );
  }
  // 和树形视图一样分成两块：手填学到的、以及模型推出来等着核对的
  const manual = rows.filter((row) => row.source !== "AI推算");
  const inferred = rows.filter((row) => row.source === "AI推算");
  return (
    <div className="py-4">
      <div className="mb-3 flex items-center justify-between gap-3">
        <span className="t-micro" style={{ color: "var(--faint)" }}>
          共 {rows.length} 条，换个官网再遇到同名列时会自动带上。
        </span>
        <GhostButton danger onClick={onClear}>
          <Trash2 size={13} /> 全部清空
        </GhostButton>
      </div>

      {manual.length > 0 && (
        <div className="g-divided-soft">
          {manual.map((row) => (
            <LearnedRow key={row.key} row={row} onForget={onForget} />
          ))}
        </div>
      )}

      {inferred.length > 0 && (
        <div className="mt-5">
          <div className="mb-2 flex flex-wrap items-center gap-2">
            <span className="t-label font-semibold" style={{ color: "var(--ink)" }}>
              AI 推理（待核对）
            </span>
            <Badge color="ink">待核对</Badge>
            <span className="t-micro" style={{ color: "var(--faint)" }}>
              模型按已有资料推出来的。核对办法：看「依据」——推得对就点「推得对，保留」，
              推错了就点「忘掉」；留着的会随网申面板的「填入浏览器」一起入库。
            </span>
          </div>
          <div className="g-divided-soft">
            {inferred.map((row) => (
              <LearnedRow key={row.key} row={row} onConfirm={onConfirm} onForget={onForget} />
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

