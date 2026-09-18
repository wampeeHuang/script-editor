# Script Editor

本地文稿与音频工作台。项目自动保存在受管草稿箱，项目名称与稳定 ID 分离；只有导出时生成对外交付文件。当前阶段的能力与限制以 [录音阶段合同](docs/RECORDING-STAGE.md) 和 [存储合同](docs/STORAGE-CONTRACT.md) 为准，历史版本说明不代表当前验收。

## 启动与检查

```text
python service.py --no-browser --port 8781
python checks/workspace.py check
python checks/workspace.py map --check
python tests/test_project_deletion.py
python tests/test_workspace_checks.py
python tests/smoke.py
```

浏览器入口 http://127.0.0.1:8781/ 。Python 使用现有工作环境，不在整理过程中安装全局依赖。默认草稿箱为本目录 projects/，默认导出根为 exports/；设置页可以修改位置。源码清理不会清理草稿或导出。不要运行 `tests/smoke.py --live`，除非明确授权付费测试。

## 阅读入口

- [当前交接](HANDOFF.md)：当前真实状态、验证和限制。
- [产品需求](docs/PRD.md)、[界面设计](docs/UI-DESIGN.md)：分别服务产品与设计读者。
- [结构与生命周期](docs/WORKSPACE-STRUCTURE.md)：目录所有者、保留规则和恢复方法。
- [Agent 规则](AGENTS.md)：编码与写入边界。

## 文件机制

应用源码仍采用原生根布局：根目录 Python 模块及工作台 HTML 是代码槽；media/ 与 tokens/ 是正式资源，不为整理目录重写导入路径。projects/ 是用户数据与应用登记槽，narration/project.json 是单个项目内容的权威来源，library.json 是地址与列表状态登记，deleted-projects.json 是删除保护。它们不进入普通源码提交；被 Git 忽略不代表可删除，恢复快照必须覆盖它们。

运行依赖 `_runtime/cosyvoice-deps/` 仍被兼容启动器消费，不能当缓存清空。历史脚本和验证产物完整保存在 archive/，不是当前程序的一部分。目录地图由 checks/workspace.py 从磁盘生成，不手工维护。

<!-- DIRECTORY-MAP:START -->
```text
script-editor/
  .playwright-cli/ — 浏览器工具原生会话缓存
  .workbuddy/ — 外部 Agent 原生工作区记录；不擅自清理
  __pycache__/ — Python 可再生成缓存
  _runtime/ — 声明用途的依赖、日志和测试输出
  archive/ — 历史版本、操作脚本及永久验证证据
  checks/ — 确定性结构门禁、地图生成与清理器
  docs/ — 产品、设计、存储与结构合同
  media/ — 正式字体与静态资源
  projects/ — 应用管理的用户项目与机器登记；备份保护
  tests/ — 可复用验证代码；不写正式项目
  tokens/ — 正式设计 token
  .gitignore — 源码与本地数据的版本控制边界
  AGENTS.md — Agent 写入与验证规则
  align.py — 应用源码：音频对齐
  app_settings.py — 应用源码：存储设置
  cosyvoice_server.py — 应用源码：现有 CosyVoice 兼容启动器
  default-project.txt — 应用消费的本地默认入口
  exporter.py — 应用源码：交付导出
  HANDOFF.md — 当前交接入口
  material_import.py — 应用源码：资料导入
  model.py — 应用源码：项目模型
  project_library.py — 应用源码：本地登记与删除保护
  README.md — 人类使用入口及派生目录地图
  service.py — 应用源码：HTTP 服务入口
  tts.py — 应用源码：配音接口
  文案脚本工作台.html — 应用源码：当前前端
```
<!-- DIRECTORY-MAP:END -->
