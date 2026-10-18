/**
 * 网申资料信息树：真长成一棵树——类别是树枝，字段是叶子。
 *
 * 和「表单」视图共用同一份数据（后端 labels / values / tree 同源），只是换个摆法：
 * 树干从「个人信息库」往下走，一个分组岔出一根树枝，字段挂在枝上，枝上再挂这条
 * 认下过的别名。用来看「库里到底有什么、有没有同一个东西存成了好几条」。
 *
 * 三条规矩不变：
 * - 值只在叶子这一级就地改；敏感字段（身份证、银行卡）只显示掩码，明文不出后端。
 * - 别名挂在叶子下面，用户手动并上去的能撤销，内置别名表里的只读。
 * - 「AI 推理（待核对）」单独一枝：模型推出来的先挂在这儿，看着不对就删；
 *   留着的那些，等网申面板点过「填入浏览器」才算认可入库。
 */

import { Check, ChevronDown, ChevronRight, GitMerge, Trash2, X } from "lucide-react";
import {
  type CSSProperties,
  type PointerEvent as ReactPointerEvent,
  useRef,
  useState,
} from "react";
import { Badge, Dropdown, GhostButton, SoftButton, TextInput } from "../../ui";
import { DelayInput } from "./parts";
import type { ApplyTree, ApplyTreeGroup, ApplyTreeNode } from "./types";

/** 来源 → 徽章配色：一眼看出这条是资料原文、本机记的，还是模型推的。
 *  「表单」视图也用它，两边同一套口径。 */
export function sourceTone(source: string): "gray" | "green" | "blue" | "teal" | "ink" {
  if (source === "资料" || source === "简历") return "green";
  if (source === "记忆" || source === "沿用") return "blue";
  if (source === "自定义" || source === "手动") return "teal";
  if (source === "AI推算" || source === "推算") return "ink";
  return "gray";
}

/** 归并目标：树上所有规范字段，挑一个当「这条其实是它」。 */
interface Target {
  key: string;
  label: string;
}

/** 叶子下面挂着的别名（共指项）。内置表里的只读，用户并上去的可撤销。 */
function AliasRow({
  node,
  onUnmerge,
  bare = false,
}: {
  node: ApplyTreeNode;
  onUnmerge: (alias: string) => void;
  /** 外面已经写了「同义」这个标题时，这里就不要再顶一个图标 + 文字。 */
  bare?: boolean;
}) {
  if (node.aliases.length === 0) return null;
  return (
    <div className="flex flex-wrap items-center gap-1.5">
      {!bare && (
        <span className="t-micro inline-flex items-center gap-1" style={{ color: "var(--faint)" }}>
          <GitMerge size={11} /> 同义叫法
        </span>
      )}
      {node.aliases.map((alias) => (
        <span
          key={alias.text}
          className="t-micro inline-flex items-center gap-1 font-medium"
          title={alias.builtin ? "内置别名表里就有" : "你手动并上去的"}
          style={{
            background: alias.builtin ? "var(--soft)" : "var(--accent-soft)",
            color: alias.builtin ? "var(--muted)" : "var(--accent-deep)",
            borderRadius: 4,
            padding: "2px 6px",
          }}
        >
          {alias.text}
          {!alias.builtin && (
            <button
              type="button"
              aria-label={`撤销合并 ${alias.text}`}
              onClick={() => onUnmerge(alias.text)}
              style={{
                background: "none",
                border: 0,
                padding: 0,
                cursor: "pointer",
                color: "inherit",
                lineHeight: 1,
              }}
            >
              <X size={11} />
            </button>
          )}
        </span>
      ))}
    </div>
  );
}

