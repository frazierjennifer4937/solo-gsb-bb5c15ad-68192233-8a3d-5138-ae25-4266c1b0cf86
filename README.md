# 🔎 OpenAPI 版本兼容性审查工具

对比新旧两份 **OpenAPI 3.x**（JSON / YAML）文档，递归解析 `$ref`（**支持循环引用**）、合并 `allOf`，按**请求 / 响应两个方向**的语义规则判断每一处变更是否向后兼容，精确定位破坏性变更的 JSON 路径并给出原因。支持结果筛选、**根因聚合视图**（有向影响组）、引用展开树浏览、审查报告下载、**单条发现的最小复现包导出**、历史记录与**破坏项对账**（基线审查 vs 当前审查），以及**限时豁免**：按 API 标识对破坏项登记带 UTC 到期时间的豁免，读取审查时实时重算匹配状态。

技术栈：**FastAPI + SQLite + 原生 JavaScript**（前端零构建、零框架依赖）。

---

## 🚀 一条命令启动

需要安装 Docker 与 Docker Compose v2：

```bash
docker compose up -d --build
```

启动后访问：

> **http://localhost:8000**

端口说明：容器内监听 `8000`，通过 `docker-compose.yml` 映射到宿主机 **8000**；审查数据保存在 Docker 卷 `review-data`（容器内 `/data/reviews.db`），重启不丢失。应用启动时自动完成建表与数据库结构升级（如新增 `api_id` 列、`exemptions` 表），无需手工迁移。镜像内置 `HEALTHCHECK`，周期性探测 `GET /api/health`，可通过 `docker ps` 查看健康状态。

停止 / 更新：

```bash
docker compose down          # 停止（保留数据卷）
docker compose up -d --build # 修改代码后重新构建
```

如需换端口，修改 `docker-compose.yml` 中 `"8000:8000"` 的左侧宿主机端口即可。

### 本地直接运行（不使用 Docker）

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

---

## 🧭 使用方式

1. 打开 http://localhost:8000 ，在左右两栏**粘贴**或点击「上传」选择旧版 / 新版 OpenAPI 文件（`.json` / `.yaml` / `.yml` 均可，也可混用）。
2. 可先点击内置示例：**典型破坏性变更 / 循环 $ref / 纯兼容新增 / 枚举约束收窄**。
3. （可选）在「API 标识」输入框填写标识（如 `petstore`）——填写后审查结果才能登记与应用**限时豁免**。
4. 点击「开始审查 →」。
5. 结果区提供：
   - 顶部统计卡片与总体结论（✅ 向后兼容 / ❌ N 项破坏性变更）；
   - **🛡️ 限时豁免面板**（填写 API 标识时）：已豁免 / 未豁免破坏项数量、「未豁免为零才可放行」的放行结论、豁免列表（状态 / 理由 / 到期时间 / 撤销）；
   - **逐条发现 / 根因聚合** 视图切换：聚合视图把同根因的发现折叠为有向影响组（详见下文「🧩 根因聚合视图」）；
   - **级别筛选**：🔴 破坏性 / 🟢 兼容 / 🟡 废弃提示 / ⚪ 信息；
   - **方向筛选**（请求 / 响应 / 结构性）、**分类筛选**、关键字搜索（两种视图均生效）；
   - 每条发现可展开：人类可读定位、旧/新版 JSON Pointer、`$ref` 锚点、旧值 / 新值、规则编号；破坏项还可**创建限时豁免**（理由 + UTC 到期时间）；
   - 点击发现项上的 **🌲旧 / 🌲新** 按钮，右侧「引用展开树」自动切到对应版本并展开到该 `$ref`；
   - 每条发现可点击 **📦 导出最小复现包**，下载仅含该发现现场的最小文档对（ZIP，详见下文）；
   - 「⬇ 下载 Markdown / JSON 报告」获取可归档的审查报告（含豁免匹配状态、理由与到期时间）。
6. 「历史记录」标签页可重新打开、下载或删除保存在 SQLite 中的历史审查；并支持**破坏项对账**：选一条已保存审查作基线，与当前打开的审查比较，输出新增 / 延续 / 已消失三类破坏项（详见下文「⚖️ 破坏项对账」）。

