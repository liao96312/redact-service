# BLOCKERS

记录无法在预期环境内解决的问题。规则：同一个错误超过 3 次仍未解决即记录在此，
不擅自更换方案或库。

## B1 LibreOffice headless 未安装（T0.3）

- 现象：`winget install TheDocumentFoundation.LibreOffice` 下载成功但 MSI 安装返回
  退出码 `1622`（installation failed，通常为权限/安装器限制）。
- 影响：`.doc` / `.xls` 旧格式无法自动转换为 `.docx` / `.xlsx`，对应文件在
  T2.6 dispatch 中会返回 `needs_review` 并附 warning，而不是静默通过。
- 代码处理：`handlers/dispatch.py` 启动时探测 `soffice`，缺失时优雅降级。
- 待办：在内网镜像中预装 LibreOffice（Dockerfile 已预留），或提供本机安装包。

## B2 HanLP 模型自动下载失败

- 现象：`hanlp.load(...)` 内置下载器报
  `Chunk downloaded size 197 mismatches with expected size ...`。
- 处理：改用 `Invoke-WebRequest` / `curl` 直接下载 zip 到
  `%APPDATA%\hanlp\...` 缓存目录后即可正常加载。已成功加载：
  - `ner/msra_ner_electra_small_20220215_205503.zip`
  - `tok/coarse_electra_small_20220616_012050.zip`
- 结论：非阻塞，记录下载方法供内网复现（见 README 的离线依赖章节）。
