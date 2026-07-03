# AIAD 样机测试时间预约

一个轻量的本地预约系统，用于 AIAD 样机测试时间预约。前台给测试人员填写姓名、导师、邮箱并选择时间段；后台用于查看预约情况、按条件预览、取消预约和导出表格。

## 功能

- 前台预约页：姓名、导师、邮箱、邮箱验证码、日期、10 分钟时间段选择。
- 可预约日期：`2026-07-04`，上午 `09:30-11:30`，下午 `13:00-18:00`。
- 每个时间段 10 分钟，最后可选开始时间为 `11:20` 和 `17:50`。
- 预约按上午和下午分别连续开放：上午第一段 `09:30`、下午第一段 `13:00` 可直接预约；其他时间段必须在前一个时间段已预约或后台占用后才可预约。
- SQLite 数据库存储预约记录。
- 数据库唯一约束防止同一日期同一时间段被重复预约。
- 同一个邮箱只能预约一个时间段；创建或取消预约前必须先完成邮箱验证码验证。
- 已预约邮箱验证通过后，前台只允许取消已有预约；如需调整时间，必须先取消后重新预约。
- 后台可单击日历时间块后在详情面板手动占用或释放时间段；已有人预约的时间段不能释放。
- 预约成功或预约时间修改后自动发送确认邮件，写明导师、预约日期、时间和测试地点。
- 前台不显示后台入口，也不会公开预约人的姓名和邮箱。
- 后台需要密码登录，默认密码为 `aiad-admin-2026`，可用 `ADMIN_PASSWORD` 修改。
- 后台日历式看板：类似前台周日历，按周查看每个时间点是否已预约。
- 后台表格预览：按日期范围、时间段、状态、姓名、导师或邮箱筛选。
- 后台支持取消预约。
- 导出 CSV 表格，内容跟随后台当前筛选条件。
- 支持 Docker Compose 部署，数据库通过 volume 持久化。

## 页面

- 预约页：`http://127.0.0.1:8000/`
- 后台页：`http://127.0.0.1:8000/admin.html`
- 导出接口：`http://127.0.0.1:8000/api/export.csv`（需要后台登录）

## 本地运行

项目只依赖 Python 标准库，不需要安装额外 Python 包。

```bash
python3 server.py
```

启动后打开：

```text
http://127.0.0.1:8000/
```

默认数据库文件会创建在项目目录：

```text
bookings.sqlite3
```

这些本地数据库文件已加入 `.gitignore`，不会提交到仓库。

如果要修改后台密码：

```bash
ADMIN_PASSWORD='your-strong-password' python3 server.py
```

邮箱验证码需要在项目根目录创建本地 `.env` 文件：

```text
SMTP_HOST=your-smtp-host
SMTP_PORT=465
SMTP_USER=your-smtp-login
SMTP_PASSWORD=your-smtp-authorization-code
SMTP_FROM=your-sender-address
SMTP_USE_SSL=1
SMTP_STARTTLS=0
```

`.env` 已加入 `.gitignore`，不会提交到仓库。

## Docker 运行

```bash
docker compose up --build
```

如需修改后台密码：

```bash
ADMIN_PASSWORD='your-strong-password' docker compose up --build
```

如需启用邮箱验证码发送，请先在 `.env` 中配置完整 SMTP 环境变量，再启动：

```bash
docker compose up --build
```

启动后打开：

```text
http://127.0.0.1:8000/
```

Docker 环境中数据库路径为：

```text
/data/bookings.sqlite3
```

Compose 会用命名卷 `aiad-booking-data` 持久化数据库。

停止服务：

```bash
docker compose down
```

如果需要同时删除数据库卷：

```bash
docker compose down -v
```

## 后台使用

进入后台：

```text
http://127.0.0.1:8000/admin.html
```

默认后台密码：

```text
aiad-admin-2026
```

后台支持：

- 选择开始日期和结束日期。
- 选择全部时间段、上午、下午，或某一个具体 10 分钟时间段。
- 按状态筛选：全部、只看已预约、只看已占用、只看可预约。
- 按预约人姓名、导师或邮箱搜索。
- 在日历式预约看板中点击时间段可多选，也可一键选择当天可预约或已占用时间；右侧面板支持批量占用或批量释放，表格行也提供单个占用/释放按钮。
- 在详情面板或表格行中取消预约；取消会直接删除数据库中的对应预约记录。
- 点击“导出表格”导出当前预览条件下的 CSV。

## API

### 获取占用时间

```http
GET /api/availability
```

前台使用这个接口只获取已占用的 `date` 和 `time`，不会返回姓名、导师或邮箱。
后台手动占用的时间段也会出现在这个接口里，前台会同样置灰不可预约。

### 后台登录

```http
POST /api/admin/login
Content-Type: application/json

{
  "password": "aiad-admin-2026"
}
```

登录成功后浏览器会保存 HttpOnly Cookie，用于访问后台接口。

### 获取预约名单

```http
GET /api/bookings
```

需要后台登录。

### 创建、修改或取消预约

创建预约前必须先发送并验证邮箱验证码。
如果邮箱已有预约，验证成功后前台会进入修改模式，可以更新或取消该邮箱对应的预约。
同一页面会话中邮箱验证通过后会复用验证状态，继续创建、修改或取消都不需要重新输入验证码；更换邮箱会重置验证状态。

