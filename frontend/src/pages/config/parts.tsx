import { useEffect, useRef, useState } from "react";

/* ------------------------------------------------------------------ */
/* 输入件：失焦 / 停顿后才提交，避免每敲一个字就请求                    */
/* ------------------------------------------------------------------ */

export function DelayInput({
  value,
  onCommit,
  placeholder,
  width,
  delay = 600,
  type = "text",
  align,
}: {
  value: string;
  onCommit: (v: string) => void;
  placeholder?: string;
  width?: number | string;
  delay?: number;
  type?: string;
  align?: "left" | "right" | "center";
}) {
  const [local, setLocal] = useState(value);
  const last = useRef(value);
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);

  useEffect(() => {
    if (document.activeElement?.tagName !== "INPUT") {
      setLocal(value);
      last.current = value;
    }
  }, [value]);

  useEffect(() => () => clearTimeout(timer.current), []);

  const commit = (raw: string) => {
    if (raw !== last.current) {
      last.current = raw;
      onCommit(raw);
    }
  };

  return (
    <input
      type={type}
      value={local}
      placeholder={placeholder}
      onChange={(e) => {
        const v = e.target.value;
        setLocal(v);
        clearTimeout(timer.current);
        timer.current = setTimeout(() => commit(v), delay);
      }}
      onBlur={() => commit(local)}
      onKeyDown={(e) => e.key === "Enter" && (e.target as HTMLInputElement).blur()}
      className="g-field g-input outline-none"
      style={{ width, textAlign: align }}
    />
  );
}

export function NumInput({
  value,
  onCommit,
  width = 78,
  min,
  max,
  step,
}: {
  value: number;
  onCommit: (v: number) => void;
  width?: number | string;
  min?: number;
  max?: number;
  step?: number;
}) {
  const [local, setLocal] = useState(String(value));
  const last = useRef(value);

  useEffect(() => {
    if (document.activeElement?.tagName !== "INPUT") {
      setLocal(String(value));
      last.current = value;
    }
  }, [value]);

  const commit = (raw: string) => {
    let n = Number(raw);
    if (!Number.isFinite(n)) n = last.current;
    if (min !== undefined) n = Math.max(min, n);
    if (max !== undefined) n = Math.min(max, n);
    setLocal(String(n));
    if (n !== last.current) {
      last.current = n;
      onCommit(n);
    }
  };

  return (
    <input
      type="number"
      value={local}
      min={min}
      max={max}
      step={step}
      onChange={(e) => setLocal(e.target.value)}
      onBlur={(e) => commit(e.target.value)}
      onKeyDown={(e) => e.key === "Enter" && (e.target as HTMLInputElement).blur()}
      className="g-field g-input t-num outline-none"
      style={{ width }}
    />
  );
}


/* ------------------------------------------------------------------ */
/* 布局件                                                              */
/* ------------------------------------------------------------------ */

/**
 * 单个配置项：标题（+ 说明）在上、控件整行铺开在下。
 * 标题左、控件右的老做法会中留大片空白，并把说明挤成一条窄竖排，故改为纵向。
 * inline：说明照常独占一行，控件挪到整行右侧并与文字垂直居中，行高只由文字决定。
 * right 放该项的次要操作（如「AI 生成」）。
 */
export function Field({
  label,
  hint,
  right,
  inline,
  flow,
  children,
}: {
  label?: React.ReactNode;
  hint?: React.ReactNode;
  right?: React.ReactNode;
  inline?: boolean;
  flow?: boolean;
  children: React.ReactNode;
}) {
  /* flow：说明文字在左、控件固定在本行中间，控件位置不随文字长短漂移。 */
  if (flow) {
    return (
      <div className="flex h-full flex-col">
        {label && (
          <div className="t-label font-semibold" style={{ color: "var(--ink)" }}>
            {label}
          </div>
        )}
        <div
          className={`grid items-center gap-x-6 gap-y-2 sm:grid-cols-[minmax(0,1fr)_auto_minmax(0,1fr)] ${
            label ? "mt-1" : ""
          }`}
        >
          {hint && (
            <span className="t-micro" style={{ color: "var(--faint)" }}>
              {hint}
            </span>
          )}
          {children}
        </div>
      </div>
    );
  }
  /* inline：说明独占一行，控件独立成整行右侧的一列并与文字垂直居中，行高只由文字决定。 */
  if (inline) {
    return (
      <div className="flex w-full items-start gap-4">
        <div className="min-w-0 flex-1">
          {label && (
            <div className="t-body font-semibold" style={{ color: "var(--ink)" }}>
              {label}
            </div>
          )}
          {hint && (
            <div className="t-meta mt-1" style={{ color: "var(--muted)", maxWidth: "68ch" }}>
              {hint}
            </div>
          )}
        </div>
        <div className="flex shrink-0 flex-wrap items-center gap-x-4 gap-y-2 self-center">
          {children}
        </div>
        {right}
      </div>
    );
  }
  return (
    <div className="flex h-full flex-col">
      <div className="flex flex-wrap items-start justify-between gap-x-6 gap-y-2">
        <div className="min-w-0">
          {label && (
            <div className="t-label font-semibold" style={{ color: "var(--ink)" }}>
              {label}
            </div>
          )}
          {hint && (
            <div className="t-micro mt-1" style={{ color: "var(--faint)", maxWidth: "72ch" }}>
              {hint}
            </div>
          )}
        </div>
        {right}
      </div>
      {/* mt-auto：并排时把控件压到底部，让同一行的控件彼此对齐 */}
      <div className="mt-auto min-w-0 pt-2.5">{children}</div>
    </div>
  );
}

