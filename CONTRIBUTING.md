# 贡献指南

感谢你愿意为「小投」出力。这个项目是本地自用的个人项目，代码以「够用、好懂、不脆弱」为先，改动请尽量小步、聚焦。

## 开发环境

- Python 3.12，依赖用 [uv](https://docs.astral.sh/uv/) 管理
- Node.js 22+（自带 npm），用于构建前端
- Google Chrome（自动投递通过本机 Chrome 完成）

```bash
uv sync                              # 后端依赖，生成 .venv
cd frontend && npm ci && npm run build   # 前端依赖与构建产物
```

## 目录速览

- `job/`：FastAPI 后端。`job/api/` 放 HTTP 层，`job/models/` 放 SQLModel 数据层，`job/boss/` 放浏览器自动化与业务逻辑
- `frontend/src/`：React 19 + TypeScript + Vite 前端。`Shell.tsx` 是外壳与顶栏，`pages/` 是各页面，样式令牌集中在 `theme.css`
- `tests/`：pytest 测试
- `user_data/`：运行期数据（数据库、简历、Chrome 登录态、日志），**不会提交**

## 提交前自检

```bash
uv run ruff check . --output-format concise   # 代码风格与静态检查
uv run pytest -q                              # 单元测试
cd frontend && npm run build                  # 前端类型检查与构建
```

三项都要通过再提交。改了 `frontend/` 之后必须重新 `npm run build`，前端产物由后端在 8000 端口一起伺服。

## 代码约定

- 后端行宽 110，import 由 ruff 排序；不要引入未使用的变量或导入
- 数据层的列表查询请带上稳定的次级排序键（如 `uid`、`mid`），避免相同时间戳下顺序不稳定
- 前端调用后端接口统一走 `src/api.ts`，不要直接 `fetch`；长任务接口需要更长的超时
- 文案用中文，风格与现有界面保持一致

## 安全与隐私

- 任何用户数据（API Key、简历、聊天记录）只能落在本机 `user_data/`，不要新增上传到服务器的路径
- 新增对外请求时，注意不要把 API Key、简历原文写进日志

## 许可

本项目采用 PolyForm Noncommercial License 1.0.0，不接受任何商业用途的改动。