### 发送邮箱验证码

```http
POST /api/verification/send
Content-Type: application/json

{
  "email": "zhangsan@example.com"
}
```

### 验证邮箱验证码

```http
POST /api/verification/verify
Content-Type: application/json

{
  "email": "zhangsan@example.com",
  "code": "123456"
}
```

验证成功会返回 `verificationToken`，前台会在当前页面会话中保存并用于后续创建、修改或取消预约。

```http
POST /api/bookings
Content-Type: application/json

{
  "name": "张三",
  "mentor": "李老师",
  "email": "zhangsan@example.com",
  "verificationToken": "邮箱验证接口返回的 token",
  "date": "2026-07-04",
  "time": "09:30"
}
```

如果同一日期同一时间已被预约，或同一邮箱已经预约过，接口返回 `409`。

预约成功后，系统会向预约邮箱发送确认邮件。默认测试地点：

```text
重庆大学A区校医院四楼阿尔兹海默症样机测试
```

### 修改预约时间

```http
PUT /api/bookings/by-email
Content-Type: application/json

{
  "email": "zhangsan@example.com",
  "verificationToken": "邮箱验证接口返回的 token",
  "date": "2026-07-04",
  "time": "13:10"
}
```

系统不支持直接修改预约。该接口会返回 `405`，提示先取消已有预约后重新预约。

### 前台取消预约

取消预约前必须先发送并验证邮箱验证码。同一页面会话已验证过该邮箱时可直接取消。

```http
DELETE /api/bookings/by-email
Content-Type: application/json

{
  "email": "zhangsan@example.com",
  "verificationToken": "邮箱验证接口返回的 token"
}
```

取消成功后会直接从 `bookings` 表删除该邮箱对应的预约记录。

### 后台占用时间段

```http
GET /api/blocked-slots
```

需要后台登录。返回后台手动占用的时间段。

```http
POST /api/blocked-slots
Content-Type: application/json

{
  "date": "2026-07-04",
  "time": "13:10"
}
```

需要后台登录。如果该时间已有预约，接口返回 `409`。

```http
DELETE /api/blocked-slots
Content-Type: application/json

{
  "date": "2026-07-04",
  "time": "13:10"
}
```

需要后台登录。只能释放后台手动占用的空闲时间段；已有预约的时间段不能释放。

### 取消预约

```http
DELETE /api/bookings/{id}
```

需要后台登录。
取消成功后会直接从 `bookings` 表删除对应记录。

### 导出 CSV

```http
GET /api/export.csv?startDate=2026-07-04&endDate=2026-07-04&time=all&status=all
```

需要后台登录。

可用查询参数：

- `startDate`：开始日期，格式 `YYYY-MM-DD`。
- `endDate`：结束日期，格式 `YYYY-MM-DD`。
- `time`：`all`、`morning`、`afternoon`，或具体时间如 `09:30`。
- `status`：`all`、`booked`、`blocked`、`available`。
- `search`：姓名、导师或邮箱关键词。

CSV 使用 UTF-8 BOM，方便用 Excel 打开中文内容。

## 环境变量

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `HOST` | `127.0.0.1` | 服务监听地址 |
| `PORT` | `8000` | 服务端口 |
| `BOOKING_DB_PATH` | `./bookings.sqlite3` | SQLite 数据库路径 |
| `ADMIN_PASSWORD` | `aiad-admin-2026` | 后台登录密码 |
| `SMTP_HOST` | 空 | SMTP 服务器；发送验证码时必填 |
| `SMTP_PORT` | 空 | SMTP 端口；发送验证码时必填 |
| `SMTP_USER` | 空 | SMTP 登录账号；发送验证码时必填 |
| `SMTP_PASSWORD` | 空 | SMTP 授权码；发送验证码时必填，不建议写入代码或提交到仓库 |
| `SMTP_FROM` | `SMTP_USER` | 发件人地址；发送验证码时必填 |
| `SMTP_USE_SSL` | `1` | 是否使用 SSL 连接 |
| `SMTP_STARTTLS` | `0` | 非 SSL 模式下是否启用 STARTTLS |
| `VERIFICATION_TTL_MINUTES` | `10` | 验证码有效分钟数 |
| `EMAIL_SESSION_TTL_HOURS` | `12` | 邮箱验证通过后，本次页面会话 token 的有效小时数 |

Dockerfile 中默认：

```text
HOST=0.0.0.0
PORT=8000
BOOKING_DB_PATH=/data/bookings.sqlite3
ADMIN_PASSWORD=aiad-admin-2026
```

## 测试

Python 语法检查：

```bash
python3 -m py_compile server.py
```

预约创建接口快速测试需要先完成邮箱验证。验证码发送接口示例：

```bash
curl -s -H 'Content-Type: application/json' \
  -d '{"email":"zhangsan@example.com"}' \
  http://127.0.0.1:8000/api/verification/send
```

完成验证码验证后，重复提交同一 `date + time` 或同一邮箱应返回冲突错误。

后台接口测试示例：

```bash
curl -c /tmp/aiad-cookie.txt -s -H 'Content-Type: application/json' \
  -d '{"password":"aiad-admin-2026"}' \
  http://127.0.0.1:8000/api/admin/login

curl -b /tmp/aiad-cookie.txt -s http://127.0.0.1:8000/api/bookings
```
