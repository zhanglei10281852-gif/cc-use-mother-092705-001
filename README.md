# 城市生态运营服务

这是一个面向城市湿地保护团队的 Python 后端服务。项目提供本地 HTTP 接口、SQLite 持久化、身份与角色管理、审计记录、任务编排和可扩展的生态数据处理边界，便于在单机环境中保存运营状态并复核业务决定。

## 运行环境

- Python 3.11 或更高版本
- SQLite 3（使用 Python 标准库）

## 安装

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
```

默认数据文件位于 `data/compute-operations.db`，可以复制 `.env.example` 后调整本地路径。

## 初始化与启动

```bash
python -m app.cli init-db
uvicorn app.main:app --host 0.0.0.0 --port 8432
```

健康接口为 `GET /api/system/health`。所有状态变化都写入 SQLite，并由应用内事务保证关联记录的一致性。

## 测试

```bash
python -m pytest
```

测试覆盖参数校验、身份权限、事务边界、任务状态、失败恢复、审计写入和现有生态计算接口。

## 编译检查

```bash
python -m compileall -q app tests
```

## 本地验收

```bash
python -m app.cli check-db
python -m app.cli smoke
python -m app.cli patrol-demo
```

`check-db` 检查 SQLite 完整性和外键设置，`smoke` 在进程内调用健康接口并验证基础路由，`patrol-demo` 通过接口提交一批巡护样例数据并打印生成分派的依据。项目不依赖外部数据库、消息队列或网络服务。

## 湿地巡护排班

`/api/patrol` 提供湿地巡护排班服务，覆盖登记、排班、改派与复核的完整闭环：

- **登记**：`POST /zones` 登记生境（含路线分组与巡护时长），`POST /protection-windows` 登记保护窗口（鸟类繁殖期、滩涂积水、游客密集时段，可声明所需技能、最小巡护间隔与优先级加权），`POST /rangers` 与 `POST /rangers/{code}/availability` 登记人员技能与可用时段，`POST /risk-observations` 登记风险观察。
- **排班**：`POST /schedules` 按批次提交巡护要求，调度器按保护窗口、路线冲突（同路线分组不同时段）、同区域最小巡护间隔、人员技能与可用时段生成日程，每条分派都附带中文依据（`rationale`）。同一 `requested_by + batch_key` 重复提交返回原日程（HTTP 200，`replayed=true`），不会生成第二份；批次键对应不同要求时返回 409。
- **改派**：`POST /disruptions` 上报暴雨（`heavy_rain`）、区域封闭（`zone_closed`）或人员临时退出（`ranger_unavailable`）。系统只重排受影响时段内尚未开始的分派：原分派保留为 `rescheduled` 并记录改派原因，替代分派关联 `origin_assignment_id`；已开始或已完成的巡护列入 `skipped`，绝不静默改写。扰动上报按 `actor + disruption_key` 幂等。
- **生命周期**：`POST /assignments/{id}/start|complete|cancel|reassign` 驱动状态流转，均支持 `expected_version` 乐观锁；已开始的巡护不可改派或取消（409）。
- **复核**：`GET /schedules/{id}` 查看日程与全部分派，`GET /assignments/{id}` 查看单条分派的依据、当前状态与变更历史（`events`），`GET /assignments` 按状态、区域、人员、日期过滤，`GET /summary` 查看汇总。