/** 把这一条叫法并到别的字段上（「专业类」其实是「专业类别」）。 */
function MergeControl({
  node,
  targets,
  onMerge,
}: {
  node: ApplyTreeNode;
  targets: Target[];
  onMerge: (alias: string, key: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const [draft, setDraft] = useState("");
  // 目标按「标签｜键」拼成下拉项，挑回来时再拆出键——Dropdown 只吃字符串数组
  const options = targets
    .filter((t) => t.key !== node.canonical)
    .map((t) => `${t.label}｜${t.key}`);
  const keyOf = (option: string) => option.slice(option.lastIndexOf("｜") + 1);

  if (!open) {
    return (
      <GhostButton variant="plain" onClick={() => setOpen(true)}>
        <GitMerge size={12} /> 并到别的字段
      </GhostButton>
    );
  }
  return (
    <div className="flex flex-wrap items-center gap-2">
      <span className="t-micro" style={{ color: "var(--faint)" }}>
        「{node.key}」其实是
      </span>
      <TextInput
        value={draft}
        onChange={setDraft}
        placeholder="这条的叫法，如「专业类」"
        width={180}
      />
      <Dropdown
        value=""
        options={options}
        placeholder="选一个已有字段"
        width={220}
        onChange={(option) => {
          const key = keyOf(option);
          if (!key) return;
          onMerge(draft.trim() || node.key, key);
          setDraft("");
          setOpen(false);
        }}
      />
      <GhostButton variant="plain" onClick={() => setOpen(false)}>
        取消
      </GhostButton>
    </div>
  );
}

/** 一片叶子：一条字段资料。收起时只露「叫什么、值是什么」，点开才就地改。 */
function LeafCard({
  node,
  targets,
  onSave,
  onConfirm,
  onForget,
  onMerge,
  onUnmerge,
}: {
  node: ApplyTreeNode;
  targets: Target[];
  onSave: (values: Record<string, string>) => void;
  onConfirm: (key: string) => void;
  onForget: (key: string) => void;
  onMerge: (alias: string, key: string) => void;
  onUnmerge: (alias: string) => void;
}) {
  const [open, setOpen] = useState(false);
  // 本机学会 / AI 推算的几条存在另一张表里，不归「网申资料」管，只能整条删
  const stored = node.kind === "learned";
  // 认不出叫法的，才需要「并到别的字段」；内置字段并走了会缺栏
  const canMerge = node.orphan || node.kind === "learned";
  const tone = sourceTone(node.source);
  const shown = node.value;
  // 这条的值来自本机记忆（学到 / 推出来的）时，删的是 ``apply_learned`` 里那一条；
  // 「网申资料」里自己写的那份不走这里，避免误删。
  const forgetKey = node.forget_key || (stored ? node.key : "");

  return (
    <div className={`g-tree3d-chip${open ? " is-open" : ""}`}>
      <button
        type="button"
        className="g-tree3d-chip-head"
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
      >
        <span className="g-tree3d-leaf-mark" data-tone={tone} />
        <span
          className="t-label g-tree3d-chip-label truncate font-semibold"
          style={{ color: "var(--ink)" }}
          title={node.key}
        >
          {node.label}
        </span>
        {/* 展开后下面就是「内容」那一行，抬头再写一遍值只会显得啰嗦 */}
        {!open && (
          <span className="t-body g-tree3d-chip-value truncate" title={shown || undefined}>
            {shown || <span style={{ color: "var(--faint)" }}>未填</span>}
          </span>
        )}
        <Badge color={tone}>{node.source}</Badge>
        {node.pending && <Badge color="ink">待核对</Badge>}
        {node.orphan && <Badge color="orange">待归并</Badge>}
        <span className="g-tree3d-chip-caret">
          {open ? <ChevronDown size={13} /> : <ChevronRight size={13} />}
        </span>
      </button>

      {open && (
        <div className="g-tree3d-editor">
          {/* 左边一列小标题、右边一列内容：每行都齐着排，不再左一块右一块 */}
          <span className="g-tree3d-term">内容</span>
          <div className="g-tree3d-value">
            {stored ? (
              <p className="g-tree3d-read">{node.value}</p>
            ) : (
              <DelayInput
                value={node.value}
                onCommit={(v) => onSave({ [node.key]: v })}
                width="100%"
                delay={600}
              />
            )}
          </div>

          {node.note && (
            <>
              <span className="g-tree3d-term">{node.pending ? "依据" : "说明"}</span>
              <p className="g-tree3d-note-body">{node.note}</p>
            </>
          )}

          {(node.domain || node.updated_at) && (
            <>
              <span className="g-tree3d-term">来源</span>
              <div className="g-tree3d-meta">
              {node.domain && <Badge color="gray">{node.domain}</Badge>}
              {node.updated_at && (
                <span className="t-micro" style={{ color: "var(--faint)" }}>
                  {node.updated_at}
                </span>
              )}
              </div>
            </>
          )}

          {node.aliases.length > 0 && (
            <>
              <span className="g-tree3d-term">同义</span>
              <AliasRow node={node} onUnmerge={onUnmerge} bare />
            </>
          )}

          <div className="g-tree3d-actions">
            {/* 「待核对」的这条：推得对就点「保留」，从此不再标；推错了就「忘掉这条」 */}
            {node.pending && forgetKey && (
              <SoftButton onClick={() => onConfirm(forgetKey)}>
                <Check size={13} /> 推得对，保留
              </SoftButton>
            )}
            {forgetKey && (
              <GhostButton danger onClick={() => onForget(forgetKey)}>
                <Trash2 size={13} /> 忘掉这条
              </GhostButton>
            )}
            {canMerge && targets.length > 1 && (
              <MergeControl node={node} targets={targets} onMerge={onMerge} />
            )}
          </div>
        </div>
      )}
    </div>
  );
}

/** 一根树枝：一个分组。点一下展开挂在枝上的叶子。 */
function Branch({
  group,
  open,
  onToggle,
  targets,
  onSave,
  onConfirm,
  onForget,
  onMerge,
  onUnmerge,
}: {
  group: ApplyTreeGroup;
  open: boolean;
  onToggle: () => void;
  targets: Target[];
  onSave: (values: Record<string, string>) => void;
  onConfirm: (key: string) => void;
  onForget: (key: string) => void;
  onMerge: (alias: string, key: string) => void;
  onUnmerge: (alias: string) => void;
}) {
  const inferred = group.kind === "inferred";
  const filled = group.fields.filter((f) => f.filled).length;
  const pct = group.fields.length ? Math.round((filled / group.fields.length) * 100) : 0;

  return (
    <li className={`g-tree3d-branch${open ? " is-open" : ""}${inferred ? " is-inferred" : ""}`}>
      <button type="button" className="g-tree3d-node" aria-expanded={open} onClick={onToggle}>
        <span className="g-tree3d-node-caret">
          {open ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
        </span>
        <span className="t-label font-semibold" style={{ color: "var(--ink)" }}>
          {group.title}
        </span>
        <span
          className="g-tree3d-meter"
          style={{ "--pct": `${pct}%` } as CSSProperties}
          title={`已填 ${filled} / ${group.fields.length}`}
        />
        <span className="t-micro" style={{ color: "var(--faint)" }}>
          {filled}/{group.fields.length}
        </span>
        {inferred && <Badge color="ink">待核对</Badge>}
      </button>

      {open && inferred && (
        <p className="t-micro g-tree3d-note">
          这几条库里本来没有，是模型按你已有的资料推的。核对办法：逐条点开看「依据」，
          推得对就点「推得对，保留」，推错了就点「忘掉这条」；留着没删的，会随网申面板
          的「填入浏览器」一起入库，下个官网再遇到同类栏目就直接有值可填。
        </p>
      )}

      {open && (
        <ul className="g-tree3d-leaves">
          {group.fields.map((field) => (
            <li key={`${group.title}:${field.key}`} className="g-tree3d-leaf">
              <LeafCard
                node={field}
                targets={targets}
                onSave={onSave}
                onConfirm={onConfirm}
                onForget={onForget}
                onMerge={onMerge}
                onUnmerge={onUnmerge}
              />
            </li>
          ))}
        </ul>
      )}
    </li>
  );
}

export function ProfileTree({
  tree,
  onSave,
  onConfirm,
  onForget,
  onMerge,
  onUnmerge,
}: {
  tree: ApplyTree;
  onSave: (values: Record<string, string>) => void;
  onConfirm: (key: string) => void;
  onForget: (key: string) => void;
  onMerge: (alias: string, key: string) => void;
  onUnmerge: (alias: string) => void;
}) {
  // 一百多个叶子全铺开太长，默认只露树枝，点开才看枝上的叶子
  const [open, setOpen] = useState<Record<string, boolean>>({});
  const stage = useRef<HTMLDivElement>(null);
  const toggle = (title: string) => setOpen((prev) => ({ ...prev, [title]: !prev[title] }));
  const allOpen = tree.groups.length > 0 && tree.groups.every((group) => open[group.title]);
  /** 一个个点开太累：给一个「全部展开 / 全部收起」。 */
  const toggleAll = () =>
    setOpen(allOpen ? {} : Object.fromEntries(tree.groups.map((g) => [g.title, true])));

  /** 指针扫过时整棵树绕 Y 轴偏一点点，像真的在看一个有厚度的东西。
   *  偏角写进 CSS 变量、直接改 DOM 样式：树上一百多个节点，每次移动都触发
   *  React 重渲染会掉帧。幅度封在几度以内，不至于让人看着晃。
   *  这个变量由树枝各自取用——透视挂在每根枝上，才不会把长树扭歪。 */
  function track(event: ReactPointerEvent<HTMLDivElement>) {
    const el = stage.current;
    if (!el) return;
    const box = el.getBoundingClientRect();
    if (!box.width) return;
    const px = (event.clientX - box.left) / box.width - 0.5;
    el.style.setProperty("--yaw", `${(7 + px * 5).toFixed(2)}deg`);
  }

  /** 指针离开就归位，别让整棵树歪着停在那儿。 */
  function rest() {
    const el = stage.current;
    if (!el) return;
    el.style.setProperty("--yaw", "7deg");
  }

  const targets: Target[] = [];
  const seen = new Set<string>();
  for (const group of tree.groups) {
    if (group.kind !== "group") continue;
    for (const field of group.fields) {
      const key = field.canonical || field.key;
      if (seen.has(key)) continue;
      seen.add(key);
      targets.push({ key, label: field.label });
    }
  }

  const total = tree.groups.reduce((sum, group) => sum + group.fields.length, 0);
  const totalFilled = tree.groups.reduce(
    (sum, group) => sum + group.fields.filter((f) => f.filled).length,
    0,
  );
  return (
    <div className="g-tree3d flex flex-col gap-4">
      <div className="flex flex-wrap items-center gap-2">
        <span className="t-micro" style={{ color: "var(--faint)" }}>
          共 {tree.groups.length} 组 · {total} 项 · 已填 {totalFilled} 项
        </span>
        <GhostButton variant="plain" className="ml-auto" onClick={toggleAll}>
          {allOpen ? "全部收起" : "全部展开"}
        </GhostButton>
      </div>

      {tree.orphans.length > 0 && (
        <div
          className="px-4 py-3"
          style={{
            borderRadius: "var(--r-md)",
            background: "var(--warn-soft)",
            border: "1px solid var(--warn-line)",
          }}
        >
          <span className="t-micro" style={{ color: "var(--warn)" }}>
            有 {tree.orphans.length} 条叫法内置表认不出（{tree.orphans.slice(0, 6).join("、")}
            {tree.orphans.length > 6 ? " 等" : ""}）。如果它其实是某个已有字段，展开那根树枝点
            「并到别的字段」，以后就不会重复存了。
          </span>
        </div>
      )}

      <div ref={stage} className="g-tree3d-stage" onPointerMove={track} onPointerLeave={rest}>
        <div className="g-tree3d-base">
          <span className="t-label font-semibold" style={{ color: "var(--ink)" }}>
            个人信息库
          </span>
          <span className="t-micro" style={{ color: "var(--faint)" }}>
            {tree.groups.length} 类 · {total} 条
          </span>
        </div>
        <ul className="g-tree3d-root">
          {tree.groups.map((group) => (
            <Branch
              key={group.title}
              group={group}
              open={Boolean(open[group.title])}
              onToggle={() => toggle(group.title)}
              targets={targets}
              onSave={onSave}
              onConfirm={onConfirm}
              onForget={onForget}
              onMerge={onMerge}
              onUnmerge={onUnmerge}
            />
          ))}
        </ul>
      </div>
    </div>
  );
}
