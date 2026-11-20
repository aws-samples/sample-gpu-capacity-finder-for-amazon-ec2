# GPU Capacity Finder administrator guide

This guide is for you if you deploy and run GPU Capacity Finder in your AWS account. The page helps your users find graphics processing unit (GPU) capacity on Amazon Elastic Compute Cloud (Amazon EC2). It covers deployment, settings, users, monitoring, data, security, and teardown. The people who use the page have a separate [user guide](user-guide.md). The [README](../README.md) is the reference for the architecture, the settings table with defaults and ranges, and the limitations.

The page is a decision-support tool: it reads capacity signals and runs one bounded test launch. It does not manage instances, create Capacity Reservations, or buy Capacity Blocks; users buy Capacity Blocks in the EC2 console, and the page shows them an AWS Command Line Interface (AWS CLI) command to copy for a Capacity Reservation (**Reserve with CLI** on the **On-Demand and Spot** tab), which they run with their own credentials.

## What gets created

The [architecture diagram](../architecture-diagram.png) has its own step numbers. This guide uses these names for the DynamoDB tables (the logical ID in the stack is in parentheses): the probe history table (`Probes`), the cache table (`OfferingCache`), and the alert table (`Subscriptions`).

| # | Resource | Notes |
|---|---|---|
| 1 | Amazon CloudFront distribution and a private Amazon Simple Storage Service (Amazon S3) bucket | Serves the static page through origin access control. The bucket blocks public access. |
| 2 | Amazon Cognito user pool, app client, and sign-in domain | Self sign-up off, multi-factor authentication (MFA) required with a time-based one-time password (TOTP). The domain prefix comes from `stackName`. |
| 3 | Amazon API Gateway Hypertext Transfer Protocol (HTTP) API with a JSON Web Token (JWT) authorizer | Rejects a request without a valid ID token. The stage throttle is `apiRateLimit` (10 requests per second by default) with a burst of three times that value, shared by every user. |
| 4 | AWS Lambda API function and its AWS Identity and Access Management (IAM) role | Reads signals and list prices, reads cached Spot scores, runs test launches, and manages alerts. 1024 MB of memory, 25-second timeout. |
| 5 | Spot score updater Lambda function on an Amazon EventBridge schedule (minutes 0, 15, 30, and 45 of every hour) | Builds the Spot score groups and keeps the scores current. The only code that creates or changes a Spot score request. 256 MB, 5-minute timeout. See [How Spot scores stay current](#how-spot-scores-stay-current). |
| 6 | DynamoDB probe history table | Test launch records expire after 30 days. The table also holds the cooldown locks, the per-user daily counters, and the deployment-wide daily counter. |
| 7 | Cleanup Lambda function on a 15-minute schedule, and a stack-delete teardown function with a custom resource | Only when `enableTestLaunch=true`. The cleanup function terminates test instances older than 10 minutes and deletes test launch templates older than 1 hour in every enabled Region. The teardown function runs once, when you delete the stack. |
| 8 | DynamoDB cache table | Shared cache items: what each Region offers, the Spot score groups and scores, Spot and On-Demand prices, quotas, and the Region list. See [Backup and data](#backup-and-data). |
| 9 | DynamoDB alert table, encrypted Amazon Simple Notification Service (Amazon SNS) topic, notifier Lambda function and its schedule | Only when `enableNotifications=true` (the default). |
| 10 | CloudWatch alarms for the functions | Errors, a run close to the timeout, and throttles per function, plus the two alarms of the test launch safety net. They notify the `alarmTopicArn` topic when you set one, and otherwise have no action. See [Alarms](#alarms). |

The API, updater, cleanup, notifier, and teardown functions each have a log group with one-month retention. The stack also contains AWS Cloud Development Kit (AWS CDK) helper functions that copy the page to the bucket and empty the bucket on delete.

Probe security groups (one per virtual private cloud, or VPC, that a test launch used) are created at run time. The teardown function removes them when you delete the stack; see [Teardown](#teardown). Capacity Reservations that users create with the copied commands, and Capacity Blocks that users buy in the EC2 console, belong to the users, not to the stack.

## Prerequisites

- An AWS account and credentials that can deploy the stack. Use a non-production account.
- Python 3.12 or later for the CDK app and the local tests (the Lambda functions run on Python 3.13), Node.js 20 or later (tested with 22), AWS CLI version 2, and the latest AWS CDK CLI: `npm install -g aws-cdk`.
- Run the commands in this guide from the sample root, the folder that holds `deploy.sh`, unless a command says otherwise.
- The CDK commands (`cdk bootstrap`, `cdk diff`, `cdk deploy`, `cdk destroy`) run the Python CDK app, so they need the Python environment of the sample. `deploy.sh` creates it in `.venv`, but activates it only inside the script. Before another CDK command, run `source .venv/bin/activate` in your shell.
- The account is bootstrapped for the CDK in the Region where you deploy. Before the first deploy, the environment does not exist yet: `python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt && cdk bootstrap`.
- For test launches: a default VPC with default subnets in each Region where users test. Test launches use the default subnet of every zone. A Region without one shows `No default VPC in this Region` and its test launch stays off. To create a default VPC, run `aws ec2 create-default-vpc --region <Region>`; the call creates a VPC, subnets, and an internet gateway in that Region, so run it only where you accept that.
- For Spot test launches: Spot access and the Spot service-linked role. Create the role once if the account has not used Spot:

  ```bash
  aws iam create-service-linked-role --aws-service-name spot.amazonaws.com
  ```

  If your account cannot use Spot, deploy with `-c testLaunchAllowSpot=false`.

## Deploy

### With the deploy script

```bash
export AWS_PROFILE=<profile>   # or sign in another way
export AWS_REGION=<region>
./deploy.sh
```

The script checks for `python3`, `aws`, and `cdk`, creates the Python environment in `.venv`, and shows the CDK CLI version, account, identity, and Region. It asks you to confirm, then runs `cdk deploy`. Pass options after the script name:

```bash
./deploy.sh -c testLaunchAllowSpot=false -c excludeRegions=me-south-1
```

Set `ASSUME_YES=1` to skip the confirmation prompt of the script. The `cdk` command still asks you to approve IAM changes; add `--require-approval never` after the script name to skip that prompt too. When the deploy finishes, the script prints the site URL, the user pool ID, and a template for the command that creates your first user. If `cdk deploy` fails and its output says the CLI is not compatible with the CDK library, the script prints a hint to upgrade the CDK CLI with `npm install -g aws-cdk@latest`.

### With cdk deploy

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cdk deploy -c excludeRegions=me-south-1   # options are optional
```

The stack deploys to the default account and Region of your CDK environment. It prints the `SiteUrl` and `UserPoolId` outputs. To read them later (the commands in this guide use the default stack name `gpu-capacity-finder`; replace it if you set `stackName`):

```bash
aws cloudformation describe-stacks --region <StackRegion> --stack-name gpu-capacity-finder \
  --query "Stacks[0].Outputs" --output table
```

The first Spot score groups are built on the first page request after the deploy, not at the next daily update. The page shows a short building state; reload after a minute.

### One deployment per account

Use one deployment of this sample per AWS account. The probe resources use fixed tag names, and the cleanup and teardown functions scan the enabled Regions of the account, not only the stack Region. A second deployment in the same account, even in another Region, would clean up the test resources of the first one. The AWS restriction on Spot placement score requests is also shared by the whole account, so two deployments would draw on the same budget. For several accounts, deploy one copy per account.

## Settings

Pass each setting with `-c key=value`. The [Settings section of the README](../README.md#settings) lists each key with its default and range. The stack does not remember the values between deployments, so pass the same options each time you deploy, or put them in the `context` block of `cdk.json`.

| Key | Change it when |
|---|---|
| `stackName` | You want a stack name other than the default. Pass the same `-c stackName=<name>` to `cdk deploy` and `cdk destroy`. Use one deployment of this sample per AWS account: see [One deployment per account](#one-deployment-per-account). |
| `excludeRegions` | An enabled Region does not answer, or you do not want it. See [Regions](#regions). |
| `spotScoreFamilies` | You use other GPU families than the default list. See [Choose the Spot score families](#choose-the-spot-score-families). |
| `spotScoreConfigsPerDay` | You want to cap how many different Spot score requests the sample keeps in use. Lower it when your account makes other Spot score calls, because the AWS restriction is shared by the whole account. See [Spot score budget](#spot-score-budget). |
| `spotScoreCacheMinutes` | 15 to 240. The updater repeats each accepted request every 15 minutes; a value above 15 makes it repeat less often (an answer is repeated once it is that old, less 5 minutes). |
| `enableTestLaunch` | Set `false` for a read-only deployment. The stack then has no launch or terminate permissions, no cleanup function, no teardown custom resource, and the page has no test launch panel. Test launches are the only feature that starts instances. Turning it off on an existing stack removes the cleanup function and the teardown custom resource without removing probe resources; first run the commands in [Find leftovers](#find-leftovers) and delete the probe security groups and launch templates, or delete them after the update. |
| `testLaunchAllowSpot` | Set `false` when the account cannot use Spot, or when you want On-Demand test launches only. The Spot score and price views stay. |
| `testLaunchCooldownMinutes` | Raise it to reduce the number of launches. One request per Region, market, and group (or size) launches in each window, across users, so the cooldown does not cap volume; the daily caps do. |
| `maxTestLaunchesPerUserPerDay` | Lower it to cap each user; `0` means no cap. The count is per user and per UTC day (it resets at 00:00 UTC), across Regions, groups, and markets. A test that EC2 processes counts, including one that EC2 refuses for no capacity or a quota limit, and one that ends without a clear result. A cached cooldown result, an in-progress answer, a request that EC2 throttles or that was never sent, a request the server had too little time to start, and a failure before the fleet request (quota too low, no default VPC, an AWS error) do not count. |
| `maxTestLaunchesPerDay` | Lower it to cap the whole deployment; `0` means no cap. One counter for every user, per UTC day, with the same counting rule. The page shows `Test launches today: N of M (all users)` and the user's own count. |
| `enableNotifications` | Set `false` to remove email alerts and the **Alerts** tab. Turning it off later deletes the alert table and the SNS topic with its subscriptions. |
| `notifyIntervalMinutes` | Raise it to check less often. Lower values catch short-lived offerings sooner and run the notifier more often. |
| `maxSubscriptionsPerUser` | Raise it when users watch many Region and type pairs; each alert adds checks to every notifier run. |
| `offeringCacheHours` | Raise it to reduce `DescribeInstanceTypeOfferings` calls; lower it when users need to see a newly offered type sooner. |
| `quotaCacheMinutes` | Raise it to reduce Service Quotas and CloudWatch calls; lower it when users need to see a limit or usage change sooner. |
| `apiRateLimit` | Many users work at the same time and the page often shows `The service is busy.` The stage applies it, and a burst of three times the value, to every user together. |
| `sessionHours` | Users have to sign in again too often, or you want shorter sessions. See [Change the sign-in session length](#change-the-sign-in-session-length). |
| `alarmTopicArn` | You want the stack's CloudWatch alarms by email or another SNS subscription. See [Alarms](#alarms). |

### Regions

Every feature uses one Region list: the commercial Regions your account has enabled, minus `excludeRegions`. Supply, prices, quotas, Capacity Blocks, Spot scores, test launches, the cleanup function, and the teardown function use it. The list comes from EC2 `DescribeRegions` and is stored in the cache table for 24 hours, so a Region you enable appears within a day without a redeploy. If the call fails, the stored list stays in use.

`excludeRegions` is a comma-separated list, up to 40 Regions. Use it for an enabled Region that does not answer or that you do not want to call. The sample does not call an excluded Region. The Spot score updater removes an excluded Region from its score groups at the next daily update, and the page does not show it before then.

`us-east-1` cannot be excluded: every Spot score request goes to the `us-east-1` endpoint, so the deploy refuses the setting. To bring an excluded Region back, remove it from `excludeRegions` and deploy again. It joins its score groups at the next daily update, and until then the page shows `Spot score after the next daily update`.

### Choose the Spot score families

AWS restricts how many different Spot placement score requests an account can make in a day, and the restriction is shared by everything in the account. Each scored family costs one request per GPU count and per 10 Regions that sell it. Scoring every GPU type in every Region would exceed it, so you list only the GPU families you plan to use in `spotScoreFamilies`. The default list is `g6e`, `g7`, `g7e`, `p5`, `p5e`, `p5en`, `p6-b200`, and `p6-b300` (the patterns `g6e.*`, `g7.*`, and so on).

To customise the list, pass the full list with `-c`, and quote it so that the shell leaves the stars alone:

```bash
./deploy.sh -c 'spotScoreFamilies=g6e.*,p5en.*,p6-b200.*'
```

Each pattern names a family and its sizes (`p5.*`) or a wildcard (`g7*`), contains a dot, a star, or a question mark, and names a `g` or `p` GPU family. The list holds up to 20 patterns. `g7.*` matches `g7.xlarge` but not `g7e.xlarge`. A pattern starts with the family letter and its generation digit, such as `g7*` or `p5.*`, so the deploy refuses a pattern such as `g*` or `g?.*`.

What updates by itself, and what does not:

- **A new size** of a scored family joins its group at the next daily update.
- **A new Region**, or a Region that starts to sell a size, joins the group at the next daily update. Until then the page shows `Spot score after the next daily update` for that cell.
- **A new family** gets no Spot score until you add it to `spotScoreFamilies` and deploy again. A deploy that changes the list builds the new groups on the first request, not the next day. Groups of a family you remove stop being repeated, and their requests still count against the account's AWS restriction until 24 hours pass.
- **Supply, prices, quotas, test launches, and Capacity Blocks** cover every GPU family by the `g` or `p` naming rule (a family name that starts with `g` or `p` and a digit) and pick up new families on their own.

A Spot score alert needs a type of a scored family. The API refuses a Spot score alert for a type outside the scored families.

### How Spot scores stay current

A **score group** is one family with one GPU count, such as `g6e · 4 GPUs` (the key is `g6e/4`). The GPU count comes from `DescribeInstanceTypes` and is stored once per type. A group's sizes are every size of that family with that GPU count that any enabled Region sells; the list only grows. A group's Region list holds the Regions that sell at least one of its sizes, in chunks of up to 10 Regions. The sample sends one request per chunk, so a group score is the Region-level chance that any listed size can be fulfilled, not the chance for one size.

The lists are append-only so that requests stay the same from day to day. New Regions go into the last chunk while it has room, then a new chunk opens, in the order first-seen date and then name. A Region that stops selling stays in its chunk. A Region whose offering lookup failed changes nothing. The one exception is a Region in `excludeRegions`: it leaves its chunk at the next daily update.

| Run | When | What it does |
|---|---|---|
| Daily update | The first run at or after 00:30 UTC | Reads the enabled Regions, what they sell, and the GPU count of new sizes. Adds new sizes, Regions, and groups to the stored lists, removes excluded Regions, and asks AWS for the requests that fit the budget. A request that does not fit stays planned. |
| Bootstrap | The first request after the first deploy, or after a deploy that changes `spotScoreFamilies` | Does the same work as the daily update at once. The API starts the updater once, in the background, and the page shows a building state until the groups exist. |
| Refresh | Every 15 minutes, at minutes 0, 15, 30, and 45 | Repeats every request AWS accepted in the last day (a repeat is free and returns a fresh score), stores the answers and one history reading per group, and grants planned requests when the budget has room. It also stores the latest Spot prices of every Region that sells a scored type. |

A page load, the **Refresh** button, and the email alerts only read the cache or repeat accepted requests. They do not create or change a request or a group. **Refresh** repeats a group at most once per 60 seconds for every user together.

A cell shows the state of its Region: scored, no score from AWS, waiting for the next refresh, planned (the request waits for budget room), limited (AWS refused it, or new requests pause), or after the next daily update. `GET /api/config` returns the schedule and the counts: `spotScoreState`, `spotScoreGroups`, `spotScoreConfigsNeeded`, `spotScoreConfigsPerDay`, `spotScoreConfigsInUse`, `remainingNewConfigs`, `spotScoreLimitedUntil`, `lastDailyUpdateAt`, `nextDailyUpdateAt`, `nextWarmAt`, and `dailyUpdateUtc`.

### Spot score budget

AWS restricts how many different Spot placement score request configurations an account can issue in 24 hours. AWS does not publish the size of the restriction, and you cannot raise it. Repeating a configuration that AWS accepted in the last 24 hours is allowed. Every request that the sample sends uses `TargetCapacity` 1, and this value is not a setting (a different value is a different configuration). A chunk's request changes when its Region list or its group's sizes change. Adding a Region to a chunk that has room creates one new configuration for that chunk. A new size changes the request of every chunk of its group, so it creates one new configuration per chunk of the group.

`spotScoreConfigsPerDay` (default 40, 1 to 100) is the most different configurations the sample keeps in use in a rolling 24 hours. The daily update and the refresh grant new requests within it, and what does not fit stays planned until room opens as old requests age out. The sample counts its own configurations in the cache table:

- `spscfg#<fingerprint>` is one item per configuration sent: the request, when AWS first accepted it and when it was last used, the last result, and until when it is blocked after a refusal. It expires 8 days after its last write.
- `#sps-budget` is one item for the budget: the new configurations of the last 24 hours, the pause after a refusal, and an index of the registered configurations. A new configuration takes a slot with a conditional write before the request goes out, so two copies of a function cannot take the last slot. If the item cannot be read or written, no new configuration is sent.

After AWS refuses a new configuration, that configuration is blocked for 24 hours and the sample asks for no new configuration for 24 hours. Accepted configurations keep repeating during the pause. If this deployment had already created at least 3 new configurations in the 24 hours before the refusal, the daily budget also stays at that count for 7 days; with fewer, the sample treats the budget as used up elsewhere in the account and only blocks and pauses.

Old configurations of earlier versions are not sent again, but they count in `spotScoreConfigsInUse` until they age out of the 24-hour window.

**History items.** Each group keeps one small history item, `spshist#<group>#regions`, in the cache table. It keeps at most 48 readings, drops readings older than 24 hours, takes at most one reading per 5 minutes, and expires after 2 days. A failed history read or write is logged and does not fail a request.

### Test launch

A test launch asks EC2 for one instance of any size of a score group (or of one chosen size) in a zone of the Region, and terminates it right away. The API function:

1. Checks the guards in order: `enableTestLaunch`, the Region (enabled and not excluded), the market (`testLaunchAllowSpot` for Spot), the GPU naming rule, the caller, the cooldown, the per-user cap, and the deployment-wide cap. A failure before the fleet request (quota too low, no default VPC, a security group conflict, an AWS error) gives back both counts and the cooldown slot.
2. Finds every default subnet of the Region (one per zone) and every size of the group that the Region sells and that fits the free vCPUs of the quota that limits the family in that market. If no size fits, the answer is `quota-too-low` and no launch request is made. An unknown quota or vCPU count does not exclude a size.
3. Creates one launch template for the test (probe tag, self-shutdown user data, no public address, the probe security group), and an EC2 Fleet of type `instant` with one instance. Spot uses `price-capacity-optimized`. On-Demand uses `lowest-price` and does not launch into an open Capacity Reservation, so it measures free capacity.
4. Terminates the instance and deletes the launch template right after the fleet answer. An instant fleet needs no delete call: EC2 removes it itself.

The answer is launched (with the zone and size EC2 picked), no capacity, quota, unsupported, or error. The page also lists the launch results of the last 24 hours for each group and market in the Region, for every user and without user names.

The security group `gpu-capacity-finder-probe` has no inbound rules. The function creates it once per VPC on the first test launch, with the tag `gpu-capacity-finder-probe-sg`.

The two daily counters live in the probe history table. The per-user counter is `#probe-count#...` and the deployment-wide counter is `#probes#YYYY-MM-DD`, a conditional write that decides the race for the last slot. The counter items expire two days after their day ends. The API role can update only those partitions.

### Tune API call volume

The Lambda functions call EC2, Service Quotas, CloudWatch, and the AWS Price List API in your account, so the dashboard's calls count against your account-level API quotas, such as the EC2 Describe API request limits, together with your other workloads. The cache table is shared by every user, so one answer serves everyone until its cache time ends. Raising a cache time reduces calls and shows older data; lowering it does the opposite.

Each API container also keeps cached items in memory for 60 seconds, so a refresh that another container wrote can take up to 60 seconds to show in this one.

The page reads cached data when a tab opens. When the data on screen is older than its cache time, the page reads it again when the user opens the tab or returns to the page, without forcing a refresh. **Refresh** sends `refresh=true`. For Spot scores, that repeats the accepted requests of the group; for other data it skips the cache for that item. Each cached item has its own forced-refresh lock, a conditional write in the cache table, so AWS is asked again at most once per minute per item (for example one score group, or one Region's quotas). A refresh inside that minute returns the cached answer and the seconds left, and the page says so.

| Data | AWS API it calls when the cache does not answer | Cache and time | What skips it |
|---|---|---|---|
| What each Region offers (the offering matrix, size lists, request checks) | EC2 `DescribeInstanceTypeOfferings`, two paginated calls per enabled Region | Cache table (`offerings#<Region>`), `offeringCacheHours`, with a 60-second memory layer. After that window the stored answer is still served while one request asks EC2 again (see [Answers served while they are updated](#answers-served-while-they-are-updated)); the item is kept for 7 days | Nothing on the page |
| The instance types that support Capacity Blocks | EC2 `DescribeInstanceTypes` with the filter `supported-usage-class=capacity-block`, one call per Region | Cache table (`cbtypes#<Region>`), asked again once a day and kept for 7 days. If the call fails, the last list is used, else the family list in `lambda/capacity_blocks.py` | Nothing on the page |
| The list of enabled Regions | EC2 `DescribeRegions` (enabled commercial Regions) | Cache table (`regions#enabled`), 24 hours, kept for 7 days. The updater also reads it live at each daily update | Nothing on the page |
| Score groups and Spot scores | EC2 `GetSpotPlacementScores`, sent to `us-east-1`, by the score updater only (and by **Refresh**, for accepted requests) | Score groups in `#sps-groups` (no expiry); each answer in the cache table for `spotScoreCacheMinutes` plus 10 minutes. The page and the notifier read the same items | **Refresh** repeats accepted requests of a group |
| Spot prices | EC2 `DescribeSpotPriceHistory`, one call per Region for every scored type, by the updater at each run | Cache table (`price-spot#<Region>`), 30 minutes. A type outside the scored families gets its own call and item when a user asks | Nothing; the updater keeps it current |
| On-Demand list price | Price List `GetProducts`, sent to `us-east-1` | Cache table, 24 hours (1 hour when the Price List has no match; a failed call is not cached) | Nothing |
| **Quotas** (one Region at a time) | Service Quotas `ListServiceQuotas` and `ListAWSDefaultServiceQuotas`, CloudWatch `GetMetricData` | Cache table, `quotaCacheMinutes` | **Refresh**, per Region |
| **Capacity Blocks** search and **Max capacity** | EC2 `DescribeCapacityBlockOfferings`, one request per Region and type pair (**Max capacity** runs in up to three requests, with a bounded number of EC2 calls, each request well within the 25-second function timeout) | Not cached. A search runs up to six pairs at once | **Search again** runs the last search again |
| Test launch history and alerts | DynamoDB queries | Not cached | **Refresh** |

The page retries a GET request that gets HTTP 429 or 503, or fails on the network, up to three times, waiting about 0.6, 1.2, and 2.4 seconds plus a random extra, or the `Retry-After` header value (up to 8 seconds) when the response has one. After that, it shows `The service is busy. Wait a moment and choose Retry.`, and a Capacity Block search tries the pair once more after the other pairs. It does not retry the daily test launch caps (`probe-daily-limit`, `daily-cap-all`), and it does not repeat POST, PUT, or DELETE requests, so a test launch runs once.

### Change the sign-in session length

`sessionHours` (default 12) is the longest time from one full sign-in, with password and MFA code, to the next. It sets the refresh token expiration of the Cognito app client. ID and access tokens last 60 minutes, a fixed value; shortly before the ID token expires, the page gets a new one with the refresh token, without the sign-in page. When `sessionHours` ends, the page sends the user to the sign-in page.

The page keeps the tokens for the current browser tab only, so closing the tab ends the session before `sessionHours` ends. To change the value, deploy again with the new value and your other options:

```bash
./deploy.sh -c sessionHours=24
```

The update changes only the Cognito app client; it does not replace the user pool or its users. Sessions that started before the update can keep their earlier length until the user signs in again. If you change **Refresh token expiration** in the Amazon Cognito console instead, the next deployment overwrites it with the value from the deploy command.

## Manage users

Run these commands in the stack Region. Users sign in with their email address.

### Create a user

```bash
aws cognito-idp admin-create-user \
  --region <StackRegion> \
  --user-pool-id <UserPoolId> \
  --username user@example.com \
  --user-attributes Name=email,Value=user@example.com Name=email_verified,Value=true \
  --desired-delivery-mediums EMAIL
```

Cognito emails a temporary password. At first sign-in, the user sets a password and enrolls an authenticator app. Send the user the site URL and the [user guide](user-guide.md). Set `email_verified=true` as shown: the API refuses alerts for an unverified address.

You can also create a user in the AWS Management Console. Open Amazon Cognito, choose **User pools**, and open your pool (`deploy.sh` prints the user pool ID). Choose **Users**, then **Create user**. Choose **Send an email invitation**, enter the email address, select **Mark email address as verified**, choose **Generate a password**, and choose **Create user**. The temporary password is valid for 7 days. The **Users** tab of the page shows the same steps, with a direct link to the user pool.

### Resend an invitation or reset a password

If the temporary password expired before first sign-in, resend it:

```bash
aws cognito-idp admin-create-user --region <StackRegion> --user-pool-id <UserPoolId> \
  --username user@example.com --message-action RESEND --desired-delivery-mediums EMAIL
```

For a user who has signed in before, reset the password. Cognito emails a code, and the user sets a new password at the next sign-in:

```bash
aws cognito-idp admin-reset-user-password --region <StackRegion> \
  --user-pool-id <UserPoolId> --username user@example.com
```

A user who lost their authenticator app cannot sign in. The sample has not been tested with an MFA reset. One option is to turn off the user's TOTP setting so that the user enrolls again; test it in your pool first:

```bash
aws cognito-idp admin-set-user-mfa-preference --region <StackRegion> \
  --user-pool-id <UserPoolId> --username user@example.com \
  --software-token-mfa-settings Enabled=false,PreferredMfa=false
```

The other option is to [remove the user](#remove-a-user-and-their-leftovers) and create the user again. A new user gets a new user ID (`sub`), so the old alerts do not carry over. In the console, open the user pool, choose **Users**, open the user by email address, and use the resend, **Reset password**, or authenticator options on the details page. Menu labels can differ between console versions.

### Disable a user

```bash
aws cognito-idp admin-disable-user --region <StackRegion> \
  --user-pool-id <UserPoolId> --username user@example.com
```

A disabled user cannot sign in. An ID token issued before you disable the user stays valid until it expires, up to 60 minutes, because the API checks the token signature and expiry. Disabling does not stop the user's alert emails. In the console, open the user's details page and choose **Disable user access**.

### Remove a user and their leftovers

Deleting a user in the user pool does not remove the user's alerts or SNS subscription. Remove them first. The user's list of instance types lives in the user's browser, not in the stack.

1. Find the user's `sub`:

   ```bash
   aws cognito-idp admin-get-user --region <StackRegion> --user-pool-id <UserPoolId> \
     --username user@example.com --query "UserAttributes[?Name=='sub'].Value" --output text
   ```

2. Find the alert table name (logical ID starting with `Subscriptions`):

   ```bash
   aws cloudformation describe-stack-resources --region <StackRegion> --stack-name gpu-capacity-finder \
     --query "StackResources[?ResourceType=='AWS::DynamoDB::Table'].[LogicalResourceId,PhysicalResourceId]" \
     --output table
   ```

3. If alerts are on, list the user's items in the alert table (`<SubscriptionsTable>`), unsubscribe the address, and delete each item:

   ```bash
   aws dynamodb query --region <StackRegion> --table-name <SubscriptionsTable> \
     --key-condition-expression "userId = :u" \
     --expression-attribute-values '{":u":{"S":"<sub>"}}' \
     --query "Items[].[sk.S, subscriptionArn.S]" --output text
   aws sns unsubscribe --region <StackRegion> --subscription-arn <SubscriptionArn>   # from the #profile item
   aws dynamodb delete-item --region <StackRegion> --table-name <SubscriptionsTable> \
     --key '{"userId":{"S":"<sub>"},"sk":{"S":"<sk>"}}'                               # repeat per item, including #profile
   ```

4. Delete the user:

   ```bash
   aws cognito-idp admin-delete-user --region <StackRegion> \
     --user-pool-id <UserPoolId> --username user@example.com
   ```

Test launch records keep the user name of the person who ran each test, and they expire after 30 days.

## Upgrade

To apply a new version of the sample or a settings change, run the deploy again with the same options:

```bash
source .venv/bin/activate      # needed for cdk diff
cdk diff -c <same options>     # optional: preview the change
./deploy.sh -c <same options>
```

The deploy updates the functions and the page, and invalidates the CloudFront cache. Users, the site URL, and table data stay, except for a table that belongs to a feature you turn off. If you change `stackName`, the CDK creates a new, separate stack.

### Upgrade to Spot score groups

This version changes how Spot scores, Regions, and test launches work. Before you deploy it over an earlier stack:

1. **Remove or rename settings.** The deploy stops with a message that names the setting:
   - Removed: `testLaunchRegions` and `regions` (test launches now run in every enabled Region; use `excludeRegions` to leave Regions out), `regionScope`, `enableReservation`, `reservationHours`, `maxReservationsPerUser`, `apiBurstLimit` (now three times `apiRateLimit`), `tokenMinutes` (now 60 minutes), and `instanceTypes`.
   - Renamed: `instanceFamilies` is now `spotScoreFamilies` (it now picks only the families that get Spot scores; the page covers every GPU family), `enableSpotTestLaunch` and `enableSpotProbe` are now `testLaunchAllowSpot`, `enableProbe` is now `enableTestLaunch`, `probeCooldownMinutes` is now `testLaunchCooldownMinutes`, and `maxProbesPerUserPerDay` is now `maxTestLaunchesPerUserPerDay`. The three Spot score budget settings (`spotScoreNewConfigsPerDay`, `spotScoreReservePerDay`, `spotScoreMaxConfigs`) are now the single `spotScoreConfigsPerDay`.
2. **Zone scores are gone.** The page and the API show Region-level scores for GPU groups only (`scope=az` returns an error). New test launch records carry no score; older records keep the score they stored.
3. **Expect a build step.** The first request after the deploy starts the score updater, which builds the groups. Until it finishes, the page shows a building state. Old per-type Spot score requests are not sent again.
4. **Test launches changed.** A test launch now asks for a group or one size in a zone of the Region, and the cooldown keys on the group or size, market, and Region. The new deployment-wide cap `maxTestLaunchesPerDay` (default 100) applies; set `0` to turn it off. The API role gains test launch permissions in the enabled Regions. The IAM condition on the probe instance type is now `g*` or `p*`, because the API checks the GPU family rule before any launch.
5. **Drop the old deploy flags.** Remove `-c regions` and `-c regionScope` from your deploy command. Test launches then cover the enabled Regions minus `excludeRegions`.
6. **Know what a delete or rollback does.** The teardown custom resource acts only when the stack itself is deleted: its function reads the status of its own stack and removes probe resources only while that status is `DELETE_IN_PROGRESS`. When an update removes the custom resource (for example with `enableTestLaunch=false`), or CloudFormation rolls back a failed upgrade, the function removes nothing. Test instances still end themselves after 5 minutes; check for other probe resources with [Find leftovers](#find-leftovers). After a rollback to an older version, Spot scores can wait up to a day, because the requests of the new code still count against the AWS restriction.
7. **Set `alarmTopicArn`.** The new version adds CloudWatch alarms. Pass `-c alarmTopicArn=<TopicArn>` to get notified; see [Alarms](#alarms).

### Upgrade from a version with capacity holds or a saved list

Earlier versions of the sample could hold capacity with On-Demand Capacity Reservations and stored each user's list of instance types in a DynamoDB table. This version removes both. Before you deploy it over such a stack:

1. **Cancel the holds.** The new version cannot list or cancel them, and they bill until their end time. In each enabled Region:

   ```bash
   aws ec2 describe-capacity-reservations --region <Region> \
     --filters Name=tag:gpu-capacity-finder-reservation,Values=true Name=state,Values=active,pending,assessing \
     --query 'CapacityReservations[].CapacityReservationId'
   aws ec2 cancel-capacity-reservation --region <Region> --capacity-reservation-id <ReservationId>
   ```

2. **Tell users about their lists.** The deploy deletes the saved-list table (`Watchlist`) and the hold lease table (`Holds`). Users make their list again; the page now keeps it in each browser.
3. **Delete the backups you do not need.** The removed tables had point-in-time recovery, so DynamoDB keeps a system backup of each for 35 days (see [Teardown](#teardown)).

## Cost control checklist

The resources incur charges while they run. The largest charges come from GPU instances that test launches start.

- [ ] Use `enableTestLaunch=false` for a read-only deployment.
- [ ] Lower `maxTestLaunchesPerDay` and `maxTestLaunchesPerUserPerDay` to the daily number of test launches you accept, and raise `testLaunchCooldownMinutes` if tests run more often than users need.
- [ ] Keep `spotScoreFamilies` to the families you use. Fewer families mean fewer Spot score requests.
- [ ] Remind users that what they buy or create is billed, and the stack does not track it: a Capacity Block bought in the EC2 console is a purchase, and a Capacity Reservation (from the command that **Reserve with CLI** gives them) bills at the On-Demand rate from creation until it ends or the user cancels it.
- [ ] Raise `notifyIntervalMinutes`, `offeringCacheHours`, and `quotaCacheMinutes` to reduce scheduled and API calls. Lower a cache time only when users need fresher data, and expect more calls.
- [ ] Keep `apiRateLimit` no higher than your users need. The dashboard's bursts count against your account's EC2 Describe API limits.
- [ ] Check for leftover test instances after heavy use, and attach an SNS action to the two cleanup alarms (see [Alarms](#alarms)) so that you hear about a failing cleanup.
- [ ] Tear down the stack when you no longer need it.

## Monitor

### Log groups

The functions run in the stack Region and write to log groups with one-month retention. Find the names:

```bash
aws cloudformation describe-stack-resources --region <StackRegion> --stack-name gpu-capacity-finder \
  --query "StackResources[?ResourceType=='AWS::Logs::LogGroup'].[LogicalResourceId,PhysicalResourceId]" \
  --output table
aws logs tail <LogGroupName> --since 1h --region <StackRegion>
```

| Log group (logical ID prefix) | Function | What to look for |
|---|---|---|
| `ApiLogs` | API function | `AWS call failed` (the page shows a 502 error), `unhandled error`, `usage metrics failed in <Region>`, `call failed in <Region>`, `Spot score groups unavailable`, `terminate failed for` (a test instance that the cleanup function then terminates) |
| `ScoreUpdaterLogs` | Spot score updater | One line per run: `score updater: {...}` with `mode` (`warm`, `daily`, or `bootstrap`), `updated`, and the counts of configurations, repeated, granted, planned, failed, and limited requests. `daily update: N groups, M configurations, K Regions answered of L` after an update. `daily update skipped` means no Region list or no offering lookup answered, and the next run tries again. `offering lookup failed in <Region>` means that Region's groups stay as they are. `Spot placement score request failed for` marks a failed request. |
| `CleanupLogs` | Cleanup function | One line per run: `terminated leftover probe instances: {...}; deleted leftover probe launch templates: {...}; failed Regions: [...]`. `probe cleanup failed in <Region>` (a warning) means the function could not clean that Region. |
| `NotifierLogs` | Notifier function | One line per run: `alert check: {...}` with the counts `subscriptions`, `skipped`, `groups`, `failedGroups`, `notified`, `unconfirmed`, `rejected`, and `failedItems`. `signal check failed for ...` marks a failed query; the alert keeps its state and the next run tries again. `capacity block check rejected for ...` marks a Capacity Block alert that EC2 refused, with a code such as `cb-invalid-duration`; the page shows it to the user, and no email goes out. |
| `ProbeTeardownLogs` | Teardown function | Runs when you delete the stack. `probe teardown left ... and failed in ...` names what it could not remove. The stack delete removes this log group too. |

Each API request writes one structured INFO line to `ApiLogs` with the fields `msg` (`request`), `method`, `route`, `status`, `durationMs`, `cacheHits`, `cacheMisses`, `coldStart`, and, on an error, `errorCode`. The line also holds `region` when it is one of the enabled Regions and `instanceType` when it is a well-formed instance type name. It does not hold tokens, claims, email addresses, or other query values. The API Gateway access log is not enabled. To see the median and 90th percentile duration per route, run this query in CloudWatch Logs Insights on the `ApiLogs` group:

```
filter msg = "request"
| stats count(*) as requests, pct(durationMs, 50) as p50, pct(durationMs, 90) as p90 by route
| sort p90 desc
```

### Watch for throttling

When more requests arrive than `apiRateLimit` and its burst allow, API Gateway answers with HTTP 429, and the page shows `The service is busy.` after its retries. In the API Gateway metrics for the HTTP API in Amazon CloudWatch, the `4xx` metric counts these responses together with other client errors, so a steady rise at busy times points to the throttle. If it does, raise `apiRateLimit`.

### Watch for slow Regions

EC2 calls use a 3-second connect timeout and a 15-second read timeout, with 3 attempts in total, so an unreachable Region cannot hold a request for long. A page that reads every enabled Region waits about 12 seconds in total. A Region that has not answered by then shows as an error row (`The Region did not answer.`) and is skipped for 5 minutes; the other Regions are not affected. To see how often it happens, search the API function log group for `did not answer`. A Region the account has not enabled shows `Not enabled` instead and is not a fault. If a Region keeps failing, add it to `excludeRegions`.

The other AWS clients that the API makes are bounded too. Service Quotas, CloudWatch, Price List, SSM, and SNS calls use a 3-second connect timeout and a 10-second read timeout, with 2 attempts in total. The call that starts the score updater uses a 5-second read timeout and 1 attempt; a failed start is tried again by a later request. When the CloudWatch usage metrics fail or time out, the Quotas page still shows the limits, with the Used column empty and the note `Usage is not available right now for <Region>`.

The skip is shared. A Region that gave no answer gets one small item in the cache table, `#down#<Region>`, with the time until which it is skipped as both its value and its TTL, so it clears itself after 5 minutes. A Region that answered with an error, including throttling, is not marked, and a marker does not mean that the Region sells nothing. To see the current markers:

```bash
aws dynamodb scan --region <StackRegion> --table-name <OfferingCacheTableName> \
  --filter-expression "begins_with(cacheKey, :p)" \
  --expression-attribute-values '{":p":{"S":"#down#"}}' \
  --query 'Items[].[cacheKey.S,downUntil.N]' --output table
```

### A failed lookup is not read as "not sold"

What each Region offers is the base of almost every answer, so a failed lookup must not become an empty answer. A Region whose `DescribeInstanceTypeOfferings` call fails, times out, or is skipped by a marker is reported as an error row or as unknown, and nothing is written to its cache item. The score updater treats a failed lookup the same way: the Region's groups stay as they are until a later daily update gets an answer.

### Answers served while they are updated

Each Region's offering item has a freshness window (`offeringCacheHours`) and is kept for 7 days. Once the window ends, the next request answers from the stored item right away, with the item's own `Updated ...` time, and asks EC2 again in the same request. Only the request that takes that item's refresh lock does so, at most once a minute per Region, and it gives up after 4 seconds, leaving the last 6 seconds of the function timeout free. A failed refresh leaves the stored answer in place. An item older than 7 days is gone, and the next reader waits for the lookup.

### What the scheduled functions do

- The **score updater** runs at minutes 0, 15, 30, and 45 of every hour. The run at 00:30 UTC is the daily update. It holds a lock item so that runs do not overlap. See [How Spot scores stay current](#how-spot-scores-stay-current).
- The **cleanup function** runs every 15 minutes. It reads the enabled Regions (minus `excludeRegions`) and, in each, lists instances with the tag `gpu-capacity-finder-probe=true` in the `pending`, `running`, `stopping`, or `stopped` state and terminates those launched more than 10 minutes ago. It also deletes launch templates with that tag that are older than 1 hour. One Region that fails does not stop the others.
- A **test instance ends itself** as a second backstop. Every test launch passes the user data script `shutdown -h +5` and sets the shutdown behaviour to `terminate`, so the instance powers off 5 minutes after the operating system boots and terminates, even if the page's terminate call and the cleanup function both fail. A successful test launch is normally terminated immediately.
- Two **CloudWatch alarms** of the safety net exist when `enableTestLaunch=true`: `<stack>-probe-cleanup-errors` (the `Errors` metric of the cleanup function) and `<stack>-probe-cleanup-region-failures` (a metric filter on the cleanup log group for `probe cleanup failed in`, in the namespace `GpuCapacityFinder/<stack>`). Both use a 15-minute period, one scheduled run.
- The **notifier function** runs every `notifyIntervalMinutes`. It reads the whole alert table, skips alerts that no longer match the enabled Regions, and queries each distinct signal once with read-only calls. The Capacity Block check asks for the alert's instance count and duration with no date window. Spot score alerts read the score updater's cached group scores and make no Spot score request. It emails a user only when an alert turns from not available to available, and only after the user confirmed the SNS subscription. A failed check keeps the last state and retries at the next run.

### Alarms

The stack creates CloudWatch alarms for its own functions. They read the `AWS/Lambda` metrics that the platform publishes, so they need no code and no extra permission, and each one costs a small monthly amount.

| Alarm | Metric | Period and threshold | What it means |
|---|---|---|---|
| `<stack>-api-errors` | `Errors` of the API function | 5 minutes, above 0 | A request failed, or the platform stopped a run at the 25-second timeout. |
| `<stack>-api-duration` | `Duration` (Maximum) of the API function | 5 minutes, above 20,000 ms | A request came close to the timeout: a cold cache, a slow Region, or a large fan-out. |
| `<stack>-api-throttles` | `Throttles` of the API function | 5 minutes, above 0 | Lambda refused runs because the account's concurrency limit was reached. |
| `<stack>-score-updater-errors`, `-duration`, `-throttles` | the same metrics of the updater | 5 minutes; `Duration` above 240,000 ms (80% of its 5-minute timeout) | A Spot score update failed, ran long, or was throttled. One failed run delays the scores until the next run. |
| `<stack>-notifier-errors`, `-duration`, `-throttles` | the same metrics of the notifier function | 5 minutes; `Duration` above 192,000 ms (80% of its 4-minute timeout) | An alert check failed, ran long, or was throttled. |
| `<stack>-probe-cleanup-duration`, `-throttles` | the same metrics of the cleanup function | 5 minutes; `Duration` above 96,000 ms (80% of its 2-minute timeout) | The safety net ran long or was throttled. |
| `<stack>-probe-cleanup-errors`, `-probe-cleanup-region-failures` | see [What the scheduled functions do](#what-the-scheduled-functions-do) | 15 minutes, one scheduled run | Leftover test instances may keep running. |

Every alarm uses one data point of one period and treats a period with no data as not breaching. The cleanup alarms exist only with `enableTestLaunch=true` and the notifier alarms only with `enableNotifications=true`.

**Get notified.** Set `alarmTopicArn` to an SNS topic you already own, in the stack Region, and every alarm of the stack publishes to it. Deploy with the topic and your other options, because a setting you leave out returns to its default:

```bash
aws sns create-topic --region <StackRegion> --name gpu-capacity-finder-alarms
aws sns subscribe --region <StackRegion> --topic-arn <TopicArn> --protocol email --notification-endpoint you@example.com
./deploy.sh -c alarmTopicArn=<TopicArn> -c <your other options>
```

The stack only references the topic, so its access policy stays yours and no IAM permission is added to a role. The policy must let CloudWatch publish:

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

If the topic is encrypted, use a customer managed key, not the AWS managed key `alias/aws/sns`: CloudWatch cannot publish to a topic encrypted with the AWS managed key. The key policy must let `cloudwatch.amazonaws.com` call `kms:GenerateDataKey*` and `kms:Decrypt` on the key. An alarm whose publish is refused changes state in the console but delivers nothing, so send one test notification after you deploy:

```bash
aws cloudwatch set-alarm-state --region <StackRegion> --alarm-name <stack>-api-errors \
  --state-value ALARM --state-reason "testing the notification path"
```

The alert topic of the email alerts is not used for alarms: it belongs to the users who subscribed, and it uses the AWS managed key.

### Find leftovers

Test instances, launch templates, and probe security groups, in each Region where users tested:

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

## Backup and data

The tables use on-demand capacity and have point-in-time recovery turned on. `cdk destroy` deletes the tables, and DynamoDB keeps a system backup of each table for 35 days (see [Teardown](#teardown)). To keep data on your own terms, create an on-demand backup or an export before you destroy the stack.

| Table (logical ID) | What it stores | Retention |
|---|---|---|
| Probe history table (`Probes`) | One record per test launch: Region, group or size, market, result, the zone and size obtained, EC2 error code, and the user name of the caller. Also short-lived cooldown locks, one daily counter per user and UTC day (`#probe-count#...`), and one deployment-wide counter per UTC day (`#probes#YYYY-MM-DD`). | Records expire after 30 days. Lock items expire one day after their cooldown, and counters two days after their day ends. |
| Cache table (`OfferingCache`) | Shared items, no user data: `offerings#<Region>` (offered types, zones, family names), `cbtypes#<Region>`, `regions#enabled`, `#regions-first-seen` (no expiry), `#sps-groups` (the stored score groups: sizes, Region chunks, GPU counts, offered types per Region, and the scored families; no expiry), the Spot score answers, `spshist#<group>#regions` (2 days), `spscfg#<fingerprint>` (8 days after the last write) and `#sps-budget`, `quota#<Region>`, `price-od#<type>#<Region>`, `price-spot#<Region>`, `#lock#<item>` refresh locks, and `#down#<Region>` markers. | The cache time of each item (see [Tune API call volume](#tune-api-call-volume)). Lock items expire one day after their window. |
| Alert table (`Subscriptions`) | The user's alerts, and one `#profile` item with the email address, the SNS subscription Amazon Resource Name (ARN), and the alert count. | Until the user deletes the alerts. The profile goes with the last alert. |

Expired items stay until DynamoDB deletes them in the background. Each user's list of instance types, recent Capacity Block searches, last used Region and type, and language choice stay in that user's browser storage, not in the stack. Other data: the user pool stores users and their email addresses, the SNS topic stores email subscriptions, and the bucket stores the page and a `config.json` file with the API URL, app client ID, sign-in domain, and site URL. Those values are not secrets; the page needs them to sign in.

## Security notes

- The S3 bucket blocks public access, requires Transport Layer Security (TLS), and serves only through CloudFront with origin access control. CloudFront adds a content security policy, HTTP Strict Transport Security (HSTS), and frame and referrer headers.
- Self sign-up is off. MFA with an authenticator app is required, and text-message MFA is off. Passwords need 12 characters with mixed case, digits, and symbols.
- The API authorizer rejects a request without a valid ID token from the user pool for the app client. The function takes the user ID from the token, not from the request.
- Session length is a trade-off. A longer `sessionHours` keeps a signed-in browser tab usable for longer without a new sign-in, and MFA applies at sign-in only, not when the page renews a token. Disabling a user stops renewals, and **Sign out** revokes the refresh token, but an ID token that was already issued still works at the API until it expires, up to 60 minutes.
- The app client does not enable direct password (Secure Remote Password, SRP) sign-in through the API. The page signs in through the hosted sign-in page with the authorization code flow.
- Test launches use the default VPC with no public IP address, Instance Metadata Service Version 2 (IMDSv2) required, and a dedicated security group with no inbound rules. An On-Demand test launch sets its Capacity Reservation preference to `none`. Every test passes a static user data script (`shutdown -h +5`, no secrets, no account data, no network calls) so that the instance powers off, and with the shutdown behaviour `terminate` ends itself, after 5 minutes counted from the moment the operating system boots.
- The API role has no permission to create, modify, or cancel Capacity Reservations or to buy Capacity Blocks. The page only links to the EC2 console and shows a Capacity Reservation command; users act with their own credentials.
- **Test launch permissions cover every Region.** Because test launches run in every enabled Region minus `excludeRegions`, the EC2 resource ARNs of the launch statements name every Region, and the function refuses an excluded or not enabled Region before it calls EC2. The conditions stay: `RunInstances` only for instance types that match `g*` or `p*` and only with the probe tag (the function narrows that to the naming rule), `CreateFleet` and `CreateLaunchTemplate` only with the probe tag, `RunInstances`, `CreateFleet`, and `DeleteLaunchTemplate` on launch templates only when the template carries the tag, `CreateTags` only at creation, `TerminateInstances` only on instances that carry the tag, the probe security group only with its own tag, and `RunInstances` and `CreateFleet` only with images owned by Amazon and only into the tagged probe security group. `DescribeInstances` lets the function find the instance of a launch call with an unclear result, so it can terminate it. The statements are `LaunchTaggedProbeInstance`, `CreateTaggedProbeFleet`, `LaunchSupportingResources`, `LaunchAmazonImageOnly`, `LaunchInProbeSecurityGroupOnly`, `FindUnknownOutcomeProbe`, `UseProbeLaunchTemplate`, `CreateProbeLaunchTemplate`, `CreateProbeSecurityGroup`, `CreateProbeSecurityGroupInVpc`, `TagProbeSecurityGroupOnCreate`, `TagOnLaunchOnly`, and `TerminateProbeInstancesOnly`. `CountDailyProbes` allows DynamoDB `UpdateItem` only on the counter partitions. The role can read the two Amazon Linux 2023 image parameters (x86_64 and arm64) in AWS Systems Manager Parameter Store.
- On the tables, the API role can `Query` the probe history table and write probe records and cooldown locks with `PutItem` (`StoreProbeRecords`, with a `dynamodb:LeadingKeys` condition that keeps it off the counter partitions). It can read and write cache table items (`GetItem`, `BatchGetItem`, and `PutItem`). In every deployment, it can also start only the score updater function (`lambda:InvokeFunction`) and read the AWS public Region long-name parameters in the stack Region (`ssm:GetParameter` and `ssm:GetParameters`).
- With email alerts on, the API role can also read and write the alert table (`Query`, `PutItem`, `DeleteItem`, and `UpdateItem`), subscribe only email endpoints to the alert topic (`sns:Subscribe` with the condition `sns:Protocol=email`), and unsubscribe only that topic's subscriptions.
- The notifier role exists only with `enableNotifications` set to `true`. It can `Scan` and `UpdateItem` the alert table, read and `PutItem` cache table items (for the shared enabled-Region list and its lock), call `ec2:DescribeCapacityBlockOfferings` and `ec2:DescribeRegions` on resource `*` (`ReadAlertSignals`), publish only to the alert topic (`sns:Publish`), and read the attributes of that topic's subscriptions (`sns:GetSubscriptionAttributes`).
- The cleanup role can terminate and delete only tagged resources. The teardown role can also delete security groups that carry the probe security group tag, and it can read the status of its own stack (`cloudformation:DescribeStacks` on that stack only), so it removes probe resources only when the stack is deleted. The updater role has read-only EC2 actions and access to the cache table.
- The read actions use resource `*` because they have no resource-level permissions: the EC2 Describe and offering reads, `GetSpotPlacementScores`, the Service Quotas list calls, `ec2:DescribeInstanceTypes`, `ec2:DescribeRegions`, `cloudwatch:GetMetricData`, and `pricing:GetProducts`. `pricing:GetProducts` reads public list prices; the function sends it to the `us-east-1` endpoint.
- One Lambda role serves the users of the deployment, and IAM cannot tell users apart inside one role. The function keys alert table access by the user's `sub` from the token. The role reads and writes whole tables.
- The page has no AWS WAF web ACL, no custom domain, and no access logging, and the default API Gateway endpoint stays reachable on its own. Add them before you share the page widely.
- The world outline of the Region map comes from Natural Earth, which is in the public domain. The [THIRD-PARTY-LICENSES](../THIRD-PARTY-LICENSES) file at the sample root lists it. Keep that file if you copy the page.
- The SNS topic uses the AWS managed key for Amazon SNS. Use a customer managed AWS Key Management Service (AWS KMS) key if you need your own key policy.

The [Limitations section of the README](../README.md#limitations) lists these and other limits.

## Teardown

The resources incur charges while they run, including GPU instances that test launches start. Remove them in this order.

1. **Destroy the stack.**

   ```bash
   source .venv/bin/activate   # the CDK app needs the Python environment
   cdk destroy   # add -c stackName=<name> if you deployed with a custom stack name
   ```

   This removes the bucket contents, the tables, the SNS topic with its subscriptions, the user pool, and the log groups that the stack creates for its functions. When `enableTestLaunch=true`, the stack also runs a custom resource (`Custom::ProbeTeardown`) when it is deleted. Its function terminates test instances that still run, then deletes the tagged probe launch templates and the tagged probe security groups in every enabled Region minus `excludeRegions`. The functions that create these resources are deleted first, so nothing creates a new one while the teardown runs. The custom resource reports success within a bounded time (the function stops its own work about 30 seconds before its 5-minute timeout, and CloudFormation stops waiting after 10 minutes), so a Region that fails does not block the delete. The function logs what it could not remove, as `probe teardown left ...`, but its log group is deleted with the stack.

   Each DynamoDB table has point-in-time recovery turned on. When you delete such a table, DynamoDB creates a system backup and keeps it for 35 days. The backup of the alert table contains the email addresses of your users. List the backups in the stack Region and delete the ones you do not need:

   ```bash
   aws dynamodb list-backups --backup-type SYSTEM --region <region>
   aws dynamodb delete-backup --backup-arn <arn> --region <region>
   ```

2. **Check for probe resources in every Region.** The custom resource skips a Region you added to `excludeRegions` after testing there, and a Region it could not reach in time. In each Region where users ran test launches, run the commands in [Find leftovers](#find-leftovers). Terminate each leftover instance, delete each leftover launch template with `aws ec2 delete-launch-template --region <Region> --launch-template-id <Id>`, and delete each leftover security group with `aws ec2 delete-security-group --region <Region> --group-id <GroupId>` (a security group cannot be deleted while a test instance that uses it still exists).

3. **Delete leftover log groups of CDK helper functions.** These log groups can remain after `cdk destroy`. List them in the stack Region (the default `<stackName>` is `gpu-capacity-finder`), check each name, and delete the ones that belong to this stack:

   ```bash
   aws logs describe-log-groups --region <StackRegion> \
     --log-group-name-prefix /aws/lambda/<stackName>- --query 'logGroups[].logGroupName'
   aws logs delete-log-group --region <StackRegion> --log-group-name <LogGroupName>
   ```

The tags are the same in each deployment, so these commands and the teardown function also match resources of another deployment in the same account. Use one deployment per account: see [One deployment per account](#one-deployment-per-account).

`cdk destroy` does not touch Capacity Blocks that users bought in the EC2 console or Capacity Reservations that users created with the copied commands. They belong to the users and keep billing until they end or are cancelled; ask users to check the EC2 console.

## Hand over to your team

Confirm these items after the deploy, then hand over the site URL and the [user guide](user-guide.md).

- [ ] **The API refuses anonymous calls.** Read the API URL from the page's configuration, then call it without a token. The answer is HTTP 401:

  ```bash
  API_URL=$(curl -s <SiteUrl>config.json | python3 -c 'import json,sys; print(json.load(sys.stdin)["apiUrl"])')
  curl -s -o /dev/null -w '%{http_code}\n' "$API_URL/api/config"   # expect 401
  ```

- [ ] **The first user exists and can sign in.** Create the user, sign in, set a password, and enroll an authenticator app.
- [ ] **The Spot score groups exist.** On the **Spot score** tab, the first load shows a building state, and after a minute or two the table shows Regions as rows and score groups as columns, with an `Updated` label. Cells for a Region that joins later read `Spot score after the next daily update`. If the list of families is long or the budget is small, some cells can read planned until the budget has room.
- [ ] **A read-only check works.** On the **Quotas** tab, quotas load. On the **Capacity Blocks** tab, a search for one Region and one P5 type ends with offerings or `No blocks match.`, not an error line. No red message with **Retry** stays on screen.
- [ ] **One test launch works** (if `enableTestLaunch=true`). On the **On-Demand and Spot** tab, run one test launch on the smallest group in a Region with a default VPC (the **Test launch** button in the Region detail of the **Spot score** tab jumps there), read the result, and confirm that no instance tagged `gpu-capacity-finder-probe` remains after a few minutes and that the page shows today's counts.
- [ ] **Alert email works** (if `enableNotifications=true`). Add one alert, confirm the subscription email from AWS Notifications, and check that **Last checked** updates after the next check.
- [ ] **Users know what the page does not do.** It does not create reservations or buy blocks. A Capacity Block that they buy in the EC2 console is a purchase, and a copied **Reserve with CLI** command creates a Capacity Reservation that bills at the On-Demand rate from creation until it ends or they cancel it.
- [ ] **You know where the logs are.** Note the `ApiLogs`, `ScoreUpdaterLogs`, `CleanupLogs`, and `NotifierLogs` log group names from [Monitor](#monitor).
- [ ] **You know the teardown order.** See [Teardown](#teardown).
