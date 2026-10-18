"""构建 Windows 便携版：自带 Python 运行时与依赖，不需要 uv / Node / Python。

用法（在项目根目录，用项目自带的 .venv 运行）：
    .venv\\Scripts\\python.exe build-portable.py
    .venv\\Scripts\\python.exe build-portable.py --no-zip

产物默认生成在项目上一级：
    JobCourier-portable-win64\\        可直接运行的目录
    JobCourier-portable-win64.zip      发给别人解压即用
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DEFAULT_OUT = ROOT.parent / "JobCourier-portable-win64"
SKIP = shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo", ".pytest_cache", ".ruff_cache")


def step(text: str) -> None:
    print(f"\n== {text}", flush=True)


def run(cmd: list[str], cwd: Path = ROOT) -> None:
    print("   $ " + " ".join(cmd), flush=True)
    subprocess.run(cmd, cwd=cwd, check=True)


def find_runtime() -> Path:
    """找项目虚拟环境背后的那个独立 Python，而不是 .venv 里的转发壳。"""
    cfg = ROOT / ".venv" / "pyvenv.cfg"
    if cfg.is_file():
        for line in cfg.read_text(encoding="utf-8").splitlines():
            key, _, value = line.partition("=")
            if key.strip() == "home":
                home = Path(value.strip())
                if (home / "python.exe").is_file():
                    return home
    raise SystemExit("找不到 .venv 背后的 Python 运行时，请先执行 uv sync。")


def copy_project_file(rel: str, out: Path) -> None:
    src = ROOT / rel
    dst = out / rel
    dst.parent.mkdir(parents=True, exist_ok=True)
    if src.is_dir():
        shutil.copytree(src, dst, ignore=SKIP)
    else:
        shutil.copy2(src, dst)


def write_launchers(out: Path) -> None:
    (out / "启动小投.cmd").write_text(
        """@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONUTF8=1

if not exist "runtime\\python.exe" (
  echo 运行环境不完整，请重新解压整个压缩包。
  pause
  exit /b 1
)

if not exist "%ProgramFiles%\\Google\\Chrome\\Application\\chrome.exe" if not exist "%ProgramFiles(x86)%\\Google\\Chrome\\Application\\chrome.exe" if not exist "%LocalAppData%\\Google\\Chrome\\Application\\chrome.exe" (
  echo [提示] 未检测到 Google Chrome。
  echo        自动投递和网申需要在本机 Chrome 中运行，请先安装：
  echo        https://www.google.com/chrome/
  echo.
)

runtime\\python.exe main.py
if errorlevel 1 pause
""",
        encoding="utf-8",
        newline="\r\n",
    )
    (out / "后台启动小投.cmd").write_text(
        """@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONUTF8=1
set JOB_COURIER_BACKGROUND=1

start "" "%~dp0runtime\\pythonw.exe" "%~dp0main.py" --background
echo 小投已在后台启动，面板：http://127.0.0.1:8000
echo 日志：user_data\\logs\\backend.log
timeout /t 3 >nul
""",
        encoding="utf-8",
        newline="\r\n",
    )
    (out / "使用说明.txt").write_text(
        """小投 · JobCourier 便携版
==========================

不需要安装 Python、Node.js 或 uv。

1. 解压整个文件夹（不要只拖出 exe / cmd）。
2. 双击「启动小投.cmd」。
3. 浏览器会自动打开 http://127.0.0.1:8000 。
4. 需要本机已安装 Google Chrome（自动投递与网申使用）。
5. 想让它后台跑，双击「后台启动小投.cmd」；日志在 user_data\\logs\\backend.log。
6. 所有数据都在 user_data\\ 里，备份或换电脑时整个目录拷走即可。
7. 企业官网的提交按钮永远由你自己点。

停止：
  - 前台窗口按 Ctrl+C。
  - 后台运行在任务管理器结束 pythonw.exe / python.exe。
""",
        encoding="utf-8",
        newline="\r\n",
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--no-zip", action="store_true")
    args = ap.parse_args()

    out = args.out.resolve()
    if out == ROOT or ROOT in out.parents:
        raise SystemExit("输出目录不能放在项目内，避免把运行时提交进仓库。")
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)

    step("复制 Python 3.12 运行时")
    runtime_src = find_runtime()
    shutil.copytree(runtime_src, out / "runtime", ignore=SKIP)

    step("安装后端依赖（uv 缓存会加速）")
    req = Path(tempfile.gettempdir()) / "jobcourier-portable-req.txt"
    run(["uv", "export", "--format", "requirements.txt", "--no-dev", "--no-emit-project", "-o", str(req)])
    run(["uv", "pip", "install", "--python", str(out / "runtime" / "python.exe"), "--break-system-packages", "-r", str(req)])
    req.unlink(missing_ok=True)

    step("复制程序与已构建前端")
    for rel in ("job", "main.py", "frontend/dist", "assets", "README.md", "LICENSE", "pyproject.toml"):
        copy_project_file(rel, out)
    (out / "user_data").mkdir(exist_ok=True)
    shutil.copy2(ROOT / "user_data" / "README.md", out / "user_data" / "README.md")

    step("写入启动脚本")
    write_launchers(out)

    print("\n便携版目录：" + str(out))
    if not args.no_zip:
        step("压缩（体积较大，请稍候）")
        zip_path = out.with_suffix(".zip")
        if zip_path.exists():
            zip_path.unlink()
        started = time.time()
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            for path in out.rglob("*"):
                if path.is_file():
                    zf.write(path, path.relative_to(out.parent))
        mb = zip_path.stat().st_size / 1024 / 1024
        print(f"压缩完成：{zip_path}（{mb:.0f} MB，{time.time() - started:.0f} 秒）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
