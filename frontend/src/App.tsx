/** 路由：六个页面（工作台 / 岗位管理 / 网申助手 / 消息 / 数据 / 配置中心）+ 连接遮罩。 */

import { Route, Routes, useLocation } from "react-router-dom";
import { ConnectionOverlay } from "./ConnectionGate";
import { ErrorBoundary } from "./ErrorBoundary";
import Workbench from "./pages/Workbench";
import Jobs from "./pages/Jobs";
import Apply from "./pages/Apply";
import Messages from "./pages/Messages";
import Stats from "./pages/Stats";
import Config from "./pages/Config";

export default function App() {
  const location = useLocation();
  return (
    <>
      <ConnectionOverlay />
      {/* 路由级兜底：某个页面崩了不连累整体，切换页面时自动复位。 */}
      <ErrorBoundary resetKey={location.pathname}>
        <Routes>
          <Route path="/" element={<Workbench />} />
          <Route path="/jobs" element={<Jobs />} />
          <Route path="/apply" element={<Apply />} />
          <Route path="/messages" element={<Messages />} />
          <Route path="/stats" element={<Stats />} />
          <Route path="/config" element={<Config />} />
          <Route path="*" element={<Workbench />} />
        </Routes>
      </ErrorBoundary>
    </>
  );
}
