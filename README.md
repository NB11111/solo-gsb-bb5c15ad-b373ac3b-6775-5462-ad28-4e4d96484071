# 工业二进制报文解析 API

基于 FastAPI 的工业二进制报文解析服务。用户注册**带版本的报文定义**（位宽、端序、条件分支、校验字段），提交指定版本的十六进制报文后，服务按位解析、验证校验和，并返回字段值、位偏移及实际分支路径；反向地，也可以提交字段值**编码**出十六进制报文（校验和自动计算写入）。SQLite 持久化，启动时自动建表。

## 快速启动

```bash
docker compose up --build
```

单条命令即可启动，服务监听 **8000** 端口：

- 交互式文档（Swagger UI）：http://localhost:8000/docs
- 健康检查：http://localhost:8000/health

## 配置

通过环境变量配置（见 `docker-compose.yml`）：

| 变量      | 默认值             | 说明                 |
| --------- | ------------------ | -------------------- |
| `DB_PATH` | `/data/parser.db`  | SQLite 数据库文件路径（容器内），已挂载 named volume `parser-data` 持久化 |

端口映射在 `docker-compose.yml` 中为 `8000:8000`，如需修改主机端口，改左侧即可（如 `"9000:8000"`）。

## 报文定义格式

```json
{
  "device_type": "temp_sensor",
  "version": "1.0.0",
  "description": "温湿度传感器报文",
  "fields": [
    {"name": "header",      "type": "uint", "bit_length": 8},
    {"name": "msg_type",    "type": "uint", "bit_length": 4},
    {"name": "flags",       "type": "uint", "bit_length": 4},
    {"name": "temperature", "type": "int",  "bit_length": 16, "endianness": "little",
     "scale": 0.1, "condition": {"field": "msg_type", "op": "eq", "value": 1}},
    {"name": "humidity",    "type": "uint", "bit_length": 8,
     "condition": {"field": "msg_type", "op": "eq", "value": 2}},
    {"name": "crc",         "type": "uint", "bit_length": 16}
  ],
  "checksum": {"field": "crc", "algorithm": "crc16-ccitt"}
}
```

**字段属性**

| 属性         | 说明                                                                 |
| ------------ | -------------------------------------------------------------------- |
| `name`       | 字段名，唯一定位                                                     |
| `type`       | `uint` / `int`（二进制补码）/ `bool`（1 位）/ `hex` / `ascii`        |
| `bit_length` | 位宽，支持非字节对齐（如 4 位）；`hex`/`ascii`/小端字段需为 8 的倍数 |
| `endianness` | `big`（默认）/ `little`，仅对多字节 `uint`/`int` 有效                |
| `scale`/`offset` | 可选，仅数值字段：`value = raw * scale + offset`                 |
| `condition`  | 可选，条件不满足时跳过该字段（条件比较的是**原始值 raw**，非缩放值） |

**条件操作符**：`eq` `ne` `gt` `ge` `lt` `le` `in`（`in` 的 `value` 为整数数组）。条件只能引用**之前定义**的数值/布尔字段。

**校验和算法**：`sum8`、`xor8`、`crc16-ccitt`（8 位字段用前两者，16 位字段用后两者）。计算范围为校验字段之前的全部字节，校验字段需字节对齐。

## API 一览

| 方法 | 路径 | 说明 |
| ---- | ---- | ---- |
| POST | `/api/v1/definitions` | 注册报文定义（重复 `device_type+version` 返回 409） |
| GET  | `/api/v1/definitions` | 列出定义，参数：`device_type`、`include_inactive` |
| GET  | `/api/v1/definitions/{device_type}/latest` | 查询该设备类型最新**启用**版本 |
| GET  | `/api/v1/definitions/{device_type}/{version}` | 查询指定版本 |
| POST | `/api/v1/definitions/{device_type}/{version}/deactivate` | 停用定义 |
| POST | `/api/v1/definitions/{device_type}/compatibility` | 两个已注册版本的报文布局兼容性分析（>256 个等价类组合返回 422） |
| POST | `/api/v1/parse` | 解析报文（`version` 缺省时使用最新启用版本） |
| POST | `/api/v1/encode` | 反向编码：由字段值生成报文（版本选择规则同 parse） |

> 停用后的定义不参与列表（默认）和 latest 查询，但仍可按显式版本解析/编码历史报文。

## curl 示例

### 1. 注册定义

