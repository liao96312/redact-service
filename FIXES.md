# 修复记录（2026-10）

在真实语料（桌面「图纸」目录：7 个压缩包、3.4 GB、约 1100 个文件，含 PDF 图纸、
Excel 报价/包装单、微信截图、现场照片、CAD/DWG）上跑通时暴露并修复的问题。
每条都对应一个可复现的现象，并补了回归测试（`tests/test_realworld.py`）。

## 一、会直接让服务跑不起来的

### 1. 上传即 `PermissionError`（Windows）
`paths.quarantine_dir: /data/quarantine` 在 Windows 上被解析成 `D:\data\quarantine`，
普通账户无写权限，接口 500、pytest 4 条用例全红。
→ 新增 `core/config.py: data_path()`：绝对路径优先，不可写自动回落到项目内 `.data/`；
并提供 `REDACT_DATA_HOME` 整体重定向。

### 2. `tempfile.mkdtemp()` 造出的目录**拒绝写入**
受限 ACL 环境下 `mkdtemp` 生成的目录带拒绝写 ACL，导致每张内嵌图片都
`PermissionError`，一份 xlsx 里 31 张图全被记为「处理失败」并整体 `needs_review`。
→ 新增 `core/workdir.py`：用 `mkdir()` + 随机名，带可写探针与多级回落；
全项目替换 `mkdtemp` / `TemporaryDirectory`。

### 3. 处理超时杀不掉
原实现用 `ThreadPoolExecutor` + `future.result(timeout)`，超时只是放弃等待，
线程还在跑，进程退出时挂住。
→ 改为**常驻工作进程池**：模型只加载一次，超时真正 `terminate()`；
进程池异常时自动降级为同进程执行（`Report.degraded` 记录，不污染状态）。

### 4. 工作进程每个文件都重新加载模型
`mp.current_process()._redact_ready` 在 spawn 模式下不生效，实测每份文件多花 5~7 秒。
→ 改用模块级 `_WORKER_READY`，第二个文件起恢复 1 秒内。

## 二、会导致漏脱敏的

### 5. PDF 文案抽取与坐标对不上
旧实现拿 `page.get_text()` 的字符偏移去 `page.search_for(fragment)` 逐段搜索；
双栏/页眉页脚/旋转文字的偏移与坐标并非一一对应，实测整页漏检。
→ 改用 `get_text("dict")` 拿每个 span 的**真实 bbox**，在 span 级检测并直接对
bbox 打 `add_redact_annot`，不再二次搜索。

### 6. 无文字层的矢量图纸被漏掉
33 页的安装图既没有位图也不是文字（CAD 导出），`_is_scanned()` 判 False，
于是既不 OCR 也不打码，同时"文字页兜底 OCR"逐页全page渲染 → 单文件 300 秒超时。
→ 新增 `_is_textless()`：无文字即走 OCR；并给 OCR 加**时间预算 + 页数上限 + 单页像素上限**。

### 7. 复核把占位符也一起抹掉了
`_neutralize()` 用标签表做替换，`[姓名]` 里的「姓名」被命中，占位符变成 `[　]`，
既丢了「已脱敏」的证据，也让残留判定失效。
→ 先保护占位符（哨兵替换）再做标签中和。

### 8. `.xls` 全部进 needs_review
LibreOffice 装不上（B1），Office 未激活导致 COM 全部拒绝调用（B3），内网无 xlrd。
→ 自写 `handlers/xls_reader.py`（OLE2/CFB + BIFF8，零依赖纯 Python），
把 SST/LABELSST/NUMBER/RK/MULRK/FORMULA 读出来转 xlsx；文字完整保留。
另兼容**HTML 伪装的 .xls**（ERP/扫描件导出，文件头是 `<html>`）。

### 9. `.docx` 里的内容控件与修订文本漏检
`./w:r/w:t | ./w:hyperlink/w:r/w:t` 只取直属 run，`w:sdt`（内容控件）、
`w:smartTag`、表格里的 run 都漏；`w:del` 里的旧文字也没清理。
→ 改为遍历段落内全部 `w:t`（显式排除 `w:del` 子树），并补图表缓存、custom.xml。

