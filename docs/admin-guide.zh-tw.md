# GPU Capacity Finder 管理員指南

> 本文件是[英文版管理員指南](admin-guide.md)的直譯，未增減任何內容。如有出入，以英文版為準。

如果您在自己的 AWS 帳戶中部署並執行 GPU Capacity Finder，這份指南就是為您撰寫的。此頁面協助您的使用者在 Amazon Elastic Compute Cloud（Amazon EC2）上尋找圖形處理器（GPU）容量。本指南涵蓋部署、設定、使用者、監控、資料、安全性和拆除。使用此頁面的人另有一份[使用者指南](user-guide.zh-tw.md)。[README](../README.zh-tw.md) 是架構、含預設值與範圍的設定表，以及各項限制的參考文件。

此頁面是決策輔助工具：它讀取容量訊號，並執行一次有界限的測試啟動。它不管理執行個體、不建立 Capacity Reservation，也不購買 Capacity Block；使用者在 EC2 主控台購買 Capacity Block，而頁面會向使用者顯示可複製的 Capacity Reservation AWS Command Line Interface（AWS CLI）命令（透過**隨需與 Spot**索引標籤上的**使用 CLI 預留**取得），由使用者以自己的憑證執行。

## 建立的資源

[架構圖](../architecture-diagram.png)有自己的步驟編號。本指南對 DynamoDB 資料表使用以下名稱（括號內為堆疊中的邏輯 ID）：探測歷史資料表（`Probes`）、快取資料表（`OfferingCache`）和提醒資料表（`Subscriptions`）。

