# 骑马订拼版 API（Saddle-stitched Imposition API）

纯后端 FastAPI 服务。制版员输入整本书的总页数，服务按纸张**由外到内**
输出每张纸正面、背面从左到右的页码次序，用于骑马订画册交付印厂前的
正反面页码核对。

- 运行环境：Python 3.12
- 页码全部为**一基整数**（第 1 页开始），**不旋转**、**不补空白页**
- 对任意合法输入，1..total_pages 中**每个页码恰好出现一次**
- 结果唯一且可手工复算

## 纸张顺序是怎么排的

骑马订把整叠对折后的纸张从书脊中缝订住，因此页码必须**首尾配对**、
从最外面的一张纸开始向内嵌套排列。

设纸张由外到内编号 `i = 0 … total_pages/4 − 1`，每张纸对折后承载 4 个页码：

| 面 | 从左到右的页码 |
| --- | --- |
| 正面 front | `[total_pages − 2i, 1 + 2i]` |
| 背面 back  | `[2 + 2i, total_pages − 1 − 2i]` |

以 **16 页 / 4 张纸**为例（`i=0` 是最外层，越往里 `i` 越大）：

| 纸张 i | 正面（左→右） | 背面（左→右） | 物理含义 |
| --- | --- | --- | --- |
| 0 | **16, 1** | **2, 15** | 最外层一张：封面 16 与首页 1 同面 |
| 1 | 14, 3 | 4, 13 | 套在第 0 张内侧 |
| 2 | 12, 5 | 6, 11 | 再向内 |
| 3 | 10, 7 | 8, 9 | 最中一张：8 与 9 相邻于中缝 |

把 4 张纸按 i=0,1,2,3 的顺序**依次嵌套对折、沿中缝骑马装订**，从
第 1 页顺序翻阅即可得到 1→16 的连续画册。规律：

- 每张正面左侧为偶数页、右侧为奇数页（背面同理：左偶右奇）；
- 最外层固定为 `front=[末页, 1]`、`back=[2, 末页−1]`；
- 最内层背面两个号永远相邻（如 8、9）。

## 接口

### `POST /imposition`

请求体**只能**包含一个字段 `total_pages`：

- 必须是**整数**；
- 取值范围 **4 ≤ total_pages ≤ 128**；
- 必须能被 **4** 整除；
- **不得携带任何其他字段**——拼写错误（如 `total_page`）与无关字段
  （如 `rotate`、`blank_pages`）一律拒绝，不做静默忽略。

任一条件不满足都返回 **HTTP 422**，错误体为 FastAPI/Pydantic 标准的
`detail` 数组，其中每个错误的 `loc` 数组都以对应字段名结尾（页码错误
为 `["body","total_pages"]`，多余字段为 `["body","<字段名>"]`），调用
方可直接定位出错字段；校验失败时**不会输出任何拼版结果（无 sheets
字段）**。多个问题会在同一次响应中全部列出。

#### 请求示例

```bash
curl -s -X POST http://localhost:8000/imposition \
  -H 'Content-Type: application/json' \
  -d '{"total_pages": 16}'
```

#### 合法响应（200）

```json
{
  "total_pages": 16,
  "sheet_count": 4,
  "sheets": [
    {"index": 0, "front": [16, 1], "back": [2, 15]},
    {"index": 1, "front": [14, 3], "back": [4, 13]},
    {"index": 2, "front": [12, 5], "back": [6, 11]},
    {"index": 3, "front": [10, 7], "back": [8, 9]}
  ]
}
```

字段说明：

- `total_pages`：回显的合法总页数；
- `sheet_count`：纸张总数 = `total_pages / 4`；
- `sheets[]`：纸张由**外**到**内**排列；
  - `index`：纸张序号，0 基，0 为最外层；
  - `front` / `back`：该张纸正面 / 背面从左到右的两个页码。

#### 校验失败示例（422）

请求：`POST /imposition`，`{"total_pages": 18}`（不能被 4 整除）

```json
{
  "detail": [
    {
      "type": "value_error",
      "loc": ["body", "total_pages"],
      "msg": "Value error, total_pages must be divisible by 4.",
      "input": 18
    }
  ]
}
```

请求：`POST /imposition`，`{"total_pages": 8, "rotate": true}`（夹带多余字段）

```json
{
  "detail": [
    {
      "type": "extra_forbidden",
      "loc": ["body", "rotate"],
      "msg": "Extra inputs are not permitted",
      "input": true
    }
  ]
}
```

| 输入 | 结果 |
| --- | --- |
| `{"total_pages": 8}` | 200，2 张纸 |
| `{"total_pages": 4}` / `128` | 200，边界值 |
| `{"total_pages": 0}` / `2` / `129` | 422，`…between 4 and 128.` |
| `{"total_pages": 18}` / `126` | 422，`…divisible by 4.` |
| `{"total_pages": "16"}` / `16.0` / `true` / `null` | 422，`…must be an integer.` |
| `{"total_pages": 8, "rotate": true}` | 422，`loc` 指向多余字段 `rotate`（`extra_forbidden`） |
| `{"total_page": 8}`（字段名拼错） | 422，同时报 `total_pages` 缺失与 `total_page` 多余 |
| `{"total_pages": 18, "rotate": true}` | 422，同次响应同时列出两处错误 |
| `{}` | 422，字段缺失 |

> 布尔值虽然是 Python 的 `int` 子类，也会被明确拒绝。

### `GET /health`

返回 `{"status": "ok"}`，供容器健康检查使用。
交互文档（Swagger UI）位于 `/docs`，OpenAPI 描述位于 `/openapi.json`。

## 本地运行（不使用 Docker）

```bash
python3.12 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

## Docker Compose

仓库提供两个服务（同一镜像，Python 3.12）：

- `api`：常驻 API 服务，容器内监听 8000，宿主机发布端口由环境变量
  **`API_PORT`** 覆盖（默认 8000）；
- `verify`：一次性服务，运行 pytest 测试套并退出，退出码即验证结果。

```bash
# 构建并启动 API（默认 http://localhost:8000）
docker compose up --build api

# 用自定义宿主端口运行
API_PORT=9000 docker compose up api
# 或复制 .env.example 为 .env 后修改 API_PORT

# 一次性验证：跑完全部测试后退出（成功退出码为 0）
docker compose build verify
docker compose run --rm verify
```

## 测试

pytest 覆盖排列不变量与错误处理：

- 对全部 32 个合法值（4, 8, …, 128）验证所有页码 1..N 的多重集恰好
  出现一次（不重、不漏）；
- 每张纸严格等于题述公式 `front=[N−2i, 1+2i]`、
  `back=[2+2i, N−1−2i]`，并核对 8 页、16 页手算签名与 4/128 边界；
- 越界、不被 4 整除、类型错误、缺字段、非法 JSON 均返回 422，
  且错误 `loc` 可定位到 `total_pages`，响应中不含局部结果。

```bash
pip install -r requirements.txt
pytest
```

## 目录结构

```
app/
  __init__.py
  imposition.py   # 纯算法核心（无框架依赖，可独立复用/测试）
  schemas.py      # Pydantic 请求/响应模型与字段级校验
  main.py         # FastAPI 路由
tests/
  conftest.py
  test_imposition_core.py
  test_api.py
Dockerfile
docker-compose.yml
requirements.txt
```
