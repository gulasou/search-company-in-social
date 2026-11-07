# Eye of Truth · 公司求职口碑

输入公司名，选择 B站或抖音，搜索待遇、加班、面试与入职体验相关视频，读取公开一级评论，展示原文与来源链接。

## 运行

需要 Windows、Python 3.11 或更新版本，以及已安装的 Microsoft Edge。首次在 PowerShell 中运行：

```powershell
cd D:\apt\eyeoftruth
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe app.py
```

浏览器访问 [本地页面](http://127.0.0.1:8765)，输入公司名、选择搜索平台后点击搜索。按 Ctrl+C 停止服务。更新代码后需重启服务并刷新页面。

以后直接运行 `.\.venv\Scripts\python.exe app.py`。Playwright 复用已安装的 Edge，无需执行浏览器下载命令。若下载依赖超时，可在 pip 安装命令后追加 `--index-url https://pypi.tuna.tsinghua.edu.cn/simple`。

## 数据范围

- 每个选中的话题搜索第一页、最多 5 个候选视频；综合检索会搜索四个话题，轮流取结果并按视频 ID 去重。
- 仅保留标题或简介包含输入公司名的视频；最多展示 8 个视频，默认 6 个。公司别名不会自动合并，请尝试常用简称并核对主体。
- 每个视频读取最多 10 条一级评论：B站读取热门评论，抖音读取官网页面默认返回的评论，不保证按热度排序。不采集视频语音、字幕、弹幕或楼中楼，不代表全平台或全体员工的意见。
- 评论可能在视频语境中指代公司，也可能与求职无关。结果作为线索保留原文，不自动生成好评率或公司结论。
- 没有历史数据库；数据保留在当前页面，可以导出 JSON。启动后不会自动执行爬取。
- 接口超时和部分评论获取失败会明确显示；遇到登录、验证或访问限制，停止本次后续请求，不自动重试。

## 扫码登录

1. 点击页面上的「登录 B站」，程序打开独立的 Edge 窗口，访问 `https://passport.bilibili.com/login`。
2. 用自己的 B站 App 扫码并确认。账号密码不经过本应用，登录过程在 B站官网完成。
3. 程序检测到新的登录 Cookie 后，调用 B站身份接口确认登录成功，再保存凭据并关闭该登录窗口。
4. 回到页面搜索公司即可，后续请求自动使用保存的登录态，无需重启或手动复制 Cookie。

登录等待最多 3 分钟，可点击「取消登录」；过期或失败后可重新登录。重启后显示「已保存本机登录态」，表示存在本机保存的会话，并不保证服务器仍认可该会话。如提示会话失效，请重新扫码。

凭据通过 Windows DPAPI 加密，保存在 `.local/bilibili-session.bin`，仅供当前 Windows 用户解密；`.local/` 已加入 Git 忽略规则。页面和日志不返回 Cookie。点击「清除本机登录态」可删除本应用保存的凭据，并取消尚未完成的登录；这不会注销官网或手机 App 中的账号。

浏览器窗口独立于你日常使用的 Edge，不读取日常浏览器的账号、历史记录或其他网站 Cookie。请从普通本机终端启动服务；受限沙箱或组织策略可能阻止浏览器启动。

## 抖音搜索

1. 在「搜索平台」选择「抖音」，输入公司名与话题，点击搜索。
2. 程序打开独立的 Edge 窗口，在抖音官网执行搜索；如官网提示登录，请在该窗口内自行完成登录，每次话题搜索最多等待 2 分钟。搜索过程中请保持窗口打开。
3. 程序读取官网本次实际返回的搜索结果与视频一级评论，结束后自动关闭窗口，在本页展示结果。可查看视频、评论来源并导出 JSON；抖音长视频 ID 与评论 ID 以字符串导出，避免数字精度丢失。

抖音窗口仅供当前采集任务使用，不保存登录态、不读取 B站登录态；下次搜索可能需要再次登录。当前不另设抖音登录管理入口。抖音未公开播放量时显示「播放量未公开」。评论链接附带评论 ID，官网是否定位到原评论取决于当时页面支持情况。

遇到验证页、访问限制、关闭窗口或未返回有效数据时停止后续请求并提示原因；不自动重试、不绕过验证。已经获取的结果会保留并标记评论不可用。抖音页面与内部接口变化可能导致搜索或评论不可用。

## 网络限制与兼容入口

匿名读取是否可用取决于 B站当时的访问策略，登录也不能保证解除 HTTP 412、验证码或频率限制。遇到限制时停止采集，请在官网确认账号状态后再试。

旧的手动 Cookie 入口仍保留，扫码保存的会话优先使用。仅在未保存扫码会话时读取环境变量：

```powershell
$credential = Get-Credential -UserName 'Bilibili' -Message '密码栏填写自己的 Cookie，用户名保持默认'
$env:BILIBILI_COOKIE = $credential.GetNetworkCredential().Password
.\.venv\Scripts\python.exe app.py
```

Cookie 是账号凭据，不要提交到 GitHub 或发给他人。服务只监听 `127.0.0.1`。清除登录态也会清除服务进程中的旧 Cookie 环境变量，但父 PowerShell 终端中的变量需执行 `Remove-Item Env:BILIBILI_COOKIE` 单独清除。

## 验证

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

测试使用构造数据，不向平台发出请求；加密读写测试会调用 Windows DPAPI，应在普通本机终端运行。真实联调需要本机可访问 B站，且实际可用性受平台接口、账号与访问限制影响。

抖音的浏览器流程与页面验证（使用已安装的 Edge 和构造平台响应，不访问抖音）：

```powershell
.\.venv\Scripts\python.exe test-results/verify-douyin-flow.py
```

2026-11-07：新增抖音平台参数校验、搜索/评论结果解析、长 ID 与来源链接、限制后停止及浏览器退出测试。Edge 构造响应验证覆盖搜索与评论页面跳转、响应读取、平台选择、JSON 导出、HTML 文本安全、桌面/手机布局与错误提示。真实匿名抖音访问进入验证页，尚未完成本人登录后的真实搜索与评论采集验证。

2026-10-17：27 项测试通过，包含加密保存/重启读取、登录校验、取消后禁止写入、清除会话、凭据域名隔离、搜索自动复用登录态及原有采集流程。真实 Edge 已验证官网登录页显示二维码，以及登录任务打开、等待、取消的生命周期；桌面/手机页面的登录状态展示、取消和清除交互使用构造数据验证。真实匿名接口曾返回数据，后续触发 HTTP 412；本人扫码后的完整采集仍需用户在本机完成确认后验证。

## 接口与参考

本项目读取 B站网站使用的搜索和评论接口，以及抖音官网浏览器实际返回的搜索和评论响应，不使用平台官方开放 API，也不保证接口长期稳定。公开原文不等于已核实事实。

- 搜索：`https://api.bilibili.com/x/web-interface/search/type`
- 评论：`https://api.bilibili.com/x/v2/reply`
- 抖音搜索页面：`https://www.douyin.com/search/关键词?type=video`；读取官网 `/aweme/v1/web/search/item/` 或 `/aweme/v1/web/general/search/single/` 的第一页响应。
- 抖音评论：打开 `https://www.douyin.com/video/视频ID`，读取官网 `/aweme/v1/web/comment/list/` 的第一页响应。
- 官方能力说明：[抖音关键词视频管理](https://open.douyin.com/platform/resource/docs/ability/search-management/video/)需要申请权限，本项目未接入；[Playwright 网络响应文档](https://playwright.dev/python/docs/network)介绍本项目使用的浏览器响应监听能力。
- 登录流程参考：[MediaCrawler 的 B站登录实现](https://github.com/NanmiCoder/MediaCrawler/blob/c03ab60aac595ccc0c1a3984c69608dc59025196/media_platform/bilibili/login.py)，参考其官网扫码、检查会话、复用 Cookie 的流程，本项目独立编写实现，未复制或引入其代码。
- 浏览器接口依据：[Playwright 的 Edge 支持](https://playwright.dev/python/docs/browsers#google-chrome--microsoft-edge)与[登录态文档](https://playwright.dev/python/docs/auth)。仅依赖 Playwright，不依赖 MediaCrawler。

如以后扩展快手等平台，应分别实现采集入口，并保持同样的结果结构与来源链接。
