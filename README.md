# 环境投诉资料基础服务

登记投诉区域、企业排放时段、值班人员和气象快照来源，为投诉研判提供规范化基础数据；在此之上提供夜间异味投诉关联模块：合并重复来电、按带版本规则生成候选企业与贡献因素、记录带依据的人工研判、冻结事实快照，并支持从匿名化案件完整追溯。

系统采用 Python 标准库和 SQLite，可在单个 Linux 进程中运行。现有能力包括操作者与角色登记、场所台账、领域资料记录、请求幂等校验、哈希串联审计以及轻量 HTTP/JSON 接口。领域资料类别为：complaint_zone、operation_window、duty_roster、weather_source。所有写操作都在事务中完成，同一请求编号携带相同内容时返回原结果，内容发生变化时返回业务冲突。

## 投诉关联模块

业务边界（自动结果只是研判建议，不直接作为执法结论）：

1. **重复来电合并与联系信息保护**：按区域、时间窗（默认 6 小时）和文本 shingle 指纹（Jaccard 阈值）合并；联系姓名/电话存于独立受限表，案件与追溯视图只返回匿名编码 `CASE-xxxx` 和脱敏信息，原文仅 admin 可取，每次查看写入审计。
2. **带版本的关联规则**：规则以 `rule_id + version` 存储，带内容哈希，同时至多一个 `active` 版本（部分唯一索引保证）。引擎是纯函数，对同一规则版本与冻结快照输出确定，候选含 0..1 评分、贡献因素（时间重叠、文本匹配、空间邻近、风向输送、生产运行、治污离线）和随证据缺口展宽的置信区间，并给出现场核查建议。
3. **人工决定必须留证**：值班员可排除、追加或确认候选，但必须填写 4-500 字书面依据；每次决定冻结当时使用的事实快照（事件指纹、候选状态、规则版本、生成快照与引擎结果哈希链）。
4. **已确认案件不可变**：确认时冻结结论快照；之后的补录资料、规则更新、重新生成均被拒绝；高相似的晚到来电只留痕、不并入冻结案件。
5. **撤销确认保留原结论**：仅 admin 可撤销，案件进入 `revoked` 终态，原结论、快照哈希与撤销依据都保留在追溯链中。
6. **并发只允许一个当前版本**：进程内事务锁 + `BEGIN IMMEDIATE` + 部分唯一索引，并以 `expected_revision` 乐观锁检测并发编辑。

## 主要接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/correlation-rules` | 登记规则新版本（admin），自动退役旧生效版本 |
| GET | `/correlation-rules?rule_id=` | 查询规则版本与参数哈希 |
| POST | `/complaint-events` | 登记来电/投诉，自动去重合并并生成关联版本 |
| POST | `/correlation-versions/regenerate` | 用当前事实重新生成版本（已确认案件拒绝） |
| POST | `/candidate-decisions` | 排除/追加/确认候选，需 `reason` 与 `expected_revision` |
| POST | `/case-confirmations` | 确认案件结论并冻结，声明是否需要现场核查 |
| POST | `/case-confirmations/revoke` | admin 撤销确认，原结论留存 |
| GET | `/cases` | 匿名化案件列表（可按 zone_id、status 过滤） |
| GET | `/cases/{id}` | 匿名化案件视图（无联系信息原文） |
| GET | `/cases/{id}/trace` | 追溯：输入事件、合并依据、规则版本、候选/因素、人工决定与冻结快照、确认结论、现场核查标志 |
| GET | `/cases/{id}/contacts` | 联系信息（值班员看脱敏，admin 看原文），访问入审计 |

## 目录

- `src/complaint_core/`：领域模型、SQLite 存储、权限服务、审计链、关联引擎、研判服务、HTTP 路由和离线验收；
- `tests/`：引擎规则、去重合并、快照冻结、人工研判、确认/撤销、并发约束、接口路由和端到端验收测试。

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

命令会在临时 SQLite 数据库中登记操作者、场所、领域资料、规则版本、两起重复来电、生产/气象快照，完成候选确认与案件确认，核对幂等回执、审计链与脱敏结果，成功时输出一行 `status` 为 `ok` 的 JSON 并以退出码 `0` 结束。

## HTTP 服务

```bash
PYTHONPATH=src python3 -m complaint_core.api --database complaint_core.sqlite3 --host 127.0.0.1 --port 8080
```

健康检查使用 `GET /health`。业务写入接口通过 `X-Actor-Id` 标识操作者，支持操作者、场所、领域资料、投诉事件、规则版本、候选决定和案件确认的登记，以及案件追溯、联系信息和审计事件查询。服务重启后，SQLite 中的业务状态、审计链和冻结结论继续保留。
