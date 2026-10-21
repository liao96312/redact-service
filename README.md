<div align="center">
  <img src="assets/banner.svg" alt="redact-service banner" width="860"/>
</div>

# redact-service

[![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![Celery](https://img.shields.io/badge/Celery-Redis-37814A?logo=celery&logoColor=white)](https://docs.celeryq.dev/)
[![Docker](https://img.shields.io/badge/Docker-compose-2496ED?logo=docker&logoColor=white)](docker-compose.yml)
[![tests](https://img.shields.io/badge/tests-70%20passed-brightgreen)](tests/)
[![recall](https://img.shields.io/badge/recall-100%25-success)](reports/eval.md)
[![云依赖](https://img.shields.io/badge/cloud-none%20%C2%B7%20offline-critical)](#)
[![平台](https://img.shields.io/badge/OS-Windows%20%7C%20Linux%20%7C%20Docker-informational)](#)
[![License](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

纯内网、不上云的上传敏感信息自动打码服务。中英文；支持 Word / Excel / PDF / 图片。
设计为独立 Python 服务，主项目通过 HTTP 调用。

## 架构

![架构图](assets/architecture.svg)

## 目录

```
redact-service/
  config.yaml          # 全部配置（路径/阈值/占位符）
  requirements.txt
  BLOCKERS.md
  core/       span.py config.py detect.py rules.py ctx.py dict_match.py ner.py workdir.py
  handlers/   base.py ocr.py media.py docx_h.py xlsx_h.py pdf_h.py image_h.py pptx_h.py
              dispatch.py verify.py convert.py xls_reader.py
  api/        main.py tasks.py storage.py audit.py
  tools/      gen_testdata.py evaluate.py integration_client.py batch_redact.py
  tests/      test_detect.py test_api.py
  dicts/      customers.txt        # 客户词典（主项目导出）
  testdata/   reports/             # 测试数据与评测报告
  Dockerfile  docker-compose.yml
```

## 快速开始（开发，单机同步模式）

```bash
pip install -r requirements.txt
python tools/gen_testdata.py          # 生成 100 份测试文件与 labels
python tools/evaluate.py              # 评测，输出 reports/eval.md
python -m pytest tests/ -q            # 单元 + 接口测试
uvicorn api.main:app --port 8080      # 启动服务
```

`config.yaml` 中 `service.sync_mode: true` 为进程内同步处理；生产用 Celery 时设为
`false`（或环境变量 `REDACT_SYNC_MODE=false`）。

**Windows 本地跑**：`paths.*` 里的 `/data/...` 会自动回落到项目内 `.data/`；
也可用环境变量整体重定向：

```powershell
$env:REDACT_DATA_HOME = "D:\redact-data"
$env:REDACT_WORK_DIR  = "D:\redact-work"
```

## 批量处理目录 / 压缩包（推荐入口）

一次性处理一个目录（含嵌套 zip/rar/7z）：源目录**只读**，产物按原相对路径写到输出目录。

```bash
python tools/batch_redact.py "C:\Users\x\Desktop\图纸" -o "D:\redacted"
# 只处理子目录、限量、续跑
python tools/batch_redact.py 图纸 -o D:\redacted --only 散单 --max-files 50
python tools/batch_redact.py 图纸 -o D:\redacted --resume
```

输出目录包含：
- 各压缩包展开后的 `*_redacted/` 目录（保持包内路径，便于原样替换）；
- `REPORT.md`：状态分布、按类型命中统计、**未处理类型清单**、错误与待复核文件；
- `report.json`：机器可读版本（便于二次统计）；
- `progress.jsonl`：逐文件增量日志，配合 `--resume` 断点续跑。

报告只记录「类型 + 数量 + 状态」，**不写任何明文敏感内容**。

### 配套工具

```bash
# 不信任服务自身报告，重新抽文比对残留 + 结构校验
python tools/audit_output.py 源目录 输出目录 -n 60

# 修正输出目录里的扩展名（.xls/.et 已转成 xlsx 内容时）
python tools/fix_extensions.py 输出目录 --apply
```

`FIXES.md` 记录了本项目在真实图纸语料上修掉的 21 类问题（误杀、漏检、
Windows 兼容、性能超时），每条都有对应回归测试。

## 接口（T3.1）

- `POST /v1/redact`  表单字段 `file` -> `{task_id, status}`
- `GET  /v1/tasks/{id}` -> `{status, kind, counts, warnings}`
  状态：`queued | processing | done | needs_review | failed | skipped`
- `GET  /v1/tasks/{id}/result`  仅 `done` 返回脱敏文件（其余返回 409）
- `GET  /v1/reviews`  待人工处理（needs_review / failed）列表
- `GET  /healthz`  健康检查，含 NER / OCR / 旧格式转换后端状态

curl 示例：

```bash
TID=$(curl -s -F file=@sample.docx http://127.0.0.1:8080/v1/redact | jq -r .task_id)
curl -s http://127.0.0.1:8080/v1/tasks/$TID
curl -s -o clean.docx http://127.0.0.1:8080/v1/tasks/$TID/result
```

## 主项目接入（T3.5）

上传入口改为：上传 -> `/v1/redact` -> 轮询任务 -> 仅 `done` 的文件进入业务流程。
`needs_review` 进待人工处理列表，`failed` 提示重传。参考 `tools/integration_client.py`。

## 离线依赖（T0.2）

1. 联网机器：
   ```bash
   pip download -r requirements.txt -d wheels/
   # 轻量视觉模型 rapidocr 的 ONNX 模型随包分发；
   # HanLP MSRA NER + 分词模型、spaCy en_core_web_sm 见下
   ```
2. HanLP 模型（内置下载器在部分网络下会报 size mismatch，直接下载 zip 即可）：
   - `https://file.hankcs.com/hanlp/ner/msra_ner_electra_small_20220215_205503.zip`
     -> `%APPDATA%\\hanlp\\ner\\`（Linux：`~/.hanlp/ner/`）
   - `https://file.hankcs.com/hanlp/tok/coarse_electra_small_20220616_012050.zip`
     -> `%APPDATA%\\hanlp\\tok\\`（Linux：`~/.hanlp/tok/`）
3. spaCy 模型：`en_core_web_sm-3.x-py3-none-any.whl` 放入 `wheels/` 后离线安装。
4. 内网安装：`pip install --no-index --find-links wheels/ -r requirements.txt`
5. 设置 `HANLP_HOME`（可选），`REDACT_CONFIG` 指向配置。

## 部署（T5）

```bash
echo "REDACT_KEY=$(python -c "from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())")" > .env
docker compose up -d --build
```

- `api`：FastAPI；`worker`：Celery（并发=CPU 一半，内存上限 4g 可调）；`redis`：队列。
- 单文件上限 50MB；CPU 可跑，GPU 非必须。
- 密钥仅来自环境变量 `REDACT_KEY`，不进入代码库。
- 原件隔离在 `/data/quarantine`（Fernet 加密），脱敏结果在 `/data/clean`。
- 日志轮转由 compose `logging` 配置（10MB × 5）。

## 模型 / 词典热更新（T5.3）

- 词典：编辑 `dicts/customers.txt`（`词<TAB>类型`，类型 PERSON/ORG/ADDRESS），
  服务会按文件修改时间自动热加载（`dict_match` 监测 mtime），无需重启。
- 模型：拷入新模型文件到挂载目录 -> 重启 `api`/`worker` 容器 -> 重跑
  `python tools/evaluate.py` 确认指标不下降。

## 设计要点

- 检测顺序与优先级：词典(0.95) > 规则(0.9) > 上下文(0.85) > NER(0.6)；
  重叠时保留更长 Span。
- **NER 结果必须过结构过滤**：图纸/聊天截图里 NER 会把尺寸数字、`PDF`/`TEL`
  这类全大写缩写、时间戳判成实体，直接采信会大面积误涂。`core/detect.py`
  按类型做约束（PERSON 不含数字、ORG 不含数字且排除短缩写、ADDRESS 必须含
  行政区划/街道后缀、短于 12 字的文本不跑 NER）。
- 复核（T2.7）只用高置信来源（规则/词典/上下文）；NER 在"标签:占位符"文本上
  噪声大，不计入残留判定（详见 `handlers/verify.py` 注释）。
- 真删除：PDF 用 `apply_redactions`（按 span 的真实 bbox 落框，不做二次搜索）；
  图片命中片段涂实心黑（优先用 OCR 逐字框，中英混排不会涂偏）；docx/xlsx/pptx
  直接改文本节点。
- 大文件保护：单份 PDF 的 OCR 有时间预算、单页有像素上限、扫描页有页数上限；
  大 xlsx 自动切「快速路径」；处理跑在常驻工作进程池里，超时可真正杀死。
- 拒绝：含宏文档、加密文件、压缩包、>50MB、>200 页；损坏文件返回 failed；
  缩略图缓存等无正文文件返回 skipped（不算失败）。

## 旧格式与 Office 依赖

`handlers/convert.py` 按以下顺序自动选择后端：

| 目标 | 后端顺序 | 说明 |
|---|---|---|
| `.xls` / `.et` | soffice → **内置 BIFF 读取器** → Office/WPS COM | 内置读取器零依赖，纯 Python 解析 OLE2+BIFF8 |
| `.doc` | soffice → Office/WPS COM | 需要 LibreOffice 或已激活的 Office |
| HTML 伪装 `.xls` | 内置 HTML 表格解析 | ERP/扫描件常把 HTML 存成 .xls |

内置 BIFF 读取器只保留文字（格式/图表会丢），但文字正是敏感信息所在；
转换后会写入报告说明"格式已简化"。

## 模型 / 词典热更新（T5.3）

- 词典：编辑 `dicts/customers.txt`（`词<TAB>类型`，类型 PERSON/ORG/ADDRESS），
  服务会按文件修改时间自动热加载（`dict_match` 监测 mtime），无需重启。
- 模型：拷入新模型文件到挂载目录 -> 重启 `api`/`worker` 容器 -> 重跑
  `python tools/evaluate.py` 确认指标不下降。

## 可选：本地小模型复核（T6，未启用）

阶段 4 已在合成集达标（各类型召回 100%，verify 通过率 100%），故未引入本地 LLM。
如需在真实样本上进一步降误报/漏报，可按清单用 vLLM/llama.cpp 部署 Qwen 小模型，
仅对"NER 低分命中"和"verify 疑似遗漏"片段二判，提示词要求只输出 JSON 敏感片段列表，
原文不得离开内网。

## 许可证

本项目以 [MIT License](LICENSE) 发布，版权归 2026 liao96312 所有。
第三方依赖及其许可见 [NOTICE](NOTICE)。

