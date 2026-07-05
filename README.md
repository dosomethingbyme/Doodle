# 通用多任务预约系统

一个只依赖 Python 标准库和 SQLite 的轻量预约系统。管理员可以同时创建多个预约任务，每个任务拥有独立、稳定的公开 URL、开放日期、时间段、预约时长、表单字段和预约名单。

## 主要功能

- 一个后台管理多个预约任务。
- 每个任务自动生成独立链接，例如 `/b/7KQ2M9AB`。
- 任务支持草稿、进行中、暂停、结束和归档状态。
- 生命周期采用合法状态迁移，暂停、结束、归档等危险操作需要确认。
- 自定义公开标题、说明、地点、联系人和时区。
- 直接指定一个或多个预约日期，也可按日期范围和星期批量添加。
- 每个任务支持多个每日开放时段。
- 单次预约时长支持 5–240 分钟。
- 支持“全部开放”和“按顺序逐个开放”。
- 姓名、邮箱为系统字段，可增加最多 10 个单行文本、多行文本、手机号或选择字段。
- 邮箱验证码验证；同一邮箱在同一个任务中限约一次，但可预约其他任务。
- 后台使用可刷新、可返回的任务路由，例如 `/admin/tasks/12/bookings`。
- 后台按任务搜索、筛选名单，占用/释放时间段，取消预约和导出 CSV。
- 名单筛选会保留在 URL 中，CSV 导出与当前筛选条件一致。
- 取消采用软取消：保留取消时间、原因和原始答案，用户可以重新预约。
- 邮箱验证后会先查询并显示该任务中的现有预约，再提供取消入口。
- 暂停或结束任务仍允许已有预约人验证邮箱、查询和取消预约。
- 自定义字段使用稳定字段键，修改字段名称不会丢失历史答案。
- 修改开放规则如果会影响未来已有预约，系统会阻止保存并列出冲突。
- SQLite 事务和任务级唯一约束防止重复预约。
- 当天已经开始的时间段会按任务时区自动关闭。
- 可设置最少提前预约时间和最远可预约天数。
- 可针对某个日期关闭预约，或设置不同于默认值的独立开放时段。
- 进行中的任务采用“编辑草稿 → 显式发布”流程；保存修改不会立即影响公开页，每次发布生成不可变版本快照。
- 邮箱验证后可直接改期；原时间立即释放，预约编号不变，改期前后时间会保留在历史记录中。
- V3 为每次预约生成 `APT-...` 编号、安全管理链接和稳定 UID 的 ICS 日历文件。
- 可分别配置取消/改期截止时间、最多改期次数、预约政策、隐私说明和数据保留周期。
- 管理员可按日查看跨任务议程、代客预约、代客改期、维护内部备注并查看预约事件时间线。
- 后台账号和会话持久化，支持 Owner/Operator 两级权限、CSRF 防护和统一审计日志。
- 邮件先写入可靠 Outbox，失败可自动或手动重试；预约保存不再依赖 SMTP 是否即时可用。
- SMTP 可在后台“系统设置”覆盖；未覆盖的每个字段继续以 `.env` 为默认值，密码永不回显。

## 页面

- 后台：`http://127.0.0.1:8000/admin`
- 公开预约页：`http://127.0.0.1:8000/b/{publicId}`
- 根地址 `/` 会重定向到从旧系统迁移得到的默认 AIAD 任务。

## 本地运行

```bash
python3 server.py
```

工程采用无第三方运行时依赖的分层结构：

```text
server.py                  兼容启动入口
booking_app/config.py      环境配置
booking_app/database.py    SQLite 连接、建表与迁移
booking_app/domain.py      任务校验、时段与预约规则
booking_app/emailer.py     邮件发送
booking_app/handler.py     HTTP 基础设施与路由分发
booking_app/admin_routes.py   管理接口
booking_app/public_routes.py  公开预约接口
static/admin.*             管理端样式与脚本
static/admin_product.js    时间规则与发布版本交互
static/admin_v3.js         V3 日程、通知、邮件和账号界面
static/public.*            公开端样式与脚本
```

运行完整回归测试：

```bash
python3 -m unittest -v
```

测试包含数据库迁移与领域规则单元测试，以及基于真实本机 HTTP 服务的后台登录、任务发布、邮箱验证、预约、查询、取消和静态资源集成流程。

本地开发默认后台密码：

```text
aiad-admin-2026
```

建议部署时修改：

```bash
ADMIN_PASSWORD='your-strong-password' python3 server.py
```

后台使用默认密码时会持续显示安全告警；Docker 启动则必须显式提供强密码。
当 `APP_ENV=production` 时，程序会拒绝使用默认密码启动。

默认数据库为项目目录中的 `bookings.sqlite3`。可通过环境变量修改：

```bash
BOOKING_DB_PATH=/data/bookings.sqlite3 python3 server.py
```

## 邮箱配置

`.env` 是邮件配置的默认层。Owner 可在后台的“系统设置 → 邮件服务器”保存覆盖值，或逐项恢复为 `.env` 默认值。密码留空表示保持当前值，API 永不返回明文密码。

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
ADMIN_PASSWORD='your-strong-password' docker compose up --build
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

全新数据库不会创建任何 AIAD 专用任务，后台会从“创建第一个预约任务”的通用空状态开始。

建议正式升级前备份 `bookings.sqlite3`。

创建并立即校验在线一致性备份：

```bash
python3 scripts/backup.py
python3 scripts/backup.py --verify backups/booking-YYYYMMDD-HHMMSS.sqlite3
```

恢复会先执行 `PRAGMA quick_check`，且默认拒绝覆盖现有数据库：

```bash
python3 scripts/backup.py --restore /path/to/backup.sqlite3 --force
```

## 核心 API

公开接口：

```text
GET    /api/public/tasks/{publicId}
POST   /api/public/tasks/{publicId}/bookings
GET    /api/public/bookings/{bookingRef}?token={signedToken}
GET    /api/public/bookings/{bookingRef}.ics?token={signedToken}
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
POST   /api/admin/tasks/{id}/publish
GET    /api/admin/tasks/{id}/versions
POST   /api/admin/tasks/{id}/copy
GET    /api/admin/tasks/{id}/bookings
POST   /api/admin/tasks/{id}/bookings
PUT    /api/admin/tasks/{id}/bookings/{bookingId}
GET    /api/admin/tasks/{id}/bookings/{bookingId}
DELETE /api/admin/tasks/{id}/bookings/{bookingId}
POST   /api/admin/tasks/{id}/blocked-slots
DELETE /api/admin/tasks/{id}/blocked-slots
GET    /api/admin/tasks/{id}/export.csv
GET    /api/admin/agenda
GET    /api/admin/notifications
POST   /api/admin/notifications/retry
GET    /api/admin/settings/email
PUT    /api/admin/settings/email
POST   /api/admin/settings/email/test
GET    /api/admin/users
POST   /api/admin/users
PUT    /api/admin/users/{userId}
GET    /api/admin/audit
```

公开预约改期接口：

```text
PUT    /api/public/tasks/{publicId}/bookings/by-email
```
