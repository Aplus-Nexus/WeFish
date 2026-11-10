# WeFish / 上班墨鱼验收清单

## 已在当前工作区验证

| 要求 | 当前实现 | 证据 |
| --- | --- | --- |
| macOS 菜单栏逐行显示 | `Tray.setTitle()` 接收当前行；默认 1 秒换行，可调 0.5–10 秒 | `desktop/main.js`、`desktop/renderer/app.js`、Electron self-test 检查 tray line IPC |
| Windows 任务栏托盘 | `Tray`、托盘右键菜单、tooltip 逐行文本；快捷键使用 `CommandOrControl` | `desktop/main.js`、`desktop/package.js`、Windows Actions workflow |
| 小悬浮窗/隐蔽阅读 | 390×270 置顶小窗、88% 透明；普通阅读舱默认隐藏；工作文档标题模式 | `desktop/main.js`、Electron mock self-test |
| 快捷键和托盘控制 | 显示/隐藏、分页、翻章、逐行、暂停、同步、标题和小窗切换 | `desktop/main.js`、`desktop/renderer/app.js` |
| 一章分页且阅读区无滚动 | 章节拆为 page model，阅读区 `overflow: hidden` | `desktop/renderer/pagination.js`、self-test 断言 `overflow=hidden` |
| 预加载后续内容 | 当前页后两页缓存，并预取下一章首屏 | self-test 输出 `pages=24 preloaded=2` |
| 快速调整位置 | 章节选择器、位置滑杆、页内行位置恢复 | self-test 覆盖远端偏移恢复和 `jumpToProgress(50)` |
| 书架、正文、摘录、书友想法、我的想法 | Gateway 与 Web Reader 分离；Cookie-only 正文/书架/进度回退 | `wefish_core.py`、`wefish_service.py`、53 项 Python 回归 |
| 阅读数据接口 | renderer 使用 GET `/api/stats`，错误返回结构化 capability | `wefish_service.py`、self-test 确认没有“未知接口” |
| 云端进度和阅读时长 | GET `/api/reader/progress` 拉取；Reader 签名上报章节偏移、进度、摘要、时长；退出 flush 和断网队列 | `report_read()`、renderer 待同步队列、mock round-trip 回归 |
| 会员/权益 | 读取 Web Reader `reader.memberCardSummary`，目录显示 `paid/price`；空摘要不猜测 | `membership_summary()`、会员回归 |
| 跨平台分发 | macOS/Windows 分别用宿主机 PyInstaller + electron-builder 构建 | `.github/workflows/build.yml`、`desktop/package.js` |
| 上游许可和来源 | AGPL-3.0、NOTICE、桌面“关于 / 许可证”入口随包分发 | `LICENSE`、`NOTICE`、`desktop/package.json` |

## 可重复验证

```bash
QT_QPA_PLATFORM=offscreen python3 -m unittest discover -s tests -v
cd desktop
npm run test:pagination
npm run smoke:mock
npm run package:mac
npm run smoke:package
unzip -t 'release/上班墨鱼-mac.zip'
```

当前工作区的最近一次结果为 53 项 Python 测试通过、分页测试通过、Electron mock/self-test 通过、macOS ARM64 封装 smoke 通过，ZIP 无损坏。

## 仍需要外部环境的验收

1. 用真实微信读书账号完成一次扫码或配置 `WEREAD_API_KEY`/`WEREAD_COOKIE`，验证真实书架、章节正文、进度、阅读时长、统计和会员摘要。
2. 在 Windows runner 上执行 `python -m PyInstaller --noconfirm --clean WeFishService.spec`、`npm run package:win:zip` 和 `npm run smoke:package`，确认 Windows 侧车和任务栏托盘运行。
3. macOS 分发前使用开发者证书签名并进行 Gatekeeper/辅助功能权限验收。
4. 桌面点按验收需要解锁当前 macOS 会话；锁屏状态只能完成代码级和封装级 smoke。

这些是运行环境验证门槛，不是 mock 或源码测试可以替代的结果。
