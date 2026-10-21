# BLOCKERS

记录无法在预期环境内解决的问题。规则：同一个错误超过 3 次仍未解决即记录在此，
不擅自更换方案或库。

---

## 已解决的阻塞（保留记录，供内网复现）

### B1 LibreOffice 未安装（T0.3）— 已绕过

- 现象：`winget install TheDocumentFoundation.LibreOffice` 下载成功但 MSI 安装返回
  退出码 `1622`（installation failed，通常为权限/安装器限制）。
- 影响面：`.doc` / `.xls` 旧格式。
- 解决：
  - `.xls` **完全解决**：新增 `handlers/xls_reader.py`，自带 OLE2(CFB) + BIFF8
    解析器（零依赖、纯 Python），把单元格文字完整读出来再走脱敏管线。
    实测能读出 `单号 / 制单员 / 客户名 / 地址 / 电话` 等字段。
  - `.doc` **仍受限**：见下方 B3。
- 代码处理：`handlers/convert.py` 的多后端自动降级
  （`soffice` → `native_xls` → `Office/WPS COM`），缺失时如实降级为
  `needs_review` 并在报告里点名，不静默通过。

### B2 HanLP 模型自动下载失败 — 已绕过

- 现象：`hanlp.load(...)` 内置下载器报
  `Chunk downloaded size 197 mismatches with expected size ...`。
- 处理：改用 `Invoke-WebRequest` / `curl` 直接下载 zip 到
  `%APPDATA%\hanlp\...` 缓存目录后即可正常加载。已成功加载：
  - `ner/msra_ner_electra_small_20220215_205503.zip`
  - `tok/coarse_electra_small_20220616_012050.zip`
- 结论：非阻塞，记录下载方法供内网复现（见 README 的离线依赖章节）。

---

## 当前未解决

### B3 Microsoft Office 未激活，COM 自动化全部拒绝调用

- 现象：目标机装了 Office 16（`C:\Program Files\Microsoft Office\root\Office16`），
  但启动时弹「Excel(产品激活失败)」；任何 COM 调用都返回
  `(-2147418111, '被呼叫方拒绝接收呼叫。')`，包括最基础的 `Workbooks.Add()`。
- 已验证：不是路径/权限/沙箱问题（同一脚本在放开沙箱后仍失败）；WPS 未安装，
  `KET.Application` / `KWPS.Application` 均未注册；机器无网络，无法安装 xlrd/xlwt。
- 影响：`.doc`（图纸包里 2 个合同文件）无法自动转 docx；`.xls` 已由 B1 的方案解决。
- 处理：`handlers/convert.py` 对 `.doc` 明确降级为 `needs_review` 并在报告里
  写明「缺少 LibreOffice，且 Office/WPS COM 不可用（目标机 Office 未激活）」。
- 待办（任选其一）：
  1. 在内网镜像里预装 LibreOffice（Dockerfile 已预留）；
  2. 激活 Office 或安装 WPS 后重跑批处理；
  3. 把 `.doc` 单独用 Word 另存为 `.docx` 后重跑。

### B4 四份「散单」压缩包本身是残缺文件（非本服务问题）

- 现象：以下文件是**打包中断产生的半成品**（无 ZIP 目录结尾 `PK\x05\x06`），
  任何工具都打不开：
  - `2022.8.23-文莱 Darren’s house.zip`（686 B）
  - `2023.06.17 Kulkarni Residence-1套全屋定制.zip`（661 B）
  - `SN-哥斯达黎加David 全屋【阿里4号】.zip`（695 B）
  - `Alt-印尼-Meity.zip`（24 KB，末尾被截断）
- 处理：工具会在 `REPORT.md` 的「错误 / 异常」里逐条列出，不静默跳过。
- 待办：从原始来源重新导出这四个包。

### B5 DWG / BAK / 数据库等二进制件无解析器

- 现象：图纸包里 122 个 `.dwg` + 50 个 `.bak`（合计 ~1.7 GB）是 AutoCAD 二进制格式，
  标题栏里通常直接写着客户名、项目名、电话。
- 影响：这部分**没有做脱敏**，只在报告里点名数量。
- 待办：内网如有 AutoCAD，可加一个「导出 DWG 文字层」的后端；否则这部分需人工确认。
