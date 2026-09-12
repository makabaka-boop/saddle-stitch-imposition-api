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

### `POST /imposition/locate`（单页定位）

抽查单个页码时不必遍历整份拼版结果：提交总页数与要定位的页码，服务复用
同一套拼版对象，直接返回该页码所在的**纸张序号、正面/背面、左/右位置及同面
搭档页码**。位置由既有排列结果确定，因此与 `POST /imposition` 永远一致。

请求体**只能**包含两个字段：

- `total_pages`：校验规则与 `POST /imposition` **完全一致**
  （严格整数、4..128、能被 4 整除、拒绝多余字段）；
- `page_number`：**严格整数**（拒绝 `"8"`、`8.0`、布尔值、`null` 等），
  取值范围 **1 ≤ page_number ≤ total_pages**。

任一条件不满足都返回 **HTTP 422**，错误 `loc` 以对应字段名结尾
（`["body","page_number"]` 或 `["body","total_pages"]`），响应中
**不附带任何局部定位结果**（无 sheet_index/side/position/partner_page）。
当 `total_pages` 本身非法时，`page_number` 的一基下界（≥ 1）仍然检查：
下界越界（如第 0 页）会与 `total_pages` 错误在**同一次响应中同时列出**，
只有依赖未知上界的检查被跳过。

OpenAPI 描述（`/openapi.json`）中 `LocateRequest` 携带结构化约束，
生成客户端无需阅读散文即可获知合法包络：`total_pages` 为
`minimum: 4`、`maximum: 128`、`multipleOf: 4`，`page_number` 为
`minimum: 1`（上界取决于当次请求的 `total_pages`，故仅见诸字段描述）。

#### 请求示例

```bash
curl -s -X POST http://localhost:8000/imposition/locate \
  -H 'Content-Type: application/json' \
  -d '{"total_pages": 16, "page_number": 8}'
```

#### 合法响应（200）

定位 16 页画册第 1 页（最外层、正面、右位，搭档为第 16 页）：

```json
{
  "total_pages": 16,
  "page_number": 1,
  "sheet_index": 0,
  "side": "front",
  "position": "right",
  "partner_page": 16
}
```

定位第 8 页（最内层纸张 index 3、背面、左位，搭档为第 9 页）：

```json
{
  "total_pages": 16,
  "page_number": 8,
  "sheet_index": 3,
  "side": "back",
  "position": "left",
  "partner_page": 9
}
```

字段说明：

- `total_pages` / `page_number`：回显的合法入参；
- `sheet_index`：纸张序号，0 基、由外到内（0 为最外层）；
- `side`：所在面，`front`（正面）或 `back`（背面）；
- `position`：该面上的左右位置，`left` 或 `right`；
- `partner_page`：同面搭档页码（该面另一个槽位的页码）。

边界画册：4 页时第 1 页为 `sheet 0 / front / right / partner 4`，第 4 页为
`sheet 0 / front / left / partner 1`；128 页时第 1 页搭档为 128、第 128 页
搭档为 1，均位于最外层正面。

| 输入 | 结果 |
| --- | --- |
| `{"total_pages": 16, "page_number": 1}` | 200，`0 / front / right / partner 16` |
| `{"total_pages": 16, "page_number": 8}` | 200，`3 / back / left / partner 9` |
| `{"total_pages": 4, "page_number": 1}` / `…, 4` | 200，边界画册首末页 |
| `{"total_pages": 128, "page_number": 1}` / `…, 128` | 200，边界画册首末页 |
| `{"total_pages": 16, "page_number": 0}` / `17` | 422，`…page_number must be between 1 and 16.` |
| `{"total_pages": 16, "page_number": "8"}` / `8.0` / `true` / `null` | 422，`…page_number must be an integer.`，loc 指向 `page_number` |
| `{"total_pages": 16}` | 422，`page_number` 缺失 |
| `{"total_pages": 18, "page_number": 8}` | 422，仅 `total_pages` 报错（`…divisible by 4.`） |
| `{"total_pages": 18, "page_number": 0}` | 422，`total_pages`（`…divisible by 4.`）与 `page_number`（`…at least 1.`）同时报错 |
| `{"total_pages": 16, "page_number": 8, "rotate": true}` | 422，loc 指向多余字段 `rotate`（`extra_forbidden`） |

### `GET /health`

返回 `{"status": "ok"}`，供容器健康检查使用。
交互文档（Swagger UI）位于 `/docs`，OpenAPI 描述位于 `/openapi.json`。

