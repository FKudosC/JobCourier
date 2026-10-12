/** 消息：与 HR 的会话列表、聊天记录、沟通状态与待确认回复。 */

import {
  Check,
  ChevronDown,
  Download,
  ExternalLink,
  Inbox,
  MessagesSquare,
  RefreshCw,
  Search,
  Send,
  X,
} from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { toast } from "sonner";
import { api } from "../api";
import { RiskBadgeMini } from "../components/CompanyDialog";
import { PageShell } from "../Shell";
import { useRuntime } from "../store";
import {
  Badge,
  Card,
  Dropdown,
  EmptyState,
  GhostButton,
  IconButton,
  PrimaryButton,
  Skeleton,
  SoftButton,
  useDebouncedValue,
} from "../ui";

/** /api/messages/conversations 里的一条会话。 */
interface Conversation {
  boss_id: string;
  job_uid: string;
  hr_name: string;
  hr_title: string;
  company: string;
  title: string;
  salary: string;
  location: string;
  link: string;
  last_text: string;
  last_from_hr: boolean;
  last_time: string;
  status: string;
  status_label: string;
  follow_up: boolean;
  risk: string;
  risk_label: string;
}

/** 待确认回复（开启「发送前确认」后才会有）。 */
interface PendingItem {
  id: string;
  boss_id: string;
  job_uid: string;
  hr_name: string;
  company: string;
  title: string;
  text: string;
  outcome: string;
  send_resume: boolean;
  status: string;
  status_label: string;
  created_at: string;
}

interface ChatMsg {
  mid: string;
  from_hr: boolean;
  text: string;
  auto: boolean;
  time: string;
}

interface ConvResponse {
  conversations: Conversation[];
  pending: PendingItem[];
  status_filters: string[];
  status_choices: string[];
}

/** 沟通状态 → 徽章颜色。 */
const STATUS_TONE: Record<string, "green" | "blue" | "red" | "gray"> = {
  invited: "green",
  passed: "green",
  done: "blue",
  failed: "red",
  hr_rejected: "red",
  blocked: "red",
};

const CARD_FALLBACK = "[卡片消息，请在 BOSS 直聘查看]";

