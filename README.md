# 本地字幕切分 API（Python + FastAPI）

接收**按时间排序、互不重叠的词级转写**（每个词含文字与起止毫秒），用**动态规划**寻找覆盖全部词、保持原有顺序的合法字幕切分，并支持 SRT 导出。

- 优化目标（总代价）：

  ```
  Σ (该条字幕时长_秒 − 目标时长_秒)²
      + 4 ×（被字幕跨过的句末标点 / 长停顿边界数量）
  ```

  - 两个相邻词之间，若前一个词以句末标点（`. ! ? … 。 ！ ？`，允许后面再跟引号/括号）结尾，或两词间隔 ≥ `pause_threshold_ms`，即为一个**自然边界**；
  - 自然边界落在字幕内部（而不是字幕边缘）就算“被跨过”，每条加 4 分；
  - **总代价平局时，选择字幕条数更少的方案。**

- 硬约束（违反则不存在合法切分，返回 `422`）：
  - 每条字幕字数 ≤ `max_chars`；
  - 每条字幕时长 ∈ [`min_duration_ms`, `max_duration_ms`]（含最后一条）；
  - 全部词必须且只能出现一次，顺序不变。

---

## 一键启动（Docker Compose）

前置条件：已安装 Docker（含 Compose v2）。

```bash
docker compose up --build
```

启动后访问：

| 用途 | 地址 |
| --- | --- |
| 服务根信息 | http://localhost:8000/ |
| 健康检查 | http://localhost:8000/health |
| Swagger 交互文档 | http://localhost:8000/docs |
| OpenAPI 描述 | http://localhost:8000/openapi.json |

端口固定为 **8000**，在 `docker-compose.yml` 的 `ports` 中映射；如需改端口，例如改成 9000：

```yaml
ports:
  - "9000:8000"
```

后台运行：`docker compose up --build -d`；停止：`docker compose down`。

---

## 接口

### `POST /segment` —— JSON 切分

请求体：

| 字段 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `words` | 数组 | 必填（至少 1 个） | 词级转写，按时间排序且互不重叠 |
| `words[].text` | string | 必填 | 词文本，非空 |
| `words[].start_ms` | int | 必填 | 起始毫秒，≥ 0 |
| `words[].end_ms` | int | 必填 | 结束毫秒，须 > `start_ms` |
| `max_chars` | int | `42` | 单条字幕字数上限，≥ 1 |
| `min_duration_ms` | int | `1000` | 单条字幕最短时长（毫秒） |
| `max_duration_ms` | int | `7000` | 单条字幕最长时长（毫秒） |
| `target_duration_ms` | int | `3000` | 目标时长（毫秒），代价按秒计算 |
| `pause_threshold_ms` | int | `500` | 词间间隔达到该值即视为长停顿边界 |

> 约束：`max_duration_ms >= min_duration_ms`；参数省略时使用环境变量配置的默认值。

请求示例：

```bash
curl -X POST http://localhost:8000/segment \
  -H 'Content-Type: application/json' \
  -d @examples/request.json
```

响应示例（`200`）：

```json
{
  "segment_count": 3,
  "total_cost": 5.96,
  "segments": [
    {
      "index": 0,
      "text": "Hello world.",
      "start_ms": 0,
      "end_ms": 1400,
      "duration_ms": 1400,
      "word_start": 0,
      "word_end": 1,
      "crossed_boundaries": 0
    },
    {
      "index": 1,
      "text": "How are you doing today?",
      "start_ms": 2000,
      "end_ms": 4600,
      "duration_ms": 2600,
      "word_start": 2,
      "word_end": 6,
      "crossed_boundaries": 0
    },
    {
      "index": 2,
      "text": "Fine thanks.",
      "start_ms": 5200,
      "end_ms": 6400,
      "duration_ms": 1200,
      "word_start": 7,
      "word_end": 8,
      "crossed_boundaries": 0
    }
  ]
}
```

