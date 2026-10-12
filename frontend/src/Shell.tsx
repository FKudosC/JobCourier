/**
 * 页面外壳：顶部横向导航 + 内容容器。
 *
 * 与旧版的左侧栏控制台彻底不同：导航在顶部，内容区一列居中，靠发丝线与留白分层。
 * 页面不再有独立页头——导航已经标明所在位置，页面直接进入内容。
 */

import {
  Activity,
  Briefcase,
  Eye,
  LayoutDashboard,
  MessagesSquare,
  Moon,
  Settings2,
  Sun,
  X,
} from "lucide-react";
import { useState } from "react";
import { NavLink } from "react-router-dom";
import { toast } from "sonner";
import { api } from "./api";
import { useRuntime } from "./store";
import { toggleTheme, useTheme } from "./theme";
import { GhostButton, IconButton } from "./ui";

const NAV = [
  { to: "/", label: "工作台", icon: LayoutDashboard },
  { to: "/jobs", label: "岗位管理", icon: Briefcase },
  { to: "/messages", label: "消息", icon: MessagesSquare },
  { to: "/stats", label: "数据", icon: Activity },
  { to: "/config", label: "配置中心", icon: Settings2 },
] as const;

/**
 * 品牌标记：纸飞机。
 *
 * 「投」就是把简历送出去，纸飞机的指向直接对应这个动作；
 * 靛青圆底 + 纯白剪影，缩到 20px 仍认得出来，不用通用图标。
 */
function BrandMark() {
  return (
    <span className="g-brandmark" aria-hidden>
      <svg width="20" height="20" viewBox="0 0 24 24" fill="none">
        <path d="M3.4 20.6 21.6 12 3.4 3.4 3.4 10.1 14.6 12 3.4 13.9Z" fill="var(--paper)" />
      </svg>
    </span>
  );
}

/** 顶栏右侧的本地服务状态芯片。 */
function ServiceChip() {
  const { conn, everConnected } = useRuntime();
  const online = conn === "open";
  const booting = !everConnected && conn === "connecting";
  const color = online ? "var(--good)" : conn === "closed" ? "var(--bad)" : "var(--warn)";
  const text = online
    ? "本地服务在线"
    : booting
      ? "正在启动"
      : conn === "closed"
        ? "已断开，重连中"
        : "正在连接";
  return (
    <span className="g-chip" title={online ? "已连接到本地服务" : "与本地服务的连接已断开，正在自动重连"}>
      <span
        className={online ? "g-dot" : "g-dot g-breathe"}
        style={{ background: color }}
      />
      {text}
    </span>
  );
}

/**
 * 顶栏右侧的浏览器窗口控制。
 *
 * 弹出的浏览器是全局资源（登录、投递、回复都在里面进行），窗口又常常被静默
 * 移出屏幕，所以把「调出 / 关闭」放在顶栏，任何页面都能随手拿到。
 */
function BrowserControl() {
  const [pending, setPending] = useState<string | null>(null);

  async function call(key: string, path: string) {
    setPending(key);
    try {
      await api.post(path);
    } catch (err) {
      toast.error((err as Error).message);
    } finally {
      setPending(null);
    }
  }

  return (
    <div className="flex shrink-0 items-center gap-2">
      <GhostButton
        disabled={pending !== null}
        title="把被静默移出屏幕的浏览器窗口调出来"
        onClick={() => call("show", "/api/browser/show")}
      >
        <Eye size={13} /> 查看浏览器
      </GhostButton>
      <GhostButton
        disabled={pending !== null}
        title="关闭弹出的浏览器窗口"
        onClick={() => call("close", "/api/browser/close")}
      >
        <X size={13} /> 关闭浏览器
      </GhostButton>
    </div>
  );
}

/**
 * 顶栏的主题开关。
 *
 * 日夜切换是"看一眼就想动手"的操作，留在顶栏比埋进配置中心顺手；
 * 图标本身就是当前状态的反面（深色时显示太阳 = 点它回日间）。
 */
function ThemeToggle() {
  const dark = useTheme() === "dark";
  const label = dark ? "切换到日间模式" : "切换到夜间模式";
  return (
    <IconButton title={label} onClick={toggleTheme}>
      {dark ? <Sun size={15} strokeWidth={1.9} /> : <Moon size={15} strokeWidth={1.9} />}
    </IconButton>
  );
}

function TopNav({ active }: { active: string }) {
  return (
    <header className="g-top">
      <div className="g-top-inner">
        <a className="g-brand" href="/" aria-label="小投 首页">
          <BrandMark />
          <span className="flex flex-col leading-none">
            <span className="t-h2" style={{ color: "var(--ink)", letterSpacing: "-0.02em" }}>
              小投
            </span>
            <span className="t-micro mt-1" style={{ color: "var(--faint)", letterSpacing: "0.04em" }}>
              BOSS 投递助手
            </span>
          </span>
        </a>

        <nav className="flex min-w-0 items-center gap-1.5" aria-label="主导航">
          {NAV.map(({ to, label, icon: Icon }) => (
            <NavLink
              key={to}
              to={to}
              end={to === "/"}
              className="g-navlink"
              data-active={active === to}
            >
              <Icon size={14} strokeWidth={1.9} />
              {label}
            </NavLink>
          ))}
        </nav>

        <div className="g-top-side ml-auto flex items-center gap-3">
          <BrowserControl />
          <ServiceChip />
          <ThemeToggle />
        </div>
      </div>
    </header>
  );
}

export function PageShell({
  active,
  fill,
  children,
}: {
  active: string;
  /** true 时撑满视口、内容区各自滚动（岗位 / 消息 / 配置）；否则整页滚动。 */
  fill?: boolean;
  children: React.ReactNode;
}) {
  return (
    <div className="g-shell">
      <div className="g-grain" aria-hidden />
      <a className="g-skip" href="#gj-main">
        跳到主要内容
      </a>
      <TopNav active={active} />
      <main
        id="gj-main"
        className={`g-body g-scroll ${fill ? "g-body-fill" : "g-body-scroll"}`}
      >
        <div className="g-container">
          {children}
        </div>
      </main>
    </div>
  );
}
