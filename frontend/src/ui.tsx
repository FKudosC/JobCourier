/**
 * UI 基础件（Radix 封装）。
 *
 * 颜色 / 圆角 / 阴影 / 字号一律取自 theme.css 的令牌，组件里不出现字面色值。
 * 视觉语言：抬升面只用发丝线 + 暖影，下沉面用更深的纸色，层级靠排版而不是阴影堆叠。
 */

import * as Dialog from "@radix-ui/react-dialog";
import * as SwitchPrimitive from "@radix-ui/react-switch";
import { Check, ChevronDown, X } from "lucide-react";
import { useEffect, useRef, useState } from "react";

/* ---- 设计令牌（与 theme.css 的 :root 一一对应） ---- */

export const ACCENT = "var(--accent)";
export const ACCENT_HI = "var(--accent-hi)";
export const ACCENT_DEEP = "var(--accent-deep)";
export const ACCENT_INK = "var(--accent-ink)";
export const ACCENT_SOFT = "var(--accent-soft)";
export const ACCENT_TINT = "var(--accent-tint)";
export const ACCENT_LINE = "var(--accent-line)";
export const INK = "var(--ink)";
export const TEXT = "var(--text)";
export const MUTED = "var(--muted)";
export const FAINT = "var(--faint)";
export const BORDER = "var(--line)";
export const BORDER_STRONG = "var(--line-2)";
export const SOFT = "var(--soft)";
export const SURFACE = "var(--sheet)";
export const SURFACE_2 = "var(--sheet-2)";
export const SURFACE_3 = "var(--sheet-3)";
export const GOOD = "var(--good)";
export const GOOD_SOFT = "var(--good-soft)";
export const BAD = "var(--bad)";
export const BAD_SOFT = "var(--bad-soft)";
export const BAD_LINE = "var(--bad-line)";
export const WARN = "var(--warn)";
export const WARN_SOFT = "var(--warn-soft)";
export const WARN_LINE = "var(--warn-line)";
export const SALARY = "var(--salary)";

export const BTN = "g-btn";

/* ---- 按钮 ---- */

export function PrimaryButton({
  children,
  onClick,
  disabled,
  loading,
  tone = "accent",
  className = "",
  title,
}: {
  children: React.ReactNode;
  onClick?: () => void;
  disabled?: boolean;
  loading?: boolean;
  tone?: "accent" | "danger";
  className?: string;
  title?: string;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled || loading}
      title={title}
      className={`g-btn ${tone === "danger" ? "g-btn-danger" : "g-btn-primary"} ${className}`}
    >
      {loading && <Spinner size={13} />}
      {children}
    </button>
  );
}

/** 次级实心按钮：白纸面 + 稍重的描边，层级低于主按钮。 */
export function SoftButton({
  children,
  onClick,
  disabled,
  loading,
  title,
  className = "",
}: {
  children: React.ReactNode;
  onClick?: () => void;
  disabled?: boolean;
  loading?: boolean;
  title?: string;
  className?: string;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled || loading}
      title={title}
      className={`g-btn g-btn-soft ${className}`}
    >
      {loading && <Spinner size={13} />}
      {children}
    </button>
  );
}

export function GhostButton({
  children,
  onClick,
  disabled,
  danger,
  variant = "outline",
  className = "",
  title,
}: {
  children: React.ReactNode;
  onClick?: () => void;
  disabled?: boolean;
  danger?: boolean;
  /** outline = 描边按钮；plain = 三级文字按钮（无边框无底色） */
  variant?: "outline" | "plain";
  className?: string;
  title?: string;
}) {
  const plain = variant === "plain";
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      title={title}
      className={`g-btn ${plain ? "g-btn-plain" : "g-btn-ghost"} ${className}`}
      style={danger ? { color: BAD } : undefined}
    >
      {children}
    </button>
  );
}

