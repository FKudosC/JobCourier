/** 简历工坊：以上传的简历为母版，按岗位重排改写，并做数字校验。 */

import { Download, FileText, Sparkles, TriangleAlert } from "lucide-react";
import { useEffect, useState } from "react";
import { toast } from "sonner";
import { api } from "../api";
import { GhostButton, PrimaryButton, SlideOver, Spinner } from "../ui";

export function TailorDialog({
  open,
  onOpenChange,
  uid,
}: {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  uid: string;
}) {
  const [label, setLabel] = useState("");
  const [content, setContent] = useState("");
  const [issues, setIssues] = useState<string[]>([]);
  const [generating, setGenerating] = useState(false);

  useEffect(() => {
    if (!open || !uid) return;
    api
      .get<{ job_label: string; content: string; issues: string[] }>(
        `/api/tailor/${encodeURIComponent(uid)}`,
      )
      .then((res) => {
        setLabel(res.job_label ?? "");
        setContent(res.content ?? "");
        setIssues(res.issues ?? []);
      })
      .catch((err) => toast.error((err as Error).message));
  }, [open, uid]);

  async function generate() {
    setGenerating(true);
    try {
      const res = await api.post<{ content: string; issues: string[] }>(
        `/api/tailor/${encodeURIComponent(uid)}/generate`,
      );
      setContent(res.content ?? "");
      setIssues(res.issues ?? []);
      toast.success("定制简历已生成");
    } catch (err) {
      toast.error((err as Error).message);
    } finally {
      setGenerating(false);
    }
  }

  function exportTxt() {
    if (!content.trim()) {
      toast.error("还没有生成结果");
      return;
    }
    window.open(`/api/tailor/${encodeURIComponent(uid)}/export?content=${encodeURIComponent(content)}`);
  }

  return (
    <SlideOver
      open={open}
      onOpenChange={onOpenChange}
      title="定制简历"
      description={label ? `${label}。以上传的简历为母版，只重排与改写，出现母版没有的数字会被程序拦截。` : "以上传的简历为母版，只重排与改写。"}
      width={780}
      footer={
        <>
          <PrimaryButton loading={generating} onClick={generate}>
            <Sparkles size={13} /> {content ? "重新生成" : "生成定制简历"}
          </PrimaryButton>
          <GhostButton onClick={exportTxt} disabled={!content.trim()}>
            <Download size={13} /> 导出 txt
          </GhostButton>
          {generating && (
            <span className="t-micro ml-1" style={{ color: "var(--faint)" }}>
              AI 正在按岗位重写简历，约十几秒…
            </span>
          )}
        </>
      }
    >
      {issues.length > 0 && (
        <div
          className="mb-5 px-4 py-3.5"
          style={{ borderRadius: "var(--r-md)", background: "var(--warn-soft)", border: "1px solid var(--warn-line)" }}
        >
          <div className="flex items-center gap-2" style={{ color: "var(--warn)" }}>
            <TriangleAlert size={15} />
            <span className="t-meta font-semibold">以下校验未通过，导出前请人工确认：</span>
          </div>
          <ul className="mt-2 flex flex-col gap-1.5">
            {issues.map((it, i) => (
              <li key={i} className="t-meta" style={{ color: "var(--warn)" }}>
                · {it}
              </li>
            ))}
          </ul>
        </div>
      )}

      {content ? (
        <>
          <span className="t-eyebrow">定制简历正文</span>
          <textarea
            value={content}
            onChange={(e) => setContent(e.target.value)}
            className="g-field t-body mt-2.5 w-full px-3.5 py-3 outline-none"
            style={{ minHeight: 460, resize: "vertical", lineHeight: 1.7 }}
            spellCheck={false}
          />
        </>
      ) : (
        <div className="flex flex-col items-center gap-3 py-20 text-center">
          <span
            className="flex items-center justify-center"
            style={{ width: 44, height: 44, borderRadius: 12, background: "var(--accent-soft)", color: "var(--accent-deep)" }}
          >
            {generating ? <Spinner size={18} /> : <FileText size={20} />}
          </span>
          <span className="t-body font-semibold" style={{ color: "var(--ink)" }}>
            {generating ? "AI 正在重写简历…" : "还没有定制简历"}
          </span>
          <span className="t-meta" style={{ color: "var(--muted)", maxWidth: "40ch" }}>
            点下方「生成定制简历」，AI 会以上传的简历为母版按岗位重排改写。
          </span>
        </div>
      )}
    </SlideOver>
  );
}
