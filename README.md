# Smart Memo

A Windows desktop prototype that turns a Chinese note into an editable reminder plan with DeepSeek, then saves approved events to iCloud Calendar through CalDAV. It supports multiple actions, bounded recurrence, single-occurrence titles, and related preparation dates. Once events and alarms have synchronized to an iPhone, Apple Calendar handles the reminders.

**中文智能备忘录原型：写下一段话 → 逐项核对计划 → 同步到 iCloud 日历 → iPhone 日历提醒。**

项目使用 Python 3.12、Tkinter 和标准库。正常运行无需安装额外 Python 包。当前版本适合个人试用和继续改造，尚不是经过完整验证的生产应用。

## 已有功能

- 一次描述多个独立事项，生成可编辑的计划，而不是只保留第一件事。
- 区分事件/截止时间与首次提醒时间，例如“15点开例会，提前30分钟提醒”。
- 识别每天、每周、工作日、每月重复；有限系列按确认范围展开为独立事件。
- 为有限系列中的某一次替换标题，例如把其中一次“例会”改为“主持例会”，不另加一条重复会议。
- 从指定场次推导准备事项的日期；缺少具体钟点时要求补充。
- 可选择有限次数的额外跟进，并在预览中查看实际提醒时间。
- 本地保存事项、同步队列、成功收据和失败状态；失败或结果不确定时可重试。
- 通过 CalDAV 写入专用 iCloud 日历，使用事件内的 `VALARM` 提醒。
- DeepSeek API 密钥和 Apple App 专用密码分别使用 Windows 当前用户的 DPAPI 加密保存。

模型只负责提出计划。计划通过本地检查后，还需要你在界面确认，才会加入日历同步队列。

## 快速开始

### 1. 准备 Windows 和 Python

