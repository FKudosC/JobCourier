/**
 * 连接遮罩：只有确定连上本地服务后才放行。
 *
 * 判定逻辑与旧版一致：连上过且当前 open 才返回 null；此前或掉线时盖一层启动/重连提示。
 */

import { Wifi, WifiOff } from "lucide-react";
import { useRuntime } from "./store";

/** 启动/重连遮罩上的品牌标记：与顶栏 BrandMark 同一枚纸飞机。 */
function BrandGlyph({ pulse }: { pulse?: boolean }) {
  return (
    <span
      className="flex items-center justify-center"
      style={{
        width: 54,
        height: 54,
        borderRadius: 16,
        background: "var(--ink)",
        boxShadow: "var(--sh-3)",
      }}
    >
      <svg
        width="30"
        height="30"
        viewBox="0 0 24 24"
        fill="none"
        className={pulse ? "g-breathe" : undefined}
      >
        <path d="M3.4 20.6 21.6 12 3.4 3.4 3.4 10.1 14.6 12 3.4 13.9Z" fill="var(--paper)" />
      </svg>
    </span>
  );
}

export function ConnectionOverlay() {
  const { conn, everConnected } = useRuntime();
  if (everConnected && conn === "open") return null;

  const reconnecting = everConnected;

  return (
    <div
      className="g-in"
      style={{
        position: "fixed",
        inset: 0,
        zIndex: "var(--z-boot)",
        display: "flex",
        flexDirection: "column",
        alignItems: "center",
        justifyContent: "center",
        gap: "1.1rem",
        background: "var(--paper)",
        textAlign: "center",
        padding: "2rem",
      }}
    >
      <BrandGlyph pulse={!reconnecting} />
      <div className="flex items-center gap-2" style={{ color: "var(--muted)" }}>
        {reconnecting ? <WifiOff size={15} /> : <Wifi size={15} />}
        <span className="t-body font-semibold" style={{ color: "var(--ink)" }}>
          {reconnecting ? "与本地服务的连接已断开，正在重连…" : "小投正在启动，请稍候…"}
        </span>
      </div>
      <p className="t-meta" style={{ color: "var(--faint)", maxWidth: "34ch" }}>
        {reconnecting
          ? "请确认后台服务仍在运行，连接会自动恢复"
          : "刚打开程序时需要几秒钟连接后台服务"}
      </p>
      <span
        className="g-breathe"
        style={{
          marginTop: 4,
          height: 3,
          width: 96,
          borderRadius: 999,
          background: "var(--accent)",
        }}
      />
    </div>
  );
}
