# user_data

运行期产生的用户信息都放在这个目录，**不会提交到仓库**（见根目录 `.gitignore`）。

| 内容 | 说明 |
| --- | --- |
| `job-courier.db` | 岗位、求职配置、聊天记录等本地数据库 |
| `resumes/` | 上传的简历原件与 AI 生成的定制简历 |
| `chrome-profile/` | BOSS 直聘的 Chrome 登录态（登录一次长期保持） |
| `logs/` | 后端日志 |

备份或迁移时直接整体拷贝 `user_data/` 即可；换电脑后连同 Chrome 登录态一起恢复。

需要把数据放到别处（例如同步盘）时，设置环境变量 `JOB_COURIER_DATA_DIR` 指向目标目录。