## 纸张成本报价（Quotes）

接单员在拼版前即可为一次印刷形成**可追溯**的纸张成本报价：每张报价单拥有
**独立编号**（`Q-000001`、`Q-000002`…）、创建时固化且永不改写的**金额快照**，
以及 **待确认（pending）→ 已确认（confirmed）** 的生命周期。报价数据持久化在
**同一进程内**的 SQLite 数据库中（迁移随应用启动自动执行，不引入任何新服务）。

纸张总量与金额的计算规则（纯领域对象，可手工复算）：

```
每册纸张数 sheets_per_booklet = total_pages / 4
基准纸张   base_sheets        = sheets_per_booklet × print_run
损耗纸张   loss_sheets        = ⌈base_sheets × loss_rate⌉   （向上取整）
纸张总量   total_sheets       = base_sheets + loss_sheets
金额       total_amount       = total_sheets × unit_price   （Decimal，保留两位）
```

金额一律使用 `Decimal` 并按 **ROUND_HALF_UP 保留两位小数**；单价、损耗率、金额
在响应中均以字符串形式原样返回（如 `"5250.00"`），不经过二进制浮点。

### `POST /quotes`（创建报价）

请求体**只能**包含四个字段：

- `total_pages`：校验规则与拼版接口**完全一致**（严格整数、4..128、能被 4 整除）；
- `print_run`：印量，**严格整数**且 **1 ≤ print_run ≤ 2⁶³−1**（拒绝布尔值、
  `"1000"`、`1000.0`）；上界是 SQLite `INTEGER`（有符号 64 位）存储宽度，
  同时要求 `(total_pages / 4) × print_run` 的基准纸张数不超过 2⁶³−1——印量
  本身可存、但基准纸张列存不下时同样在边界以 `print_run` 字段错误拒绝；
- `unit_price`：每张纸单价，**精确十进制**——只接受**字符串**（如 `"1.25"`）或
  **整数**（如 `2`）；**JSON 浮点数一律拒绝**（`1.25`、`2.0` 都不行），因为二进制
  浮点无法精确表示十进制价格；不得为负数；指数过大、乘以纸张总量会越过 Decimal
  指数容量（`Emax = 999999`）的“有限但天文数字”单价（如 `"1E1000000"`）也会在
  边界以 `unit_price` 字段错误拒绝，而不是在金额计算阶段溢出；
- `loss_rate`：损耗率，非负十进制（如 `0.05` 表示 5%），可给数值或字符串；
  不得为负数；**向上取整后的纸张总量必须不超过 2⁶³−1**——损耗率会把总量推过
  存储宽度时（含指数大到乘积本身会溢出 Decimal 的情形），在**创建计算之前**即以
  `loss_rate` 字段错误明确拒绝。

请求体不得携带任何其他字段。任一条件不满足都返回 **HTTP 422**，错误体为
FastAPI/Pydantic 标准的 `detail` 数组，`loc` 以出错字段名结尾，且不附带任何
报价结果；校验失败的请求**不会写入任何数据**（编号不会被消耗）。

成功时返回 **HTTP 201** 与持久化后的完整快照：

```bash
curl -s -X POST http://localhost:8000/quotes \
  -H 'Content-Type: application/json' \
  -d '{"total_pages": 16, "print_run": 1000, "unit_price": "1.25", "loss_rate": 0.05}'
```

```json
{
  "quote_id": "Q-000001",
  "status": "pending",
  "total_pages": 16,
  "print_run": 1000,
  "unit_price": "1.25",
  "loss_rate": "0.05",
  "sheets_per_booklet": 4,
  "base_sheets": 4000,
  "loss_sheets": 200,
  "total_sheets": 4200,
  "total_amount": "5250.00",
  "created_at": "2026-09-12T03:09:39.386087+00:00",
  "confirmed_at": null
}
```

即验收基准：**1000 册 16 页画册**，单价 1.25、损耗率 5% → 每册 4 张、基准
4000 张、损耗 200 张、**总量 4200 张、金额 5250.00**。

