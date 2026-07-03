# 通用多任务预约系统

一个只依赖 Python 标准库和 SQLite 的轻量预约系统。管理员可以同时创建多个预约任务，每个任务拥有独立、稳定的公开 URL、开放日期、时间段、预约时长、表单字段和预约名单。

## 主要功能

- 一个后台管理多个预约任务。
- 每个任务自动生成独立链接，例如 `/b/7KQ2M9AB`。
- 任务支持草稿、进行中、暂停、结束和归档状态。
- 自定义公开标题、说明、地点、联系人和时区。
- 直接指定一个或多个预约日期，也可按日期范围和星期批量添加。
- 每个任务支持多个每日开放时段。
- 单次预约时长支持 5–240 分钟。
- 支持“全部开放”和“按顺序逐个开放”。
- 姓名、邮箱为系统字段，可增加最多 10 个单行文本、多行文本、手机号或选择字段。
- 邮箱验证码验证；同一邮箱在同一个任务中限约一次，但可预约其他任务。
- 后台使用可刷新、可返回的任务路由，例如 `/admin/tasks/12/bookings`。
- 后台按任务搜索、筛选名单，占用/释放时间段，取消预约和导出 CSV。
- 取消采用软取消：保留取消时间、原因和原始答案，用户可以重新预约。
- 邮箱验证后会先查询并显示该任务中的现有预约，再提供取消入口。
- 自定义字段使用稳定字段键，修改字段名称不会丢失历史答案。
- 修改开放规则如果会影响未来已有预约，系统会阻止保存并列出冲突。
- SQLite 事务和任务级唯一约束防止重复预约。

## 页面

- 后台：`http://127.0.0.1:8000/admin`
- 公开预约页：`http://127.0.0.1:8000/b/{publicId}`
- 根地址 `/` 会重定向到从旧系统迁移得到的默认 AIAD 任务。

## 本地运行

```bash
python3 server.py
```

默认后台密码：

```text
aiad-admin-2026
```

建议部署时修改：

```bash
ADMIN_PASSWORD='your-strong-password' python3 server.py
```

默认数据库为项目目录中的 `bookings.sqlite3`。可通过环境变量修改：

```bash
BOOKING_DB_PATH=/data/bookings.sqlite3 python3 server.py
```

## 邮箱配置

在项目根目录创建 `.env`：

```text
SMTP_HOST=your-smtp-host
SMTP_PORT=465
SMTP_USER=your-smtp-login
SMTP_PASSWORD=your-smtp-authorization-code
SMTP_FROM=your-sender-address
SMTP_USE_SSL=1
SMTP_STARTTLS=0
```

本地自动化测试可以显式设置固定验证码；生产环境不要设置此变量：

```bash
DEV_VERIFICATION_CODE=123456 python3 server.py
```

## Docker

```bash
docker compose up --build
```

数据库通过 `aiad-booking-data` volume 持久化。

## 管理流程

1. 登录后台并点击“新建预约任务”。
2. 在四步向导中填写基本信息、开放时间和收集字段。
3. 在实时预约页预览中检查生成结果。
4. 选择“保存草稿”或“保存并发布”。
5. 发布后复制独立预约链接。
6. 在任务详情中查看名单、导出或后台占用时间。

复制任务会复制配置并生成新的公开 URL，但不会复制预约记录、后台占用或验证码。

## 数据迁移

首次启动新版服务时会自动迁移旧数据库：

- 旧 AIAD 配置会成为默认任务。
- 当前配置日期之外的旧记录会进入“历史预约（迁移）”归档任务。
- 原预约和后台占用不会被删除。
- 原先写死日期范围的邮箱唯一索引会替换为任务级约束。

建议正式升级前备份 `bookings.sqlite3`。

## 核心 API

公开接口：

```text
GET    /api/public/tasks/{publicId}
POST   /api/public/tasks/{publicId}/bookings
POST   /api/public/tasks/{publicId}/bookings/lookup
DELETE /api/public/tasks/{publicId}/bookings/by-email
POST   /api/verification/send
POST   /api/verification/verify
```

管理接口需要后台登录：

```text
GET    /api/admin/tasks
POST   /api/admin/tasks
GET    /api/admin/tasks/{id}
PUT    /api/admin/tasks/{id}
POST   /api/admin/tasks/{id}/status
POST   /api/admin/tasks/{id}/copy
GET    /api/admin/tasks/{id}/bookings
DELETE /api/admin/tasks/{id}/bookings/{bookingId}
POST   /api/admin/tasks/{id}/blocked-slots
DELETE /api/admin/tasks/{id}/blocked-slots
GET    /api/admin/tasks/{id}/export.csv
```