```bash
curl -X POST http://localhost:8000/api/v1/definitions \
  -H 'Content-Type: application/json' \
  -d '{
    "device_type": "temp_sensor",
    "version": "1.0.0",
    "description": "温湿度传感器报文",
    "fields": [
      {"name": "header",      "type": "uint", "bit_length": 8},
      {"name": "msg_type",    "type": "uint", "bit_length": 4},
      {"name": "flags",       "type": "uint", "bit_length": 4},
      {"name": "temperature", "type": "int",  "bit_length": 16, "endianness": "little",
       "scale": 0.1, "condition": {"field": "msg_type", "op": "eq", "value": 1}},
      {"name": "humidity",    "type": "uint", "bit_length": 8,
       "condition": {"field": "msg_type", "op": "eq", "value": 2}},
      {"name": "crc",         "type": "uint", "bit_length": 16}
    ],
    "checksum": {"field": "crc", "algorithm": "crc16-ccitt"}
  }'
```

### 2. 解析报文（温度分支）

报文 `aa10eb008936`：`header=0xAA`，`msg_type=1`，温度原始值 `0x00EB=235`（小端）× 0.1 = 23.5 ℃，CRC-16/CCITT = `0x8936`。

```bash
curl -X POST http://localhost:8000/api/v1/parse \
  -H 'Content-Type: application/json' \
  -d '{"device_type": "temp_sensor", "version": "1.0.0", "hex": "aa10eb008936"}'
```

响应（节选）：

```json
{
  "device_type": "temp_sensor",
  "version": "1.0.0",
  "fields": [
    {"name": "header",      "bit_offset": 0,  "bit_length": 8,  "value": 170},
    {"name": "msg_type",    "bit_offset": 8,  "bit_length": 4,  "value": 1},
    {"name": "flags",       "bit_offset": 12, "bit_length": 4,  "value": 0},
    {"name": "temperature", "bit_offset": 16, "bit_length": 16, "value": 23.5, "raw_value": 235},
    {"name": "crc",         "bit_offset": 32, "bit_length": 16, "value": 35126}
  ],
  "branch_path": [
    {"field": "temperature", "condition": "msg_type == 1", "actual_value": 1, "matched": true,  "action": "parsed"},
    {"field": "humidity",    "condition": "msg_type == 2", "actual_value": 1, "matched": false, "action": "skipped"}
  ],
  "checksum": {"field": "crc", "algorithm": "crc16-ccitt", "valid": true, "expected_hex": "0x8936", "calculated_hex": "0x8936"},
  "message_bits": 48, "consumed_bits": 48, "trailing_bits": 0, "warnings": []
}
```

湿度分支报文（`msg_type=2`，湿度 0x55=85%）：`aa2055ba77`。校验和错误时仍返回 200，但 `checksum.valid` 为 `false`；报文过短或 hex 非法返回 422。

### 3. 编码报文（解析的逆操作）

提交字段值，服务按位宽连续写入（支持非字节对齐、大小端、`int` 补码），自动计算并写入校验字段，返回 `hex`、实际编码字段和分支路径：

```bash
curl -X POST http://localhost:8000/api/v1/encode \
  -H 'Content-Type: application/json' \
  -d '{"device_type": "temp_sensor", "version": "1.0.0",
       "values": {"header": 170, "msg_type": 1, "flags": 0, "temperature": 23.5}}'
```

响应（节选）：

```json
{
  "device_type": "temp_sensor",
  "version": "1.0.0",
  "hex": "aa10eb008936",
  "fields": [
    {"name": "header",      "bit_offset": 0,  "bit_length": 8,  "value": 170,  "raw_value": 170},
    {"name": "msg_type",    "bit_offset": 8,  "bit_length": 4,  "value": 1,    "raw_value": 1},
    {"name": "flags",       "bit_offset": 12, "bit_length": 4,  "value": 0,    "raw_value": 0},
    {"name": "temperature", "bit_offset": 16, "bit_length": 16, "value": 23.5, "raw_value": 235},
    {"name": "crc",         "bit_offset": 32, "bit_length": 16, "value": 35126, "raw_value": 35126}
  ],
  "branch_path": [
    {"field": "temperature", "condition": "msg_type == 1", "actual_value": 1, "matched": true,  "action": "encoded"},
    {"field": "humidity",    "condition": "msg_type == 2", "actual_value": 1, "matched": false, "action": "skipped"}
  ],
  "checksum": {"field": "crc", "algorithm": "crc16-ccitt", "range": "bytes [0, 4)", "calculated": 35126, "calculated_hex": "0x8936"},
  "message_bits": 48, "padding_bits": 0
}
```

**编码规则**

- `values` 中各值采用**解析响应的类型**：`uint`/`int` 为整数（带 `scale`/`offset` 时可为浮点数），`bool` 为布尔值，`hex` 为 `"0x..."` 字符串，`ascii` 为字符串（长度须等于 `bit_length/8`）。
- 带 `scale`/`offset` 的数值按 `raw = (value - offset) / scale` 反算；无法精确还原或 raw 超出位宽范围时返回 422。
- 条件判断统一使用 **raw**（与 parse 一致）：条件未命中的字段不要求输入（提供了也返回 422）；命中字段缺失、出现未知字段均返回 422。
- 校验字段**不接受调用方赋值**，服务在其位置自动计算写入；校验位置未字节对齐时返回 422。

