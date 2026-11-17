# 运行手册 metricgraph

## 1 启动

```bash
docker compose up -d --build
curl -s localhost:18215/api/health
```

宿主端口 `18215`，容器内 `8000`。启动时如果库是空的，服务会自动把 `sample_data/` 里的样例跑一遍，页面打开即有数据。

## 2 截图与验收命令

```bash
docker compose config --quiet
docker compose exec -T app python -m pytest -q
```

## 3 主流程

1. 打开 `http://localhost:18215/`，看规格单的字段清单与采信诊断。
2. `/types` 比较两个类型写法，看读者 / 写者两向结论。
3. `/mapping` 看旧名到新名、别名 SQL、冲突项。
4. `/impact` 看受影响节点、环、断链、单边登记。
5. `/batches` 点「跑一次」，再点「陈旧提交」验证 409。
6. `/merge` 点「按共同基线合并」，对未裁决项整组选边后发布新批次。
7. `/spec` 导出迁移单与三份账目 CSV。

## 4 重建数据库

```bash
docker compose down -v
docker compose up -d --build
```

命名卷 `metricgraph-data` 挂着 `/data/metricgraph.sqlite`。删卷即清空批次、快照与隔离区。
## 5 接口

| 方法 | 路径 | 作用 |
|---|---|---|
| GET | `/api/health` | 服务与库状态、当前批次号 |
| POST | `/api/schemas/import` | 采信样例文件，返回快照摘要与诊断 |
| GET | `/api/schemas/quarantine` | 隔离区 JSON |
| GET | `/api/schemas/quarantine.csv` | 隔离区 CSV |
| GET | `/api/types` | 类型格与别名表 |
| POST | `/api/types/compare` | 两个类型写法比较 |
| POST | `/api/compat` | 一对版本的两向判定 |
| GET | `/api/mapping` | 映射表、别名、冲突 |
| GET | `/api/impact` | 影响面、环、断链 |
| POST | `/api/runs` | 跑批次，可带基线版本号与生效区间 |
| GET | `/api/batches` | 批次历史 |
| POST | `/api/merges` | 按共同基线建合并会话（三份文件） |
| GET | `/api/merges` | 合并会话清单 |
| GET | `/api/merges/{merge_id}` | 会话详情与冲突组 |
| GET | `/api/merges/{merge_id}/preview` | 套用已存裁决的合成预览 |
| POST | `/api/merges/{merge_id}/selections` | 提交整组选边，可带 `expected_revision` |
| POST | `/api/merges/{merge_id}/publish` | 发布新批次，过期基线即 409 |
| POST | `/api/merges/{merge_id}/recover` | 写入失败后恢复，不重复生成批次 |
| GET | `/api/reconcile` | 四账对账 |
| GET | `/api/export/spec.md` | 迁移单 Markdown |
| GET | `/api/export/fields.csv` | 字段账 CSV |
| GET | `/api/export/refs.csv` | 引用账 CSV |
| GET | `/api/export/ledger.csv` | 账目 CSV |
| GET | `/api/export/manifest.json` | 运行清单 |

## 6 失败处置

| 现象 | 原因 | 处置 |
|---|---|---|
| 409 `VERSION_STALE` | 提交带的基线序号落后 | 去掉 `base_version_seq` 或填当前序号 |
| 400 `WINDOW_INVERTED` | 生效区间起点不早于终点 | 检查 `effective_from` / `effective_to` |
| 400 `BAD_INPUT` | 文件名、参数或 JSON 结构不对 | 看返回的 `detail` |
| 404 `NOT_FOUND` | 还没跑过批次 | 先在 `/batches` 点「跑一次」 |
| 409 `MERGE_BLOCKED` | 合并还有未裁决项、非法裁决或合成后重名 | 逐组整条选边；重名先删一条再发布 |
| 409 `MERGE_STALE` | 分支内容已变或会话被别的写入推进 | 重新建会话，或刷新后按当前版本重试 |
| 409 `MERGE_WRITE_FAILED` | 发布写库失败，`pending` 标记保留 | 点「失败后恢复」或 POST `/recover`，同内容不会重复生成批次 |

## 7 端口与并发

本服务固定 18215。同时起多个栈时改 `docker-compose.yml` 的端口映射与 `metricgraph-data` 卷名，避免与本机其他项目抢端口、串数据。
