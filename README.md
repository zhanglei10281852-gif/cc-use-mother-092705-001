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

## 湿地巡护排班

`app/patrol/` 提供湿地巡护排班服务，接口前缀为 `/api/patrol`：

- `POST /zones`、`POST /zones/{id}/windows`：登记生境与保护窗口（如鸟类繁殖期 `avoid`、游客密集时段 `prefer`）。
- `POST /rangers`、`POST /rangers/{id}/availability`：登记巡护人员技能与可用时段。
- `POST /observations`：登记风险观察（滩涂积水回避、游客密集重点覆盖），同一区域同一时段重复上报返回原记录。
- `POST /runs`：按批次提交巡护要求并生成日程。`batch_key` 幂等，重复提交同一批要求返回原日程，内容不同的同键批次返回 409。每条分派都带有可解释依据（时段选择原因、人员选择原因、保护窗口/风险观察/路线冲突/可用时段等校验结果）。
- `POST /disruptions`：登记暴雨、区域封闭或人员临时退出。系统只重排受影响且尚未开始的分派：原分派保留为“已改派”并记录改派原因，新分派关联扰动来源；已开始的巡护列入 `skipped_in_progress`，不会被静默改写。`idempotency_key` 保证同一扰动只改派一次。
- `POST /assignments/{id}/start|complete|cancel`：分派生命周期，状态迁移带乐观校验，已开始的巡护不能取消或改写。
- `GET /assignments/{id}`、`GET /runs/{id}`、`GET /runs/{id}/events`：查询每次分派的依据、当前状态与完整变更历史。

排班引擎按 15 分钟粒度扫描需求窗口，依次排除保护窗口、风险观察、生效中扰动、区域路线冲突与人员冲突的时段，在可行时段中优先覆盖重点窗口，再按负载均衡选择人员，全过程确定性可复现。

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
```

`check-db` 检查 SQLite 完整性和外键设置，`smoke` 在进程内调用健康接口并验证基础路由。项目不依赖外部数据库、消息队列或网络服务。