### 4. 列表 / 最新版本 / 停用

```bash
# 列出全部启用中的定义（可加 ?device_type=temp_sensor 或 ?include_inactive=true）
curl http://localhost:8000/api/v1/definitions

# 查询设备类型的最新启用版本
curl http://localhost:8000/api/v1/definitions/temp_sensor/latest

# 停用指定版本
curl -X POST http://localhost:8000/api/v1/definitions/temp_sensor/1.0.0/deactivate
```

### 5. 版本兼容性分析

比较同一 `device_type` 的两个**已注册**版本（无需提供报文）。分析器：

1. 收集所有被条件引用的字段（控制字段），按条件中出现的整数常量把每个控制字段的原始值域划分为有限个**等价类**——同一等价类内的取值使该字段上的全部条件真假恒定（如 `eq 1` 分出 `[0] / [1] / [2..2ⁿ-1]`，`gt 5` 在 `6` 处切分）；
2. 枚举各控制字段等价类代表值的笛卡尔积，对每个可达组合**模拟布局**：按条件保留/跳过字段，计算每个字段的位偏移与布局总长度（条件引用的字段被更早分支跳过时该组合不可达，自动跳过）；
3. 对旧版每个可达布局，在新版的**相同条件代表值**下寻找对应布局：字段名称、类型、位宽、位偏移、总长度及校验算法必须全部一致。任一旧布局找不到对应布局即判为 `breaking`，逐分支返回差异；否则判为 `compatible`。

等价类组合总数超过 **256** 时返回 `422`（边界值 256 正常分析）。

```bash
curl -X POST http://localhost:8000/api/v1/definitions/temp_sensor/compatibility \
  -H 'Content-Type: application/json' \
  -d '{"old_version": "1.0.0", "new_version": "2.0.0"}'
```

兼容时响应（节选）：

```json
{
  "device_type": "temp_sensor",
  "old_version": "1.0.0",
  "new_version": "2.0.0",
  "compatible": true,
  "status": "compatible",
  "equivalence_classes": [
    {"field": "msg_type", "classes": 4, "representatives": [0, 1, 2, 3]}
  ],
  "combination_count": 4,
  "reachable_branches": 4,
  "unreachable_combinations": 0,
  "breaking_branches": 0,
  "branches": [
    {
      "representative": {"msg_type": {"op": "==", "value": 1}},
      "compatible": true,
      "old_layout": {
        "fields": [
          {"name": "header",      "type": "uint", "bit_length": 8,  "bit_offset": 0},
          {"name": "msg_type",    "type": "uint", "bit_length": 4,  "bit_offset": 8},
          {"name": "flags",       "type": "uint", "bit_length": 4,  "bit_offset": 12},
          {"name": "temperature", "type": "int",  "bit_length": 16, "bit_offset": 16},
          {"name": "crc",         "type": "uint", "bit_length": 16, "bit_offset": 32}
        ],
        "total_bits": 48,
        "checksum_algorithm": "crc16-ccitt"
      },
      "new_layout": { "...": "结构相同..." }
    }
  ]
}
```

发生破坏时，顶层 `"status"` 为 `"breaking"`、`"compatible"` 为 `false`，不兼容分支的 `"differences"` 逐项列出差异，`kind` 取值：

| `kind` | 含义 |
| ------ | ---- |
| `field_added` / `field_removed` | 新版多出 / 删除了字段（含因新分支引入的字段） |
| `field_changed` | 同名字段的 `type` / `bit_length` / `bit_offset` 变化 |
| `total_length_changed` | 布局总位数变化 |
| `checksum_algorithm_changed` | 校验算法变化（如 `crc16-ccitt` → `crc16-modbus`） |
| `branch_unreachable` | 旧版可达分支在新版下条件无法满足（控制字段被跳过） |

两版本对同一控制字段的位宽不同时，按两者原始值域的**交集**划分（无交集返回 422）；新版新增控制字段或删除旧控制字段时，仅在受影响的等价类上产生差异，未受影响分支仍可判为兼容。

## 本地开发（不用 Docker）

```bash
pip install -r requirements-dev.txt
uvicorn app.main:app --reload        # http://localhost:8000
pytest                               # 运行测试
```

## 项目结构

```
app/
  main.py       # FastAPI 路由与生命周期
  schemas.py    # 请求模型与定义语义校验
  parser.py     # 按位解析器（位读取、条件分支、校验和验证）
  encoder.py    # 按位编码器（位写入、scale/offset 反算、校验和自动写入）
  analyzer.py   # 版本兼容性分析（条件等价类划分、可达分支枚举、布局比对）
  checksum.py   # sum8 / xor8 / crc16-ccitt / crc16-modbus
  database.py   # SQLite 持久化（启动自动建表）
tests/test_api.py
Dockerfile
docker-compose.yml
```