`word_start` / `word_end` 为该字幕覆盖的词下标（含端点）；`crossed_boundaries` 是该字幕内部跨过的自然边界数（理想切分通常为 0）。

### `POST /segment/srt` —— SRT 导出

请求体与 `/segment` 完全相同，返回 `text/plain; charset=utf-8` 的 SRT 文档：

```bash
curl -X POST http://localhost:8000/segment/srt \
  -H 'Content-Type: application/json' \
  -d @examples/request.json
```

```srt
1
00:00:00,000 --> 00:00:01,400
Hello world.

2
00:00:02,000 --> 00:00:04,600
How are you doing today?

3
00:00:05,200 --> 00:00:06,400
Fine thanks.
```

保存到文件：

```bash
curl -X POST http://localhost:8000/segment/srt \
  -H 'Content-Type: application/json' \
  -d @examples/request.json -o subtitles.srt
```

### `GET /health`

返回 `{"status":"ok"}`（容器健康检查同样使用该端点）。

---

## 错误处理（422）

以下情况返回 HTTP `422`，响应体 `detail` 说明原因：

- 某个词自身字数超过 `max_chars`（单词无法容纳）；
- 某个词自身时长超过 `max_duration_ms`；
- 词序列未按时间排序或存在重叠；
- 参数本身不合法（空词、`end_ms <= start_ms`、`max_duration_ms < min_duration_ms` 等）；
- 没有任何切分能满足全部约束（例如最后一个词短于 `min_duration_ms`）——会返回类似：

  ```json
  {"detail": "no legal segmentation covers all words under the given constraints (max_chars=42, min_duration_ms=1000, max_duration_ms=7000); every subtitle — including the last one — must satisfy the duration limits, so try relaxing min_duration_ms or the other limits"}
  ```

---

## 配置

所有切分参数都可**按请求**传入；省略时使用环境变量配置的默认值（见 `docker-compose.yml`）：

| 环境变量 | 默认值 | 对应请求字段 |
| --- | --- | --- |
| `DEFAULT_MAX_CHARS` | `42` | `max_chars` |
| `DEFAULT_MIN_DURATION_MS` | `1000` | `min_duration_ms` |
| `DEFAULT_MAX_DURATION_MS` | `7000` | `max_duration_ms` |
| `DEFAULT_TARGET_DURATION_MS` | `3000` | `target_duration_ms` |
| `DEFAULT_PAUSE_THRESHOLD_MS` | `500` | `pause_threshold_ms` |

不需要外部数据库或密钥，开箱即用。

---

## 本地开发（不使用 Docker）

需要 Python 3.11+：

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

访问 http://localhost:8000/docs 调试。

运行测试（核心切分逻辑只依赖标准库；API 测试需要 fastapi + httpx）：

```bash
python3 -m unittest discover -s tests
# 或
pytest
```

## 项目结构

```
app/
  config.py      # 环境变量默认值与罚分常量
  models.py      # Pydantic 请求/响应模型
  segmenter.py   # 动态规划切分核心（纯 Python，无 Web 依赖）
  srt.py         # SRT 时间戳与文档格式化
  main.py        # FastAPI 入口（/segment、/segment/srt、/health）
tests/           # unittest 测试（核心逻辑 + API 集成）
examples/        # 请求示例
Dockerfile
docker-compose.yml
requirements.txt
```

## 算法说明

动态规划状态 `dp[i]` 为“从第 `i` 个词切分到结尾”的最小代价与字幕条数：

```
dp[n] = (0, 0)
dp[i] = 按 (总代价, 字幕条数) 字典序取最小，枚举合法的字幕结尾 j：
        ((dur_ij秒 − target秒)² + 4 × crossed(i,j) + dp[j+1].代价, 1 + dp[j+1].条数)
```

字幕从 `i` 扩展到 `j` 时字数与时长单调增长，超限即可提前终止；自然边界计数用前缀和做 O(1) 区间查询。整体时间复杂度 O(n²)，空间 O(n)。