/** 一行一个配置项（上下留白；分隔线由外层 .g-divided 负责）。 */
export function Row(props: React.ComponentProps<typeof Field>) {
  return (
    <div className="py-4">
      <Field {...props} />
    </div>
  );
}

/** 把几个短配置并排放，避免页面拉成长条；窄屏自动落成单列。 */
export function SetGrid({
  cols = "sm:grid-cols-3",
  children,
}: {
  cols?: string;
  children: React.ReactNode;
}) {
  return <div className={`grid gap-x-8 gap-y-6 py-4 ${cols}`}>{children}</div>;
}

export function RangeField({
  label,
  lo,
  hi,
  onLo,
  onHi,
  unit,
  step,
}: {
  label: string;
  lo: number;
  hi: number;
  onLo: (v: number) => void;
  onHi: (v: number) => void;
  unit: string;
  step?: number;
}) {
  return (
    <div>
      <div className="t-label font-semibold" style={{ color: "var(--ink)" }}>
        {label}
      </div>
      <div className="mt-2.5 flex items-center gap-2">
        <NumInput value={lo} onCommit={onLo} width={72} min={0} max={3600} step={step} />
        <span className="t-micro" style={{ color: "var(--faint)" }}>–</span>
        <NumInput value={hi} onCommit={onHi} width={72} min={0} max={3600} step={step} />
        <span className="t-micro" style={{ color: "var(--faint)" }}>{unit}</span>
      </div>
    </div>
  );
}

export function Seg({
  value,
  onChange,
  tabs,
}: {
  value: string;
  onChange: (v: string) => void;
  tabs: { id: string; label: string; icon: React.ReactNode }[];
}) {
  return (
    <div
      className="flex flex-wrap items-center gap-1 p-1"
      style={{ background: "var(--sheet-3)", border: "1px solid var(--line)", borderRadius: "var(--r-md)", width: "fit-content" }}
    >
      {tabs.map((t) => {
        const on = value === t.id;
        return (
          <button
            key={t.id}
            type="button"
            onClick={() => onChange(t.id)}
            className="g-btn"
            style={{
              background: on ? "var(--sheet)" : "transparent",
              color: on ? "var(--accent-deep)" : "var(--muted)",
              border: `1px solid ${on ? "var(--line-2)" : "transparent"}`,
              boxShadow: on ? "var(--sh-1)" : "none",
              fontWeight: on ? 600 : 500,
              padding: "0.44rem 0.82rem",
            }}
          >
            {t.icon}
            {t.label}
          </button>
        );
      })}
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* 局部辅助件                                                          */
/* ------------------------------------------------------------------ */

export function TagEditor({
  tags,
  onAdd,
  onRemove,
  placeholder,
}: {
  tags: string[];
  onAdd: (v: string) => void;
  onRemove: (v: string) => void;
  placeholder?: string;
}) {
  const [draft, setDraft] = useState("");
  const commit = () => {
    const v = draft.trim();
    if (v) onAdd(v);
    setDraft("");
  };
  return (
    <div className="g-field flex min-h-[38px] w-full flex-wrap items-center gap-1.5 px-2 py-1.5">
      {tags.map((t) => (
        <span
          key={t}
          className="t-micro inline-flex items-center gap-1 font-medium"
          style={{ background: "var(--accent-soft)", color: "var(--accent-deep)", borderRadius: 4, padding: "2px 6px" }}
        >
          {t}
          <button
            type="button"
            aria-label={`删除 ${t}`}
            onClick={() => onRemove(t)}
            style={{ background: "none", border: 0, padding: 0, cursor: "pointer", color: "inherit", lineHeight: 1 }}
          >
            ×
          </button>
        </span>
      ))}
      <input
        value={draft}
        placeholder={tags.length === 0 ? placeholder : ""}
        onChange={(e) => setDraft(e.target.value)}
        onKeyDown={(e) => e.key === "Enter" && commit()}
        className="t-body min-w-[120px] flex-1 border-none bg-transparent px-1 outline-none"
      />
    </div>
  );
}

