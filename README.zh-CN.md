<div align="center">

# Jetriever

### Similarity ✗ &nbsp;·&nbsp; Utility ✓

[![代码](https://img.shields.io/badge/Code-Open_Source-2563eb?style=flat-square)](./app)
[![测试](https://img.shields.io/github/actions/workflow/status/imlrz/Jetriever/tests.yml?branch=main&style=flat-square&label=Tests)](https://github.com/imlrz/Jetriever/actions)
[![许可证](https://img.shields.io/badge/License-MIT-059669?style=flat-square)](./LICENSE)
[![技术报告](https://img.shields.io/badge/Technical_Report-Coming_Soon-7c3aed?style=flat-square)](#引用)

[English](./README.md) · [简体中文](./README.zh-CN.md)

</div>

Jetriever 是一个面向 Agent Memory 的检索服务。它不只比较文本相似度，
而是利用效用导向的精排模型判断每条历史交互是否有助于回答当前问题。
系统原样存储 `Add` 写入的记忆块，对指定 `user_id` 下的全部候选进行评分，
并返回分数最高的五条证据。Jetriever 不生成最终答案，也不扩展相邻消息。

本仓库包含完整的 Add/Search API、SQLite 持久化、全局并发控制、重试与
限流处理、隐私保护审计日志、Docker 部署文件以及接口契约测试。

## 方法

Jetriever 通过 OpenRouter Decisions API 调用 `~typesafe/jev-latest`，为每个
问题—记忆对生成 Noul 效用分数。一次 Search 会：

1. 只读取与请求中 `user_id` 完全一致的记忆块；
2. 在全局并发上限内为全部候选评分；
3. 要求所有候选均成功，绝不会把不完整排序作为有效 top-5 返回；
4. 按效用分数排序，返回至多 `min(top_k, 5)` 条原始证据。

更多方法边界与比赛适配见[方法卡](./docs/METHOD_CARD.md)。

## API

服务提供以下端点：

- `POST /v1/memories/add`
- `POST /v1/memories/search`
- `GET /health`

`/add` 和 `/search` 是兼容别名。Add 按 `request_id` 同步、幂等写入；只有
全部消息已经存储并可检索时才返回 HTTP 200。检索隔离严格使用完整的
`user_id`，`session_id` 仅作为来源元数据保存。

## 快速开始

需要 Docker Compose，以及能够访问 Decisions API 的 OpenRouter Key。

```bash
cp .env.example .env
# 在 .env 中填写 OPENROUTER_API_KEY，并设置私有 JETRIEVER_API_KEY。
docker compose up --build -d
curl http://127.0.0.1:8000/health
```

启动后可运行真实链路测试：

```bash
export JETRIEVER_API_KEY="与 .env 相同的本地服务 Key"
python scripts/smoke_test.py http://127.0.0.1:8000
```

请勿提交 `.env`、API Key、数据库、审计日志或评测数据。

## 配置

| 环境变量 | 默认值 | 用途 |
|---|---:|---|
| `OPENROUTER_API_KEY` | 必填 | 服务端模型供应商凭据 |
| `JETRIEVER_API_KEY` | Compose 外可选 | 保护 Add/Search 的访问凭据 |
| `JEV_MODEL` | `~typesafe/jev-latest` | Decisions 模型 |
| `JEV_CONCURRENCY` | `32` | 全局候选评分并发上限 |
| `JEV_TIMEOUT_SECONDS` | `20` | 单次供应商请求超时 |
| `JEV_MAX_RETRIES` | `5` | 每个候选的最大尝试次数 |
| `BACKEND_RETRY_AFTER_SECONDS` | `15` | 后端失败时返回的重试等待时间 |
| `JETRIEVER_RESULT_LIMIT` | `5` | 固定结果上限 |
| `RETENTION_DAYS` | `30` | 自动数据保留期限 |

临时的供应商或存储故障会映射为带 `Retry-After` 的 HTTP 429。供应商原生
429 会保留其重试等待时间。审计日志只记录事件元数据和哈希标识符，不会
记录记忆文本、问题、模型响应或凭据。

## 开发

项目支持 Python 3.9 及以上版本。我们的实验统一使用 `longmemeval` Conda
环境：

```bash
conda run -n longmemeval python -m pip install -r requirements-dev.txt
conda run -n longmemeval python -m ruff check app tests
conda run -n longmemeval python -m pytest
```

测试使用模拟的模型接口，覆盖同步可见性、幂等写入、用户隔离、鉴权、
原始分块粒度、选项传递、top-5 排序、重试、完整精排和日志脱敏。

## 前序工作

Jetriever 延续了我们在 Agent Memory 方向上的研究：

- **ReFind** — [论文](https://arxiv.org/abs/2608.12888) ·
  [代码](https://github.com/imlrz/ReFind)。ReFind 获得第一届 Agent Memory
  Leaderboard 学术文本赛道第二名，研究 Agent 如何主动搜索原始聊天记录。
- **InMind** — [论文](https://arxiv.org/abs/2607.24368) ·
  [代码](https://github.com/imlrz/InMind) ·
  [项目主页](https://keep-it-inmind.github.io/)。InMind 评测历史事实与当前问题
  之间依赖隐含世界知识联系的记忆场景。

## 引用

技术报告正在整理中。目前可以引用 [`CITATION.cff`](./CITATION.cff) 中的
软件条目。

## 许可证

Jetriever 使用 [MIT License](./LICENSE) 开源。