export function IconButton({
  children,
  onClick,
  disabled,
  title,
  size = 30,
  active,
}: {
  children: React.ReactNode;
  onClick?: () => void;
  disabled?: boolean;
  title?: string;
  size?: number;
  active?: boolean;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      title={title}
      aria-label={title}
      className="g-btn g-btn-ghost"
      style={{
        width: size,
        height: size,
        padding: 0,
        ...(active ? { background: ACCENT_SOFT, color: ACCENT_DEEP, borderColor: ACCENT_LINE } : null),
      }}
    >
      {children}
    </button>
  );
}

export function Spinner({ size = 14 }: { size?: number }) {
  return (
    <span
      aria-hidden
      className="inline-block shrink-0"
      style={{
        width: size,
        height: size,
        border: `${Math.max(2, Math.round(size / 7))}px solid currentColor`,
        borderTopColor: "transparent",
        borderRadius: "999px",
        opacity: 0.9,
        animation: "g-spin .65s linear infinite",
      }}
    />
  );
}

/* ---- 开关 ---- */

export function Toggle({
  checked,
  onChange,
  disabled,
}: {
  checked: boolean;
  onChange: (v: boolean) => void;
  disabled?: boolean;
}) {
  return (
    <SwitchPrimitive.Root
      checked={checked}
      disabled={disabled}
      onCheckedChange={onChange}
      className="relative h-[22px] w-[38px] shrink-0 cursor-pointer rounded-full disabled:cursor-not-allowed disabled:opacity-45"
      style={{
        background: checked ? ACCENT : "var(--sheet-3)",
        border: `1px solid ${checked ? ACCENT : BORDER_STRONG}`,
        transition: "background-color .18s var(--ease), border-color .18s var(--ease)",
      }}
    >
      <SwitchPrimitive.Thumb
        className="block h-[16px] w-[16px] rounded-full"
        style={{
          transform: checked ? "translateX(19px)" : "translateX(2px)",
          background: checked ? ACCENT_INK : "var(--faint)",
          boxShadow: "var(--sh-1)",
          transition: "transform .2s var(--ease)",
        }}
      />
    </SwitchPrimitive.Root>
  );
}

export function SwitchRow({
  label,
  hint,
  checked,
  onChange,
  inline,
  children,
}: {
  label: string;
  hint: string;
  checked: boolean;
  onChange: (v: boolean) => void;
  inline?: boolean;
  children?: React.ReactNode;
}) {
  return (
    <div className="flex w-full items-start gap-4 py-4">
      <div className="min-w-0 flex-1">
        <div className="t-body font-semibold" style={{ color: INK }}>
          {label}
        </div>
        <div className="t-meta mt-1" style={{ color: MUTED, maxWidth: "68ch" }}>
          {hint}
        </div>
        {/* inline 的控件不占额外行高：整行右侧独立一列、垂直居中，行高只由文字决定 */}
        {!inline && children && <div className="mt-2.5 flex flex-col gap-1.5">{children}</div>}
      </div>
      {inline && children && (
        <div className="flex shrink-0 flex-wrap items-center gap-x-4 gap-y-2 self-center">
          {children}
        </div>
      )}
      <Toggle checked={checked} onChange={onChange} />
    </div>
  );
}

/* ---- 勾选框 ---- */

export function CheckBox({
  checked,
  onChange,
  disabled,
  size = 17,
}: {
  checked: boolean;
  onChange: () => void;
  disabled?: boolean;
  size?: number;
}) {
  return (
    <span
      role="checkbox"
      aria-checked={checked}
      aria-disabled={disabled}
      tabIndex={disabled ? -1 : 0}
      onKeyDown={(e) => {
        if (!disabled && (e.key === " " || e.key === "Enter")) {
          e.preventDefault();
          onChange();
        }
      }}
      onClick={() => !disabled && onChange()}
      className="flex shrink-0 cursor-pointer items-center justify-center"
      style={{
        width: size,
        height: size,
        borderRadius: 5,
        border: `1.5px solid ${checked ? ACCENT : BORDER_STRONG}`,
        background: checked ? ACCENT : "var(--sheet)",
        opacity: disabled ? 0.4 : 1,
        color: ACCENT_INK,
        transition: "background-color .16s var(--ease), border-color .16s var(--ease)",
      }}
    >
      {checked && <Check size={Math.round(size * 0.7)} strokeWidth={3.2} />}
    </span>
  );
}