---

## 🧠 判定原理

同一类 schema 变化在两个方向上结论可能相反，工具按方向独立判定：

| 视角 | 核心原则 |
| --- | --- |
| **请求方向**（参数、requestBody） | 客户端原本合法发送的内容**被收紧**即为破坏：必填变多、枚举变少、范围收窄、类型收窄等 |
| **响应方向**（responses） | 客户端原本基于契约的预期**被改变**即为破坏：字段消失、枚举变多、承诺范围扩大、变 nullable、字段变 writeOnly 等 |

部分规则示例：

| 变更 | 请求方向 | 响应方向 |
| --- | :---: | :---: |
| 新增必填字段 | 🔴 破坏 | 🟢 兼容（保证更多数据） |
| 新增可选字段 | 🟢 兼容 | 🟢 兼容（严格模型需确认） |
| 删除字段 | 🟢 兼容* | 🔴 破坏 |
| 可选 → 必填 | 🔴 破坏 | 🟢 兼容 |
| 必填 → 可选 | 🟢 兼容 | 🔴 破坏（字段可能消失） |
| 枚举取值增加 | 🟢 兼容 | 🔴 破坏 |
| 枚举取值减少 | 🔴 破坏 | 🟢 兼容 |
| `minimum` 升高 / `maximum` 降低 | 🔴 破坏（收紧） | 🟢 兼容 |
| `minimum` 降低 / `maximum` 升高 | 🟢 兼容 | 🔴 破坏（承诺漂移） |
| 变 `nullable` | 🟢 兼容 | 🔴 破坏 |
| 字段变 `readOnly` | 🔴 破坏 | 🟢 兼容 |
| 字段变 `writeOnly` | 🟢 兼容 | 🔴 破坏 |
| `additionalProperties: true→false` | 🔴 破坏 | 🟢 兼容 |

\* 请求字段从契约移除时服务端可能开始忽略/拒绝，工具以兼容记录并在原因中注明。

结构层面还覆盖：路径与操作的新增/删除、pathItem 级与操作级参数合并比较、参数必填性 / `style` / `explode` / `deprecated`、请求体必填性变化、媒体类型增删（请求 415 / 响应 Content-Type）、响应状态码增删、`servers` 增删、`operationId` 变化、`oneOf/anyOf` 分支增删等。

> 页面内「规则说明」标签有完整列表；每条发现都带稳定的 `rule_id`（如 `property-became-required`、`enum-value-added`），便于在 CI 中按规则处理。

### `$ref` 与循环引用处理

- 参数、请求体、响应、schema 中的 `$ref` 在比较前先解引用；`allOf` 按合取语义合并（属性深度合并、`required` 取并集、min 取大 / max 取小、枚举取交集）。
- schema 递归比较同时维护 **DFS 活跃栈**与**已比较节点对集合**：遇到 A→B→A 形式的自引用 / 互相引用时立即终止该分支，且同一条差异不会沿环路重复刷屏；循环节点在展开树中以 **⟳ 循环引用** 高亮。
- 兼容 OpenAPI 3.0（`nullable: true`）与 3.1（`type: ["string","null"]`）两种可空写法。

发现项给出**双重定位**：

- `location`：载荷语义路径，如 `requestBody > application/json → 请求体.address.zip`
- `old_json_path` / `new_json_path`：文档内 JSON Pointer（引用组件时锚点指向 `/components/schemas/...`），可直接用于在引用树中跳转。

---

## 🧩 根因聚合视图

逐条发现列表在大型变更中容易"见树不见林"：一次类型整体替换会连带产生多条子字段删除，共享组件被多处引用时同一根因会以多条发现出现。详情页的「**根因聚合**」切换把发现折叠为**有向影响组**，每组一个主因。

**分组规则**（服务端 `GET /api/reviews/{id}/groups` 实时计算，纯函数、无额外存储）：

