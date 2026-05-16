# AIAD 样机测试时间预约

一个轻量的本地预约系统，用于 AIAD 样机测试时间预约。前台给测试人员填写姓名、邮箱并选择时间段；后台用于查看预约情况、按条件预览、取消预约和导出表格。

## 功能

- 前台预约页：姓名、邮箱、日期、20 分钟时间段选择。
- 可预约时间：周二、周三、周四，上午 `09:00-12:00`，下午 `14:00-17:00`。
- 每个时间段 20 分钟，最后可选开始时间为 `11:40` 和 `16:40`。
- SQLite 数据库存储预约记录。
- 数据库唯一约束防止同一日期同一时间段被重复预约。
- 前台不显示后台入口，也不会公开预约人的姓名和邮箱。
- 后台需要密码登录，默认密码为 `aiad-admin-2026`，可用 `ADMIN_PASSWORD` 修改。
- 后台日历式预览：按日期查看每个时间段的预约人。
- 后台表格预览：按日期范围、时间段、状态、姓名或邮箱筛选。
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

## Docker 运行

```bash
docker compose up --build
```

如需修改后台密码：

```bash
ADMIN_PASSWORD='your-strong-password' docker compose up --build
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
- 选择全部时间段、上午、下午，或某一个具体 20 分钟时间段。
- 按状态筛选：全部、只看已预约、只看可预约。
- 按预约人姓名或邮箱搜索。
- 在日历式预览中点击日期和时间段查看详情。
- 在详情面板或表格行中取消预约。
- 点击“导出表格”导出当前预览条件下的 CSV。

## API

### 获取占用时间

```http
GET /api/availability
```

前台使用这个接口只获取已占用的 `date` 和 `time`，不会返回姓名或邮箱。

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

### 创建预约

```http
POST /api/bookings
Content-Type: application/json

{
  "name": "张三",
  "email": "zhangsan@example.com",
  "date": "2026-05-19",
  "time": "09:00"
}
```

如果同一日期同一时间已被预约，接口返回 `409`。

### 取消预约

```http
DELETE /api/bookings/{id}
```

需要后台登录。

### 导出 CSV

```http
GET /api/export.csv?startDate=2026-05-19&endDate=2026-05-21&time=all&status=all
```

需要后台登录。

可用查询参数：

- `startDate`：开始日期，格式 `YYYY-MM-DD`。
- `endDate`：结束日期，格式 `YYYY-MM-DD`。
- `time`：`all`、`morning`、`afternoon`，或具体时间如 `09:00`。
- `status`：`all`、`booked`、`available`。
- `search`：姓名或邮箱关键词。

CSV 使用 UTF-8 BOM，方便用 Excel 打开中文内容。

## 环境变量

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `HOST` | `127.0.0.1` | 服务监听地址 |
| `PORT` | `8000` | 服务端口 |
| `BOOKING_DB_PATH` | `./bookings.sqlite3` | SQLite 数据库路径 |
| `ADMIN_PASSWORD` | `aiad-admin-2026` | 后台登录密码 |

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

接口快速测试示例：

```bash
curl -s -H 'Content-Type: application/json' \
  -d '{"name":"张三","email":"zhangsan@example.com","date":"2026-05-19","time":"09:00"}' \
  http://127.0.0.1:8000/api/bookings
```

重复提交同一 `date + time` 应返回冲突错误。

后台接口测试示例：

```bash
curl -c /tmp/aiad-cookie.txt -s -H 'Content-Type: application/json' \
  -d '{"password":"aiad-admin-2026"}' \
  http://127.0.0.1:8000/api/admin/login

curl -b /tmp/aiad-cookie.txt -s http://127.0.0.1:8000/api/bookings
```