/* ---- 徽章：方角小旗，不用胶囊 ---- */

export function Badge({
  children,
  color = "gray",
}: {
  children: React.ReactNode;
  color?: "gray" | "green" | "red" | "blue" | "orange" | "teal" | "ink";
}) {
  const palette: Record<string, { bg: string; fg: string }> = {
    gray: { bg: "var(--soft)", fg: MUTED },
    green: { bg: GOOD_SOFT, fg: GOOD },
    red: { bg: BAD_SOFT, fg: BAD },
    blue: { bg: ACCENT_SOFT, fg: ACCENT_DEEP },
    orange: { bg: WARN_SOFT, fg: WARN },
    teal: { bg: ACCENT_SOFT, fg: ACCENT_DEEP },
    ink: { bg: "var(--ink)", fg: "var(--paper)" },
  };
  const p = palette[color] ?? palette.gray;
  return (
    <span
      className="t-micro inline-flex shrink-0 items-center whitespace-nowrap font-semibold"
      style={{
        background: p.bg,
        color: p.fg,
        borderRadius: 4,
        padding: "2px 6px",
        letterSpacing: "0.02em",
      }}
    >
      {children}
    </span>
  );
}

/* ---- 面 ---- */

export function Card({
  children,
  className = "",
  pad = "1.15rem 1.25rem",
  style,
}: {
  children: React.ReactNode;
  className?: string;
  pad?: string;
  style?: React.CSSProperties;
}) {
  return (
    <div className={`g-card ${className}`} style={{ padding: pad, ...style }}>
      {children}
    </div>
  );
}

/** 分节标题：短竖线 + 标题（+ 可选说明），比"卡片标题"更轻。 */
export function SectionTitle({
  title,
  hint,
  right,
}: {
  title: React.ReactNode;
  hint?: React.ReactNode;
  right?: React.ReactNode;
}) {
  return (
    <div className="flex w-full items-start gap-3">
      <div className="min-w-0 flex-1">
        <div className="g-sect t-h2" style={{ color: INK }}>
          {title}
        </div>
        {hint && (
          <p className="t-meta mt-1.5" style={{ color: MUTED, maxWidth: "68ch" }}>
            {hint}
          </p>
        )}
      </div>
      {right}
    </div>
  );
}

/* ---- 指标 ---- */

export function Stat({
  label,
  value,
  unit,
  hint,
}: {
  label: React.ReactNode;
  value: React.ReactNode;
  unit?: string;
  hint?: string;
}) {
  return (
    <div className="flex min-w-0 flex-col">
      <span className="t-micro font-semibold" style={{ color: FAINT, letterSpacing: "0.06em" }}>
        {label}
      </span>
      <span className="mt-2 flex items-baseline gap-1">
        <span className="t-num" style={{ fontSize: "1.75rem", fontWeight: 500, color: INK, lineHeight: 1.1 }}>
          {value}
        </span>
        {unit && (
          <span className="t-meta" style={{ color: MUTED }}>
            {unit}
          </span>
        )}
      </span>
      {hint && (
        <span className="t-micro mt-1.5" style={{ color: FAINT }}>
          {hint}
        </span>
      )}
    </div>
  );
}

/* ---- 输入 ---- */

export function TextInput({
  value,
  onChange,
  placeholder,
  type = "text",
  width,
  disabled,
}: {
  value: string;
  onChange: (v: string) => void;
  placeholder?: string;
  type?: string;
  width?: number | string;
  disabled?: boolean;
}) {
  return (
    <input
      type={type}
      value={value}
      disabled={disabled}
      placeholder={placeholder}
      onChange={(e) => onChange(e.target.value)}
      className="g-field g-input outline-none"
      style={{ width }}
    />
  );
}