安装包含 Tkinter 的 [Python 3.12](https://www.python.org/downloads/windows/)，下载或克隆本仓库，并把项目放在当前 Windows 用户可写的目录。

在项目目录运行：

```powershell
py -3.12 desktop_demo.py
```

也可以使用已经指向 Python 3.12 的命令：

```powershell
python desktop_demo.py
```

Windows 用户可以双击仓库中的 `start.vbs`。如果启动器没有打开窗口，请使用上面的命令查看错误信息。

### 2. 配置 iCloud 日历

1. 在 iPhone 的“设置 → 你的名字 → iCloud → iCloud 日历”中开启“在此 iPhone 上使用”。详见 [Apple 官方设置说明](https://support.apple.com/zh-cn/guide/icloud/mme4d73a8727/icloud)。
2. 在 **iCloud 账户**中创建一个私人日历，例如“智能备忘录”。不要把它建在“我的 iPhone”本地账户下，也不要选择与别人共享的日历。
3. Apple 账户开启双重认证后，前往 [Apple 账户页面](https://account.apple.com/)，在“登录与安全 → App 专用密码”中生成一个专用密码。请阅读 [Apple 官方专用密码说明](https://support.apple.com/zh-cn/102654)，不要使用 Apple 账户主密码。
4. 在程序里点击“连接 iCloud 日历”，填写账户邮箱和专用密码，读取日历，并明确选择刚建的专用日历。密码只填写到程序，不要提交到 GitHub issue。
5. iPhone 开启“设置 → 通知 → 日历 → 允许通知”。在“日历 App → 日历 → 专用日历右侧的简介按钮”中开启“日程提醒”。两层开关都需要检查。参见 [日历通知设置](https://support.apple.com/zh-cn/guide/iphone/iphdafdf98a1/ios)与[单个日历的日程提醒](https://support.apple.com/zh-cn/guide/iphone/iph3d1110d4/ios)。

“iCloud 已保存”只表示程序确认了服务器上的事件内容，不能证明 iPhone 已同步或通知已显示。首次使用时，先创建测试日程，在手机日历里确认事件和提醒，再关闭电脑验证。

### 3. 配置 DeepSeek

在 [DeepSeek 官方平台](https://platform.deepseek.com/)获取 API 密钥，在程序的“配置 DeepSeek”窗口填写。不要把密钥放进源码、命令示例或 issue。

默认模型为 `deepseek-flash`。保存配置本身不会调用模型；每次点击“AI 识别”会发起一次 API 请求，可能产生费用。程序不会自动重复付费请求，错误后由你决定是否重新识别。

请求发送当前事项、当前上海时间和你填写的提醒偏好，不自动发送整个待办列表，也不会把 Apple 密码发给 DeepSeek。详见 [DeepSeek JSON Output 文档](https://api-docs.deepseek.com/guides/json_mode/)。

### 4. 录入并核对

1. 输入一段事项描述，点击“AI 识别”。当前桌面界面的录入入口需要先配置 DeepSeek。
2. 补充缺少的日期、钟点或有限重复范围。
3. 在预览中逐项核对标题、事件时间、首次提醒、次数、例外标题及关联日期；按需要修改和选择。
4. 确认保存后，查看每条事项的同步状态。批次内可能只有部分事项已保存，不能只看第一条成功就关闭程序。

确认后的计划会先一次性写入本地队列；**iCloud 端按事件逐条提交，没有跨事件的批量原子事务**。失败状态会保留，重新打开程序或重试时仍需要核对结果。

## 一个复合事项示例

下面是虚构的功能示例，基准日期为 2026-10-13。实际使用时请填写未来日期：

> 从2026年10月19日开始，每周一下午3点开例会，连续10次。12月14日这次由我主持例会。那次例会前两天提醒我跟负责人领取资料。

预期计划应保留三个意思：

| 内容 | 应有行为 |
| --- | --- |
| 每周例会，共10次 | 从10月19日开始，最后一次为12月21日，不生成无限重复 |
| 12月14日主持例会 | 替换那次的标题；会议总数仍为10，不再增加一场同时间会议 |
| 提前两天领取资料 | 日期为12月12日，但领取资料的钟点缺失，需要你补充；不擅自沿用例会时间 |

模型可能漏项或理解错误，预览是必要步骤。有限系列展开后，“完成这条”针对当前那次事项；整批取消则需要检查本批的所有事项是否已在云端取消。

若修改后的新计划替代一条旧的无限重复计划，应先在程序中完成旧事项并确认云端取消，再录入新计划，避免重复提醒。程序不会按标题自动扫描、修改其他真实日历事件。

## 当前限制

| 项目 | 当前范围 |
| --- | --- |
| 平台 | Windows，Python 3.12；凭证加密依赖 Windows DPAPI |
| AI 单次方案 | 最多12个逻辑事项；一个重复系列计为一个逻辑事项 |
| 有限系列 | 最多100次；`count`包含首场，`until`包含结束范围；两者不能同时使用 |
| 结束范围 | 有限系列的结束时间最多为首场之后一个日历年 |
| 展开后的批次 | 最多200条主事项；跟进资源另计 |
| 单次标题例外 | 仅有限系列，模型方案最多10个例外；无限系列的单次例外需要先确认结束范围 |
| 额外跟进 | 仅一次事项，最多3次，每次在事件后7天内；可避开配置的静默时段 |
| 每月重复 | 固定日号；无该日期的月份会跳过，不等同于“每月月底” |
| 时间 | 按上海时间（UTC+8）理解和预览；手机日历可能按其显示时区展示 |

额外跟进是你确认后**预先安排**的提醒。程序没有常驻服务器，不能在电脑关闭时判断你是否已经完成事项，也不会自动检测邮件、位置、审批或其他外部条件。完成后请在电脑程序中标记并同步取消后续提醒。

电脑需要联网才能创建、改期或取消云端事件。手机离线时不能同步这些新变化；手机关机、通知关闭、专注模式或同步延迟都可能影响实际提醒。已保存到 iCloud 不代表已在手机验证。

当前桌面界面尚未提供对已保存事项直接改期或“稍后10分钟”的入口。需要调整已保存的计划时，请先核对原计划的取消结果，再安排新计划。

## 本地数据与隐私

源码运行时，数据保存在项目目录；打包版本的数据保存在可执行文件所在目录。请使用可写的私人目录。主要包括：

- `memo-data.json`：事项、同步队列和收据，可能包含完整的私人描述。
- `memo-data.before-icloud.json`：旧数据备份（若已生成）。
- `icloud-settings.json`：账户标识和专用日历配置，不是匿名信息。
- `icloud-credentials.dpapi.json`：加密的 Apple 专用密码。
- `deepseek-credentials.dpapi.json`：加密的 DeepSeek 密钥及提醒偏好。
- 其他本地配置、锁文件和临时文件。

这些文件不属于公开源码，**即使密码文件已经加密，也不要上传**。DPAPI通常绑定当前 Windows 用户和电脑；换电脑或用户后可能需要重新填写密钥。它保护静态文件，不是同一用户下所有程序都无法访问的保险箱。

确认上传的事件标题、描述、时间和提醒会交给 Apple iCloud。AI识别时当前输入与提醒偏好会交给 DeepSeek。请根据内容敏感程度决定是否使用对应服务。

提交问题时，请使用虚构输入，清除 API 密钥、Apple 密码、邮箱、日历地址、UID、真实事项正文和截图中的私人内容。不要上传本地数据目录、加密凭证文件或完整日志。更详细的协作说明见 [CONTRIBUTING.md](CONTRIBUTING.md)。

## 开发与验证状态

仓库包含用于时间解析、AI方案验证、有限系列展开、CalDAV同步队列等逻辑的测试文件。**本轮开源准备仅做静态源码审查，没有实际运行测试、打开应用，或调用 DeepSeek/iCloud验证。测试文件存在不代表当前版本已经通过测试。**

开发者可在独立的开发副本中自行运行：

```powershell
py -3.12 -m unittest discover -p "test_*.py" -v
```

真实账户验证由使用者在私人专用日历中进行。不要把公开 CI 配置成会创建或删除真实账户的日程。

### 可选：打包 Windows 程序

打包依赖与运行依赖分开，使用 `requirements-build.txt` 中的 PyInstaller：

```powershell
py -3.12 -m pip install -r requirements-build.txt
py -3.12 -m PyInstaller memo.spec
```

打包结果和 `build/`、`dist/` 不应与私人运行数据一起提交。可执行文件也需要实际验证后才能作为已测试的发布版本描述。

## 许可证

项目采用 [MIT License](LICENSE)。欢迎阅读、修改和贡献；请保留许可证要求的声明。
