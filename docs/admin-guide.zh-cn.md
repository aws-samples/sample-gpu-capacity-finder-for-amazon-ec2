# GPU Capacity Finder 管理员指南

> 本文档是[英文版](admin-guide.md)的直译，不含额外内容。如有出入，以英文版为准。

如果您在自己的 AWS 账户中部署和运行 GPU Capacity Finder，本指南就是为您准备的。该页面帮助您的用户在 Amazon Elastic Compute Cloud（Amazon EC2）上查找图形处理器（GPU）容量。本指南涵盖部署、设置、用户、监控、数据、安全和拆除。使用该页面的人员另有一份[用户指南](user-guide.zh-cn.md)。[README](../README.zh-cn.md) 是架构、带有默认值和范围的设置表，以及限制的参考。

该页面是一个决策辅助工具：它读取容量信号，并执行一次有边界的测试启动。它不管理实例，不创建 Capacity Reservation，也不购买 Capacity Block；用户在 EC2 控制台中购买 Capacity Block，而页面向用户显示可复制的 AWS Command Line Interface（AWS CLI）命令，用于创建 Capacity Reservation（通过 **按需与 Spot** 标签页上的 **使用 CLI 预留**），由用户使用自己的凭证执行。

## 创建的资源

[架构图](../architecture-diagram.png)有自己的步骤编号。本指南对 DynamoDB 表使用以下名称（括号中是堆栈中的逻辑 ID）：探测历史表（`Probes`）、缓存表（`OfferingCache`）和提醒表（`Subscriptions`）。