- **端点 × 方向是硬边界**：同一组件（含循环引用场景）在请求侧与响应侧**各自成组**，绝不跨方向合并；
- **同源归并**：共享 / 循环组件被多处引用时，引擎可能对同一组件指针重复报告同一规则——`rule_id`、JSON Pointer 与 `$ref` 锚点完全一致的发现折叠为同一节点，多余条目记为 **同源**；
- **祖先吸收**：父路径被**整体替换**（`type-changed`、schema 变为布尔等）或**整体删除**（`property-removed`、`response-removed` 等）时，其 JSON Pointer 后代发现只是下游影响，被父发现吸收，记为 **下游影响**（有向边 父 → 子，挂接最近祖先；`parameter-removed` 等容器级指针不参与吸收，避免误并同操作下其他参数的独立发现）。

**每组返回**：

| 字段 | 说明 |
| --- | --- |
| `group_id` | 稳定组标识：端点 / 方向 / 规则 / 锚点指针的 SHA-256 短哈希，同一审查重复查询结果一致 |
| `primary` | 主因发现（完整字段，含 `relation: "primary"`） |
| `affected` | 受影响条目（完整发现字段 + `relation`（`absorbed` / `same_cause`）、`depth`、`absorbed_by`） |
| `locations` | 可展开定位：每个成员的语义路径 + 双侧 JSON Pointer + `$ref` 锚点 |
| `severity` / `member_count` / `breaking_count` | 组级统计（严重度取组内最高） |

聚合视图中主因与受影响条目均可展开定位、跳转引用树（🌲）并导出最小复现包（📦），与逐条视图能力一致；级别 / 方向 / 分类 / 关键字筛选同样生效。

---

## ⚖️ 破坏项对账

「历史记录」页顶部的对账面板用于回答一个问题：**相对上次基线，这次审查的破坏性变更多了哪些、还有哪些没修、哪些已经消失**。

**使用方式**：先在历史记录中「打开」一条审查作为当前审查（或刚执行完一次新审查），再在下拉框选一条已保存审查作基线（也可直接点历史行的「对账」按钮），点击「开始对账」。

**对账原理**（服务端 `GET /api/reviews/{id}/reconcile?baseline_id=N` 实时计算，纯函数、无额外存储）：

- **只对账破坏性发现**（`severity = breaking`）；某条发现若在新审查中降级为兼容/废弃，会体现为"已消失"；
- **稳定身份** = 端点 + 请求/响应方向 + 规则（`rule_id`）+ 语义定位（`location`）——刻意忽略发现项 ID 与列表顺序，同一文档重新审查、序号变化不影响配对；
- **同身份重复发现按出现次数一一配对**（多重集差）：基线 2 条、当前 3 条同身份发现 → 2 对"延续" + 1 条"新增"；
- 每条结果**保留两侧原发现 ID**（`current_finding_id` / `baseline_finding_id`），便于回查定位。

**结果展示**：新增 🆕 / 延续 🔁 / 已消失 ✅ 三列及各自数量。新增与延续项可点击「↗ 当前 #N」跳回当前审查的对应发现（自动切到审查页、清筛选、展开并高亮该卡片）；延续与已消失项可点击「↗ 基线 #N」打开基线审查并定位。

**明确提示**：基线与当前审查相同（HTTP 400）、基线不存在或已删除（HTTP 404）、尚未打开当前审查时，页面都会给出可读提示，不会静默失败。

---

## 🛡️ 限时豁免

破坏性变更有时需要"临时放行"：客户端已在迁移、灰度期内的计划内破坏等。限时豁免允许对**破坏项**登记一条带**理由**与**未来 UTC 到期时间**的豁免记录，持久保存在 SQLite 中，可随时**撤销**。

**使用方式**：审查前在「API 标识」输入框填写标识（如 `petstore`）→ 执行审查 → 展开某条破坏项 → 点击「🛡️ 创建限时豁免…」→ 填写理由与 UTC 到期时间提交。豁免面板与发现卡片会显示匹配状态（🛡️ 生效中 / ⏰ 已过期 / 🚫 已撤销）、理由与到期时间；生效中的豁免可一键撤销。

**匹配规则**（服务端读取审查时实时重算，不使用发现序号）：

