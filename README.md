# 环境投诉资料基础服务

登记投诉区域、企业排放时段、值班人员和气象快照来源，为投诉研判提供规范化基础数据；并提供一套夜间异味投诉关联模块。

系统采用 Python 标准库和 SQLite，可在单个 Linux 进程中运行。现有能力包括操作者与角色登记、场所台账、领域资料记录、请求幂等校验、哈希串联审计以及轻量 HTTP/JSON 接口。领域资料类别为：complaint_zone、operation_window、duty_roster、weather_source、treatment_status、weather_snapshot（后两类分别表示治污设施状态和区域气象快照，气象快照可不挂靠具体场所）。所有写操作都在事务中完成，同一请求编号携带相同内容时返回原结果，内容发生变化时返回业务冲突。

## 投诉关联模块

处理夜间异味投诉时，系统把居民描述、附近企业的生产/治污状态与平台已接收的气象快照关联起来，但**自动判断永远只是候选，不直接形成执法结论**。

流程与不变量：

1. **合并去重与隐私保护**：来电按区域、时间窗和文本指纹（归一化字符 n-gram 重叠系数）判定是否重复，重复来电并入同一匿名化案件。联系方式只保存不可逆令牌（同人识别）和掩码（展示），案件与溯源接口不返回明文。
2. **带版本的关联规则**：规则规格发布后冻结为 `rule_version`。研判按距离、下风向、生产时段/治污异常、文本命中、气象证据给出候选企业、贡献因素、评分与 90% 置信区间，并标记是否需要现场核查。规则更新只影响之后生成的版本，历史版本始终按其冻结规则与事实复现。
3. **人工研判并冻结事实**：值班员可排除、追加或确认候选，**每次人工决定都必须填写依据（reason）**，并冻结当时使用的事实快照（输入事件、企业、资料、气象）与规则版本。每个案件版本带 `state_hash`，决定记录串联 `from/to state_hash`。
4. **确认冻结**：案件确认后结论（候选、确认对象、规则版本、事实快照）整体冻结。之后补录数据或发布新规则都不能改变已确认案件；若事实自当前版本后已变化，确认会被拒绝并要求先生成新版本。
5. **撤销需更高权限**：撤销确认者的角色等级必须严格高于原确认者（operator &lt; reviewer &lt; admin）。撤销后原结论以 `revoked` 状态保留，不删除、不改写。
6. **并发安全**：所有写事务在进程内串行化并以 SQLite `BEGIN IMMEDIATE` 加写锁；部分唯一索引保证同一案件任意时刻只有一个当前版本、只有一条生效结论。
7. **完整溯源**：`GET /cases/{id}/lineage` 可从匿名化案件追到输入事件、各规则版本、冻结事实快照、全部人工判断、结论历史和现场核查建议；`GET /snapshots/{id}` 可还原当时冻结的事实内容。

### 投诉关联接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/rule-versions` | 发布一版关联规则（admin；body 可省略 spec 冻结内置规格） |
| GET | `/rule-versions` | 列出规则版本 |
| POST | `/complaints` | 登记来电，自动判定合并或建新案件 |
| GET | `/cases` | 列出案件，可按 `?status=open\|confirmed` 过滤 |
| POST | `/cases/{id}/versions` | 用当前生效规则与事实生成下一版候选研判 |
| POST | `/cases/{id}/decisions` | 排除/追加候选（`decision=exclude\|add`，必填 `reason`） |
| POST | `/cases/{id}/confirm` | 确认并冻结结论（operator/reviewer/admin） |
| POST | `/cases/{id}/revoke` | 撤销确认（角色等级须高于原确认者） |
| GET | `/cases/{id}/lineage` | 案件完整溯源（匿名化） |
| GET | `/cases/{id}/snapshots` | 列出案件冻结事实快照 |
| GET | `/snapshots/{id}` | 读取某个冻结事实快照 |

## 目录

- `src/complaint_core/`：领域模型、SQLite 存储、权限服务、关联规则引擎、案件服务、审计链、HTTP 路由和离线验收；
- `tests/`：核心规则、事务边界、接口路由和端到端验收测试。

## 测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## 构建检查

```bash
python3 -m compileall -q src tests
```

## 离线验收

```bash
PYTHONPATH=src python3 -m complaint_core.acceptance
```

命令会在临时 SQLite 数据库中登记操作者、场所、生产/治污资料和气象快照，发布关联规则，录入两通重复来电并合并，生成候选研判、排除候选、确认冻结、尝试补录改写（应被阻止）、按权限撤销，最后核对幂等回执与审计链，成功时输出一行 `status` 为 `ok` 的 JSON 并以退出码 `0` 结束。

## HTTP 服务

```bash
PYTHONPATH=src python3 -m complaint_core.api --database complaint_core.sqlite3 --host 127.0.0.1 --port 8080
```

健康检查使用 `GET /health`。业务写入接口通过 `X-Actor-Id` 标识操作者，支持操作者、场所和领域资料的登记，规则发布、投诉录入、案件研判与溯源，以及审计事件查询。服务重启后，SQLite 中的业务状态和审计链继续保留。