| 输入（其余字段同验收例） | 结果 |
| --- | --- |
| `unit_price: "1.25"` / `2` | 201，精确十进制 |
| `unit_price: 1.25` / `2.0`（浮点） | 422，`…not a float.`，loc 指向 `unit_price` |
| `unit_price: "-0.01"` | 422，`…must not be negative.` |
| `print_run: 0` / `-1` | 422，`…must be at least 1.` |
| `print_run: 2^63` / `10^30` | 422，`…must be at most 9223372036854775807…`，loc 指向 `print_run` |
| `print_run` 本身合法但 `(页数/4) × print_run` 超 2⁶³−1 | 422，`…must keep base sheets at most…`，loc 指向 `print_run` |
| `loss_rate` 使纸张总量超 2⁶³−1（如印量 10¹⁸、损耗率 10） | 422，`…must keep total sheets at most…`，loc 指向 `loss_rate` |
| `unit_price: "1E1000000"`（有限但指数极大） | 422，`…exponent is too large…`，loc 指向 `unit_price` |
| `print_run: true` / `"1000"` / `1000.0` | 422，`…must be an integer.` |
| `loss_rate: -0.01` | 422，`…must not be negative.` |
| 任一字段为布尔值 | 422，loc 指向对应字段 |
| 夹带 `discount` 等多余字段 | 422，`extra_forbidden`，loc 指向多余字段 |
| `{}` | 422，四个字段全部报缺失 |

### `POST /quotes/{quote_id}/confirm`（确认采用）

只有**待确认**的报价可以转为已确认；确认只翻转状态并写入 `confirmed_at`，
**绝不改写原快照**（金额、数量、创建时间保持创建时的值）。

- 成功：**HTTP 200**，返回确认后的完整快照（`status: "confirmed"`）；
- 编号不存在或无法识别：**HTTP 404**，`{"detail": "Unknown quote_id: '…'."}`；
- 重复确认：**HTTP 409**，`{"detail": "Quote '…' is already confirmed."}`，
  原快照（含首次确认时间）保持不变。

```bash
curl -s -X POST http://localhost:8000/quotes/Q-000001/confirm
```

### `GET /quotes/{quote_id}`（重新读取）

按编号重新读取已持久化的报价快照，确认前后均可调用；未知编号返回
**HTTP 404**。进程重启后数据仍在（同一 SQLite 文件）。

```bash
curl -s http://localhost:8000/quotes/Q-000001
```

### `POST /quotes/compare`（两套方案差额核对）

接单员确认报价前常要比较同一画册的两套纸价或损耗方案。提交**基准报价
编号**与**候选报价编号**，服务一次批量读取两份持久化快照（按输入顺序，
不依赖存储返回顺序），返回双方编号、**纸张总量差**、**金额差**与**较低金额
的报价编号**。

请求体**只能**包含两个字段（多余字段 422）：

- `baseline_quote_id` / `candidate_quote_id`：报价编号字符串
  （`Q-000001` 形式，也接受裸数字 `1`）；两者必须是**不同编号**
  （`"Q-000001"` 与 `"1"` 也视为同一编号，返回 422）；
- 编号必须是**字符串**：布尔值、整数、`null` 等非字符串一律 **422**，
  `loc` 指向出错字段。

比较规则（纯领域层，路由只做编号解析、对象组装与错误映射）：

- 仅允许**总页数与印量均一致**的组合——即同一画册同一印量的两套计价方案；
  口径不一致返回 **HTTP 409**，错误体列出双方编号及各自页数/印量；
- `sheet_difference = 基准 total_sheets − 候选 total_sheets`（有符号整数）；
- `amount_difference = 基准 total_amount − 候选 total_amount`（有符号，
  **两位十进制字符串**，如 `"306.00"`、`"-306.00"`），直接相减两份已量化的
  金额快照，不经过二进制浮点；减法在按两侧操作数位数扩容的 Decimal 上下文中
  进行，因此即使两份金额相差超过 28 位数量级，较小金额的低位也不会被默认
  28 位精度舍弃（如 `4×10²⁹` 与 `40.00` 的差额完整保留为
  `399999999999999999999999999960.00`）；
- `lower_quote_id`：金额较低的一方编号；金额相等时为 `null`，该结果与
  基准/候选的先后无关；
- **交换基准与候选，两个差额符号同时反转**，`lower_quote_id` 不变。

任何失败（404/409/422）都是只读核对：**不改写**任一报价的状态、确认时间或
金额快照。

```bash
curl -s -X POST http://localhost:8000/quotes/compare \
  -H 'Content-Type: application/json' \
  -d '{"baseline_quote_id": "Q-000001", "candidate_quote_id": "Q-000002"}'
```

以两份 **16 页 / 1000 册** 报价为例（Q-000001：单价 1.25、损耗 5% →
4200 张、5250.00；Q-000002：单价 1.20、损耗 3% → 4120 张、4944.00）：