- 豁免按 **API 标识** 共享：同一标识下，规则（`rule_id`）、**方向**、**端点**、**语义位置**（`location`）及**旧值 / 新值**均相同的破坏项才会被覆盖——同一 API 的后续审查自动应用仍有效的豁免；
- 旧值 / 新值按**规范化 JSON** 比较：**对象键顺序忽略，数组顺序与值类型保留**；
- **同位置的变更值一旦变化**（如枚举从 `[a,b,c]` 收窄到 `[a,b]` 后又收窄到 `[a]`），旧豁免不再匹配，该破坏项须重新处理；
- **未填写 API 标识的审查不应用豁免**，也不能创建豁免。

**失效规则**：豁免状态在**每次读取审查时重新计算**——当前时间达到到期时间（`expires_at`，UTC）或已被撤销（`revoked_at` 非空）即失效，该破坏项重新计入未豁免。

**与原始结果的关系**：豁免只是读取时的覆盖层——**原始发现、统计卡片与历史对账保持原样**（对账仍基于全部破坏项）。结果页额外展示**已豁免 / 未豁免破坏项数量**与放行结论：**未豁免破坏项为零才可放行**。Markdown / JSON 报告同样包含豁免匹配状态、理由与到期时间（JSON 中见 `exemption_summary`、`exemptions` 及发现项上的 `exemption` 字段），可直接接入 CI 质量门禁。

---

## 📦 报告格式

- **Markdown**：结论、豁免结论（已豁免 / 未豁免破坏项与放行结论）、统计概览、按严重度排列的明细表（级别/分类/方向/端点/定位/原因/豁免状态）、豁免明细表，以及可折叠的 JSON Pointer 与旧值/新值附录。
- **JSON**：结构化结果，包含 `summary`（原始统计）、`exemption_summary`（已豁免 / 未豁免破坏项数量与 `releasable` 放行结论）、`exemptions`（豁免记录及状态）与全部 `findings`（含 `rule_id`、`severity`、`direction`、`location`、`old_json_path`、`new_json_path`、`old_value`、`new_value` 及匹配到的 `exemption`），适合接入 CI/CD 质量门禁。

---

## 📦 最小复现包（Minimal Repro）

针对**单条发现**导出一个可独立复现该差异的最小文档包，便于提交 issue、回归测试或在其他工具中复现。

**使用方式**：展开任意发现项 → 点击「📦 导出最小复现包」→ 下载 ZIP。新审查与「历史记录」中重新打开的旧审查均可导出。

**裁剪规则**：

- 旧、新两份文档各自只保留：文档头（`openapi` / `info`，服务器类发现含 `servers`）、该发现所属的 **path / operation**（操作级发现同时保留 path 级 `parameters`，因为引擎会将其合并进操作比较）；
- 从保留内容出发做**本地 `$ref` 闭包**：传递引用到的 `components`（schemas / parameters / requestBodies / pathItems 等）按原指针位置复制进最小文档，闭包用 visited 集合**去重**、对 A→B→A 这类**循环引用安全终止**；
- 未被引用的路径与组件一律剔除。

**导出前复核**：裁剪完成后，后端会用同一差异引擎对最小文档对重新比对，确认目标发现的 `rule_id`、方向、端点与语义定位仍然存在；**复核失败时返回可读错误（HTTP 409），不提供失真的包**。

**ZIP 内容**：

| 文件 | 说明 |
| --- | --- |
| `old.yaml` | 裁剪后的旧版最小文档（可直接重新上传审查） |
| `new.yaml` | 裁剪后的新版最小文档 |
| `manifest.json` | 原审查 ID / 标题 / 时间、发现指纹（语义身份字段的 SHA-256 短哈希）、发现详情（rule_id、方向、端点、定位、JSON Pointer 等）与复核结论 |

`manifest.json` 示例：

```json
{
  "format": "openapi-repro/1",
  "generated_at": "2026-09-18T08:00:00+00:00",
  "review": { "id": 7, "title": "Petstore 1.4.0 → 2.0.0", "..." : "..." },
  "finding": {
    "id": 12,
    "fingerprint": "3f8c1a2b9d0e4f56",
    "rule_id": "property-became-required",
    "direction": "request",
    "endpoint": "POST /pets",
    "location": "requestBody > application/json → 请求体.status"
  },
  "verification": { "status": "passed", "engine": "run_diff" },
  "files": ["old.yaml", "new.yaml", "manifest.json"]
}
```