### 10. `.pptx` 直接被拒
图纸包里有 4 个 3D 方案 pptx（最大 100 MB），原来 `return rep.fail("暂不支持 pptx")`。
→ 新增 `handlers/pptx_h.py`：幻灯片/备注/图表文本 + media 图片 + 文档属性。

## 三、会大面积误杀的（最影响可用性）

### 11. NER 在图纸文本上几乎是噪声
HanLP 把尺寸数字、`PDF`/`TEL`/`DTC` 这类缩写、`下午2:49`、`Lvjing Rd` 判成
人名/公司/地址，直接采信会把图面涂成一片黑。
→ `core/detect.py:_ner_acceptable()` 按类型做结构约束：
PERSON 不含数字、ORG 不含数字且排除≤5 位全大写、ADDRESS 必须含行政区划/街道后缀、
英文地名片段不算人名；短于 12 字的文本直接不跑 NER（既无收益又慢）。

### 12. NER 字符偏移整体错位
旧实现用「token 长度累加」推算偏移，HanLP 分词会吃掉空格与标点，一遇到就整体错位
（把「职位」「姓名」判成实体）。→ 改为在原文中按 token 顺序查找定位。

### 13. xlsx 表头规则把整列涂成 `[姓名]`
「客户名称 / 件数 / 规格型号 / 加工方式」都是 2~6 个汉字，被「2~6 汉字 = 人名」
整列替换。→ `_looks_like()` 加业务词表（60+ 词）、公司后缀、地址构件三重闸门；
表头单元格长度上限 20 字、含路径的不算表头。

### 14. xlsx 公式被当成公司名
`=G20*F20` 里的 `G20*F20` 被 NER 判成 ORG，一份报价单产生 205 条告警。
→ 公式只校验**字符串常量**，单元格引用与函数名先剥离。

### 15. xlsx 大表遍历 620 万空单元格
`max_column=16384` 是常见现象（整行设过格式），`iter_rows()` 直接 300 秒超时。
→ 用 openpyxl 的稀疏 `_cells` 字典遍历；工作表 XML > 8 MB 时切「快速路径」
（逐格只跑规则/词典/上下文 + 整表批量 NER）。

### 16. 逐格 NER 让单文件 380 秒
→ 新增 `ner_spans_batch()`：一批单元格一次 forward，实测快 4 倍。

### 17. 电话正则跨行吞尺寸
`\s` 把竖排的 `400 / 400 / 2320` 连成 400 电话。→ 分隔符限定为空格/横线，
并补澳洲 `04xx`、`+区号 (xxx) xxx-xxxx` 形态。

### 18. 跨行合并把两个字段拼成一个
`户名：李四\n电话：138…` 被拼成 `户名：李四电话：138…`，人名变成「李四电话」。
→ 合并前检查换行右侧是否为字段标签、左侧是否为句末标点。

### 19. 手写 OCR 落框按字符比例切 → 涂偏
中英混排时按字符数均分必偏，截图里手机号只涂掉前几位。
→ 使用 rapidocr 的 `return_word_box=True` 逐字框，精确到字。

### 20. 低置信告警把整批现场照打成 needs_review
质检照片里满是 `①~各`、`kgNo` 这类低分碎片。→ 改为**低置信行占比 ≥60%**
才升级复核，且只统计「有实际含义」的行。

### 21. Office 锁文件 `~$xxx.xlsx` 记为失败
→ 直接 `skipped`（只含用户名与路径，无正文）。

## 四、其它

- `/healthz` 现在返回 NER / OCR / 旧格式转换后端的真实状态（原来只回 `{"status":"ok"}`）。
- 上传文件名做路径净化，避免 `../` 穿越。
- docx/xlsx/pptx 重新打包时保留原 `ZipInfo`（压缩方式、目录权限位）。
- 新增 `tools/batch_redact.py`：递归目录 + 嵌套 zip/rar/7z，源目录只读，
  增量 `progress.jsonl` 支持 `--resume` 断点续跑，输出 `REPORT.md` / `report.json`。
- 新增 `tools/audit_output.py`：不依赖服务自身报告，重新抽文比对残留 + 结构校验。
