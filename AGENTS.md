# Script Editor · 开发与运行治理

本目录是自主开发的本地多模态文稿应用，采用 L3 运行治理。当前代码槽为根目录 Python 模块、`文案脚本工作台.html`、`media/` 和 `tokens/`；本次用户授权在原址整改，不改变 D:/tools 的第三方工具准入规则。跨目录搬家是独立批次，须遵守目标工作区规则并保存恢复点。

## 写入边界

- 人入口 README.md；跨会话状态 HANDOFF.md；产品、设计与存储合同在 docs/。
- 可复用验证代码进 tests/；目录门禁与地图生成器进 checks/。
- _runtime/ 只承载已声明的依赖、日志、浏览器记录与测试输出，不承载新补丁脚本或唯一永久证据。
- archive/ 保存历史操作和版本，不作为启动入口，不批量执行历史脚本。
- projects/ 是受管用户数据槽，不是测试夹具。项目 ID、narration/project.json 内容和相对素材引用必须保留；不得因整理目录移动项目内部资料。
- settings.json、default-project.txt 和 projects 中的登记文件由应用消费，不手工生成第二份同义注册表。无需给草稿项目新增 .project。
- 新一级入口先运行 workspace-governor preflight。不得在根目录添加截图、补丁、调研报告或一次性测试文件。
- 缓存只通过 checks/clean_runtime.py 显式清理；其默认行为为预览，不处理用户项目、依赖或历史证据。
- 不自动提交已有改动，不全仓回滚，不运行付费测试、全局安装或外部发布。

## 验证

```text
python service.py --no-browser --port 8781
python checks/workspace.py check
python checks/workspace.py map --check
python tests/test_project_deletion.py
python tests/test_workspace_checks.py
python tests/smoke.py
```

纯目录调整也须确认浏览器项目列表、回收站和按 ID 打开正常。`smoke.py --live` 会调用付费服务，不在默认验证范围内。

README 的目录地图由 checks/workspace.py 从磁盘生成；不在这里手写第二份树。历史审计与全部移动映射见 archive/structure-20260918/，其内容是证据，不是当前配置。