---

## 🗂️ 项目结构

```
.
├── app/
│   ├── main.py        # FastAPI：审查 / 历史 / 引用树 / 根因聚合 / 破坏项对账 / 限时豁免 / 报告下载 / 健康检查接口
│   ├── parser.py      # JSON·YAML 载入与 OpenAPI 3.x 校验
│   ├── refs.py        # JSON Pointer、$ref 解析、循环安全的引用展开树
│   ├── diff.py        # 语义兼容性规则引擎（请求/响应双方向）
│   ├── groups.py      # 根因聚合：端点×方向分区、同源归并、祖先吸收的有向影响组
│   ├── reconcile.py   # 破坏项对账：稳定身份配对，输出新增 / 延续 / 已消失
│   ├── exemptions.py  # 限时豁免：规范化 JSON 匹配身份、UTC 到期/撤销状态、读取时叠加
│   ├── repro.py       # 最小复现包：文档裁剪、$ref 闭包、复核与 ZIP 打包
│   ├── reports.py     # Markdown / JSON 报告生成（含豁免匹配状态）
│   └── database.py    # SQLite 持久化（评审、原文、结果、豁免；启动时自动迁移）
├── static/
│   ├── index.html     # 单页界面
│   ├── styles.css
│   ├── app.js         # 原生 JS：筛选 / 聚合视图 / 展开树 / 历史 / 对账 / 豁免 / 下载
│   └── samples.js     # 四组内置示例（JSON + YAML）
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

## 🔌 HTTP API 速览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/api/reviews` | 提交 `{old_spec, new_spec, api_id?, ...}` 执行审查并持久化（`api_id` 可选，填写后应用限时豁免） |
| `GET` | `/api/reviews` | 历史审查列表 |
| `GET` | `/api/reviews/{id}` | 审查详情（含全部 findings；读取时实时叠加豁免匹配状态与 `exemption_summary`） |
| `DELETE` | `/api/reviews/{id}` | 删除审查（不影响该 API 标识下的豁免记录） |
| `POST` | `/api/reviews/{id}/exemptions` | 对破坏项创建限时豁免：`{finding_id, reason, expires_at}`（UTC、须晚于当前时间；无 API 标识 400、非破坏项 422） |
| `POST` | `/api/exemptions/{eid}/revoke` | 撤销豁免（立即失效，幂等；不存在 404） |
| `GET` | `/api/reviews/{id}/groups` | 根因聚合：有向影响组（稳定组标识 / 主因 / 受影响条目 / 可展开定位，含豁免状态） |
| `GET` | `/api/reviews/{id}/reconcile?baseline_id=N` | 破坏项对账：基线 vs 当前审查，输出新增 / 延续 / 已消失三类破坏项及数量（基线不存在 404、与当前相同 400；基于原始发现，不受豁免影响） |
| `GET` | `/api/reviews/{id}/pointers` | 引用树根节点分组（paths / components） |
| `GET` | `/api/reviews/{id}/tree?side=old|new&pointer=/...` | 引用展开树（含循环标记） |
| `GET` | `/api/reviews/{id}/report?format=markdown|json` | 下载报告（含豁免匹配状态、理由与到期时间） |
| `GET` | `/api/reviews/{id}/findings/{fid}/repro` | 导出单条发现的最小复现包（ZIP；复核失败返回 409 及可读原因） |
| `GET` | `/api/health` | 健康检查（Docker HEALTHCHECK 与编排探活使用） |

交互式文档：http://localhost:8000/docs （FastAPI 自动生成）。

## ⚠️ 范围说明

- 仅支持 OpenAPI **3.0 / 3.1**；Swagger 2.0 会被明确拒绝并提示。
- 仅解析**文档内** `$ref`；外部文件引用（`./other.yaml#/...`）会在树中标记为无法解析。
- 规则基于 OpenAPI 社区通行的破坏性变更分类（参考 OASdiff 的方向化判定），属于静态契约分析，不验证具体业务逻辑。