| # | 资源 | 说明 |
|---|---|---|
| 1 | Amazon CloudFront 分配和一个私有的 Amazon Simple Storage Service（Amazon S3）存储桶 | 通过源访问控制提供静态页面。存储桶阻止公有访问。 |
| 2 | Amazon Cognito 用户池、应用客户端和登录域 | 自助注册已关闭，必须使用基于时间的一次性密码（TOTP）进行多重身份验证（MFA）。域前缀来自 `stackName`。 |
| 3 | 带有 JSON Web Token（JWT）授权方的 Amazon API Gateway 超文本传输协议（HTTP）API | 拒绝没有有效 ID 令牌的请求。阶段限流为 `apiRateLimit`（默认每秒 10 个请求），突发额度为该值的三倍，由各用户共用。 |
| 4 | AWS Lambda API 函数及其 AWS Identity and Access Management（IAM）角色 | 读取信号和定价，读取缓存的 Spot 分数，执行测试启动，并管理提醒。1024 MB 内存，25 秒超时。 |
| 5 | 按 Amazon EventBridge 计划（每小时的第 0、15、30 和 45 分钟）运行的 Spot 分数更新器 Lambda 函数 | 建立 Spot 分数组并使分数保持最新。它是唯一创建或更改 Spot 分数请求的代码。256 MB，5 分钟超时。请参阅 [Spot 分数如何保持最新](#spot-分数如何保持最新)。 |
| 6 | DynamoDB 探测历史表 | 测试启动记录在 30 天后过期。该表还保存冷却锁、每用户每日计数器和全部署每日计数器。 |
| 7 | 按 15 分钟计划运行的清理 Lambda 函数，以及带有自定义资源的堆栈删除拆除函数 | 仅在 `enableTestLaunch=true` 时存在。清理函数会在每个已启用的区域中终止存在超过 10 分钟的测试实例，并删除存在超过 1 小时的测试启动模板。拆除函数在您删除堆栈时运行一次。 |
| 8 | DynamoDB 缓存表 | 共享的缓存条目：各区域提供的内容、Spot 分数组和分数、Spot 和按需价格、配额，以及区域列表。请参阅[备份与数据](#备份与数据)。 |
| 9 | DynamoDB 提醒表、加密的 Amazon Simple Notification Service（Amazon SNS）主题、通知器 Lambda 函数及其计划 | 仅在 `enableNotifications=true`（默认值）时存在。 |
| 10 | 各函数的 CloudWatch 告警 | 每个函数的错误、接近超时的运行和限流，以及测试启动安全网的两个告警。设置 `alarmTopicArn` 后，它们会通知该主题，否则没有操作。请参阅[告警](#告警)。 |

API、更新器、清理、通知器和拆除函数各有一个保留期为一个月的日志组。堆栈还包含 AWS Cloud Development Kit（AWS CDK）辅助函数，用于将页面复制到存储桶，并在删除时清空存储桶。

探测安全组（测试启动使用过的每个虚拟私有云（VPC）一个）在运行时创建。拆除函数会在您删除堆栈时移除它们；请参阅[拆除](#拆除)。用户使用复制的命令创建的 Capacity Reservation，以及用户在 EC2 控制台中购买的 Capacity Block，属于用户，而不属于堆栈。

## 前提条件

- 一个 AWS 账户，以及可以部署该堆栈的凭证。请使用非生产账户。
- Python 3.12 或更高版本，用于 CDK 应用和本地测试（Lambda 函数运行在 Python 3.13 上）；Node.js 20 或更高版本（已在 22 上测试）；AWS CLI 第 2 版；以及最新的 AWS CDK CLI：`npm install -g aws-cdk`。
- 除非命令另有说明，请在示例根目录（包含 `deploy.sh` 的文件夹）中运行本指南中的命令。
- CDK 命令（`cdk bootstrap`、`cdk diff`、`cdk deploy`、`cdk destroy`）会运行 Python CDK 应用，因此需要本示例的 Python 环境。`deploy.sh` 会在 `.venv` 中创建该环境，但只在脚本内部激活它。在运行其他 CDK 命令之前，请在您的 shell 中运行 `source .venv/bin/activate`。
- 账户已在您部署所在的区域完成 CDK bootstrap。首次部署之前，该环境尚不存在：`python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt && cdk bootstrap`。
- 对于测试启动：在用户进行测试的每个区域中，需要一个带有默认子网的默认 VPC。测试启动会使用每个可用区的默认子网。没有默认 VPC 的区域会显示 `此区域没有默认 VPC`，其测试启动保持关闭。要创建默认 VPC，请运行 `aws ec2 create-default-vpc --region <Region>`；该调用会在该区域中创建 VPC、子网和互联网网关，因此请只在您接受这一点的区域中运行。
- 对于 Spot 测试启动：需要 Spot 访问权限和 Spot 服务相关角色。如果账户从未使用过 Spot，请创建一次该角色：

  ```bash
  aws iam create-service-linked-role --aws-service-name spot.amazonaws.com
  ```

  如果您的账户无法使用 Spot，请使用 `-c testLaunchAllowSpot=false` 部署。

## 部署

### 使用部署脚本

```bash
export AWS_PROFILE=<profile>   # or sign in another way
export AWS_REGION=<region>
./deploy.sh
```

该脚本会检查 `python3`、`aws` 和 `cdk`，在 `.venv` 中创建 Python 环境，并显示 CDK CLI 版本、账户、身份和区域。它会请您确认，然后运行 `cdk deploy`。在脚本名称之后传入选项：

```bash
./deploy.sh -c testLaunchAllowSpot=false -c excludeRegions=me-south-1
```

设置 `ASSUME_YES=1` 可跳过脚本的确认提示。`cdk` 命令仍会要求您批准 IAM 变更；在脚本名称之后添加 `--require-approval never` 可一并跳过该提示。部署完成后，脚本会输出网站 URL、用户池 ID，以及创建第一位用户的命令模板。如果 `cdk deploy` 失败，且其输出显示 CLI 与 CDK 库不兼容，脚本会输出一条提示，建议您使用 `npm install -g aws-cdk@latest` 升级 CDK CLI。

### 使用 cdk deploy

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cdk deploy -c excludeRegions=me-south-1   # options are optional
```

堆栈部署到您的 CDK 环境的默认账户和默认区域，并输出 `SiteUrl` 和 `UserPoolId`。要在之后读取它们（本指南中的命令使用默认堆栈名称 `gpu-capacity-finder`；如果您设置了 `stackName`，请替换它）：

```bash
aws cloudformation describe-stacks --region <StackRegion> --stack-name gpu-capacity-finder \
  --query "Stacks[0].Outputs" --output table
```

第一批 Spot 分数组会在部署之后的第一个页面请求时建立，而不是在下一次每日更新时。页面会短暂显示正在建立的状态；请在一分钟后重新加载。

### 每个账户一个部署

每个 AWS 账户只部署一份本示例。探测资源使用固定的标签名称，而清理函数和拆除函数会扫描账户中已启用的区域，而不只是堆栈所在的区域。在同一账户中的第二个部署（即使位于另一个区域）会清理掉第一个部署的测试资源。AWS 对 Spot 放置分数请求的限制也由整个账户共用，因此两个部署会占用同一份预算。如果有多个账户，请在每个账户各部署一份。

## 设置

使用 `-c key=value` 传入每项设置。[README 的设置一节](../README.zh-cn.md#设置)列出了每个键及其默认值和范围。堆栈不会在两次部署之间记住这些值，因此请在每次部署时传入相同的选项，或将它们放在 `cdk.json` 的 `context` 块中。

| 键 | 何时修改 |
|---|---|
| `stackName` | 您想使用默认值以外的堆栈名称。向 `cdk deploy` 和 `cdk destroy` 传入相同的 `-c stackName=<name>`。每个 AWS 账户只部署一份本示例：请参阅[每个账户一个部署](#每个账户一个部署)。 |
| `excludeRegions` | 某个已启用的区域没有响应，或者您不需要它。请参阅[区域](#区域)。 |
| `spotScoreFamilies` | 您使用默认列表以外的 GPU 系列。请参阅[选择 Spot 分数系列](#选择-spot-分数系列)。 |
| `spotScoreConfigsPerDay` | 您想限制本示例保持使用中的不同 Spot 分数请求数量。当您的账户还会发出其他 Spot 分数调用时，请调低它，因为 AWS 的限制由整个账户共用。请参阅 [Spot 分数预算](#spot-分数预算)。 |
| `spotScoreCacheMinutes` | 15 到 240。更新器每 15 分钟重复一次每个已被接受的请求；大于 15 的值会使重复的频率降低（答案的时间达到该值减 5 分钟时会被重复）。 |
| `enableTestLaunch` | 设为 `false` 可得到只读部署。此时堆栈没有启动或终止权限，没有清理函数，没有拆除自定义资源，页面也没有测试启动面板。测试启动是唯一会启动实例的功能。在现有堆栈上关闭它会移除清理函数和拆除自定义资源，但不会移除探测资源；请先运行[查找残留资源](#查找残留资源)中的命令，删除探测安全组和启动模板，或在更新之后删除它们。 |
| `testLaunchAllowSpot` | 当账户无法使用 Spot，或您只需要按需测试启动时，设为 `false`。Spot 分数和价格视图保留。 |
| `testLaunchCooldownMinutes` | 调高它可减少启动次数。在每个时间窗口内，每个区域、市场和组（或规格）只有一个请求会启动实例，跨用户生效，因此冷却时间不会限制总量；限制总量的是每日上限。 |
| `maxTestLaunchesPerUserPerDay` | 调低它可限制每位用户；`0` 表示不设上限。计数按用户和 UTC 日计算（在 00:00 UTC 重置），跨区域、组和市场累计。EC2 处理的测试会计入，包括 EC2 因无容量或配额上限而拒绝的测试，以及没有明确结果就结束的测试。以下情况不计入：冷却期内的缓存结果、进行中的答案、被 EC2 限流或从未发出的请求、服务器剩余时间不足以启动的请求，以及在 Fleet 请求之前发生的失败（配额过低、没有默认 VPC、AWS 错误）。 |
| `maxTestLaunchesPerDay` | 调低它可限制整个部署；`0` 表示不设上限。各用户共用一个计数器，按 UTC 日计算，计数规则相同。页面会显示 `今天的测试启动：N/M（所有用户）` 以及该用户自己的计数。 |
| `enableNotifications` | 设为 `false` 可移除电子邮件提醒和 **提醒** 标签页。之后再关闭它会删除提醒表以及带有订阅的 SNS 主题。 |
| `notifyIntervalMinutes` | 调高它可降低检查频率。较低的值能更快发现短时出现的方案，也会更频繁地运行通知器。 |
| `maxSubscriptionsPerUser` | 当用户关注许多区域与类型组合时调高它；每个提醒都会为每次通知器运行增加检查。 |
| `offeringCacheHours` | 调高它可减少 `DescribeInstanceTypeOfferings` 调用；当用户需要更快看到新提供的类型时调低它。 |
| `quotaCacheMinutes` | 调高它可减少 Service Quotas 和 CloudWatch 调用；当用户需要更快看到上限或用量的变化时调低它。 |
| `apiRateLimit` | 许多用户同时工作，页面经常显示 `服务当前繁忙。`。阶段会把它以及该值三倍的突发额度应用于全体用户。 |
| `sessionHours` | 用户需要过于频繁地重新登录，或者您希望会话更短。请参阅[更改登录会话时长](#更改登录会话时长)。 |
| `alarmTopicArn` | 您希望通过电子邮件或其他 SNS 订阅接收堆栈的 CloudWatch 告警。请参阅[告警](#告警)。 |

### 区域

各项功能使用同一个区域列表：您的账户已启用的商业区域，减去 `excludeRegions`。供应、价格、配额、Capacity Block、Spot 分数、测试启动、清理函数和拆除函数都使用它。该列表来自 EC2 `DescribeRegions`，并在缓存表中保存 24 小时，因此您启用的区域会在一天内出现，无需重新部署。如果该调用失败，会继续使用已保存的列表。

`excludeRegions` 是以逗号分隔的列表，最多 40 个区域。请将它用于没有响应或您不想调用的已启用区域。本示例不会调用被排除的区域。Spot 分数更新器会在下一次每日更新时从其分数组中移除被排除的区域，在此之前页面不会显示它。

`us-east-1` 不能被排除：每个 Spot 分数请求都发送到 `us-east-1` 端点，因此部署会拒绝该设置。要恢复一个被排除的区域，请将它从 `excludeRegions` 中移除并再次部署。它会在下一次每日更新时加入其分数组，在此之前页面会显示 `下次每日更新后获得 Spot 分数`。

### 选择 Spot 分数系列

AWS 会限制一个账户每天可发出的不同 Spot 放置分数请求数量，账户中的各项内容共用这一限制。每个获取分数的系列，按每个 GPU 数量以及销售它的每 10 个区域，各占用一个请求。为每个区域中的每种 GPU 类型获取分数会超出该限制，因此您只需在 `spotScoreFamilies` 中列出计划使用的 GPU 系列。默认列表是 `g6e`、`g7`、`g7e`、`p5`、`p5e`、`p5en`、`p6-b200` 和 `p6-b300`（即模式 `g6e.*`、`g7.*` 等）。

要自定义该列表，请使用 `-c` 传入完整列表，并加上引号，以免 shell 处理星号：

```bash
./deploy.sh -c 'spotScoreFamilies=g6e.*,p5en.*,p6-b200.*'
```

每个模式指定一个系列及其规格（`p5.*`）或一个通配符（`g7*`），包含点、星号或问号，并指定一个 `g` 或 `p` GPU 系列。列表最多包含 20 个模式。`g7.*` 匹配 `g7.xlarge`，但不匹配 `g7e.xlarge`。模式以系列字母及其代数字开头，例如 `g7*` 或 `p5.*`，因此部署会拒绝 `g*` 或 `g?.*` 这样的模式。

哪些会自动更新，哪些不会：

- 已获取分数的系列的**新规格**会在下一次每日更新时加入其分数组。
- **新区域**，或开始销售某个规格的区域，会在下一次每日更新时加入分数组。在此之前，页面会为该单元格显示 `下次每日更新后获得 Spot 分数`。
- **新系列**在您将其添加到 `spotScoreFamilies` 并再次部署之前，不会获得 Spot 分数。更改该列表的部署会在第一个请求时建立新的分数组，而不是在第二天。您移除的系列的分数组不再被重复，其请求在 24 小时过去之前仍会计入账户的 AWS 限制。
- **供应、价格、配额、测试启动和 Capacity Block** 按 `g` 或 `p` 命名规则（以 `g` 或 `p` 加一个数字开头的系列名称）涵盖每个 GPU 系列，并会自行纳入新系列。

Spot 分数提醒需要已获取分数的系列中的类型。对于已获取分数的系列以外的类型，API 会拒绝 Spot 分数提醒。

### Spot 分数如何保持最新

**分数组**是一个系列加一个 GPU 数量，例如 `g6e · 4 个 GPU`（键为 `g6e/4`）。GPU 数量来自 `DescribeInstanceTypes`，每个类型保存一次。一个组的规格是该系列中具有该 GPU 数量、且有任一已启用区域销售的各个规格；该列表只会增加。一个组的区域列表包含至少销售其中一个规格的区域，按每块最多 10 个区域分块。本示例每块发送一个请求，因此组分数是任一列出规格能被满足的区域级机会，而不是单个规格的机会。

这些列表只追加不删除，使请求每天保持不变。新区域会在最后一块还有空间时加入该块，然后开启新的一块，顺序先按首次出现日期，再按名称。停止销售的区域仍留在其块中。提供内容查询失败的区域不会改变任何内容。唯一的例外是 `excludeRegions` 中的区域：它会在下一次每日更新时离开其块。

| 运行 | 时间 | 作用 |
|---|---|---|
| 每日更新 | 00:30 UTC 当时或之后的第一次运行 | 读取已启用的区域、它们销售的内容以及新规格的 GPU 数量。向已保存的列表中加入新规格、区域和组，移除被排除的区域，并向 AWS 发出符合预算的请求。不符合预算的请求保持计划状态。 |
| 引导 | 首次部署之后，或更改 `spotScoreFamilies` 的部署之后的第一个请求 | 立即执行与每日更新相同的工作。API 在后台启动一次更新器，在分数组建立之前，页面会显示正在建立的状态。 |
| 刷新 | 每 15 分钟，在第 0、15、30 和 45 分钟 | 重复 AWS 在过去一天内接受的每个请求（重复是免费的，并会返回新的分数），保存答案以及每个组的一条历史读数，并在预算有空间时批准计划中的请求。它还会保存销售某个已获取分数类型的每个区域的最新 Spot 价格。 |

页面加载、**刷新** 按钮和电子邮件提醒只读取缓存或重复已被接受的请求。它们不会创建或更改请求或组。**刷新** 对一个组的重复，就全体用户而言最多每 60 秒一次。

单元格显示其区域的状态：已获取分数、AWS 未给出分数、等待下一次刷新、计划中（请求正在等待预算空间）、受限（AWS 拒绝了它，或新请求暂停），或在下一次每日更新之后。`GET /api/config` 返回计划和计数：`spotScoreState`、`spotScoreGroups`、`spotScoreConfigsNeeded`、`spotScoreConfigsPerDay`、`spotScoreConfigsInUse`、`remainingNewConfigs`、`spotScoreLimitedUntil`、`lastDailyUpdateAt`、`nextDailyUpdateAt`、`nextWarmAt` 和 `dailyUpdateUtc`。

### Spot 分数预算

AWS 会限制一个账户在 24 小时内可发出的不同 Spot 放置分数请求配置数量。AWS 不公布该限制的大小，您也无法提高它。允许重复 AWS 在过去 24 小时内接受过的配置。本示例发送的每个请求都使用 `TargetCapacity` 1，该值不是一项设置（不同的值就是不同的配置）。当某块的区域列表或其所属组的规格发生变化时，该块的请求就会改变。向还有空间的块中加入一个区域，会为该块创建一个新配置。新规格会改变其所属组每一块的请求，因此会为该组的每一块各创建一个新配置。

`spotScoreConfigsPerDay`（默认 40，1 到 100）是本示例在滚动 24 小时内保持使用中的不同配置的最大数量。每日更新和刷新会在该范围内批准新请求，放不下的请求保持计划状态，直到旧请求过期腾出空间。本示例在缓存表中统计自己的配置：

- `spscfg#<fingerprint>`：每个已发送的配置一个条目，包括请求、AWS 首次接受它的时间和最后使用的时间、最后的结果，以及被拒绝后的封锁截止时间。它在最后一次写入的 8 天后过期。
- `#sps-budget`：预算的一个条目，包括过去 24 小时内的新配置、被拒绝后的暂停，以及已登记配置的索引。新配置会在请求发出之前通过条件写入占用一个名额，因此函数的两个副本无法同时占用最后一个名额。如果无法读取或写入该条目，就不会发送新配置。

AWS 拒绝某个新配置之后，该配置会被封锁 24 小时，本示例在 24 小时内不再请求新配置。已被接受的配置在暂停期间继续重复。如果此部署在被拒绝之前的 24 小时内已经创建了至少 3 个新配置，每日预算还会在 7 天内保持在该数量；如果少于 3 个，本示例会认为预算已被账户中的其他用途用完，只进行封锁和暂停。

早期版本的旧配置不会再次发送，但在它们移出 24 小时窗口之前，仍会计入 `spotScoreConfigsInUse`。

**历史条目。** 每个组在缓存表中保留一个小的历史条目 `spshist#<group>#regions`。它最多保留 48 条读数，丢弃超过 24 小时的读数，每 5 分钟最多记录一条读数，并在 2 天后过期。历史读取或写入失败会被记录到日志，而不会使请求失败。

### 测试启动

测试启动会在该区域的一个可用区中，向 EC2 请求一个属于某个分数组任一规格（或一个所选规格）的实例，并立即终止它。API 函数会：

1. 按顺序检查各项防护：`enableTestLaunch`、区域（已启用且未被排除）、市场（对 Spot 检查 `testLaunchAllowSpot`）、GPU 命名规则、调用者、冷却时间、每用户上限和全部署上限。在 Fleet 请求之前发生的失败（配额过低、没有默认 VPC、安全组冲突、AWS 错误）会退回两项计数和冷却名额。
2. 找出该区域的各个默认子网（每个可用区一个），以及该组中该区域销售、且符合在该市场中限制该系列的配额可用 vCPU 的各个规格。如果没有规格符合，答案是 `quota-too-low`，并且不会发出启动请求。未知的配额或 vCPU 数量不会排除某个规格。
3. 为该测试创建一个启动模板（探测标签、自行关机的用户数据、无公有地址、探测安全组），以及一个类型为 `instant`、包含一个实例的 EC2 Fleet。Spot 使用 `price-capacity-optimized`。按需使用 `lowest-price`，并且不会启动到开放式 Capacity Reservation 中，因此它衡量的是空闲容量。
4. 在 Fleet 返回答案之后立即终止实例并删除启动模板。instant 类型的 Fleet 不需要删除调用：EC2 会自行移除它。

答案为已启动（附有 EC2 选择的可用区和规格）、无容量、配额、不支持或错误。页面还会列出该区域中每个组和市场最近 24 小时的启动结果，涵盖各用户，且不含用户名。

安全组 `gpu-capacity-finder-probe` 没有入站规则。函数在第一次测试启动时为每个 VPC 创建一次该安全组，并附加标签 `gpu-capacity-finder-probe-sg`。

两个每日计数器位于探测历史表中。每用户计数器是 `#probe-count#...`，全部署计数器是 `#probes#YYYY-MM-DD`，它通过条件写入决定由谁获得最后一个名额。计数器条目在其所属日期结束两天后过期。API 角色只能更新这些分区。

### 调整 API 调用量

Lambda 函数会在您的账户中调用 EC2、Service Quotas、CloudWatch 和 AWS Price List API，因此仪表板的调用会与您的其他工作负载一起计入账户级 API 配额，例如 EC2 Describe API 请求限制。缓存表由各用户共用，因此一个答案会在其缓存时间结束之前为大家提供服务。调高缓存时间会减少调用并显示较旧的数据；调低则相反。

每个 API 容器还会在内存中保留缓存条目 60 秒，因此另一个容器写入的刷新结果最多可能需要 60 秒才会在这个容器中显示。

页面在标签页打开时读取缓存数据。当屏幕上的数据超过其缓存时间时，页面会在用户打开标签页或回到页面时再次读取它，而不强制刷新。**刷新** 会发送 `refresh=true`。对于 Spot 分数，这会重复该组已被接受的请求；对于其他数据，它会为该条目跳过缓存。每个缓存条目都有自己的强制刷新锁，即缓存表中的一次条件写入，因此每个条目（例如一个分数组，或一个区域的配额）最多每分钟向 AWS 查询一次。在这一分钟内的刷新会返回缓存的答案和剩余秒数，页面会作出说明。

| 数据 | 缓存无法回答时调用的 AWS API | 缓存与时间 | 跳过缓存的方式 |
|---|---|---|---|
| 各区域提供的内容（方案矩阵、规格列表、请求检查） | EC2 `DescribeInstanceTypeOfferings`，每个已启用区域两次分页调用 | 缓存表（`offerings#<Region>`），`offeringCacheHours`，另有 60 秒的内存层。在该时间窗口之后，仍会提供已保存的答案，同时由一个请求再次查询 EC2（请参阅[更新期间仍提供的答案](#更新期间仍提供的答案)）；该条目保留 7 天 | 页面上没有方式 |
| 支持 Capacity Block 的实例类型 | EC2 `DescribeInstanceTypes`，使用筛选条件 `supported-usage-class=capacity-block`，每个区域一次调用 | 缓存表（`cbtypes#<Region>`），每天重新查询一次，保留 7 天。如果调用失败，使用上一次的列表，否则使用 `lambda/capacity_blocks.py` 中的系列列表 | 页面上没有方式 |
| 已启用区域的列表 | EC2 `DescribeRegions`（已启用的商业区域） | 缓存表（`regions#enabled`），24 小时，保留 7 天。更新器也会在每次每日更新时实时读取它 | 页面上没有方式 |
| 分数组和 Spot 分数 | EC2 `GetSpotPlacementScores`，发送到 `us-east-1`，仅由分数更新器发出（以及由 **刷新** 针对已被接受的请求发出） | 分数组位于 `#sps-groups`（不过期）；每个答案在缓存表中保存 `spotScoreCacheMinutes` 加 10 分钟。页面和通知器读取相同的条目 | **刷新** 会重复一个组已被接受的请求 |
| Spot 价格 | EC2 `DescribeSpotPriceHistory`，由更新器在每次运行时为各个已获取分数的类型每个区域调用一次 | 缓存表（`price-spot#<Region>`），30 分钟。已获取分数的系列以外的类型，会在用户查询时获得自己的调用和条目 | 无；由更新器使其保持最新 |
| 按需定价 | Price List `GetProducts`，发送到 `us-east-1` | 缓存表，24 小时（Price List 中没有匹配项时为 1 小时；失败的调用不缓存） | 无 |
| **配额**（一次一个区域） | Service Quotas `ListServiceQuotas` 和 `ListAWSDefaultServiceQuotas`、CloudWatch `GetMetricData` | 缓存表，`quotaCacheMinutes` | **刷新**，按区域 |
| **Capacity Block** 搜索和 **最大容量** | EC2 `DescribeCapacityBlockOfferings`，每个区域与类型组合一个请求（**最大容量** 最多分三个请求运行，EC2 调用次数有上限，每个请求都远在 25 秒的函数超时之内） | 不缓存。一次搜索最多同时运行六个组合 | **重新搜索** 会再次运行上一次的搜索 |
| 测试启动历史和提醒 | DynamoDB 查询 | 不缓存 | **刷新** |

当 GET 请求收到 HTTP 429 或 503，或因网络而失败时，页面最多重试三次，分别等待约 0.6、1.2 和 2.4 秒外加一个随机额外时间；如果响应带有 `Retry-After` 标头，则等待其值（最多 8 秒）。之后，页面会显示 `服务当前繁忙。请稍候，然后选择“重试”。`，而 Capacity Block 搜索会在其他组合之后再尝试该组合一次。它不会重试测试启动每日上限（`probe-daily-limit`、`daily-cap-all`），也不会重复 POST、PUT 或 DELETE 请求，因此测试启动只会运行一次。

### 更改登录会话时长

`sessionHours`（默认 12）是从一次完整登录（使用密码和 MFA 代码）到下一次完整登录之间的最长时间。它设置 Cognito 应用客户端的刷新令牌过期时间。ID 令牌和访问令牌的有效期为 60 分钟，这是一个固定值；在 ID 令牌即将过期之前，页面会使用刷新令牌获取新的 ID 令牌，无需经过登录页面。当 `sessionHours` 结束时，页面会将用户转到登录页面。

页面只为当前浏览器标签页保存令牌，因此关闭标签页会在 `sessionHours` 结束之前结束会话。要更改该值，请使用新值和您的其他选项再次部署：

```bash
./deploy.sh -c sessionHours=24
```

此次更新只会更改 Cognito 应用客户端；它不会替换用户池或其中的用户。更新之前开始的会话，在用户重新登录之前可能保持原来的时长。如果您改为在 Amazon Cognito 控制台中更改 **Refresh token expiration**，下一次部署会用部署命令中的值覆盖它。

## 管理用户

请在堆栈所在的区域中运行这些命令。用户使用其电子邮件地址登录。

### 创建用户

```bash
aws cognito-idp admin-create-user \
  --region <StackRegion> \
  --user-pool-id <UserPoolId> \
  --username user@example.com \
  --user-attributes Name=email,Value=user@example.com Name=email_verified,Value=true \
  --desired-delivery-mediums EMAIL
```

Cognito 会通过电子邮件发送临时密码。首次登录时，用户设置密码并注册身份验证器应用。请将网站 URL 和[用户指南](user-guide.zh-cn.md)发送给用户。请按所示设置 `email_verified=true`：对于未验证的地址，API 会拒绝提醒。

您也可以在 AWS 管理控制台中创建用户。打开 Amazon Cognito，选择 **User pools**，然后打开您的用户池（`deploy.sh` 会输出用户池 ID）。选择 **Users**，然后选择 **Create user**。选择 **Send an email invitation**，输入电子邮件地址，勾选 **Mark email address as verified**，选择 **Generate a password**，然后选择 **Create user**。临时密码的有效期为 7 天。页面的 **用户** 标签页提供相同的步骤，并附有直达用户池的链接。

### 重新发送邀请或重置密码

如果临时密码在首次登录之前已过期，请重新发送：

```bash
aws cognito-idp admin-create-user --region <StackRegion> --user-pool-id <UserPoolId> \
  --username user@example.com --message-action RESEND --desired-delivery-mediums EMAIL
```

对于以前登录过的用户，请重置密码。Cognito 会通过电子邮件发送验证码，用户在下次登录时设置新密码：

```bash
aws cognito-idp admin-reset-user-password --region <StackRegion> \
  --user-pool-id <UserPoolId> --username user@example.com
```

丢失了身份验证器应用的用户无法登录。本示例尚未针对 MFA 重置进行测试。一种方法是关闭该用户的 TOTP 设置，让用户重新注册；请先在您的用户池中测试：

```bash
aws cognito-idp admin-set-user-mfa-preference --region <StackRegion> \
  --user-pool-id <UserPoolId> --username user@example.com \
  --software-token-mfa-settings Enabled=false,PreferredMfa=false
```

另一种方法是[移除该用户](#移除用户及其残留项)，然后重新创建该用户。新用户会获得新的用户 ID（`sub`），因此旧的提醒不会延续。在控制台中，打开用户池，选择 **Users**，按电子邮件地址打开该用户，然后在详细信息页面上使用重新发送、**Reset password** 或身份验证器选项。菜单标签在不同的控制台版本之间可能有所不同。

### 停用用户

```bash
aws cognito-idp admin-disable-user --region <StackRegion> \
  --user-pool-id <UserPoolId> --username user@example.com
```

已停用的用户无法登录。在您停用该用户之前签发的 ID 令牌在过期之前仍然有效，最长 60 分钟，因为 API 检查的是令牌签名和过期时间。停用不会停止该用户的提醒邮件。在控制台中，打开该用户的详细信息页面，然后选择 **Disable user access**。

### 移除用户及其残留项

在用户池中删除用户，不会移除该用户的提醒或 SNS 订阅。请先移除它们。用户的实例类型列表保存在用户的浏览器中，而不在堆栈中。

1. 找到该用户的 `sub`：

   ```bash
   aws cognito-idp admin-get-user --region <StackRegion> --user-pool-id <UserPoolId> \
     --username user@example.com --query "UserAttributes[?Name=='sub'].Value" --output text
   ```

2. 找到提醒表的名称（逻辑 ID 以 `Subscriptions` 开头）：

   ```bash
   aws cloudformation describe-stack-resources --region <StackRegion> --stack-name gpu-capacity-finder \
     --query "StackResources[?ResourceType=='AWS::DynamoDB::Table'].[LogicalResourceId,PhysicalResourceId]" \
     --output table
   ```

3. 如果提醒已开启，请列出该用户在提醒表（`<SubscriptionsTable>`）中的条目，取消订阅该地址，然后删除每个条目：

   ```bash
   aws dynamodb query --region <StackRegion> --table-name <SubscriptionsTable> \
     --key-condition-expression "userId = :u" \
     --expression-attribute-values '{":u":{"S":"<sub>"}}' \
     --query "Items[].[sk.S, subscriptionArn.S]" --output text
   aws sns unsubscribe --region <StackRegion> --subscription-arn <SubscriptionArn>   # from the #profile item
   aws dynamodb delete-item --region <StackRegion> --table-name <SubscriptionsTable> \
     --key '{"userId":{"S":"<sub>"},"sk":{"S":"<sk>"}}'                               # repeat per item, including #profile
   ```

4. 删除该用户：

   ```bash
   aws cognito-idp admin-delete-user --region <StackRegion> \
     --user-pool-id <UserPoolId> --username user@example.com
   ```

测试启动记录会保留执行每次测试的人员的用户名，并在 30 天后过期。

## 升级

要应用本示例的新版本或更改设置，请使用相同的选项再次运行部署：

```bash
source .venv/bin/activate      # needed for cdk diff
cdk diff -c <same options>     # optional: preview the change
./deploy.sh -c <same options>
```

部署会更新函数和页面，并使 CloudFront 缓存失效。用户、网站 URL 和表数据都会保留，但属于您关闭的功能的表除外。如果您更改 `stackName`，CDK 会创建一个新的、独立的堆栈。

### 升级到 Spot 分数组

此版本更改了 Spot 分数、区域和测试启动的工作方式。在将其部署到早期的堆栈之上之前：

1. **移除或重命名设置。** 部署会停止，并显示一条指出该设置名称的消息：
   - 已移除：`testLaunchRegions` 和 `regions`（测试启动现在在每个已启用的区域中运行；请使用 `excludeRegions` 排除区域）、`regionScope`、`enableReservation`、`reservationHours`、`maxReservationsPerUser`、`apiBurstLimit`（现在为 `apiRateLimit` 的三倍）、`tokenMinutes`（现在为 60 分钟）和 `instanceTypes`。
   - 已重命名：`instanceFamilies` 现在是 `spotScoreFamilies`（它现在只选择获取 Spot 分数的系列；页面涵盖每个 GPU 系列），`enableSpotTestLaunch` 和 `enableSpotProbe` 现在是 `testLaunchAllowSpot`，`enableProbe` 现在是 `enableTestLaunch`，`probeCooldownMinutes` 现在是 `testLaunchCooldownMinutes`，`maxProbesPerUserPerDay` 现在是 `maxTestLaunchesPerUserPerDay`。三项 Spot 分数预算设置（`spotScoreNewConfigsPerDay`、`spotScoreReservePerDay`、`spotScoreMaxConfigs`）现在合并为一项 `spotScoreConfigsPerDay`。
2. **可用区分数已移除。** 页面和 API 只显示 GPU 组的区域级分数（`scope=az` 会返回错误）。新的测试启动记录不带分数；较早的记录保留其已保存的分数。
3. **预期会有一个建立步骤。** 部署之后的第一个请求会启动分数更新器，由它建立分数组。在它完成之前，页面会显示正在建立的状态。旧的按类型 Spot 分数请求不会再次发送。
4. **测试启动已更改。** 测试启动现在请求该区域某个可用区中的一个组或一个规格，冷却时间以组或规格、市场和区域为键。新的全部署上限 `maxTestLaunchesPerDay`（默认 100）开始生效；设为 `0` 可关闭它。API 角色获得已启用区域中的测试启动权限。探测实例类型的 IAM 条件现在为 `g*` 或 `p*`，因为 API 会在任何启动之前检查 GPU 系列规则。
5. **删除旧的部署标志。** 从您的部署命令中移除 `-c regions` 和 `-c regionScope`。之后测试启动会涵盖已启用的区域减去 `excludeRegions`。
6. **了解删除或回滚的作用。** 拆除自定义资源只在堆栈本身被删除时才会执行操作：它的函数会读取其所属堆栈的状态，并且只在该状态为 `DELETE_IN_PROGRESS` 时移除探测资源。当一次更新移除了该自定义资源（例如使用 `enableTestLaunch=false`），或 CloudFormation 回滚了失败的升级时，该函数不会移除任何内容。测试实例仍会在 5 分钟后自行终止；请使用[查找残留资源](#查找残留资源)检查其他探测资源。回滚到旧版本之后，Spot 分数可能需要等待最多一天，因为新代码的请求仍会计入 AWS 的限制。
7. **设置 `alarmTopicArn`。** 新版本增加了 CloudWatch 告警。传入 `-c alarmTopicArn=<TopicArn>` 以接收通知；请参阅[告警](#告警)。

### 从具有容量保留或已保存列表的版本升级

本示例的早期版本可以使用按需 Capacity Reservation 保留容量，并将每位用户的实例类型列表保存在 DynamoDB 表中。此版本移除了这两项功能。在将其部署到这样的堆栈之上之前：

1. **取消保留。** 新版本无法列出或取消它们，它们会持续计费直到结束时间。在每个已启用的区域中：

   ```bash
   aws ec2 describe-capacity-reservations --region <Region> \
     --filters Name=tag:gpu-capacity-finder-reservation,Values=true Name=state,Values=active,pending,assessing \
     --query 'CapacityReservations[].CapacityReservationId'
   aws ec2 cancel-capacity-reservation --region <Region> --capacity-reservation-id <ReservationId>
   ```

2. **告知用户他们的列表。** 部署会删除已保存列表的表（`Watchlist`）和保留租约表（`Holds`）。用户需要重新创建列表；页面现在会在每个浏览器中保存它。
3. **删除您不需要的备份。** 被移除的表开启了时间点恢复，因此 DynamoDB 会为每个表保留系统备份 35 天（请参阅[拆除](#拆除)）。

## 成本控制检查清单

资源在运行期间会产生费用。最大的费用来自测试启动所启动的 GPU 实例。

- [ ] 使用 `enableTestLaunch=false` 获得只读部署。
- [ ] 将 `maxTestLaunchesPerDay` 和 `maxTestLaunchesPerUserPerDay` 调低到您可接受的每日测试启动次数；如果测试运行的频率高于用户的需要，请调高 `testLaunchCooldownMinutes`。
- [ ] 让 `spotScoreFamilies` 只包含您使用的系列。系列越少，Spot 分数请求就越少。
- [ ] 提醒用户，他们购买或创建的内容是堆栈不跟踪的计费资源：在 EC2 控制台中购买的 Capacity Block 是一次购买，而（使用 **使用 CLI 预留** 给出的命令创建的）Capacity Reservation 从创建起以按需费率计费，直到它结束或用户取消为止。
- [ ] 调高 `notifyIntervalMinutes`、`offeringCacheHours` 和 `quotaCacheMinutes`，以减少计划调用和 API 调用。只有在用户需要更新鲜的数据时才调低缓存时间，并预期调用会增加。
- [ ] 让 `apiRateLimit` 不高于用户的需要。仪表板的突发调用会计入您账户的 EC2 Describe API 限制。
- [ ] 大量使用之后检查是否有遗留的测试实例，并为两个清理告警附加 SNS 操作（请参阅[告警](#告警)），以便在清理失败时得到通知。
- [ ] 当您不再需要堆栈时将其拆除。

## 监控

### 日志组

各函数在堆栈所在的区域中运行，并写入保留期为一个月的日志组。查找名称：

```bash
aws cloudformation describe-stack-resources --region <StackRegion> --stack-name gpu-capacity-finder \
  --query "StackResources[?ResourceType=='AWS::Logs::LogGroup'].[LogicalResourceId,PhysicalResourceId]" \
  --output table
aws logs tail <LogGroupName> --since 1h --region <StackRegion>
```

| 日志组（逻辑 ID 前缀） | 函数 | 要查找的内容 |
|---|---|---|
| `ApiLogs` | API 函数 | `AWS call failed`（页面显示 502 错误）、`unhandled error`、`usage metrics failed in <Region>`、`call failed in <Region>`、`Spot score groups unavailable`、`terminate failed for`（之后由清理函数终止的测试实例） |
| `ScoreUpdaterLogs` | Spot 分数更新器 | 每次运行一行：`score updater: {...}`，包含 `mode`（`warm`、`daily` 或 `bootstrap`）、`updated`，以及配置、已重复、已批准、计划中、失败和受限请求的计数。更新之后会有 `daily update: N groups, M configurations, K Regions answered of L`。`daily update skipped` 表示区域列表或提供内容查询都没有响应，下一次运行会再次尝试。`offering lookup failed in <Region>` 表示该区域的分数组保持不变。`Spot placement score request failed for` 标记一次失败的请求。 |
| `CleanupLogs` | 清理函数 | 每次运行一行：`terminated leftover probe instances: {...}; deleted leftover probe launch templates: {...}; failed Regions: [...]`。`probe cleanup failed in <Region>`（一条警告）表示函数无法清理该区域。 |
| `NotifierLogs` | 通知器函数 | 每次运行一行：`alert check: {...}`，包含计数 `subscriptions`、`skipped`、`groups`、`failedGroups`、`notified`、`unconfirmed`、`rejected` 和 `failedItems`。`signal check failed for ...` 标记一次失败的查询；提醒保留其状态，下一次运行会再次尝试。`capacity block check rejected for ...` 标记一个被 EC2 拒绝的 Capacity Block 提醒，并附有 `cb-invalid-duration` 之类的代码；页面会向用户显示它，并且不会发送邮件。 |
| `ProbeTeardownLogs` | 拆除函数 | 在您删除堆栈时运行。`probe teardown left ... and failed in ...` 会列出它无法移除的内容。堆栈删除也会移除此日志组。 |

每个 API 请求都会向 `ApiLogs` 写入一行结构化的 INFO 日志，包含字段 `msg`（`request`）、`method`、`route`、`status`、`durationMs`、`cacheHits`、`cacheMisses`、`coldStart`，出错时还有 `errorCode`。当 `region` 是已启用的区域之一时，该行还包含 `region`；当 `instanceType` 是格式正确的实例类型名称时，还包含 `instanceType`。它不包含令牌、声明、电子邮件地址或其他查询值。API Gateway 访问日志未启用。要查看每个路由的持续时间中位数和第 90 百分位数，请在 CloudWatch Logs Insights 中对 `ApiLogs` 日志组运行以下查询：

```
filter msg = "request"
| stats count(*) as requests, pct(durationMs, 50) as p50, pct(durationMs, 90) as p90 by route
| sort p90 desc
```

### 关注限流

当到达的请求超过 `apiRateLimit` 及其突发额度所允许的数量时，API Gateway 会以 HTTP 429 响应，页面在重试之后会显示 `服务当前繁忙。`。在 Amazon CloudWatch 中该 HTTP API 的 API Gateway 指标里，`4xx` 指标会把这些响应与其他客户端错误一起计数，因此繁忙时段的持续上升表明是限流所致。如果是这样，请调高 `apiRateLimit`。

### 关注缓慢的区域

EC2 调用使用 3 秒的连接超时和 15 秒的读取超时，总共 3 次尝试，因此无法访问的区域不会长时间占用请求。读取每个已启用区域的页面总共等待约 12 秒。届时仍未响应的区域会显示为错误行（`该区域未响应。`），并被跳过 5 分钟；其他区域不受影响。要查看这种情况发生的频率，请在 API 函数日志组中搜索 `did not answer`。账户尚未启用的区域会改为显示 `未启用`，这不是故障。如果某个区域持续失败，请将它添加到 `excludeRegions`。

API 创建的其他 AWS 客户端也有上限。Service Quotas、CloudWatch、Price List、SSM 和 SNS 调用使用 3 秒的连接超时和 10 秒的读取超时，总共 2 次尝试。启动分数更新器的调用使用 5 秒的读取超时和 1 次尝试；启动失败会由之后的请求再次尝试。当 CloudWatch 用量指标失败或超时时，配额页面仍会显示上限，“已使用”列为空，并附有提示 `目前无法获取 <Region> 的用量`。

跳过是共享的。没有响应的区域会在缓存表中获得一个小条目 `#down#<Region>`，其值和 TTL 都是该区域被跳过的截止时间，因此它会在 5 分钟后自行清除。以错误（包括限流）响应的区域不会被标记，并且标记并不表示该区域不销售任何内容。要查看当前的标记：

```bash
aws dynamodb scan --region <StackRegion> --table-name <OfferingCacheTableName> \
  --filter-expression "begins_with(cacheKey, :p)" \
  --expression-attribute-values '{":p":{"S":"#down#"}}' \
  --query 'Items[].[cacheKey.S,downUntil.N]' --output table
```

### 查询失败不会被当作“不提供”

各区域提供的内容是几乎每个答案的基础，因此查询失败不能变成空答案。`DescribeInstanceTypeOfferings` 调用失败、超时或因标记而被跳过的区域，会报告为错误行或未知，并且不会向其缓存条目写入任何内容。分数更新器以相同方式处理查询失败：该区域的分数组保持不变，直到之后的某次每日更新得到答案。

### 更新期间仍提供的答案

每个区域的提供内容条目都有一个新鲜度窗口（`offeringCacheHours`），并保留 7 天。窗口结束后，下一个请求会立即用已保存的条目作答，并附上该条目自己的 `更新于 ...` 时间，同时在同一个请求中再次查询 EC2。只有获得该条目刷新锁的请求才会这样做，每个区域最多每分钟一次，并且它会在 4 秒后放弃，为函数超时留出最后 6 秒。刷新失败会保留已保存的答案。超过 7 天的条目会被移除，下一个读取者需要等待查询完成。

### 定时函数的工作内容

- **分数更新器** 在每小时的第 0、15、30 和 45 分钟运行。00:30 UTC 的运行是每日更新。它持有一个锁条目，使各次运行不会重叠。请参阅 [Spot 分数如何保持最新](#spot-分数如何保持最新)。
- **清理函数** 每 15 分钟运行一次。它读取已启用的区域（减去 `excludeRegions`），并在每个区域中列出带有标签 `gpu-capacity-finder-probe=true`、处于 `pending`、`running`、`stopping` 或 `stopped` 状态的实例，终止其中启动超过 10 分钟的实例。它还会删除带有该标签、存在超过 1 小时的启动模板。一个区域失败不会影响其他区域。
- **测试实例自行终止**，作为第二道兜底措施。每次测试启动都会传入用户数据脚本 `shutdown -h +5`，并将关机行为设为 `terminate`，因此即使页面的终止调用和清理函数都失败，实例也会在操作系统启动 5 分钟后关机并终止。成功的测试启动通常会被立即终止。
- 当 `enableTestLaunch=true` 时，存在安全网的两个 **CloudWatch 告警**：`<stack>-probe-cleanup-errors`（清理函数的 `Errors` 指标）和 `<stack>-probe-cleanup-region-failures`（清理日志组上针对 `probe cleanup failed in` 的指标筛选器，位于命名空间 `GpuCapacityFinder/<stack>` 中）。两者都使用 15 分钟的周期，即一次计划运行。
- **通知器函数** 每 `notifyIntervalMinutes` 运行一次。它读取整张提醒表，跳过不再匹配已启用区域的提醒，并使用只读调用对每个不同的信号查询一次。Capacity Block 检查会按提醒的实例数量和时长查询，不带日期窗口。Spot 分数提醒读取分数更新器缓存的组分数，不发出 Spot 分数请求。只有当提醒从不可用变为可用，并且用户已确认 SNS 订阅之后，它才会向该用户发送邮件。失败的检查会保留上一次的状态，并在下一次运行时重试。

### 告警

堆栈会为其自身的函数创建 CloudWatch 告警。它们读取平台发布的 `AWS/Lambda` 指标，因此不需要代码，也不需要额外权限，每个告警每月产生少量费用。

| 告警 | 指标 | 周期和阈值 | 含义 |
|---|---|---|---|
| `<stack>-api-errors` | API 函数的 `Errors` | 5 分钟，大于 0 | 某个请求失败，或平台在 25 秒超时时停止了一次运行。 |
| `<stack>-api-duration` | API 函数的 `Duration`（最大值） | 5 分钟，大于 20,000 毫秒 | 某个请求接近超时：缓存未预热、区域缓慢，或扇出范围较大。 |
| `<stack>-api-throttles` | API 函数的 `Throttles` | 5 分钟，大于 0 | 由于达到账户的并发限制，Lambda 拒绝了运行。 |
| `<stack>-score-updater-errors`、`-duration`、`-throttles` | 更新器的相同指标 | 5 分钟；`Duration` 大于 240,000 毫秒（其 5 分钟超时的 80%） | Spot 分数更新失败、运行时间过长或被限流。一次失败的运行会把分数推迟到下一次运行。 |
| `<stack>-notifier-errors`、`-duration`、`-throttles` | 通知器函数的相同指标 | 5 分钟；`Duration` 大于 192,000 毫秒（其 4 分钟超时的 80%） | 提醒检查失败、运行时间过长或被限流。 |
| `<stack>-probe-cleanup-duration`、`-throttles` | 清理函数的相同指标 | 5 分钟；`Duration` 大于 96,000 毫秒（其 2 分钟超时的 80%） | 安全网运行时间过长或被限流。 |
| `<stack>-probe-cleanup-errors`、`-probe-cleanup-region-failures` | 请参阅[定时函数的工作内容](#定时函数的工作内容) | 15 分钟，一次计划运行 | 遗留的测试实例可能仍在运行。 |

每个告警都使用一个周期的一个数据点，并将没有数据的周期视为未超出阈值。清理告警只在 `enableTestLaunch=true` 时存在，通知器告警只在 `enableNotifications=true` 时存在。

**接收通知。** 将 `alarmTopicArn` 设为您已拥有、位于堆栈区域中的 SNS 主题，堆栈的每个告警都会向它发布。请使用该主题和您的其他选项进行部署，因为您省略的设置会恢复为其默认值：

```bash
aws sns create-topic --region <StackRegion> --name gpu-capacity-finder-alarms
aws sns subscribe --region <StackRegion> --topic-arn <TopicArn> --protocol email --notification-endpoint you@example.com
./deploy.sh -c alarmTopicArn=<TopicArn> -c <your other options>
```

堆栈只引用该主题，因此其访问策略仍由您掌控，也不会向任何角色添加 IAM 权限。该策略必须允许 CloudWatch 发布：

```json
{
  "Sid": "AllowCloudWatchAlarms",
  "Effect": "Allow",
  "Principal": {"Service": "cloudwatch.amazonaws.com"},
  "Action": "sns:Publish",
  "Resource": "<TopicArn>",
  "Condition": {"StringEquals": {"AWS:SourceAccount": "<AccountId>"}}
}
```

如果该主题已加密，请使用客户托管密钥，而不是 AWS 托管密钥 `alias/aws/sns`：CloudWatch 无法向使用 AWS 托管密钥加密的主题发布。密钥策略必须允许 `cloudwatch.amazonaws.com` 对该密钥调用 `kms:GenerateDataKey*` 和 `kms:Decrypt`。发布被拒绝的告警会在控制台中改变状态，但不会投递任何内容，因此请在部署后发送一条测试通知：

```bash
aws cloudwatch set-alarm-state --region <StackRegion> --alarm-name <stack>-api-errors \
  --state-value ALARM --state-reason "testing the notification path"
```

电子邮件提醒的提醒主题不用于告警：它属于已订阅的用户，并且使用 AWS 托管密钥。

### 查找残留资源

在用户进行过测试的每个区域中，查找测试实例、启动模板和探测安全组：

```bash
aws ec2 describe-instances --region <Region> \
  --filters Name=tag:gpu-capacity-finder-probe,Values=true \
            Name=instance-state-name,Values=pending,running,stopping,stopped \
  --query 'Reservations[].Instances[].[InstanceId,InstanceType,LaunchTime]' --output table

aws ec2 describe-launch-templates --region <Region> \
  --filters Name=tag:gpu-capacity-finder-probe,Values=true \
  --query 'LaunchTemplates[].[LaunchTemplateId,CreateTime]' --output table

aws ec2 describe-security-groups --region <Region> \
  --filters Name=tag:gpu-capacity-finder-probe-sg,Values=true \
  --query 'SecurityGroups[].[GroupId,VpcId]' --output table
```

## 备份与数据

这些表使用按需容量，并开启了时间点恢复。`cdk destroy` 会删除这些表，DynamoDB 会为每个表保留系统备份 35 天（请参阅[拆除](#拆除)）。要按您自己的方式保留数据，请在销毁堆栈之前创建按需备份或导出。

| 表（逻辑 ID） | 保存的内容 | 保留期 |
|---|---|---|
| 探测历史表（`Probes`） | 每次测试启动一条记录：区域、组或规格、市场、结果、获得的可用区和规格、EC2 错误代码，以及调用者的用户名。还包括短期的冷却锁、每位用户每个 UTC 日一个每日计数器（`#probe-count#...`），以及每个 UTC 日一个全部署计数器（`#probes#YYYY-MM-DD`）。 | 记录在 30 天后过期。锁条目在其冷却时间结束一天后过期，计数器在其所属日期结束两天后过期。 |
| 缓存表（`OfferingCache`） | 共享条目，不含用户数据：`offerings#<Region>`（提供的类型、可用区、系列名称）、`cbtypes#<Region>`、`regions#enabled`、`#regions-first-seen`（不过期）、`#sps-groups`（已保存的分数组：规格、区域块、GPU 数量、每个区域提供的类型，以及已获取分数的系列；不过期）、Spot 分数答案、`spshist#<group>#regions`（2 天）、`spscfg#<fingerprint>`（最后一次写入后 8 天）和 `#sps-budget`、`quota#<Region>`、`price-od#<type>#<Region>`、`price-spot#<Region>`、`#lock#<item>` 刷新锁，以及 `#down#<Region>` 标记。 | 每个条目的缓存时间（请参阅[调整 API 调用量](#调整-api-调用量)）。锁条目在其时间窗口结束一天后过期。 |
| 提醒表（`Subscriptions`） | 用户的提醒，以及一个 `#profile` 条目，包含电子邮件地址、SNS 订阅的 Amazon 资源名称（ARN）和提醒数量。 | 直到用户删除这些提醒为止。档案条目随最后一个提醒一起删除。 |

已过期的条目会一直保留，直到 DynamoDB 在后台删除它们。每位用户的实例类型列表、最近的 Capacity Block 搜索、上次使用的区域和类型，以及语言选择，都保存在该用户的浏览器存储中，而不在堆栈中。其他数据：用户池保存用户及其电子邮件地址，SNS 主题保存电子邮件订阅，存储桶保存页面以及一个 `config.json` 文件，其中包含 API URL、应用客户端 ID、登录域和网站 URL。这些值不是机密；页面需要它们来完成登录。

## 安全说明

- S3 存储桶阻止公有访问，要求使用传输层安全性（TLS），并且只通过带有源访问控制的 CloudFront 提供内容。CloudFront 会添加内容安全策略、HTTP 严格传输安全（HSTS），以及框架和引用来源标头。
- 自助注册已关闭。必须使用身份验证器应用进行 MFA，短信 MFA 已关闭。密码需要 12 个字符，并包含大小写字母、数字和符号。
- API 授权方会拒绝没有来自该用户池、针对该应用客户端的有效 ID 令牌的请求。函数从令牌中获取用户 ID，而不是从请求中获取。
- 会话时长是一种权衡。较长的 `sessionHours` 会让已登录的浏览器标签页在更长时间内无需重新登录即可使用，而 MFA 只在登录时适用，在页面续期令牌时不适用。停用用户会停止续期，**退出登录** 会撤销刷新令牌，但已经签发的 ID 令牌在过期之前（最长 60 分钟）仍可在 API 上使用。
- 应用客户端不启用通过 API 进行的直接密码（安全远程密码，SRP）登录。页面通过托管登录页面，使用授权码流程登录。
- 测试启动使用默认 VPC，不分配公有 IP 地址，要求使用实例元数据服务版本 2（IMDSv2），并使用一个没有入站规则的专用安全组。按需测试启动将其 Capacity Reservation 首选项设为 `none`。每次测试都会传入一个静态用户数据脚本（`shutdown -h +5`，不含机密、不含账户数据、不发起网络调用），使实例在操作系统启动 5 分钟后关机，并在关机行为为 `terminate` 的情况下自行终止。
- API 角色没有创建、修改或取消 Capacity Reservation 的权限，也没有购买 Capacity Block 的权限。页面只链接到 EC2 控制台并显示 Capacity Reservation 命令；用户使用自己的凭证操作。
- **测试启动权限涵盖每个区域。** 由于测试启动在每个已启用的区域（减去 `excludeRegions`）中运行，启动语句的 EC2 资源 ARN 会指定每个区域，而函数会在调用 EC2 之前拒绝被排除或未启用的区域。各项条件仍然保留：`RunInstances` 只适用于匹配 `g*` 或 `p*` 的实例类型，并且必须带有探测标签（函数会将其进一步缩小到命名规则）；`CreateFleet` 和 `CreateLaunchTemplate` 必须带有探测标签；`RunInstances`、`CreateFleet` 和 `DeleteLaunchTemplate` 对启动模板的使用，仅限该模板带有标签时；`CreateTags` 仅限创建时；`TerminateInstances` 仅限带有标签的实例；探测安全组仅限带有其自身标签；`RunInstances` 和 `CreateFleet` 仅限 Amazon 拥有的映像，并且只能进入带标签的探测安全组。`DescribeInstances` 让函数能够找到启动调用结果不明确的实例，以便终止它。这些语句是 `LaunchTaggedProbeInstance`、`CreateTaggedProbeFleet`、`LaunchSupportingResources`、`LaunchAmazonImageOnly`、`LaunchInProbeSecurityGroupOnly`、`FindUnknownOutcomeProbe`、`UseProbeLaunchTemplate`、`CreateProbeLaunchTemplate`、`CreateProbeSecurityGroup`、`CreateProbeSecurityGroupInVpc`、`TagProbeSecurityGroupOnCreate`、`TagOnLaunchOnly` 和 `TerminateProbeInstancesOnly`。`CountDailyProbes` 只允许对计数器分区执行 DynamoDB `UpdateItem`。该角色可以读取 AWS Systems Manager Parameter Store 中的两个 Amazon Linux 2023 映像参数（x86_64 和 arm64）。
- 在这些表上，API 角色可以对探测历史表执行 `Query`，并使用 `PutItem` 写入探测记录和冷却锁（`StoreProbeRecords`，带有 `dynamodb:LeadingKeys` 条件，使其无法访问计数器分区）。它可以读取和写入缓存表条目（`GetItem`、`BatchGetItem` 和 `PutItem`）。在每个部署中，它还只能启动分数更新器函数（`lambda:InvokeFunction`），并读取堆栈区域中 AWS 公开的区域长名称参数（`ssm:GetParameter` 和 `ssm:GetParameters`）。
- 当电子邮件提醒开启时，API 角色还可以读取和写入提醒表（`Query`、`PutItem`、`DeleteItem` 和 `UpdateItem`），只能将电子邮件端点订阅到提醒主题（`sns:Subscribe`，条件为 `sns:Protocol=email`），并且只能取消该主题的订阅。
- 通知器角色只在 `enableNotifications` 设为 `true` 时存在。它可以对提醒表执行 `Scan` 和 `UpdateItem`，读取缓存表条目并对其执行 `PutItem`（用于共享的已启用区域列表及其锁），在资源 `*` 上调用 `ec2:DescribeCapacityBlockOfferings` 和 `ec2:DescribeRegions`（`ReadAlertSignals`），只能向提醒主题发布（`sns:Publish`），并读取该主题订阅的属性（`sns:GetSubscriptionAttributes`）。
- 清理角色只能终止和删除带有标签的资源。拆除角色还可以删除带有探测安全组标签的安全组，并可以读取其所属堆栈的状态（仅对该堆栈的 `cloudformation:DescribeStacks`），因此它只在堆栈被删除时才移除探测资源。更新器角色拥有只读的 EC2 操作以及对缓存表的访问权限。
- 读取操作使用资源 `*`，因为它们没有资源级权限：EC2 Describe 和提供内容读取、`GetSpotPlacementScores`、Service Quotas 列表调用、`ec2:DescribeInstanceTypes`、`ec2:DescribeRegions`、`cloudwatch:GetMetricData` 和 `pricing:GetProducts`。`pricing:GetProducts` 读取公开定价；函数将其发送到 `us-east-1` 端点。
- 一个 Lambda 角色为部署的各用户提供服务，而 IAM 无法在一个角色内区分用户。函数按令牌中用户的 `sub` 确定提醒表访问的键。该角色读取和写入整张表。
- 页面没有 AWS WAF Web 访问控制列表（Web ACL）、自定义域名和访问日志，并且默认的 API Gateway 端点仍然可以单独访问。在广泛分享页面之前，请先添加它们。
- 区域地图的世界轮廓来自 Natural Earth，属于公有领域。示例根目录中的 [THIRD-PARTY-LICENSES](../THIRD-PARTY-LICENSES) 文件列出了它。如果您复制该页面，请保留该文件。
- SNS 主题使用 Amazon SNS 的 AWS 托管密钥。如果您需要自己的密钥策略，请使用客户托管的 AWS Key Management Service（AWS KMS）密钥。

[README 的限制一节](../README.zh-cn.md#限制)列出了这些以及其他限制。

## 拆除

资源在运行期间会产生费用，包括测试启动所启动的 GPU 实例。请按以下顺序移除它们。

1. **销毁堆栈。**

   ```bash
   source .venv/bin/activate   # the CDK app needs the Python environment
   cdk destroy   # add -c stackName=<name> if you deployed with a custom stack name
   ```

   这会移除存储桶内容、各张表、带有订阅的 SNS 主题、用户池，以及堆栈为其函数创建的日志组。当 `enableTestLaunch=true` 时，堆栈在被删除时还会运行一个自定义资源（`Custom::ProbeTeardown`）。它的函数会终止仍在运行的测试实例，然后在每个已启用的区域（减去 `excludeRegions`）中删除带标签的探测启动模板和带标签的探测安全组。创建这些资源的函数会先被删除，因此在拆除运行期间不会有新资源被创建。该自定义资源会在有限的时间内报告成功（函数会在其 5 分钟超时之前约 30 秒停止自己的工作，CloudFormation 会在 10 分钟后停止等待），因此某个区域失败不会阻止删除。函数会把它无法移除的内容记录为 `probe teardown left ...`，但它的日志组会随堆栈一起被删除。

   每个 DynamoDB 表都开启了时间点恢复。删除这样的表时，DynamoDB 会创建一个系统备份并保留 35 天。提醒表的备份包含您用户的电子邮件地址。请列出堆栈区域中的备份，并删除您不需要的备份：

   ```bash
   aws dynamodb list-backups --backup-type SYSTEM --region <region>
   aws dynamodb delete-backup --backup-arn <arn> --region <region>
   ```

2. **在每个区域中检查探测资源。** 如果您在某个区域测试之后才将它添加到 `excludeRegions`，或者某个区域无法及时访问，自定义资源会跳过该区域。在用户运行过测试启动的每个区域中，运行[查找残留资源](#查找残留资源)中的命令。终止每个遗留的实例，使用 `aws ec2 delete-launch-template --region <Region> --launch-template-id <Id>` 删除每个遗留的启动模板，并使用 `aws ec2 delete-security-group --region <Region> --group-id <GroupId>` 删除每个遗留的安全组（在使用它的测试实例仍然存在时，无法删除该安全组）。

3. **删除 CDK 辅助函数遗留的日志组。** 这些日志组可能在 `cdk destroy` 之后保留下来。在堆栈区域中列出它们（默认的 `<stackName>` 为 `gpu-capacity-finder`），检查每个名称，并删除属于此堆栈的日志组：

   ```bash
   aws logs describe-log-groups --region <StackRegion> \
     --log-group-name-prefix /aws/lambda/<stackName>- --query 'logGroups[].logGroupName'
   aws logs delete-log-group --region <StackRegion> --log-group-name <LogGroupName>
   ```

每个部署中的标签都相同，因此这些命令和拆除函数也会匹配同一账户中另一个部署的资源。每个账户只使用一个部署：请参阅[每个账户一个部署](#每个账户一个部署)。

`cdk destroy` 不会处理用户在 EC2 控制台中购买的 Capacity Block，或用户使用复制的命令创建的 Capacity Reservation。它们属于用户，并会持续计费，直到结束或被取消；请让用户检查 EC2 控制台。

## 移交给您的团队

部署之后确认以下各项，然后移交网站 URL 和[用户指南](user-guide.zh-cn.md)。

- [ ] **API 拒绝匿名调用。** 从页面的配置中读取 API URL，然后在不带令牌的情况下调用它。答案是 HTTP 401：

  ```bash
  API_URL=$(curl -s <SiteUrl>config.json | python3 -c 'import json,sys; print(json.load(sys.stdin)["apiUrl"])')
  curl -s -o /dev/null -w '%{http_code}\n' "$API_URL/api/config"   # expect 401
  ```

- [ ] **第一位用户已存在并可以登录。** 创建该用户，登录，设置密码，并注册身份验证器应用。
- [ ] **Spot 分数组已存在。** 在 **Spot 分数** 标签页上，第一次加载会显示正在建立的状态，一两分钟后，表格会以区域为行、分数组为列显示，并带有 `更新于` 标签。稍后才加入的区域的单元格会显示 `下次每日更新后获得 Spot 分数`。如果您的系列列表较长或预算较小，一些单元格可能会显示为计划中，直到预算有空间。
- [ ] **只读检查正常。** 在 **配额** 标签页上，配额可以加载。在 **Capacity Block** 标签页上，对一个区域和一个 P5 类型的搜索以方案或 `没有匹配的 Capacity Block。` 结束，而不是错误行。屏幕上没有一直保留带有 **重试** 的红色消息。
- [ ] **一次测试启动正常**（如果 `enableTestLaunch=true`）。在 **按需与 Spot** 标签页上，在一个有默认 VPC 的区域中对最小的组运行一次测试启动（**Spot 分数** 标签页区域详情中的 **测试启动** 按钮会跳转到那里），阅读结果，并确认几分钟后没有带有 `gpu-capacity-finder-probe` 标签的实例留下，且页面显示了今天的计数。
- [ ] **提醒邮件正常**（如果 `enableNotifications=true`）。添加一个提醒，确认来自 AWS Notifications 的订阅邮件，并检查 **上次检查** 在下一次检查后是否更新。
- [ ] **用户了解页面不做什么。** 它不创建预留，也不购买 Capacity Block。用户在 EC2 控制台中购买的 Capacity Block 是一次购买，而通过 **使用 CLI 预留** 复制的命令会创建一个 Capacity Reservation，从创建起以按需费率计费，直到它结束或用户取消为止。
- [ ] **您知道日志的位置。** 记下[监控](#监控)中的 `ApiLogs`、`ScoreUpdaterLogs`、`CleanupLogs` 和 `NotifierLogs` 日志组名称。
- [ ] **您知道拆除的顺序。** 请参阅[拆除](#拆除)。