```json
{
  "baseline_quote_id": "Q-000001",
  "candidate_quote_id": "Q-000002",
  "sheet_difference": 80,
  "amount_difference": "306.00",
  "lower_quote_id": "Q-000002"
}
```

交换基准与候选后：

```json
{
  "baseline_quote_id": "Q-000002",
  "candidate_quote_id": "Q-000001",
  "sheet_difference": -80,
  "amount_difference": "-306.00",
  "lower_quote_id": "Q-000002"
}
```

| 情形 | 结果 |
| --- | --- |
| 同口径两份报价 | 200，有符号纸张差/金额差（两位小数字符串）与较低金额编号 |
| 金额相等 | 200，`amount_difference: "0.00"`，`lower_quote_id: null` |
| 交换基准/候选 | 200，两个差额符号反转，`lower_quote_id` 不变 |
| 任一编号格式非法或不存在 | **404**，`{"detail": "Unknown quote_id: '…'."}`，指出对应编号 |
| 总页数或印量不一致 | **409**，错误体列出双方编号与各自页数、印量 |
| 两个编号相同（含 `Q-1` 与 `1`） | **422**，`…must be different.` |
| 编号为布尔值/整数/`null` | **422**，`loc` 指向 `baseline_quote_id` / `candidate_quote_id` |
| 夹带多余字段 / 缺字段 | **422**（`extra_forbidden` / 字段缺失） |

### 报价数据库配置

- 默认数据库文件为工作目录下的 `quotes.sqlite3`；
- 环境变量 **`QUOTE_DB_PATH`** 可覆盖数据库文件路径；
- 表结构迁移（`schema_migrations` 版本表）随应用启动在同一进程内执行，
  重复启动安全（幂等）。

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
- 单页定位对所有合法画册的每个页码与完整拼版逐槽位核对（纸张、面、
  左右、搭档页码完全一致），并固定 16 页第 1 页（外层正面右位）、
  第 8 页（内层背面左位）与 4/128 边界页签名；
- 越界、不被 4 整除、类型错误、缺字段、非法 JSON 均返回 422，
  且错误 `loc` 可定位到 `total_pages` / `page_number`，响应中不含局部结果；
- 报价验收基准：1000 册 16 页画册（单价 1.25、损耗率 5%）固定得到
  4200 张、5250.00；损耗向上取整（4004 张 × 5% → 201 张）、金额两位
  小数半进位、超大印量不丢精度；
- 报价生命周期：创建为待确认 → 确认后为已确认且快照不变，确认后可重新
  读取、进程重启后仍在；未知编号 404、重复确认 409 且不改写原快照；
- 方案核对：两份同为 16 页 1000 册的报价核对正/负纸张差与精确金额差，
  交换基准与候选后两个差额符号反转、较低金额编号不变；金额相等时
  `lower_quote_id` 为 `null`；缺失/非法编号 404 并指出对应编号、页数或
  印量口径冲突 409，任何失败都不改写报价状态、确认时间或金额快照；
  相同编号（含 `Q-1` 与 `1`）、布尔值、非字符串编号、多余与缺失字段均 422；
- 非法计价参数（布尔值、浮点单价、负数、多余字段、缺字段）均返回 422
  且 `loc` 指向出错字段，失败请求不消耗报价编号；
- 原 `POST /imposition`、`POST /imposition/locate` 与 `GET /health`
  契约保持不变（回归测试）。

```bash
pip install -r requirements.txt
pytest
```

## 目录结构

```
app/
  __init__.py
  imposition.py        # 纯拼版算法核心（无框架依赖，可独立复用/测试）
  quotes.py            # 纯报价领域核心：校验、纸张总量与 Decimal 金额快照
  quote_repository.py  # SQLite 仓储与版本化迁移（随应用启动、同进程）
  schemas.py           # Pydantic 请求/响应模型与字段级校验
  main.py              # FastAPI 路由与启动迁移（lifespan）
tests/
  conftest.py
  test_imposition_core.py
  test_api.py
  test_locate_api.py       # POST /imposition/locate 契约与 422 字段错误
  test_quotes_core.py      # 报价领域核心：算法定量、方案核对与输入校验
  test_quotes_repository.py# SQLite 持久化、批量取回、迁移幂等与生命周期
  test_quotes_api.py       # 报价 HTTP 契约：验收基准、compare、404/409/422
Dockerfile
docker-compose.yml
requirements.txt
```