export function Dropdown({
  value,
  options,
  onChange,
  width,
  disabled,
  placeholder,
}: {
  value: string;
  options: string[];
  onChange: (v: string) => void;
  width?: number | string;
  disabled?: boolean;
  placeholder?: string;
}) {
  return (
    <div className="relative inline-flex items-center" style={{ width }}>
      <select
        value={value}
        disabled={disabled}
        onChange={(e) => onChange(e.target.value)}
        className="g-field g-input w-full cursor-pointer appearance-none pr-7 outline-none disabled:cursor-not-allowed disabled:opacity-45"
        style={{ width }}
      >
        {placeholder && (
          <option value="" disabled hidden>
            {placeholder}
          </option>
        )}
        {options.map((o) => (
          <option key={o} value={o}>
            {o}
          </option>
        ))}
      </select>
      <ChevronDown
        size={14}
        className="pointer-events-none absolute right-2"
        style={{ color: FAINT }}
      />
    </div>
  );
}

/* ---- 下拉多选（带搜索） ---- */

export function ComboMulti({
  title,
  options,
  selected,
  onToggle,
  placeholder = "选择",
  hint,
}: {
  title: string;
  options: string[];
  selected: string[];
  onToggle: (label: string) => void;
  placeholder?: string;
  hint?: string;
}) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const inputRef = useRef<HTMLInputElement | null>(null);
  const wrapRef = useRef<HTMLDivElement | null>(null);
  const filtered = options.filter((o) => o.includes(query));

  // 点击组件外部收起。用 pointerdown 而不是 input 的 onBlur：mousedown 选选项时
  // 焦点会先丢，靠 blur + setTimeout 猜时间既脆又慢。
  useEffect(() => {
    if (!open) return;
    const onPointerDown = (e: PointerEvent) => {
      if (!wrapRef.current?.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("pointerdown", onPointerDown);
    return () => document.removeEventListener("pointerdown", onPointerDown);
  }, [open]);

  return (
    <div className="w-full">
      <div className="t-label font-semibold" style={{ color: INK }}>
        {title}
      </div>
      {hint ? (
        <div className="t-micro mt-1" style={{ color: FAINT, maxWidth: "72ch" }}>
          {hint}
        </div>
      ) : null}
      <div ref={wrapRef} className="relative mt-2.5">
        <div
          className="g-field flex min-h-[36px] w-full cursor-pointer flex-wrap items-center gap-1 px-1.5 py-1"
          style={open ? { borderColor: ACCENT, background: SURFACE, boxShadow: `0 0 0 3px ${ACCENT_TINT}` } : undefined}
          onMouseDown={(e) => {
            const t = e.target as HTMLElement;
            if (t.tagName === "INPUT" || t.closest("[data-chip]")) return;
            e.preventDefault();
            inputRef.current?.focus();
          }}
          onClick={() => !open && setOpen(true)}
        >
          {selected.map((s) => (
            <span
              key={s}
              data-chip
              className="t-micro inline-flex items-center gap-1 font-medium"
              style={{
                background: ACCENT_SOFT,
                color: ACCENT_DEEP,
                borderRadius: 4,
                padding: "2px 5px",
              }}
              onClick={(e) => {
                e.stopPropagation();
                onToggle(s);
              }}
            >
              {s}
              <X size={11} className="cursor-pointer" />
            </span>
          ))}
          <input
            ref={inputRef}
            value={open ? query : ""}
            placeholder={selected.length > 0 ? "" : placeholder}
            onChange={(e) => setQuery(e.target.value)}
            onFocus={() => setOpen(true)}
            className="t-body min-w-[56px] flex-1 border-none bg-transparent px-1 outline-none"
          />
          <ChevronDown size={15} className="mr-1 shrink-0" style={{ color: FAINT }} />
        </div>
        {open && (
          <div
            className="g-menu g-scroll absolute left-0 right-0 top-[calc(100%+4px)] max-h-64 overflow-y-auto"
            onMouseDown={(e) => e.preventDefault()}
          >
            {filtered.length === 0 && (
              <div className="t-meta px-3 py-2" style={{ color: FAINT }}>
                没有匹配项
              </div>
            )}
            {filtered.map((label) => {
              const on = selected.includes(label);
              return (
                <div
                  key={label}
                  className="g-menu-item"
                  style={on ? { color: ACCENT_DEEP, fontWeight: 600 } : undefined}
                  onClick={() => onToggle(label)}
                >
                  {label}
                  {on && <Check size={14} className="ml-auto" />}
                </div>
              );
            })}
          </div>
        )}
      </div>
    </div>
  );
}

/* ---- 居中弹窗（只用于短确认） ---- */

export function Modal({
  open,
  onOpenChange,
  title,
  description,
  children,
  width = 460,
}: {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  title?: React.ReactNode;
  description?: React.ReactNode;
  children: React.ReactNode;
  width?: number;
}) {
  return (
    <Dialog.Root open={open} onOpenChange={onOpenChange}>
      <Dialog.Portal>
        <Dialog.Overlay className="g-overlay" />
        <Dialog.Content className="g-modal" style={{ width: `min(92vw, ${width}px)` }}>
          <Dialog.Close asChild>
            <button
              aria-label="关闭"
              className="g-btn g-btn-plain absolute right-3 top-3"
              style={{ padding: 6 }}
            >
              <X size={16} />
            </button>
          </Dialog.Close>
          {title && (
            <Dialog.Title className="t-h2 pr-8" style={{ color: INK }}>
              {title}
            </Dialog.Title>
          )}
          {description && (
            <Dialog.Description className="t-meta mt-2" style={{ color: MUTED }}>
              {description}
            </Dialog.Description>
          )}
          <div className="mt-4">{children}</div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}

/* ---- 右侧滑出面板：长内容一律用它 ---- */

export function SlideOver({
  open,
  onOpenChange,
  title,
  description,
  children,
  footer,
  width = 660,
}: {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  title?: React.ReactNode;
  description?: React.ReactNode;
  children: React.ReactNode;
  footer?: React.ReactNode;
  width?: number;
}) {
  return (
    <Dialog.Root open={open} onOpenChange={onOpenChange}>
      <Dialog.Portal>
        <Dialog.Overlay className="g-overlay" />
        <Dialog.Content
          className="g-in-slide fixed right-0 top-0 flex h-full flex-col"
          style={{
            zIndex: "var(--z-dialog)",
            width: `min(94vw, ${width}px)`,
            background: "var(--sheet)",
            borderLeft: `1px solid ${BORDER_STRONG}`,
            boxShadow: "var(--sh-pop)",
          }}
          aria-describedby={undefined}
        >
          <header className="shrink-0 px-7 pb-4 pt-6" style={{ borderBottom: `1px solid ${BORDER}` }}>
            <span className="t-eyebrow">详情</span>
            {title && (
              <Dialog.Title className="t-h1 mt-1.5 pr-10" style={{ color: INK }}>
                {title}
              </Dialog.Title>
            )}
            {description && (
              <Dialog.Description className="t-meta mt-2" style={{ color: MUTED }}>
                {description}
              </Dialog.Description>
            )}
            <Dialog.Close asChild>
              <button
                aria-label="关闭"
                className="g-btn g-btn-plain absolute right-5 top-5"
                style={{ padding: 6 }}
              >
                <X size={18} />
              </button>
            </Dialog.Close>
          </header>
          <div className="g-scroll min-h-0 flex-1 overflow-y-auto px-7 py-6">{children}</div>
          {footer && (
            <footer
              className="flex shrink-0 items-center gap-2 px-7 py-4"
              style={{ borderTop: `1px solid ${BORDER}`, background: "var(--sheet-2)" }}
            >
              {footer}
            </footer>
          )}
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}

/* ---- 多行文本 ---- */

export function TextArea({
  value,
  onChange,
  placeholder,
  minHeight = 220,
  debounce = 0,
}: {
  value: string;
  onChange: (v: string) => void;
  placeholder?: string;
  minHeight?: number;
  debounce?: number;
}) {
  const [local, setLocal] = useState(value);
  const timer = useRef<ReturnType<typeof setTimeout>>(undefined);
  useEffect(() => {
    if (document.activeElement?.tagName !== "TEXTAREA") setLocal(value);
  }, [value]);
  return (
    <textarea
      value={local}
      placeholder={placeholder}
      onChange={(e) => {
        setLocal(e.target.value);
        if (debounce > 0) {
          clearTimeout(timer.current);
          timer.current = setTimeout(() => onChange(e.target.value), debounce);
        } else {
          onChange(e.target.value);
        }
      }}
      className="g-field t-body w-full px-3 py-2.5 outline-none"
      style={{ minHeight, resize: "vertical", lineHeight: 1.65 }}
    />
  );
}

/* ---- 标签框 ---- */

export function TagBox({
  tags,
  onRemove,
  onAdd,
  placeholder = "手动添加，回车确认",
  hint,
}: {
  tags: string[];
  onRemove: (tag: string) => void;
  onAdd: (tag: string) => void;
  placeholder?: string;
  hint?: string;
}) {
  const [draft, setDraft] = useState("");
  const commit = () => {
    const v = draft.trim();
    if (v) onAdd(v);
    setDraft("");
  };
  return (
    <div>
      <div className="g-field flex min-h-[36px] w-full flex-wrap items-center gap-1 px-1.5 py-1">
        {tags.map((t) => (
          <span
            key={t}
            className="t-micro inline-flex items-center gap-1 font-medium"
            style={{ background: SOFT, color: TEXT, borderRadius: 4, padding: "2px 5px" }}
          >
            {t}
            <X size={11} className="cursor-pointer" style={{ color: FAINT }} onClick={() => onRemove(t)} />
          </span>
        ))}
        <input
          value={draft}
          placeholder={tags.length === 0 ? placeholder : ""}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && commit()}
          className="t-body min-w-[80px] flex-1 border-none bg-transparent px-1 outline-none"
        />
      </div>
      {hint && (
        <div className="t-micro mt-2" style={{ color: FAINT }}>
          {hint}
        </div>
      )}
    </div>
  );
}

/* ---- 骨架屏 ---- */

export function Skeleton({
  className = "",
  style,
}: {
  className?: string;
  style?: React.CSSProperties;
}) {
  return <div className={`g-skel ${className}`} style={style} />;
}

/* ---- 空状态 ---- */

export function EmptyState({
  icon,
  title,
  hint,
  action,
}: {
  icon: React.ReactNode;
  title: string;
  hint?: string;
  action?: React.ReactNode;
}) {
  return (
    <div className="flex h-full w-full items-center justify-center p-8">
      <div className="g-empty flex w-full max-w-[400px] flex-col items-center px-8 py-10 text-center">
        <span
          className="mb-4 flex h-11 w-11 items-center justify-center"
          style={{ borderRadius: 12, background: ACCENT_SOFT, color: ACCENT_DEEP }}
        >
          {icon}
        </span>
        <div className="t-body font-semibold" style={{ color: INK }}>
          {title}
        </div>
        {hint && (
          <div className="t-meta mt-2" style={{ color: MUTED, maxWidth: "34ch" }}>
            {hint}
          </div>
        )}
        {action && <div className="mt-5">{action}</div>}
      </div>
    </div>
  );
}

/* ---- 通用 Hook ---- */

/** 搜索框之类高频输入的防抖延迟，统一走这个常量。 */
export const DEBOUNCE_MS = 320;

/** 输入防抖：值停止变化 delay 毫秒后才返回新值，用于避免每敲一个字就发请求。 */
export function useDebouncedValue<T>(value: T, delay: number = DEBOUNCE_MS): T {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const t = setTimeout(() => setDebounced(value), delay);
    return () => clearTimeout(t);
  }, [value, delay]);
  return debounced;
}