| # | 資源 | 說明 |
|---|---|---|
| 1 | Amazon CloudFront 分佈與私有 Amazon Simple Storage Service（Amazon S3）儲存貯體 | 透過來源存取控制（origin access control）提供靜態頁面。儲存貯體封鎖公開存取。 |
| 2 | Amazon Cognito 使用者集區、應用程式用戶端和登入網域 | 自助註冊已關閉，並要求以時間為基礎之一次性密碼（TOTP）的多重要素驗證（MFA）。網域前綴來自 `stackName`。 |
| 3 | 帶有 JSON Web Token（JWT）授權方的 Amazon API Gateway 超文字傳輸協定（HTTP）API | 拒絕沒有有效 ID 權杖的請求。階段限流為 `apiRateLimit`（預設每秒 10 個請求），突發量為該值的三倍，由每位使用者共用。 |
| 4 | AWS Lambda API 函式及其 AWS Identity and Access Management（IAM）角色 | 讀取訊號和定價、讀取快取的 Spot 分數、執行測試啟動，並管理提醒。1024 MB 記憶體，25 秒逾時。 |
| 5 | 依 Amazon EventBridge 排程（每小時第 0、15、30 和 45 分鐘）執行的 Spot 分數更新 Lambda 函式 | 建立 Spot 分數群組並使分數保持最新。它是唯一會建立或變更 Spot 分數請求的程式碼。256 MB，5 分鐘逾時。請參閱 [Spot 分數如何保持最新](#spot-分數如何保持最新)。 |
| 6 | DynamoDB 探測歷史資料表 | 測試啟動記錄在 30 天後過期。此資料表也保存冷卻鎖定、每位使用者的每日計數器，以及整個部署的每日計數器。 |
| 7 | 每 15 分鐘排程執行的清理 Lambda 函式，以及帶有自訂資源的堆疊刪除拆除函式 | 僅在 `enableTestLaunch=true` 時建立。清理函式會在每個已啟用的區域中，終止超過 10 分鐘的測試執行個體，並刪除超過 1 小時的測試啟動範本。拆除函式會在您刪除堆疊時執行一次。 |
| 8 | DynamoDB 快取資料表 | 共用的快取項目：每個區域提供的內容、Spot 分數群組與分數、Spot 和隨需價格、配額，以及區域清單。請參閱[備份與資料](#備份與資料)。 |
| 9 | DynamoDB 提醒資料表、加密的 Amazon Simple Notification Service（Amazon SNS）主題、通知 Lambda 函式及其排程 | 僅在 `enableNotifications=true`（預設值）時建立。 |
| 10 | 各函式的 CloudWatch 警示 | 每個函式的錯誤、接近逾時的執行和限流，以及測試啟動安全網的兩個警示。設定 `alarmTopicArn` 時，它們會通知該主題，否則沒有任何動作。請參閱[警示](#警示)。 |

API、更新、清理、通知和拆除函式各有一個保留期為一個月的日誌群組。堆疊中也包含 AWS Cloud Development Kit（AWS CDK）輔助函式，負責將頁面複製到儲存貯體，以及在刪除時清空儲存貯體。

探測安全群組（測試啟動使用的每個虛擬私有雲端（VPC）各一個）會在執行時建立。拆除函式會在您刪除堆疊時移除它們；請參閱[拆除](#拆除)。使用者以複製的命令建立的 Capacity Reservation，以及使用者在 EC2 主控台購買的 Capacity Block，屬於使用者，而不屬於堆疊。

## 先決條件

- 一個 AWS 帳戶，以及可以部署堆疊的憑證。請使用非生產帳戶。
- Python 3.12 或更新版本，用於 CDK 應用程式和本機測試（Lambda 函式在 Python 3.13 上執行）；Node.js 20 或更新版本（已以 22 測試）；AWS CLI 第 2 版；以及最新的 AWS CDK CLI：`npm install -g aws-cdk`。
- 除非命令另有說明，請在範例根目錄（包含 `deploy.sh` 的資料夾）執行本指南中的命令。
- CDK 命令（`cdk bootstrap`、`cdk diff`、`cdk deploy`、`cdk destroy`）會執行 Python CDK 應用程式，因此需要本範例的 Python 環境。`deploy.sh` 會在 `.venv` 中建立該環境，但只在指令碼內啟用它。在執行其他 CDK 命令之前，請在您的 shell 中執行 `source .venv/bin/activate`。
- 帳戶已在您部署的區域中為 CDK 執行 bootstrap。首次部署之前，環境尚不存在：`python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt && cdk bootstrap`。
- 若要執行測試啟動：在使用者進行測試的每個區域中，需要一個含有預設子網路的預設 VPC。測試啟動會使用每個可用區域的預設子網路。沒有預設 VPC 的區域會顯示 `此區域沒有預設 VPC`，其測試啟動保持關閉。若要建立預設 VPC，請執行 `aws ec2 create-default-vpc --region <Region>`；此呼叫會在該區域建立 VPC、子網路和網際網路閘道，因此請只在您接受這些資源的區域執行。
- 若要執行 Spot 測試啟動：需要 Spot 存取權限和 Spot 服務連結角色。如果帳戶從未使用過 Spot，請建立一次該角色：

  ```bash
  aws iam create-service-linked-role --aws-service-name spot.amazonaws.com
  ```

  如果您的帳戶無法使用 Spot，請以 `-c testLaunchAllowSpot=false` 部署。

## 部署

### 使用部署指令碼

```bash
export AWS_PROFILE=<profile>   # or sign in another way
export AWS_REGION=<region>
./deploy.sh
```

指令碼會檢查 `python3`、`aws` 和 `cdk`，在 `.venv` 中建立 Python 環境，並顯示 CDK CLI 版本、帳戶、身分和區域。它會請您確認，然後執行 `cdk deploy`。請在指令碼名稱之後傳入選項：

```bash
./deploy.sh -c testLaunchAllowSpot=false -c excludeRegions=me-south-1
```

設定 `ASSUME_YES=1` 可略過指令碼的確認提示。`cdk` 命令仍會請您核准 IAM 變更；在指令碼名稱之後加上 `--require-approval never` 也可略過該提示。部署完成時，指令碼會輸出網站 URL、使用者集區 ID，以及建立第一位使用者的命令範本。如果 `cdk deploy` 失敗，且其輸出表示 CLI 與 CDK 程式庫不相容，指令碼會輸出提示，建議以 `npm install -g aws-cdk@latest` 升級 CDK CLI。

### 使用 cdk deploy

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cdk deploy -c excludeRegions=me-south-1   # options are optional
```

堆疊會部署到您 CDK 環境的預設帳戶和區域。它會輸出 `SiteUrl` 和 `UserPoolId`。若要之後讀取它們（本指南中的命令使用預設堆疊名稱 `gpu-capacity-finder`；如果您設定了 `stackName`，請替換它）：

```bash
aws cloudformation describe-stacks --region <StackRegion> --stack-name gpu-capacity-finder \
  --query "Stacks[0].Outputs" --output table
```

第一批 Spot 分數群組會在部署後的第一個頁面請求時建立，而不是在下次每日更新時。頁面會短暫顯示建立中的狀態；請在一分鐘後重新載入。

### 每個帳戶一個部署

每個 AWS 帳戶只使用一份本範例的部署。探測資源使用固定的標籤名稱，而清理和拆除函式會掃描帳戶的已啟用區域，而不只是堆疊區域。在同一帳戶中的第二個部署（即使在另一個區域）會清理掉第一個部署的測試資源。AWS 對 Spot 放置分數請求的限制也由整個帳戶共用，因此兩個部署會使用同一份預算。若有多個帳戶，請在每個帳戶各部署一份。

## 設定

以 `-c key=value` 傳入每項設定。[README 的「設定」一節](../README.zh-tw.md#設定)列出每個鍵及其預設值和範圍。堆疊不會在兩次部署之間記住這些值，因此請在每次部署時傳入相同的選項，或將它們放在 `cdk.json` 的 `context` 區塊中。

| 鍵 | 何時變更 |
|---|---|
| `stackName` | 您想使用預設值以外的堆疊名稱。請將相同的 `-c stackName=<name>` 傳給 `cdk deploy` 和 `cdk destroy`。每個 AWS 帳戶只使用一份本範例的部署：請參閱[每個帳戶一個部署](#每個帳戶一個部署)。 |
| `excludeRegions` | 某個已啟用的區域沒有回應，或您不想使用它。請參閱[區域](#區域)。 |
| `spotScoreFamilies` | 您使用預設清單以外的 GPU 系列。請參閱[選擇 Spot 分數系列](#選擇-spot-分數系列)。 |
| `spotScoreConfigsPerDay` | 您想限制本範例使用中的不同 Spot 分數請求數量。當您的帳戶有其他 Spot 分數呼叫時請調低，因為 AWS 的限制由整個帳戶共用。請參閱 [Spot 分數預算](#spot-分數預算)。 |
| `spotScoreCacheMinutes` | 15 到 240。更新函式每 15 分鐘重複每個已接受的請求；大於 15 的值會降低重複頻率（回應的經過時間達到此值減 5 分鐘時才會重複）。 |
| `enableTestLaunch` | 設為 `false` 以建立唯讀部署。此時堆疊沒有啟動或終止權限、沒有清理函式、沒有拆除自訂資源，頁面也沒有測試啟動面板。測試啟動是唯一會啟動執行個體的功能。在現有堆疊上關閉它，會移除清理函式和拆除自訂資源，但不會移除探測資源；請先執行[找出殘留資源](#找出殘留資源)中的命令，並刪除探測安全群組和啟動範本，或在更新後再刪除。 |
| `testLaunchAllowSpot` | 當帳戶無法使用 Spot，或您只想要隨需測試啟動時，設為 `false`。Spot 分數和價格檢視會保留。 |
| `testLaunchCooldownMinutes` | 調高以減少啟動次數。每個時間範圍內，每個區域、市場和群組（或尺寸）跨使用者只會有一個請求啟動，因此冷卻時間不會限制使用量；每日上限才會。 |
| `maxTestLaunchesPerUserPerDay` | 調低以限制每位使用者；`0` 表示沒有上限。計數依使用者和 UTC 日計算（在 00:00 UTC 重設），跨區域、群組和市場。EC2 處理過的測試會計入，包括 EC2 因沒有容量或配額上限而拒絕的測試，以及結束時沒有明確結果的測試。以下情況不會計入：快取的冷卻時間結果、進行中的回應、被 EC2 限流或從未送出的請求、伺服器時間不足而無法開始的請求，以及在 Fleet 請求之前就失敗的情況（配額太低、沒有預設 VPC、AWS 錯誤）。 |
| `maxTestLaunchesPerDay` | 調低以限制整個部署；`0` 表示沒有上限。所有使用者共用一個計數器，依 UTC 日計算，計數規則相同。頁面會顯示 `今天的測試啟動：N／M（所有使用者）` 以及使用者自己的計數。 |
| `enableNotifications` | 設為 `false` 以移除電子郵件提醒和**提醒**索引標籤。之後再關閉它，會刪除提醒資料表以及 SNS 主題和其訂閱。 |
| `notifyIntervalMinutes` | 調高以降低檢查頻率。較低的值能更快捕捉短暫出現的方案，但通知函式會更頻繁執行。 |
| `maxSubscriptionsPerUser` | 當使用者關注許多區域與類型組合時調高；每個提醒都會增加每次通知函式執行的檢查量。 |
| `offeringCacheHours` | 調高以減少 `DescribeInstanceTypeOfferings` 呼叫；當使用者需要更快看到新提供的類型時調低。 |
| `quotaCacheMinutes` | 調高以減少 Service Quotas 和 CloudWatch 呼叫；當使用者需要更快看到上限或用量變更時調低。 |
| `apiRateLimit` | 許多使用者同時工作，且頁面經常顯示 `服務目前忙碌中。`。階段會將此值及其三倍的突發量，套用到所有使用者合計的請求。 |
| `sessionHours` | 使用者必須太頻繁地重新登入，或您想要較短的工作階段。請參閱[變更登入工作階段長度](#變更登入工作階段長度)。 |
| `alarmTopicArn` | 您想透過電子郵件或其他 SNS 訂閱接收堆疊的 CloudWatch 警示。請參閱[警示](#警示)。 |

### 區域

每項功能都使用同一份區域清單：您的帳戶已啟用的商業區域，扣除 `excludeRegions`。供應、價格、配額、Capacity Block、Spot 分數、測試啟動、清理函式和拆除函式都使用它。此清單來自 EC2 `DescribeRegions`，並在快取資料表中儲存 24 小時，因此您啟用的區域會在一天內出現，不需要重新部署。如果呼叫失敗，會繼續使用已儲存的清單。

`excludeRegions` 是以逗號分隔的清單，最多 40 個區域。請將它用於沒有回應或您不想呼叫的已啟用區域。本範例不會呼叫已排除的區域。Spot 分數更新函式會在下次每日更新時，從其分數群組中移除已排除的區域，而頁面在那之前不會顯示它。

`us-east-1` 無法排除：每個 Spot 分數請求都會送到 `us-east-1` 端點，因此部署會拒絕此設定。若要恢復已排除的區域，請將它從 `excludeRegions` 中移除並重新部署。它會在下次每日更新時加入其分數群組，在那之前，頁面會顯示 `下次每日更新後取得 Spot 分數`。

### 選擇 Spot 分數系列

AWS 會限制一個帳戶每天可發出的不同 Spot 放置分數請求數量，而這項限制由帳戶中的所有項目共用。每個取得分數的系列，每種 GPU 數量以及每 10 個提供它的區域，就會使用一個請求。為每個區域中的每種 GPU 類型取得分數會超過這項限制，因此您只需在 `spotScoreFamilies` 中列出您打算使用的 GPU 系列。預設清單為 `g6e`、`g7`、`g7e`、`p5`、`p5e`、`p5en`、`p6-b200` 和 `p6-b300`（模式為 `g6e.*`、`g7.*` 等）。

若要自訂清單，請以 `-c` 傳入完整清單，並加上引號，讓 shell 不處理星號：

```bash
./deploy.sh -c 'spotScoreFamilies=g6e.*,p5en.*,p6-b200.*'
```

每個模式指定一個系列及其尺寸（`p5.*`）或萬用字元（`g7*`），包含點、星號或問號，並指定一個 `g` 或 `p` GPU 系列。清單最多包含 20 個模式。`g7.*` 會符合 `g7.xlarge`，但不會符合 `g7e.xlarge`。模式以系列字母及其世代數字開頭，例如 `g7*` 或 `p5.*`，因此部署會拒絕 `g*` 或 `g?.*` 之類的模式。

哪些會自行更新，哪些不會：

- 已取得分數之系列的**新尺寸**，會在下次每日更新時加入其群組。
- **新區域**，或開始提供某個尺寸的區域，會在下次每日更新時加入群組。在那之前，頁面會在該儲存格顯示 `下次每日更新後取得 Spot 分數`。
- **新系列**在您將其加入 `spotScoreFamilies` 並重新部署之前，不會取得 Spot 分數。變更清單的部署會在第一個請求時建立新群組，而不是在隔天。您移除之系列的群組會停止重複，而其請求在 24 小時過去之前，仍會計入帳戶的 AWS 限制。
- **供應、價格、配額、測試啟動和 Capacity Block** 依 `g` 或 `p` 命名規則（以 `g` 或 `p` 加上數字開頭的系列名稱）涵蓋每個 GPU 系列，並自行納入新系列。

Spot 分數提醒需要屬於已取得分數之系列的類型。API 會拒絕已取得分數之系列以外類型的 Spot 分數提醒。

### Spot 分數如何保持最新

**分數群組**是一個系列搭配一種 GPU 數量，例如 `g6e · 4 個 GPU`（鍵為 `g6e/4`）。GPU 數量來自 `DescribeInstanceTypes`，每種類型儲存一次。群組的尺寸是該系列中，具有該 GPU 數量、且任一已啟用區域有提供的每個尺寸；這份清單只會增加。群組的區域清單包含至少提供其中一個尺寸的區域，分成每塊最多 10 個區域的區塊。本範例每個區塊傳送一個請求，因此群組分數是任一列出尺寸可被滿足的區域層級機會，而不是單一尺寸的機會。

這些清單只會附加，讓請求每天保持相同。新區域會在最後一個區塊還有空間時加入該區塊，之後再開啟新區塊，順序依首次出現日期，再依名稱。停止提供的區域會留在其區塊中。方案查詢失敗的區域不會造成任何變更。唯一的例外是 `excludeRegions` 中的區域：它會在下次每日更新時離開其區塊。

| 執行 | 時間 | 作用 |
|---|---|---|
| 每日更新 | 00:30 UTC 當下或之後的第一次執行 | 讀取已啟用的區域、它們提供的內容，以及新尺寸的 GPU 數量。將新尺寸、區域和群組加入已儲存的清單，移除已排除的區域，並向 AWS 送出預算容許的請求。預算容納不下的請求會保持在規劃中狀態。 |
| 初始建立 | 首次部署後的第一個請求，或變更 `spotScoreFamilies` 的部署之後的第一個請求 | 立即執行與每日更新相同的工作。API 會在背景啟動更新函式一次，頁面會顯示建立中的狀態，直到群組存在為止。 |
| 重新整理 | 每 15 分鐘，於第 0、15、30 和 45 分鐘 | 重複 AWS 在過去一天內接受的每個請求（重複不另計，並傳回新的分數），儲存結果以及每個群組的一筆歷史讀數，並在預算有空間時核准規劃中的請求。它也會儲存每個提供已取得分數之類型的區域的最新 Spot 價格。 |

頁面載入、**重新整理**按鈕和電子郵件提醒只會讀取快取或重複已接受的請求。它們不會建立或變更請求或群組。**重新整理**對每個群組，所有使用者合計每 60 秒最多重複一次。

儲存格會顯示其區域的狀態：已取得分數、AWS 未提供分數、等待下次重新整理、規劃中（請求正在等候預算空間）、受限（AWS 拒絕了請求，或新請求暫停），或在下次每日更新後。`GET /api/config` 會傳回排程和各項計數：`spotScoreState`、`spotScoreGroups`、`spotScoreConfigsNeeded`、`spotScoreConfigsPerDay`、`spotScoreConfigsInUse`、`remainingNewConfigs`、`spotScoreLimitedUntil`、`lastDailyUpdateAt`、`nextDailyUpdateAt`、`nextWarmAt` 和 `dailyUpdateUtc`。

### Spot 分數預算

AWS 會限制一個帳戶在 24 小時內可發出的不同 Spot 放置分數請求設定數量。AWS 不公布這項限制的大小，您也無法提高它。重複 AWS 在過去 24 小時內接受的設定是允許的。本範例傳送的每個請求都使用 `TargetCapacity` 1，且此值不是設定項目（不同的值就是不同的設定）。當區塊的區域清單或其群組的尺寸變更時，區塊的請求就會變更。將區域加入有空間的區塊，會為該區塊建立一個新設定。新尺寸會變更其群組每個區塊的請求，因此會為群組的每個區塊各建立一個新設定。

`spotScoreConfigsPerDay`（預設 40，範圍 1 到 100）是本範例在滾動的 24 小時內保持使用中的不同設定的最大數量。每日更新和重新整理會在此範圍內核准新請求，容納不下的請求會保持在規劃中狀態，直到舊請求過期而空出空間。本範例會在快取資料表中計算自己的設定：

- `spscfg#<fingerprint>` 是每個已傳送設定的一個項目：請求內容、AWS 首次接受它的時間和最後使用時間、最後的結果，以及被拒絕後封鎖至何時。它會在最後一次寫入的 8 天後過期。
- `#sps-budget` 是預算的一個項目：過去 24 小時內的新設定、被拒絕後的暫停，以及已登錄設定的索引。新設定會在請求送出之前，以條件式寫入佔用一個位置，因此兩個函式副本無法同時佔用最後一個位置。如果無法讀取或寫入此項目，就不會傳送任何新設定。

AWS 拒絕新設定之後，該設定會被封鎖 24 小時，且本範例在 24 小時內不會請求任何新設定。暫停期間，已接受的設定會繼續重複。如果此部署在拒絕之前的 24 小時內已建立至少 3 個新設定，每日預算也會在 7 天內維持在該數量；如果較少，本範例會將預算視為已被帳戶中的其他項目用完，只進行封鎖和暫停。

較早版本的舊設定不會再次傳送，但在它們離開 24 小時時間範圍之前，會計入 `spotScoreConfigsInUse`。

**歷史項目。** 每個群組在快取資料表中保留一個小型歷史項目 `spshist#<group>#regions`。它最多保留 48 筆讀數，捨棄超過 24 小時的讀數，每 5 分鐘最多取一筆讀數，並在 2 天後過期。歷史讀取或寫入失敗時會記錄到日誌，不會使請求失敗。

### 測試啟動

測試啟動會向 EC2 請求一個執行個體，尺寸為分數群組中的任一尺寸（或一個選定的尺寸），位於該區域的某個可用區域，並立即終止它。API 函式會：

1. 依序檢查以下防護條件：`enableTestLaunch`、區域（已啟用且未排除）、市場（Spot 需要 `testLaunchAllowSpot`）、GPU 命名規則、呼叫者、冷卻時間、每位使用者的上限，以及整個部署的上限。在 Fleet 請求之前就失敗的情況（配額太低、沒有預設 VPC、安全群組衝突、AWS 錯誤）會退回兩項計數和冷卻時間位置。
2. 找出該區域的每個預設子網路（每個可用區域一個），以及群組中該區域有提供、且符合該市場中限制此系列之配額可用 vCPU 的每個尺寸。如果沒有任何尺寸符合，回應為 `quota-too-low`，且不會發出啟動請求。未知的配額或 vCPU 數量不會排除尺寸。
3. 為此次測試建立一個啟動範本（探測標籤、自行關機的使用者資料、沒有公有位址、探測安全群組），以及一個類型為 `instant`、包含一個執行個體的 EC2 Fleet。Spot 使用 `price-capacity-optimized`。隨需使用 `lowest-price`，且不會啟動到開放的 Capacity Reservation 中，因此它測量的是可用容量。
4. 在 Fleet 回應之後立即終止執行個體並刪除啟動範本。instant Fleet 不需要刪除呼叫：EC2 會自行移除它。

回應為已啟動（附上 EC2 挑選的可用區域和尺寸）、沒有容量、配額、不支援或錯誤。頁面也會列出該區域中每個群組和市場過去 24 小時的啟動結果，涵蓋每位使用者且不含使用者名稱。

安全群組 `gpu-capacity-finder-probe` 沒有傳入規則。函式會在第一次測試啟動時，於每個 VPC 建立一次該安全群組，並加上標籤 `gpu-capacity-finder-probe-sg`。

兩個每日計數器位於探測歷史資料表中。每位使用者的計數器為 `#probe-count#...`，整個部署的計數器為 `#probes#YYYY-MM-DD`，以條件式寫入決定最後一個位置的競爭。計數器項目會在其當日結束後兩天過期。API 角色只能更新這些分割區。

### 調整 API 呼叫量

Lambda 函式會在您的帳戶中呼叫 EC2、Service Quotas、CloudWatch 和 AWS Price List API，因此儀表板的呼叫會與您的其他工作負載一起計入帳戶層級的 API 配額，例如 EC2 Describe API 請求限制。快取資料表由每位使用者共用，因此一個結果可在其快取時間結束前供每個人使用。調高快取時間會減少呼叫並顯示較舊的資料；調低則相反。

每個 API 容器也會在記憶體中保留快取項目 60 秒，因此另一個容器寫入的重新整理結果，最多可能需要 60 秒才會在此容器中顯示。

頁面會在索引標籤開啟時讀取快取資料。當畫面上的資料超過其快取時間時，頁面會在使用者開啟索引標籤或回到頁面時重新讀取，而不會強制重新整理。**重新整理**會傳送 `refresh=true`。對於 Spot 分數，這會重複群組已接受的請求；對於其他資料，則會略過該項目的快取。每個快取項目都有自己的強制重新整理鎖定，即快取資料表中的條件式寫入，因此每個項目（例如一個分數群組，或一個區域的配額）每分鐘最多只會再向 AWS 查詢一次。在這一分鐘內的重新整理會傳回快取的結果和剩餘秒數，頁面也會加以說明。

| 資料 | 快取未提供結果時呼叫的 AWS API | 快取與時間 | 略過快取的方式 |
|---|---|---|---|
| 每個區域提供的內容（方案矩陣、尺寸清單、請求檢查） | EC2 `DescribeInstanceTypeOfferings`，每個已啟用區域兩次分頁呼叫 | 快取資料表（`offerings#<Region>`），`offeringCacheHours`，另有 60 秒的記憶體層。超過該時間範圍後，仍會提供已儲存的結果，同時由一個請求再次向 EC2 查詢（請參閱[更新期間提供的回答](#更新期間提供的回答)）；項目保留 7 天 | 頁面上無 |
| 支援 Capacity Block 的執行個體類型 | EC2 `DescribeInstanceTypes`，篩選條件為 `supported-usage-class=capacity-block`，每個區域一次呼叫 | 快取資料表（`cbtypes#<Region>`），每天再次查詢一次，保留 7 天。如果呼叫失敗，會使用最後一份清單，否則使用 `lambda/capacity_blocks.py` 中的系列清單 | 頁面上無 |
| 已啟用區域的清單 | EC2 `DescribeRegions`（已啟用的商業區域） | 快取資料表（`regions#enabled`），24 小時，保留 7 天。更新函式也會在每次每日更新時即時讀取 | 頁面上無 |
| 分數群組與 Spot 分數 | EC2 `GetSpotPlacementScores`，送到 `us-east-1`，只由分數更新函式呼叫（**重新整理**也會針對已接受的請求呼叫） | 分數群組儲存在 `#sps-groups`（不過期）；每個結果在快取資料表中保留 `spotScoreCacheMinutes` 加 10 分鐘。頁面和通知函式讀取相同的項目 | **重新整理**會重複群組已接受的請求 |
| Spot 價格 | EC2 `DescribeSpotPriceHistory`，由更新函式在每次執行時，為每個已取得分數的類型，每個區域呼叫一次 | 快取資料表（`price-spot#<Region>`），30 分鐘。已取得分數之系列以外的類型，會在使用者查詢時有自己的呼叫和項目 | 無；更新函式會使其保持最新 |
| 隨需定價 | Price List `GetProducts`，送到 `us-east-1` | 快取資料表，24 小時（Price List 沒有相符項目時為 1 小時；失敗的呼叫不會被快取） | 無 |
| **配額**（一次一個區域） | Service Quotas `ListServiceQuotas` 和 `ListAWSDefaultServiceQuotas`、CloudWatch `GetMetricData` | 快取資料表，`quotaCacheMinutes` | **重新整理**，每個區域 |
| **Capacity Block** 搜尋與**最大容量** | EC2 `DescribeCapacityBlockOfferings`，每組區域與類型組合一個請求（**最大容量**最多分三個請求執行，EC2 呼叫次數有上限，每個請求都遠在 25 秒的函式逾時之內） | 不快取。一次搜尋最多同時執行六組組合 | **重新搜尋**會再次執行上次的搜尋 |
| 測試啟動歷史記錄和提醒 | DynamoDB 查詢 | 不快取 | **重新整理** |

當 GET 請求收到 HTTP 429 或 503，或因網路而失敗時，頁面最多會重試三次，分別等待約 0.6、1.2 和 2.4 秒再加上隨機的額外時間，或在回應含有 `Retry-After` 標頭時等待其值（最多 8 秒）。之後，頁面會顯示 `服務目前忙碌中。請稍候，然後選擇「重試」。`，而 Capacity Block 搜尋會在其他組合完成後再嘗試該組合一次。它不會重試每日測試啟動上限（`probe-daily-limit`、`daily-cap-all`），也不會重複 POST、PUT 或 DELETE 請求，因此測試啟動只會執行一次。

### 變更登入工作階段長度

`sessionHours`（預設 12）是從一次完整登入（含密碼和 MFA 驗證碼）到下一次完整登入的最長時間。它會設定 Cognito 應用程式用戶端的重新整理權杖到期時間。ID 權杖和存取權杖的效期為 60 分鐘，是固定值；在 ID 權杖即將到期前，頁面會以重新整理權杖取得新的權杖，而不經過登入頁面。當 `sessionHours` 結束時，頁面會將使用者帶到登入頁面。

頁面只為目前的瀏覽器索引標籤保留權杖，因此關閉索引標籤會在 `sessionHours` 結束之前就結束工作階段。若要變更此值，請以新值和您的其他選項重新部署：

```bash
./deploy.sh -c sessionHours=24
```

此更新只會變更 Cognito 應用程式用戶端；它不會取代使用者集區或其使用者。在更新之前開始的工作階段，可能會維持原本的長度，直到使用者重新登入為止。如果您改在 Amazon Cognito 主控台中變更 **Refresh token expiration**，下次部署會以部署命令中的值覆寫它。

## 管理使用者

請在堆疊區域中執行這些命令。使用者以其電子郵件地址登入。

### 建立使用者

```bash
aws cognito-idp admin-create-user \
  --region <StackRegion> \
  --user-pool-id <UserPoolId> \
  --username user@example.com \
  --user-attributes Name=email,Value=user@example.com Name=email_verified,Value=true \
  --desired-delivery-mediums EMAIL
```

Cognito 會以電子郵件寄送臨時密碼。首次登入時，使用者會設定密碼並註冊驗證器應用程式。請將網站 URL 和[使用者指南](user-guide.zh-tw.md)傳送給使用者。請如上所示設定 `email_verified=true`：API 會拒絕未驗證地址的提醒。

您也可以在 AWS 管理主控台中建立使用者。開啟 Amazon Cognito，選擇 **User pools**，然後開啟您的集區（`deploy.sh` 會輸出使用者集區 ID）。選擇 **Users**，然後選擇 **Create user**。選擇 **Send an email invitation**，輸入電子郵件地址，勾選 **Mark email address as verified**，選擇 **Generate a password**，然後選擇 **Create user**。臨時密碼的有效期為 7 天。頁面的**使用者**索引標籤顯示相同的步驟，並附有使用者集區的直接連結。

### 重新傳送邀請或重設密碼

如果臨時密碼在首次登入之前就已過期，請重新傳送：

```bash
aws cognito-idp admin-create-user --region <StackRegion> --user-pool-id <UserPoolId> \
  --username user@example.com --message-action RESEND --desired-delivery-mediums EMAIL
```

對於之前登入過的使用者，請重設密碼。Cognito 會以電子郵件寄送驗證碼，使用者在下次登入時設定新密碼：

```bash
aws cognito-idp admin-reset-user-password --region <StackRegion> \
  --user-pool-id <UserPoolId> --username user@example.com
```

遺失驗證器應用程式的使用者無法登入。本範例尚未以 MFA 重設進行測試。一個選項是關閉使用者的 TOTP 設定，讓使用者重新註冊；請先在您的集區中測試：

```bash
aws cognito-idp admin-set-user-mfa-preference --region <StackRegion> \
  --user-pool-id <UserPoolId> --username user@example.com \
  --software-token-mfa-settings Enabled=false,PreferredMfa=false
```

另一個選項是[移除使用者](#移除使用者及其殘留項目)，然後重新建立該使用者。新使用者會取得新的使用者 ID（`sub`），因此舊的提醒不會延續。在主控台中，開啟使用者集區，選擇 **Users**，依電子郵件地址開啟使用者，然後在詳細資料頁面上使用重新傳送、**Reset password** 或驗證器選項。選單標籤可能因主控台版本而異。

### 停用使用者

```bash
aws cognito-idp admin-disable-user --region <StackRegion> \
  --user-pool-id <UserPoolId> --username user@example.com
```

已停用的使用者無法登入。在您停用使用者之前核發的 ID 權杖，在到期之前（最多 60 分鐘）仍然有效，因為 API 檢查的是權杖簽章和到期時間。停用不會停止使用者的提醒電子郵件。在主控台中，開啟使用者的詳細資料頁面，然後選擇 **Disable user access**。

### 移除使用者及其殘留項目

在使用者集區中刪除使用者，不會移除該使用者的提醒或 SNS 訂閱。請先移除它們。使用者的執行個體類型清單存放在使用者的瀏覽器中，而不在堆疊中。

1. 找出使用者的 `sub`：

   ```bash
   aws cognito-idp admin-get-user --region <StackRegion> --user-pool-id <UserPoolId> \
     --username user@example.com --query "UserAttributes[?Name=='sub'].Value" --output text
   ```

2. 找出提醒資料表的名稱（邏輯 ID 以 `Subscriptions` 開頭）：

   ```bash
   aws cloudformation describe-stack-resources --region <StackRegion> --stack-name gpu-capacity-finder \
     --query "StackResources[?ResourceType=='AWS::DynamoDB::Table'].[LogicalResourceId,PhysicalResourceId]" \
     --output table
   ```

3. 如果提醒已開啟，請列出提醒資料表（`<SubscriptionsTable>`）中該使用者的項目，取消訂閱該地址，並刪除每個項目：

   ```bash
   aws dynamodb query --region <StackRegion> --table-name <SubscriptionsTable> \
     --key-condition-expression "userId = :u" \
     --expression-attribute-values '{":u":{"S":"<sub>"}}' \
     --query "Items[].[sk.S, subscriptionArn.S]" --output text
   aws sns unsubscribe --region <StackRegion> --subscription-arn <SubscriptionArn>   # from the #profile item
   aws dynamodb delete-item --region <StackRegion> --table-name <SubscriptionsTable> \
     --key '{"userId":{"S":"<sub>"},"sk":{"S":"<sk>"}}'                               # repeat per item, including #profile
   ```

4. 刪除使用者：

   ```bash
   aws cognito-idp admin-delete-user --region <StackRegion> \
     --user-pool-id <UserPoolId> --username user@example.com
   ```

測試啟動記錄會保留執行每次測試之人員的使用者名稱，並在 30 天後過期。

## 升級

若要套用本範例的新版本或設定變更，請以相同的選項再次執行部署：

```bash
source .venv/bin/activate      # needed for cdk diff
cdk diff -c <same options>     # optional: preview the change
./deploy.sh -c <same options>
```

部署會更新函式和頁面，並使 CloudFront 快取失效。使用者、網站 URL 和資料表資料會保留，但屬於您關閉之功能的資料表除外。如果您變更 `stackName`，CDK 會建立一個新的、獨立的堆疊。

### 升級至 Spot 分數群組

此版本變更了 Spot 分數、區域和測試啟動的運作方式。在您將它部署到較早的堆疊上之前：

1. **移除或重新命名設定。** 部署會停止，並顯示指出該設定的訊息：
   - 已移除：`testLaunchRegions` 和 `regions`（測試啟動現在於每個已啟用的區域執行；請使用 `excludeRegions` 排除區域）、`regionScope`、`enableReservation`、`reservationHours`、`maxReservationsPerUser`、`apiBurstLimit`（現在為 `apiRateLimit` 的三倍）、`tokenMinutes`（現在為 60 分鐘），以及 `instanceTypes`。
   - 已重新命名：`instanceFamilies` 現在為 `spotScoreFamilies`（它現在只選擇取得 Spot 分數的系列；頁面涵蓋每個 GPU 系列），`enableSpotTestLaunch` 和 `enableSpotProbe` 現在為 `testLaunchAllowSpot`，`enableProbe` 現在為 `enableTestLaunch`，`probeCooldownMinutes` 現在為 `testLaunchCooldownMinutes`，`maxProbesPerUserPerDay` 現在為 `maxTestLaunchesPerUserPerDay`。三個 Spot 分數預算設定（`spotScoreNewConfigsPerDay`、`spotScoreReservePerDay`、`spotScoreMaxConfigs`）現在合併為單一的 `spotScoreConfigsPerDay`。
2. **可用區域分數已移除。** 頁面和 API 只顯示 GPU 群組的區域層級分數（`scope=az` 會傳回錯誤）。新的測試啟動記錄不帶分數；較舊的記錄保留其已儲存的分數。
3. **預期會有建立步驟。** 部署後的第一個請求會啟動分數更新函式，由它建立群組。在它完成之前，頁面會顯示建立中的狀態。舊的各類型 Spot 分數請求不會再次傳送。
4. **測試啟動已變更。** 測試啟動現在會針對群組或一個尺寸，在該區域的某個可用區域中提出請求，而冷卻時間以群組或尺寸、市場和區域為鍵。新的整個部署上限 `maxTestLaunchesPerDay`（預設 100）會生效；設為 `0` 即可關閉。API 角色會在已啟用的區域中取得測試啟動權限。探測執行個體類型的 IAM 條件現在為 `g*` 或 `p*`，因為 API 會在任何啟動之前檢查 GPU 系列規則。
5. **移除舊的部署旗標。** 請從您的部署命令中移除 `-c regions` 和 `-c regionScope`。之後測試啟動會涵蓋已啟用的區域扣除 `excludeRegions`。
6. **了解刪除或復原會做什麼。** 拆除自訂資源只在堆疊本身被刪除時才會動作：其函式會讀取自身堆疊的狀態，只在該狀態為 `DELETE_IN_PROGRESS` 時才移除探測資源。當更新移除該自訂資源（例如使用 `enableTestLaunch=false`），或 CloudFormation 復原失敗的升級時，函式不會移除任何項目。測試執行個體仍會在 5 分鐘後自行結束；請以[找出殘留資源](#找出殘留資源)檢查其他探測資源。復原到較舊版本之後，Spot 分數可能要等上一天，因為新程式碼的請求仍會計入 AWS 的限制。
7. **設定 `alarmTopicArn`。** 新版本新增了 CloudWatch 警示。傳入 `-c alarmTopicArn=<TopicArn>` 以接收通知；請參閱[警示](#警示)。

### 從具有容量保留或已儲存清單的版本升級

本範例的較早版本可以使用隨需 Capacity Reservation 保留容量，並將每位使用者的執行個體類型清單儲存在 DynamoDB 資料表中。此版本移除了這兩項功能。在您將它部署到這類堆疊上之前：

1. **取消保留。** 新版本無法列出或取消它們，而它們會持續計費直到結束時間。在每個已啟用的區域中：

   ```bash
   aws ec2 describe-capacity-reservations --region <Region> \
     --filters Name=tag:gpu-capacity-finder-reservation,Values=true Name=state,Values=active,pending,assessing \
     --query 'CapacityReservations[].CapacityReservationId'
   aws ec2 cancel-capacity-reservation --region <Region> --capacity-reservation-id <ReservationId>
   ```

2. **告知使用者他們的清單。** 部署會刪除已儲存清單資料表（`Watchlist`）和保留租約資料表（`Holds`）。使用者需要重新建立清單；頁面現在會將清單保留在各個瀏覽器中。
3. **刪除您不需要的備份。** 已移除的資料表啟用了時間點復原，因此 DynamoDB 會為每個資料表保留 35 天的系統備份（請參閱[拆除](#拆除)）。

## 成本控制檢查清單

資源在執行期間會產生費用。最大的費用來自測試啟動所啟動的 GPU 執行個體。

- [ ] 使用 `enableTestLaunch=false` 建立唯讀部署。
- [ ] 將 `maxTestLaunchesPerDay` 和 `maxTestLaunchesPerUserPerDay` 調低到您可接受的每日測試啟動次數，如果測試執行的頻率高於使用者所需，請調高 `testLaunchCooldownMinutes`。
- [ ] 讓 `spotScoreFamilies` 只包含您使用的系列。系列越少，Spot 分數請求就越少。
- [ ] 提醒使用者，他們購買或建立的項目是堆疊不追蹤的計費資源：在 EC2 主控台購買的 Capacity Block 是一項購買，而 Capacity Reservation（來自**使用 CLI 預留**提供的命令）從建立起就依隨需費率計費，直到它結束或使用者取消為止。
- [ ] 調高 `notifyIntervalMinutes`、`offeringCacheHours` 和 `quotaCacheMinutes`，以減少排程呼叫和 API 呼叫。只在使用者需要更新的資料時才調低快取時間，並預期呼叫會增加。
- [ ] 讓 `apiRateLimit` 不高於使用者所需。儀表板的突發請求會計入您帳戶的 EC2 Describe API 限制。
- [ ] 在大量使用後檢查殘留的測試執行個體，並為兩個清理警示附加 SNS 動作（請參閱[警示](#警示)），讓您得知清理失敗。
- [ ] 不再需要堆疊時，請將其拆除。

## 監控

### 日誌群組

函式在堆疊區域中執行，並寫入保留期為一個月的日誌群組。找出名稱：

```bash
aws cloudformation describe-stack-resources --region <StackRegion> --stack-name gpu-capacity-finder \
  --query "StackResources[?ResourceType=='AWS::Logs::LogGroup'].[LogicalResourceId,PhysicalResourceId]" \
  --output table
aws logs tail <LogGroupName> --since 1h --region <StackRegion>
```

| 日誌群組（邏輯 ID 前綴） | 函式 | 要留意的內容 |
|---|---|---|
| `ApiLogs` | API 函式 | `AWS call failed`（頁面顯示 502 錯誤）、`unhandled error`、`usage metrics failed in <Region>`、`call failed in <Region>`、`Spot score groups unavailable`、`terminate failed for`（清理函式隨後會終止的測試執行個體） |
| `ScoreUpdaterLogs` | Spot 分數更新函式 | 每次執行一行：`score updater: {...}`，包含 `mode`（`warm`、`daily` 或 `bootstrap`）、`updated`，以及設定、重複、核准、規劃中、失敗和受限請求的計數。更新後會有 `daily update: N groups, M configurations, K Regions answered of L`。`daily update skipped` 表示區域清單或方案查詢都沒有回應，下一次執行會再試。`offering lookup failed in <Region>` 表示該區域的群組維持不變。`Spot placement score request failed for` 標示失敗的請求。 |
| `CleanupLogs` | 清理函式 | 每次執行一行：`terminated leftover probe instances: {...}; deleted leftover probe launch templates: {...}; failed Regions: [...]`。`probe cleanup failed in <Region>`（警告）表示函式無法清理該區域。 |
| `NotifierLogs` | 通知函式 | 每次執行一行：`alert check: {...}`，包含計數 `subscriptions`、`skipped`、`groups`、`failedGroups`、`notified`、`unconfirmed`、`rejected` 和 `failedItems`。`signal check failed for ...` 標示失敗的查詢；提醒會保留其狀態，下一次執行會再試。`capacity block check rejected for ...` 標示被 EC2 拒絕的 Capacity Block 提醒，並附上 `cb-invalid-duration` 之類的代碼；頁面會向使用者顯示，且不會寄出電子郵件。 |
| `ProbeTeardownLogs` | 拆除函式 | 在您刪除堆疊時執行。`probe teardown left ... and failed in ...` 會指出它無法移除的項目。堆疊刪除也會移除此日誌群組。 |

每個 API 請求會在 `ApiLogs` 中寫入一行結構化的 INFO 日誌，欄位包括 `msg`（`request`）、`method`、`route`、`status`、`durationMs`、`cacheHits`、`cacheMisses`、`coldStart`，以及發生錯誤時的 `errorCode`。當 `region` 是已啟用的區域之一時，該行也會包含它；當 `instanceType` 是格式正確的執行個體類型名稱時，也會包含它。它不包含權杖、宣告、電子郵件地址或其他查詢值。API Gateway 存取日誌未啟用。若要查看每個路由的中位數和第 90 百分位數持續時間，請在 CloudWatch Logs Insights 中對 `ApiLogs` 群組執行以下查詢：

```
filter msg = "request"
| stats count(*) as requests, pct(durationMs, 50) as p50, pct(durationMs, 90) as p90 by route
| sort p90 desc
```

### 留意限流

當送達的請求超過 `apiRateLimit` 及其突發量所容許的數量時，API Gateway 會回應 HTTP 429，頁面在重試之後會顯示 `服務目前忙碌中。`。在 Amazon CloudWatch 中 HTTP API 的 API Gateway 指標裡，`4xx` 指標會將這些回應與其他用戶端錯誤一起計算，因此在忙碌時段穩定上升，表示是限流造成的。如果是這樣，請調高 `apiRateLimit`。

### 留意緩慢的區域

EC2 呼叫使用 3 秒的連線逾時和 15 秒的讀取逾時，總共嘗試 3 次，因此無法連線的區域不會長時間佔住請求。讀取每個已啟用區域的頁面，總共等待約 12 秒。到那時仍未回應的區域會顯示為錯誤列（`該區域未回應。`），並被略過 5 分鐘；其他區域不受影響。若要查看發生頻率，請在 API 函式日誌群組中搜尋 `did not answer`。帳戶尚未啟用的區域會改為顯示 `未啟用`，這不是故障。如果某個區域持續失敗，請將它加入 `excludeRegions`。

API 建立的其他 AWS 用戶端也有上限。Service Quotas、CloudWatch、Price List、SSM 和 SNS 呼叫使用 3 秒的連線逾時和 10 秒的讀取逾時，總共嘗試 2 次。啟動分數更新函式的呼叫使用 5 秒的讀取逾時，只嘗試 1 次；啟動失敗時，之後的請求會再試。當 CloudWatch 用量指標失敗或逾時，「配額」頁面仍會顯示上限，「已使用」欄為空白，並顯示說明 `目前無法取得 <Region> 的用量`。

略過狀態是共用的。沒有回應的區域會在快取資料表中取得一個小項目 `#down#<Region>`，其值和存留時間（TTL）都是略過的截止時間，因此它會在 5 分鐘後自行清除。以錯誤回應（包括限流）的區域不會被標記，而標記也不代表該區域沒有提供任何項目。若要查看目前的標記：

```bash
aws dynamodb scan --region <StackRegion> --table-name <OfferingCacheTableName> \
  --filter-expression "begins_with(cacheKey, :p)" \
  --expression-attribute-values '{":p":{"S":"#down#"}}' \
  --query 'Items[].[cacheKey.S,downUntil.N]' --output table
```

### 查詢失敗不會被視為「未提供」

每個區域提供的內容是幾乎每個結果的基礎，因此查詢失敗不能變成空的結果。`DescribeInstanceTypeOfferings` 呼叫失敗、逾時或被標記略過的區域，會回報為錯誤列或未知，且不會對其快取項目寫入任何內容。分數更新函式也以相同方式處理查詢失敗：該區域的群組維持不變，直到之後的每日更新取得回應為止。

### 更新期間提供的回答

每個區域的方案項目都有新鮮度時間範圍（`offeringCacheHours`），並保留 7 天。時間範圍結束後，下一個請求會立即以已儲存的項目回應，並附上該項目自己的 `更新於 ...` 時間，同時在同一個請求中再次向 EC2 查詢。只有取得該項目重新整理鎖定的請求會這麼做，每個區域每分鐘最多一次，而且它會在 4 秒後放棄，保留函式逾時的最後 6 秒。重新整理失敗時，已儲存的結果會保留原處。超過 7 天的項目會消失，下一個讀取者會等待查詢完成。

### 排程函式的作用

- **分數更新函式**在每小時的第 0、15、30 和 45 分鐘執行。00:30 UTC 的執行是每日更新。它會持有一個鎖定項目，避免執行重疊。請參閱 [Spot 分數如何保持最新](#spot-分數如何保持最新)。
- **清理函式**每 15 分鐘執行一次。它會讀取已啟用的區域（扣除 `excludeRegions`），並在每個區域中列出帶有標籤 `gpu-capacity-finder-probe=true`、狀態為 `pending`、`running`、`stopping` 或 `stopped` 的執行個體，並終止啟動超過 10 分鐘的執行個體。它也會刪除帶有該標籤、建立超過 1 小時的啟動範本。單一區域失敗不會停止其他區域。
- **測試執行個體會自行結束**，作為第二道備援。每次測試啟動都會傳入使用者資料指令碼 `shutdown -h +5`，並將關機行為設為 `terminate`，因此即使頁面的終止呼叫和清理函式都失敗，執行個體也會在作業系統開機 5 分鐘後關機並終止。成功的測試啟動通常會立即被終止。
- 當 `enableTestLaunch=true` 時，安全網有兩個 **CloudWatch 警示**：`<stack>-probe-cleanup-errors`（清理函式的 `Errors` 指標）和 `<stack>-probe-cleanup-region-failures`（清理日誌群組上針對 `probe cleanup failed in` 的指標篩選條件，命名空間為 `GpuCapacityFinder/<stack>`）。兩者都使用 15 分鐘的期間，即一次排程執行。
- **通知函式**每 `notifyIntervalMinutes` 執行一次。它會讀取整個提醒資料表，略過不再符合已啟用區域的提醒，並以唯讀呼叫對每個不同的訊號查詢一次。Capacity Block 檢查會以提醒的執行個體數量和期間查詢，不設日期範圍。Spot 分數提醒會讀取分數更新函式快取的群組分數，不會發出 Spot 分數請求。只有當提醒從無法使用變為可用，且使用者已確認 SNS 訂閱之後，它才會寄電子郵件給使用者。檢查失敗時會保留上次的狀態，並在下一次執行時重試。

### 警示

堆疊會為自己的函式建立 CloudWatch 警示。它們讀取平台發布的 `AWS/Lambda` 指標，因此不需要程式碼，也不需要額外權限，每個警示每月會產生少量費用。

| 警示 | 指標 | 期間與門檻 | 代表的意義 |
|---|---|---|---|
| `<stack>-api-errors` | API 函式的 `Errors` | 5 分鐘，大於 0 | 有請求失敗，或平台在 25 秒逾時時停止了執行。 |
| `<stack>-api-duration` | API 函式的 `Duration`（Maximum） | 5 分鐘，大於 20,000 ms | 有請求接近逾時：快取未暖機、區域緩慢，或扇出範圍很大。 |
| `<stack>-api-throttles` | API 函式的 `Throttles` | 5 分鐘，大於 0 | 因為達到帳戶的並行限制，Lambda 拒絕了執行。 |
| `<stack>-score-updater-errors`、`-duration`、`-throttles` | 更新函式的相同指標 | 5 分鐘；`Duration` 大於 240,000 ms（其 5 分鐘逾時的 80%） | Spot 分數更新失敗、執行時間過長或被限流。一次失敗的執行會使分數延遲到下一次執行。 |
| `<stack>-notifier-errors`、`-duration`、`-throttles` | 通知函式的相同指標 | 5 分鐘；`Duration` 大於 192,000 ms（其 4 分鐘逾時的 80%） | 提醒檢查失敗、執行時間過長或被限流。 |
| `<stack>-probe-cleanup-duration`、`-throttles` | 清理函式的相同指標 | 5 分鐘；`Duration` 大於 96,000 ms（其 2 分鐘逾時的 80%） | 安全網執行時間過長或被限流。 |
| `<stack>-probe-cleanup-errors`、`-probe-cleanup-region-failures` | 請參閱[排程函式的作用](#排程函式的作用) | 15 分鐘，一次排程執行 | 殘留的測試執行個體可能仍在執行。 |

每個警示都使用一個期間的一個資料點，並將沒有資料的期間視為未違反。清理警示只在 `enableTestLaunch=true` 時存在，通知函式警示只在 `enableNotifications=true` 時存在。

**接收通知。** 將 `alarmTopicArn` 設為您已擁有、位於堆疊區域的 SNS 主題，堆疊的每個警示都會發布到該主題。請以該主題和您的其他選項部署，因為您省略的設定會回到其預設值：

```bash
aws sns create-topic --region <StackRegion> --name gpu-capacity-finder-alarms
aws sns subscribe --region <StackRegion> --topic-arn <TopicArn> --protocol email --notification-endpoint you@example.com
./deploy.sh -c alarmTopicArn=<TopicArn> -c <your other options>
```

堆疊只會參照該主題，因此其存取政策仍由您掌管，也不會為任何角色新增 IAM 權限。此政策必須允許 CloudWatch 發布：

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

如果主題已加密，請使用客戶受管金鑰，而不是 AWS 受管金鑰 `alias/aws/sns`：CloudWatch 無法發布到以 AWS 受管金鑰加密的主題。金鑰政策必須允許 `cloudwatch.amazonaws.com` 對該金鑰呼叫 `kms:GenerateDataKey*` 和 `kms:Decrypt`。發布被拒絕的警示會在主控台中變更狀態，但不會傳送任何內容，因此請在部署後傳送一則測試通知：

```bash
aws cloudwatch set-alarm-state --region <StackRegion> --alarm-name <stack>-api-errors \
  --state-value ALARM --state-reason "testing the notification path"
```

電子郵件提醒的提醒主題不會用於警示：它屬於訂閱的使用者，並使用 AWS 受管金鑰。

### 找出殘留資源

在使用者進行測試的每個區域中，檢查測試執行個體、啟動範本和探測安全群組：

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

## 備份與資料

資料表使用隨需容量，並啟用了時間點復原。`cdk destroy` 會刪除資料表，而 DynamoDB 會為每個資料表保留 35 天的系統備份（請參閱[拆除](#拆除)）。若要依您自己的條件保留資料，請在銷毀堆疊之前建立隨需備份或匯出。

| 資料表（邏輯 ID） | 儲存內容 | 保留期 |
|---|---|---|
| 探測歷史資料表（`Probes`） | 每次測試啟動一筆記錄：區域、群組或尺寸、市場、結果、取得的可用區域和尺寸、EC2 錯誤代碼，以及呼叫者的使用者名稱。另有短期的冷卻鎖定、每位使用者每個 UTC 日一個每日計數器（`#probe-count#...`），以及每個 UTC 日一個整個部署的計數器（`#probes#YYYY-MM-DD`）。 | 記錄在 30 天後過期。鎖定項目在其冷卻時間結束一天後過期，計數器在其當日結束兩天後過期。 |
| 快取資料表（`OfferingCache`） | 共用項目，不含使用者資料：`offerings#<Region>`（提供的類型、可用區域、系列名稱）、`cbtypes#<Region>`、`regions#enabled`、`#regions-first-seen`（不過期）、`#sps-groups`（已儲存的分數群組：尺寸、區域區塊、GPU 數量、每個區域提供的類型，以及已取得分數的系列；不過期）、Spot 分數結果、`spshist#<group>#regions`（2 天）、`spscfg#<fingerprint>`（最後一次寫入後 8 天）和 `#sps-budget`、`quota#<Region>`、`price-od#<type>#<Region>`、`price-spot#<Region>`、`#lock#<item>` 重新整理鎖定，以及 `#down#<Region>` 標記。 | 每個項目的快取時間（請參閱[調整 API 呼叫量](#調整-api-呼叫量)）。鎖定項目在其時間範圍結束一天後過期。 |
| 提醒資料表（`Subscriptions`） | 使用者的提醒，以及一個 `#profile` 項目，內含電子郵件地址、SNS 訂閱的 Amazon Resource Name（ARN）和提醒數量。 | 直到使用者刪除提醒為止。設定檔會隨最後一個提醒一起刪除。 |

已過期的項目會保留，直到 DynamoDB 在背景將其刪除。每位使用者的執行個體類型清單、最近的 Capacity Block 搜尋、上次使用的區域和類型，以及語言選擇，都存放在該使用者的瀏覽器儲存空間中，而不在堆疊中。其他資料：使用者集區儲存使用者及其電子郵件地址，SNS 主題儲存電子郵件訂閱，儲存貯體則儲存頁面以及一個 `config.json` 檔案，內含 API URL、應用程式用戶端 ID、登入網域和網站 URL。這些值不是機密；頁面需要它們才能登入。

## 安全性注意事項

- S3 儲存貯體封鎖公開存取、要求傳輸層安全性（TLS），且只透過帶有來源存取控制的 CloudFront 提供內容。CloudFront 會加上內容安全政策、HTTP 嚴格傳輸安全性（HSTS），以及框架和參照來源標頭。
- 自助註冊已關閉。要求使用驗證器應用程式的 MFA，簡訊 MFA 已關閉。密碼需要 12 個字元，並混合大小寫字母、數字和符號。
- API 授權方會拒絕沒有使用者集區為應用程式用戶端核發之有效 ID 權杖的請求。函式從權杖取得使用者 ID，而不是從請求取得。
- 工作階段長度是一種取捨。較長的 `sessionHours` 會讓已登入的瀏覽器索引標籤在不重新登入的情況下保持可用更久，而 MFA 只在登入時適用，在頁面更新權杖時不適用。停用使用者會停止更新，**登出**會撤銷重新整理權杖，但已核發的 ID 權杖在到期之前（最多 60 分鐘）仍可在 API 使用。
- 應用程式用戶端未啟用透過 API 的直接密碼（Secure Remote Password，SRP）登入。頁面透過託管登入頁面，以授權碼流程登入。
- 測試啟動使用預設 VPC，沒有公有 IP 位址，要求使用 Instance Metadata Service Version 2（IMDSv2），並使用沒有傳入規則的專用安全群組。隨需測試啟動會將其 Capacity Reservation 偏好設定為 `none`。每次測試都會傳入靜態的使用者資料指令碼（`shutdown -h +5`，不含機密、不含帳戶資料、不發出網路呼叫），讓執行個體在作業系統開機後 5 分鐘關機，並以關機行為 `terminate` 自行結束。
- API 角色沒有建立、修改或取消 Capacity Reservation，或購買 Capacity Block 的權限。頁面只會連結到 EC2 主控台，並顯示 Capacity Reservation 命令；使用者以自己的憑證採取行動。
- **測試啟動權限涵蓋每個區域。** 由於測試啟動在每個已啟用的區域（扣除 `excludeRegions`）中執行，啟動陳述式的 EC2 資源 ARN 會指定每個區域，而函式會在呼叫 EC2 之前拒絕已排除或未啟用的區域。條件維持不變：`RunInstances` 只適用於符合 `g*` 或 `p*` 的執行個體類型，且只能帶有探測標籤（函式會將其進一步縮小到命名規則）；`CreateFleet` 和 `CreateLaunchTemplate` 只能帶有探測標籤；`RunInstances`、`CreateFleet` 和 `DeleteLaunchTemplate` 對啟動範本只在範本帶有該標籤時適用；`CreateTags` 只在建立時適用；`TerminateInstances` 只適用於帶有該標籤的執行個體；探測安全群組只能帶有其自己的標籤；`RunInstances` 和 `CreateFleet` 只能使用 Amazon 擁有的映像，且只能使用帶有標籤的探測安全群組。`DescribeInstances` 讓函式能找出結果不明確之啟動呼叫的執行個體，以便將其終止。陳述式為 `LaunchTaggedProbeInstance`、`CreateTaggedProbeFleet`、`LaunchSupportingResources`、`LaunchAmazonImageOnly`、`LaunchInProbeSecurityGroupOnly`、`FindUnknownOutcomeProbe`、`UseProbeLaunchTemplate`、`CreateProbeLaunchTemplate`、`CreateProbeSecurityGroup`、`CreateProbeSecurityGroupInVpc`、`TagProbeSecurityGroupOnCreate`、`TagOnLaunchOnly` 和 `TerminateProbeInstancesOnly`。`CountDailyProbes` 只允許對計數器分割區執行 DynamoDB `UpdateItem`。此角色可以讀取 AWS Systems Manager Parameter Store 中兩個 Amazon Linux 2023 映像參數（x86_64 和 arm64）。
- 在資料表方面，API 角色可以對探測歷史資料表執行 `Query`，並以 `PutItem` 寫入探測記錄和冷卻鎖定（`StoreProbeRecords`，帶有 `dynamodb:LeadingKeys` 條件，使其無法存取計數器分割區）。它可以讀取和寫入快取資料表項目（`GetItem`、`BatchGetItem` 和 `PutItem`）。在每個部署中，它也只能啟動分數更新函式（`lambda:InvokeFunction`），並讀取堆疊區域中 AWS 公開的區域完整名稱參數（`ssm:GetParameter` 和 `ssm:GetParameters`）。
- 開啟電子郵件提醒時，API 角色也可以讀取和寫入提醒資料表（`Query`、`PutItem`、`DeleteItem` 和 `UpdateItem`），只能將電子郵件端點訂閱到提醒主題（帶有條件 `sns:Protocol=email` 的 `sns:Subscribe`），且只能取消該主題的訂閱。
- 通知角色只在 `enableNotifications` 設為 `true` 時存在。它可以對提醒資料表執行 `Scan` 和 `UpdateItem`，讀取快取資料表項目並對其執行 `PutItem`（用於共用的已啟用區域清單及其鎖定），對資源 `*` 呼叫 `ec2:DescribeCapacityBlockOfferings` 和 `ec2:DescribeRegions`（`ReadAlertSignals`），只能發布到提醒主題（`sns:Publish`），並讀取該主題訂閱的屬性（`sns:GetSubscriptionAttributes`）。
- 清理角色只能終止和刪除帶有標籤的資源。拆除角色還可以刪除帶有探測安全群組標籤的安全群組，並可以讀取自身堆疊的狀態（只對該堆疊執行 `cloudformation:DescribeStacks`），因此它只在堆疊被刪除時才移除探測資源。更新角色具有唯讀的 EC2 動作和快取資料表的存取權限。
- 讀取動作使用資源 `*`，因為它們沒有資源層級的權限：EC2 Describe 和方案讀取、`GetSpotPlacementScores`、Service Quotas 清單呼叫、`ec2:DescribeInstanceTypes`、`ec2:DescribeRegions`、`cloudwatch:GetMetricData` 和 `pricing:GetProducts`。`pricing:GetProducts` 讀取公開定價；函式會將它送到 `us-east-1` 端點。
- 一個 Lambda 角色服務部署的所有使用者，而 IAM 無法在單一角色內區分使用者。函式依權杖中使用者的 `sub` 來區分提醒資料表的存取。此角色可讀取和寫入整個資料表。
- 此頁面沒有 AWS WAF Web ACL、沒有自訂網域，也沒有存取日誌，而預設的 API Gateway 端點本身仍可連線。在廣泛分享此頁面之前，請先加入這些項目。
- 區域地圖的世界輪廓來自 Natural Earth，屬於公有領域。範例根目錄中的 [THIRD-PARTY-LICENSES](../THIRD-PARTY-LICENSES) 檔案列出了它。如果您複製此頁面，請保留該檔案。
- SNS 主題使用 Amazon SNS 的 AWS 受管金鑰。如果您需要自己的金鑰政策，請使用客戶受管的 AWS Key Management Service（AWS KMS）金鑰。

[README 的「限制」一節](../README.zh-tw.md#限制)列出這些及其他限制。

## 拆除

資源在執行期間會產生費用，包括測試啟動所啟動的 GPU 執行個體。請依以下順序移除它們。

1. **銷毀堆疊。**

   ```bash
   source .venv/bin/activate   # the CDK app needs the Python environment
   cdk destroy   # add -c stackName=<name> if you deployed with a custom stack name
   ```

   這會移除儲存貯體內容、資料表、SNS 主題及其訂閱、使用者集區，以及堆疊為其函式建立的日誌群組。當 `enableTestLaunch=true` 時，堆疊在刪除時也會執行一個自訂資源（`Custom::ProbeTeardown`）。其函式會終止仍在執行的測試執行個體，然後在每個已啟用的區域（扣除 `excludeRegions`）中，刪除帶有標籤的探測啟動範本和帶有標籤的探測安全群組。建立這些資源的函式會先被刪除，因此在拆除執行期間不會有任何項目建立新的資源。自訂資源會在有界限的時間內回報成功（函式會在其 5 分鐘逾時前約 30 秒停止自己的工作，而 CloudFormation 會在 10 分鐘後停止等待），因此失敗的區域不會阻擋刪除。函式會以 `probe teardown left ...` 記錄它無法移除的項目，但其日誌群組會隨堆疊一起刪除。

   每個 DynamoDB 資料表都啟用了時間點復原。當您刪除這類資料表時，DynamoDB 會建立系統備份並保留 35 天。提醒資料表的備份包含您使用者的電子郵件地址。請列出堆疊區域中的備份，並刪除您不需要的備份：

   ```bash
   aws dynamodb list-backups --backup-type SYSTEM --region <region>
   aws dynamodb delete-backup --backup-arn <arn> --region <region>
   ```

2. **檢查每個區域中的探測資源。** 自訂資源會略過您在測試之後才加入 `excludeRegions` 的區域，以及它無法及時連線的區域。在使用者執行過測試啟動的每個區域中，執行[找出殘留資源](#找出殘留資源)中的命令。終止每個殘留的執行個體，以 `aws ec2 delete-launch-template --region <Region> --launch-template-id <Id>` 刪除每個殘留的啟動範本，並以 `aws ec2 delete-security-group --region <Region> --group-id <GroupId>` 刪除每個殘留的安全群組（當使用該安全群組的測試執行個體仍存在時，無法刪除該安全群組）。

3. **刪除 CDK 輔助函式殘留的日誌群組。** 這些日誌群組可能在 `cdk destroy` 之後仍然存在。請在堆疊區域中列出它們（預設的 `<stackName>` 為 `gpu-capacity-finder`），檢查每個名稱，並刪除屬於此堆疊的日誌群組：

   ```bash
   aws logs describe-log-groups --region <StackRegion> \
     --log-group-name-prefix /aws/lambda/<stackName>- --query 'logGroups[].logGroupName'
   aws logs delete-log-group --region <StackRegion> --log-group-name <LogGroupName>
   ```

每個部署中的標籤都相同，因此這些命令和拆除函式也會比對到同一帳戶中另一個部署的資源。每個帳戶只使用一個部署：請參閱[每個帳戶一個部署](#每個帳戶一個部署)。

`cdk destroy` 不會處理使用者在 EC2 主控台購買的 Capacity Block，或使用者以複製的命令建立的 Capacity Reservation。它們屬於使用者，並會持續計費直到結束或被取消；請使用者檢查 EC2 主控台。

## 移交給您的團隊

部署後請確認以下項目，然後移交網站 URL 和[使用者指南](user-guide.zh-tw.md)。

- [ ] **API 拒絕匿名呼叫。** 從頁面的設定中讀取 API URL，然後在不帶權杖的情況下呼叫它。回應為 HTTP 401：

  ```bash
  API_URL=$(curl -s <SiteUrl>config.json | python3 -c 'import json,sys; print(json.load(sys.stdin)["apiUrl"])')
  curl -s -o /dev/null -w '%{http_code}\n' "$API_URL/api/config"   # expect 401
  ```

- [ ] **第一位使用者已存在且可以登入。** 建立使用者、登入、設定密碼，並註冊驗證器應用程式。
- [ ] **Spot 分數群組已存在。** 在 **Spot 分數**索引標籤上，第一次載入會顯示建立中的狀態，一兩分鐘後，表格會以區域為列、分數群組為欄，並附有 `更新於` 標籤。之後才加入之區域的儲存格會顯示 `下次每日更新後取得 Spot 分數`。如果您的系列清單很長或預算很小，部分儲存格可能維持在規劃中狀態，直到預算有空間為止。
- [ ] **唯讀檢查正常運作。** 在**配額**索引標籤上，配額會載入。在 **Capacity Block** 索引標籤上，針對一個區域和一種 P5 類型的搜尋會以方案或 `沒有相符的 Block。` 結束，而不是錯誤行。畫面上不會持續留有帶**重試**的紅色訊息。
- [ ] **一次測試啟動正常運作**（如果 `enableTestLaunch=true`）。在**隨需與 Spot**索引標籤上，於有預設 VPC 的區域中，對最小的群組執行一次測試啟動（**Spot 分數**索引標籤區域詳細資料中的**測試啟動**按鈕會跳到這裡），閱讀結果，並確認幾分鐘後沒有帶有 `gpu-capacity-finder-probe` 標籤的執行個體殘留，且頁面顯示今天的計數。
- [ ] **提醒電子郵件正常運作**（如果 `enableNotifications=true`）。新增一個提醒，確認來自 AWS Notifications 的訂閱電子郵件，並檢查**上次檢查**在下一次檢查後有更新。
- [ ] **使用者了解頁面不做哪些事。** 它不會建立預留或購買 Block。使用者在 EC2 主控台購買的 Capacity Block 是一項購買，而以**使用 CLI 預留**複製的命令會建立 Capacity Reservation，從建立起就依隨需費率計費，直到它結束或使用者取消為止。
- [ ] **您知道日誌的位置。** 從[監控](#監控)記下 `ApiLogs`、`ScoreUpdaterLogs`、`CleanupLogs` 和 `NotifierLogs` 日誌群組名稱。
- [ ] **您知道拆除的順序。** 請參閱[拆除](#拆除)。
