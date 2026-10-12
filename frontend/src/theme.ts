/**
 * 主题开关：日间「纸与墨」/ 夜间「星港」。
 *
 * 全部配色都写在 theme.css 的令牌里，只是分挂在 `:root` 与 `[data-theme="dark"]`
 * 两处，所以这里只负责把 data-theme 写到 <html> 上，外加同步浏览器自身会用到的
 * theme-color / color-scheme。选择存 localStorage，刷新后保持。
 */

import { useSyncExternalStore } from "react";

export type Theme = "light" | "dark";

const KEY = "gj-theme";

function read(): Theme {
  try {
    const v = localStorage.getItem(KEY);
    if (v === "dark" || v === "light") return v;
  } catch {
    /* 隐私模式下 localStorage 可能不可用，忽略即可 */
  }
  return "light";
}

let current: Theme = read();
const listeners = new Set<() => void>();

function apply(theme: Theme) {
  const root = document.documentElement;
  root.dataset.theme = theme;
  root.style.colorScheme = theme;
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta) meta.setAttribute("content", theme === "dark" ? "#070a12" : "#f3f0e9");
}

export function getTheme(): Theme {
  return current;
}

export function setTheme(next: Theme) {
  if (next === current) return;
  current = next;
  try {
    localStorage.setItem(KEY, next);
  } catch {
    /* 存不了也不影响本次会话 */
  }
  apply(next);
  listeners.forEach((fn) => fn());
}

export function toggleTheme() {
  setTheme(current === "dark" ? "light" : "dark");
}

function subscribe(fn: () => void) {
  listeners.add(fn);
  return () => {
    listeners.delete(fn);
  };
}

export function useTheme(): Theme {
  return useSyncExternalStore(subscribe, getTheme, getTheme);
}

// 模块加载即对齐一次，避免 index.html 里的预置脚本与内存状态不一致。
apply(current);