export default function Messages() {
  const navigate = useNavigate();
  const { rt, conn } = useRuntime();

  const [search, setSearch] = useState("");
  const debounced = useDebouncedValue(search);
  const [status, setStatus] = useState("全部");

  const [conv, setConv] = useState<ConvResponse | null>(null);
  const [listLoading, setListLoading] = useState(true);
  const [activeId, setActiveId] = useState("");
  const [chat, setChat] = useState<ChatMsg[]>([]);
  const [chatLoading, setChatLoading] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);

  const threadRef = useRef<HTMLDivElement | null>(null);

  const load = useCallback(
    async (keepId = true) => {
      setListLoading(true);
      try {
        const qs = new URLSearchParams({ search: debounced, status });
        const res = await api.get<ConvResponse>(`/api/messages/conversations?${qs.toString()}`);
        setConv(res);
        setActiveId((prev) => {
          if (keepId && prev && res.conversations.some((c) => c.boss_id === prev)) return prev;
          return res.conversations[0]?.boss_id ?? "";
        });
      } catch (err) {
        toast.error((err as Error).message);
      } finally {
        setListLoading(false);
      }
    },
    [debounced, status],
  );

  useEffect(() => {
    if (conn === "open") void load(false);
  }, [conn, load]);

  // 收到新消息 / 自动回复后刷新列表，但不打断当前选中的会话
  const stamp = `${rt?.recv_count ?? 0}-${rt?.reply_count ?? 0}`;
  useEffect(() => {
    if (conn === "open") void load(true);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [stamp]);

  const active = conv?.conversations.find((c) => c.boss_id === activeId) ?? null;

  useEffect(() => {
    if (!activeId) {
      setChat([]);
      return;
    }
    let dead = false;
    setChatLoading(true);
    api
      .get<ChatMsg[]>(`/api/messages/${encodeURIComponent(activeId)}/chat`)
      .then((res) => {
        if (!dead) setChat(res);
      })
      .catch((err) => {
        if (!dead) toast.error((err as Error).message);
      })
      .finally(() => {
        if (!dead) setChatLoading(false);
      });
    return () => {
      dead = true;
    };
  }, [activeId]);

  useEffect(() => {
    const el = threadRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [chat]);

  async function setChatStatus(label: string) {
    if (!active) return;
    try {
      await api.put(`/api/messages/${encodeURIComponent(active.boss_id)}/status`, { label });
      toast.success(label === "无" ? "已清除沟通状态" : `已标记为「${label}」`);
      void load(true);
    } catch (err) {
      toast.error((err as Error).message);
    }
  }

  async function decide(item: PendingItem, approve: boolean) {
    setBusy(item.id);
    try {
      await api.post(`/api/messages/pending/${encodeURIComponent(item.id)}/${approve ? "approve" : "dismiss"}`);
      toast.success(approve ? "已同意，将在自动回复中发出" : "已放弃这条回复");
      void load(true);
    } catch (err) {
      toast.error((err as Error).message);
    } finally {
      setBusy(null);
    }
  }

  function exportChat(bossId: string) {
    window.open(`/api/messages/export?boss_ids=${encodeURIComponent(bossId)}`);
  }

  async function openLink(url: string) {
    if (!url) {
      toast.error("该会话没有关联岗位链接");
      return;
    }
    try {
      await api.post("/api/open-link", { url });
    } catch (err) {
      toast.error((err as Error).message);
    }
  }

  const conversations = conv?.conversations ?? [];
  const pending = conv?.pending ?? [];

  return (
    <PageShell
      active="/messages"
      fill
    >
      <div className="flex min-h-0 flex-1 flex-col gap-4 pb-6 pt-6">
        {pending.length > 0 && (
          <Card pad="0" className="g-in shrink-0 overflow-hidden">
            <div className="flex items-center gap-2.5 px-4 py-3" style={{ borderBottom: "1px solid var(--line)" }}>
              <Send size={14} style={{ color: "var(--accent)" }} />
              <span className="t-label font-semibold" style={{ color: "var(--ink)" }}>
                待确认回复
              </span>
              <Badge color="orange">{pending.length}</Badge>
              <span className="t-micro ml-auto" style={{ color: "var(--faint)" }}>
                同意后在自动回复运行中自动发出
              </span>
            </div>
            <div className="g-scroll flex max-h-[186px] flex-col overflow-y-auto">
              {pending.map((item) => (
                <div key={item.id} className="flex items-start gap-3 px-4 py-3" style={{ borderBottom: "1px solid var(--line)" }}>
                  <div className="min-w-0 flex-1">
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="t-label font-semibold" style={{ color: "var(--ink)" }}>
                        {item.hr_name || "HR"}
                      </span>
                      {item.company && (
                        <span className="t-micro" style={{ color: "var(--faint)" }}>
                          {item.company}
                          {item.title ? ` · ${item.title}` : ""}
                        </span>
                      )}
                      <span className="t-micro" style={{ color: "var(--faint)" }}>{item.created_at}</span>
                      {item.send_resume && <Badge color="blue">附简历</Badge>}
                    </div>
                    <p
                      className="t-meta mt-2 whitespace-pre-wrap"
                      style={{ color: "var(--text)", maxWidth: "80ch" }}
                    >
                      {item.text || CARD_FALLBACK}
                    </p>
                  </div>
                  <div className="flex shrink-0 items-center gap-2">
                    <PrimaryButton loading={busy === item.id} onClick={() => decide(item, true)}>
                      <Check size={13} /> 同意发送
                    </PrimaryButton>
                    <GhostButton onClick={() => decide(item, false)}>
                      <X size={13} /> 放弃
                    </GhostButton>
                  </div>
                </div>
              ))}
            </div>
          </Card>
        )}

        <div className="flex min-h-0 flex-1 gap-4">
          {/* 会话列表 */}
          <Card pad="0" className="flex w-[336px] shrink-0 flex-col overflow-hidden">
            <div className="flex shrink-0 items-center gap-2 px-3 pb-2.5 pt-3">
              <div className="g-field g-combo min-w-0 flex-1">
                <span className="g-combo-selwrap">
                  <select
                    value={status}
                    onChange={(e) => setStatus(e.target.value)}
                    className="g-combo-sel"
                  >
                    {(conv?.status_filters ?? ["全部", "无状态", "待跟进"]).map((o) => (
                      <option key={o} value={o}>
                        {o}
                      </option>
                    ))}
                  </select>
                  <ChevronDown size={12} className="g-combo-chev" />
                </span>
                <span className="g-combo-div" />
                <span className="relative flex min-w-0 flex-1 items-center">
                  <Search
                    size={13}
                    className="pointer-events-none absolute left-2"
                    style={{ color: "var(--faint)" }}
                  />
                  <input
                    value={search}
                    onChange={(e) => setSearch(e.target.value)}
                    placeholder="搜索公司或岗位"
                    className="g-combo-input w-full min-w-0 outline-none"
                    style={{ paddingLeft: "1.6rem" }}
                  />
                </span>
              </div>
              <IconButton size={34} title="刷新列表" onClick={() => load(true)}>
                <RefreshCw size={15} />
              </IconButton>
              <IconButton size={34} title="导出全部会话" onClick={() => window.open("/api/messages/export")}>
                <Download size={15} />
              </IconButton>
            </div>
            <div className="flex shrink-0 items-center gap-2 px-3 pb-2.5">
              <span className="t-micro" style={{ color: "var(--faint)" }}>
                共{" "}
                <span className="t-num" style={{ color: "var(--ink)", fontWeight: 600 }}>
                  {conversations.length}
                </span>{" "}
                个会话
              </span>
            </div>

            <div className="g-scroll min-h-0 flex-1 overflow-y-auto">
              {listLoading && conversations.length === 0 && (
                <div className="flex flex-col gap-2 p-3">
                  {[0, 1, 2, 3, 4].map((i) => (
                    <Skeleton key={i} style={{ height: 62 }} />
                  ))}
                </div>
              )}
              {!listLoading && conversations.length === 0 && (
                <EmptyState
                  icon={<Inbox size={20} />}
                  title="暂无会话"
                  hint="在工作台开启自动回复后，与 HR 的消息会出现在这里。"
                />
              )}
              {conversations.map((c) => {
                const on = c.boss_id === activeId;
                const hasBadges = Boolean(c.status_label || c.follow_up || c.risk);
                return (
                  <button
                    key={c.boss_id}
                    type="button"
                    onClick={() => setActiveId(c.boss_id)}
                    className="g-row g-conv w-full border-0 text-left"
                    data-selected={on}
                    style={{ borderBottom: "1px solid var(--line)" }}
                  >
                    <span className="g-avatar" aria-hidden>
                      {(c.hr_name || "H").trim().slice(0, 1)}
                    </span>
                    <span className="min-w-0 flex-1">
                      <span className="flex items-center gap-2">
                        <span className="t-label min-w-0 flex-1 truncate font-semibold" style={{ color: "var(--ink)" }}>
                          {c.hr_name || "HR"}
                        </span>
                        <span className="t-micro t-num shrink-0" style={{ color: "var(--faint)" }}>
                          {c.last_time}
                        </span>
                      </span>
                      <span className="t-micro mt-1 block truncate" style={{ color: "var(--muted)" }}>
                        {c.company || "未知公司"}
                        {c.title ? ` · ${c.title}` : ""}
                      </span>
                      <span
                        className="t-meta mt-1 block truncate"
                        style={{ color: c.last_from_hr ? "var(--text)" : "var(--faint)" }}
                      >
                        {c.last_from_hr ? c.last_text || CARD_FALLBACK : `我：${c.last_text || CARD_FALLBACK}`}
                      </span>
                      {(hasBadges || c.salary) && (
                        <span className="mt-1.5 flex flex-wrap items-center gap-1.5">
                          {c.status_label && <Badge color={STATUS_TONE[c.status] ?? "gray"}>{c.status_label}</Badge>}
                          {c.follow_up && <Badge color="orange">待跟进</Badge>}
                          {c.salary && <span className="g-salary g-salary-sm t-num">{c.salary}</span>}
                          <RiskBadgeMini risk={c.risk} label={c.risk_label} />
                        </span>
                      )}
                    </span>
                  </button>
                );
              })}
            </div>

          </Card>

          {/* 聊天区 */}
          <Card pad="0" className="flex min-w-0 flex-1 flex-col overflow-hidden">
            {active ? (
              <>
                <div className="shrink-0 px-4 py-3" style={{ borderBottom: "1px solid var(--line)" }}>
                  <div className="flex items-start gap-3">
                    <div className="min-w-0 flex-1">
                      <div className="flex flex-wrap items-center gap-2">
                        <span className="t-h2" style={{ color: "var(--ink)" }}>{active.hr_name || "HR"}</span>
                        {active.status_label && (
                          <Badge color={STATUS_TONE[active.status] ?? "gray"}>{active.status_label}</Badge>
                        )}
                        {active.follow_up && <Badge color="orange">待跟进</Badge>}
                        <RiskBadgeMini risk={active.risk} label={active.risk_label} />
                      </div>
                      <div className="mt-1.5 flex min-w-0 flex-wrap items-center gap-x-2.5 gap-y-1">
                        <span className="g-salary t-num">{active.salary || "薪资未知"}</span>
                        <span className="t-meta min-w-0 truncate" style={{ color: "var(--muted)" }}>
                          {[active.title, active.company].filter(Boolean).join(" · ") || "未知岗位"}
                        </span>
                        {active.location && (
                          <span className="t-micro min-w-0 truncate" style={{ color: "var(--faint)" }}>
                            {active.location}
                          </span>
                        )}
                      </div>
                    </div>
                    <div className="flex shrink-0 items-center gap-2">
                      <div className="flex items-center gap-2">
                        <span className="t-micro" style={{ color: "var(--faint)" }}>沟通状态</span>
                        <Dropdown
                          value={active.status_label || "无"}
                          options={conv?.status_choices ?? ["无"]}
                          onChange={setChatStatus}
                          width={116}
                        />
                      </div>
                      <GhostButton onClick={() => navigate(`/jobs?job=${encodeURIComponent(active.job_uid)}`)} disabled={!active.job_uid}>
                        <ExternalLink size={13} /> 查看岗位
                      </GhostButton>
                      <GhostButton onClick={() => exportChat(active.boss_id)}>
                        <Download size={13} /> 导出会话
                      </GhostButton>
                    </div>
                  </div>
                </div>

                <div ref={threadRef} className="g-scroll min-h-0 flex-1 overflow-y-auto px-4 py-5">
                  {chatLoading && chat.length === 0 ? (
                    <div className="flex flex-col gap-3">
                      {[0, 1, 2].map((i) => (
                        <Skeleton key={i} style={{ height: 48, width: i % 2 ? "55%" : "70%", alignSelf: i % 2 ? "flex-end" : "flex-start" }} />
                      ))}
                    </div>
                  ) : (
                    <div className="flex flex-col gap-3.5">
                      {chat.map((m) => (
                        <div
                          key={m.mid}
                          className="flex max-w-[76%] flex-col"
                          style={{ alignSelf: m.from_hr ? "flex-start" : "flex-end" }}
                        >
                          <div className="mb-1 flex items-center gap-2" style={{ justifyContent: m.from_hr ? "flex-start" : "flex-end" }}>
                            <span className="t-micro" style={{ color: "var(--faint)" }}>
                              {m.from_hr ? "HR" : "我"}
                            </span>
                            {!m.from_hr && m.auto && <Badge color="teal">AI 自动回复</Badge>}
                            <span className="t-micro" style={{ color: "var(--faint)" }}>{m.time}</span>
                          </div>
                          <div
                            className="t-body whitespace-pre-wrap px-3.5 py-2.5"
                            style={{
                              background: m.from_hr ? "var(--sheet-3)" : "var(--accent-soft)",
                              color: m.from_hr ? "var(--text)" : "var(--accent-deep)",
                              border: `1px solid ${m.from_hr ? "var(--line)" : "var(--accent-line)"}`,
                              borderRadius: "var(--r-md)",
                            }}
                          >
                            {m.text || CARD_FALLBACK}
                          </div>
                        </div>
                      ))}
                    </div>
                  )}
                </div>

                <div className="flex shrink-0 items-center gap-2 px-4 py-3" style={{ borderTop: "1px solid var(--line)", background: "var(--sheet-2)" }}>
                  <MessagesSquare size={13} style={{ color: "var(--faint)" }} />
                  <span className="t-micro" style={{ color: "var(--muted)" }}>
                    消息由自动回复记录，需要亲自回复请到 BOSS 直聘 App 或网页
                  </span>
                  <SoftButton className="ml-auto" onClick={() => openLink(active.link)} disabled={!active.link}>
                    <ExternalLink size={13} /> 打开 BOSS 页面
                  </SoftButton>
                </div>
              </>
            ) : (
              <EmptyState
                icon={<MessagesSquare size={20} />}
                title="选择左侧会话开始查看"
                hint="消息由自动回复记录，需要亲自回复请到 BOSS 直聘 App 或网页。"
              />
            )}
          </Card>
        </div>
      </div>
    </PageShell>
  );
}
