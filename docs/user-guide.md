# GPU Capacity Finder user guide

This guide is for you if you sign in to GPU Capacity Finder to look for graphics processing unit (GPU) capacity on Amazon Elastic Compute Cloud (Amazon EC2). The person who deployed the page has a separate [administrator guide](admin-guide.md).

## What the tool answers

GPU Capacity Finder answers one question: where and how can you get the GPU instance you need, right now or soon? It reads the capacity signals for the Regions that your account has enabled (minus the Regions your administrator left out) and for the GPU instance families, and lets you check a signal with one test launch. It does not manage instances, reserve capacity, or launch your workloads: when you decide to act, you buy a Capacity Block in the EC2 console, and you reserve capacity in the EC2 console or with an AWS Command Line Interface (AWS CLI) command that the page gives you to copy.

The page has six tabs, one per task, in this order:

- **Spot score** compares Regions by Spot placement score for each GPU group, as a table or on a map.
- **On-Demand and Spot** shows one instance type in one Region: the On-Demand list price, the Spot score, the test launch panel, and the Spot price of each Availability Zone, with a command to reserve capacity.
- **Capacity Blocks** searches the Capacity Block offerings that you can buy, across several Regions and instance types at once, and finds the largest instance count on offer.
- **Quotas** shows your service quotas and how much of each is in use, because a quota can block a launch before capacity matters.
- **Alerts** lists your email alerts. It appears only when your administrator turned on email alerts.
- **Users** explains how to add, update, and delete users in the Amazon Cognito console. Ordinary users do not need it.

Each answer is point in time: it describes the moment of the check, not the future.

## Sign in and first-time setup

Your administrator creates your user. Self sign-up is off.

1. Look for an email with your user name and a temporary password. The temporary password expires after seven days. If it expires, ask your administrator to send a new one.
2. Open the site URL that your administrator gave you, and choose **Sign in**. The page sends you to the Amazon Cognito sign-in page.
3. Sign in with your email address and the temporary password.
4. Set a new password. It needs at least 12 characters, with an uppercase letter, a lowercase letter, a digit, and a symbol.
5. Set up multi-factor authentication (MFA). The sign-in page shows a QR code. Scan it with an authenticator app that supports time-based one-time passwords (TOTP), then enter the six-digit code from the app. The sample accepts only an authenticator app, not text messages.
6. The page opens on its first tab.

At each later sign-in, you enter your password and the current code from your authenticator app. If you forget your password, choose **Forgot your password?** on the sign-in page; the code goes to your email address.

Your session lasts up to 12 hours by default; your administrator sets the length. While the tab stays open, the page renews your sign-in in the background. The page keeps your sign-in for the current browser tab only, so closing the tab signs you out. Choose **Sign out** at the top right to end the session.

The header has a language switcher for English, Traditional Chinese, and Simplified Chinese. The page remembers your choice in this browser, and `?lang=en-US`, `?lang=zh-TW`, or `?lang=zh-CN` in the address selects a language.

## The page at a glance

Each tab has its own controls in its header, such as a **Region** list. The page remembers the last Region and instance type you used, in this browser, and uses them as the starting choice on the other tabs.

A tab loads its data the first time you open it, and again when you change its choices. A thin orange bar under the top bar shows while a request runs. A button that starts an action shows a spinner and text such as `Testing...` until it finishes. When a request fails, a red message appears where the data would be, with a **Retry** button. When only part of a tab fails, the message names that part, and the rest of the tab still shows. The address bar keeps the open tab, so a bookmark or a reload opens the same tab. You can move between tabs with the arrow keys, and with **Home** and **End**. On a narrow screen, such as a phone, each table row shows as a card.

### How fresh the data is

Every user of the deployment shares the same answers from AWS. Each data block shows when AWS answered, for example `Updated just now` or `Updated 3 minutes ago`. Hover over the label to see the exact time.

- Opening a tab, or coming back to the page after you switched away, loads again data on screen that is older than its cache time. You do not need to reload the page.
- Spot scores update on a schedule, not when you open the page. See [How Spot scores update](#how-spot-scores-update).
- **Refresh** asks AWS again now. Because the answers are shared, the deployment asks AWS again for the same item at most once per minute. If someone refreshed it less than a minute ago, you see that answer and a note: `This was refreshed less than a minute ago, so you see that answer. You can refresh again in N seconds.`
- The old data stays on screen until the new data arrives.

### Edit instance list

By default, the **Instance type** lists show every offered GPU type, in natural order and grouped by family. To keep only the ones you care about, choose **Edit instance list**, untick the types you do not want, and choose **Save**. A custom list can have up to 20 types, and you must keep at least one. The page keeps the instance list in this browser only; it does not follow you to another browser or device. To go back to every offered type, open the dialog, choose **Select all**, and choose **Save**.

## Spot score tab

The tab answers "Which Regions are likely to fulfil a Spot request for the GPU I need?".

### Read the table

The table has one row per Region and one column per **score group**. A score group is one instance family with one GPU count, for example `g6e · 4 GPUs`. Hover over a column heading, move the keyboard focus to it, or tap it to see the sizes in the group, with the lowest Spot price of each size in the table's Regions when it is known. Press Escape to close it. The cell shows the Spot placement score of that Region for that group, from 1 to 10. Regions that sell none of the shown score groups collapse into one line below the table, such as `3 Regions are in none of the shown score groups`; choose it to list them.

The score answers one flexible question: how likely is it that a Spot request for any listed size of the group is fulfilled in the Region? It is not the score of one size. To check one size, run a test launch for that size.

| Cell | Meaning |
|---|---|
| Coloured, with a number only | The Region sells at least one size of the group. The colour is the Region-level score: higher chance (`7 to 10`), medium chance (`4 to 6`), or lower chance (`1 to 3`). |
| `–` | Not scored yet. The Region sells a size but joins the group at the next daily update, or its first score arrives at the next refresh (every 15 minutes), or its request waits for room in your deployment's daily budget, or AWS paused new requests for now. |
| `•` | The Region belongs to the group, but AWS returned no Spot score for it (`Sold, no score from AWS`), or the score did not load. AWS's answer can leave a Region out with no error. You can still run a test launch there. |
| Blank | The Region does not sell the group. |

The cell itself holds only the number or the mark. The detail is in the hover text and in the label that a screen reader reads, such as `Spot score after the next daily update (HH:MM local)`, with the sizes and the lowest Spot price. That price reads `from $x/h` for the cheapest size in its cheapest Availability Zone, and the tooltip also lists the price of each size. The Region detail chip (below) shows the same price.

The table shows only the families that your administrator chose for Spot scores. Other GPU families still appear in supply, prices, quotas, test launches, and Capacity Blocks, but they have no score column.

### Table, map, and families

- **Table** and **Map** (at the top of the tab) switch the view. The map places each enabled Region as a dot with its code, coloured by the highest Spot score among the shown groups. Choose a dot to open the Region detail. To separate Regions whose dots overlap, zoom in with **+** and **−** above the map, or hold Ctrl or Cmd and use the mouse wheel; drag to move the zoomed map, and choose **Reset zoom** to see the whole world again. The dots and labels keep their size while you zoom. A Region without coordinates in the page is listed under the map.
- **Families** filters the columns. Open the list to tick the families you want, or choose **Select all families** when none is ticked.

### Region detail

Choose a Region in the table, or a dot on the map, to open its detail panel, `{region}: Spot scores and GPU types`. It has two lists of chips:

- **Spot score groups**: the score groups of the Region, each with its score and its lowest Spot price.
- **Other GPU types sold here (no Spot score)**: types of families that have no score column.

Choose a chip to see three buttons for that group or type: **Test launch**, **Check On-Demand and Spot**, and **Search Capacity Blocks**. Each one jumps to the tab that does the work, with the Region and type already chosen. **Test launch** opens the test launch panel on the **On-Demand and Spot** tab.

An offering means that EC2 sells the type in the Region. It does not mean that capacity is free.

### How Spot scores update

Spot scores update on a schedule, and the label shows the age, for example `Updated 12 minutes ago`.

- **Every 15 minutes**, the deployment asks AWS again for the scores it already asks for, and stores the answers for everyone.
- **Once a day, at 00:30 UTC**, the deployment updates the groups: it adds new sizes, new Regions, and new families that your administrator added, and removes Regions your administrator excluded. A Region that you enabled yesterday shows `Spot score after the next daily update` until then.
- **Refresh** repeats the requests that the deployment already uses, at most once a minute for a group. It does not create a new request or change a group.

AWS restricts how many different Spot score requests an account can make in a day, so a new group, Region, or size can wait for the next daily update or for room in the budget.

### How to use the Spot placement score

A Spot placement score is an AWS estimate of how likely a Spot request like this one succeeds at that moment. It changes within minutes. The introduction at the top of the tab says it in short: the score is AWS's estimate (1 to 10) of the chance that a Spot request for one instance, in one of the group's sizes, can be fulfilled in the Region right now. A high score is no promise of a launch, and a test launch is the way to check one size.

Five rules:

1. **Compare, do not read the number alone.** Try the higher scores first, and use the score to rank Regions and groups against each other.
2. **A high score is no promise of a launch, and a low score can still launch.**
3. **A group score describes the group.** It says that some listed size can likely be fulfilled, not which one. Check a size with a test launch.
4. **Look at the age of the score.** The page shows `Updated N minutes ago`.
5. **The test launch is the real check.** The result shows the zone and size that EC2 picked, and the results of the last 24 hours for every user. See [Run a test launch](#run-a-test-launch).

The score is not a stock list of Spot capacity, and it is not On-Demand or Capacity Block availability.

## On-Demand and Spot tab

The tab shows one instance type in one Region. Pick the **Region** and the **Instance type** in the tab header, and use **Edit instance list** to shorten the type list. The page starts from your last used choice, and the **Check On-Demand and Spot** button in the Spot score Region detail jumps here. The tab has these parts, top to bottom:

- **On-Demand list price**, a line such as `On-Demand list price: $x/hour`, with the age of the price. It is the public list price for Linux in the Region. It reads `Not available` when the Price List has no match.
- **Spot score by Region**: the score of the type's score group in each Region, with a note when the family gets no Spot score in your deployment.
- **Test launch**: the panel that asks EC2 for one instance. See [Run a test launch](#run-a-test-launch).
- **Availability Zones**: a table with the Spot price of each zone. See [Zone prices and Reserve with CLI](#zone-prices-and-reserve-with-cli).

### Run a test launch

A test launch asks EC2 for one instance and terminates it right after the launch call. It tells you whether capacity existed at that moment.

1. In the test launch panel on the **On-Demand and Spot** tab, use the tab's **Region** list to pick the Region, then pick a score group under **What to test**. You can also pick one size of the group.
2. Pick the market: On-Demand or Spot. The Spot choice is missing when your administrator turned the Spot test launch off.
3. Choose **Test launch**. A dialog shows the group or size and the market. An On-Demand test launches one instance and terminates it right away, and incurs a charge for the minimum billing period of the instance type. A Spot test requests one Spot Instance and terminates it right away, and can incur a small charge. Choose **Launch and terminate**, or **Cancel**.
4. The button shows `Testing...`, then the result shows with the zone and size that EC2 picked, or the reason it found no capacity.

How the launch works: the page asks EC2 for one instance across every default subnet of the Region (one per Availability Zone) and every size of the group that fits your vCPU quota. EC2 picks one. For Spot, the request uses the `price-capacity-optimized` strategy, which favours pools with capacity and a low price. For On-Demand, it uses the `lowest-price` strategy. If no size of the group fits your quota, the page says that your quota has too little free and that no instance was launched, and makes no launch call.

The test launches into the default virtual private cloud (VPC) of the Region, with no public IP address and a security group that has no inbound rules. It uses an Amazon Linux 2023 image that matches the architecture of the type. An On-Demand test does not launch into an open Capacity Reservation, so it measures free capacity. You cannot connect to the test instance or use it. Each test launch also carries a tiny startup script that powers the instance off after 5 minutes, and because the launch is set to terminate on shutdown, the instance then ends itself. This is a backstop; a successful test launch is normally terminated immediately.

If the Region has no default VPC, the test launch is off with the message `No default VPC in this Region`. Ask your administrator.

**Limits.** Your administrator sets three limits, and the page shows your counts:

- A **cooldown** (15 minutes by default). A repeat test for the same group or size, market, and Region returns the earlier result instead of launching again.
- A **per-user daily cap** (`maxTestLaunchesPerUserPerDay`, 50 by default). It resets at 00:00 UTC, not 24 hours after your first test.
- A **deployment-wide daily cap** (`maxTestLaunchesPerDay`, 100 by default), shared by every user. The page shows `Test launches today: N of M (all users)` and your own count.

A test that reaches EC2 counts, including one that EC2 refuses, for example for no capacity or a quota limit. A test that ends without a clear result also counts. A cached cooldown result, an answer that says another test is running, a request that the service or EC2 throttled (the page shows `Too many requests at once...`, or `Error` with `RequestLimitExceeded`), a request that the server had too little time to start, and a request that fails before the launch call (such as a quota with too little free, or `No default VPC in this Region`) do not count.

Under the panel, the page lists the **observed results** of the last 24 hours for the Region: for each group and market, how many tests launched, found no capacity, hit a quota, or failed, and the zones and sizes that EC2 picked. The counts include every user of the deployment, and no user names.

### How to read test launch results

| Result | What it means | Next step |
|---|---|---|
| `Launched` | EC2 launched the instance, so capacity existed for that group or size and market at that moment. The page shows the zone and size EC2 picked, and terminated it right after the launch call. | Launch your own instance soon. The capacity can be gone a few minutes later. If you need the capacity to stay, consider a Capacity Block. |
| `No capacity` | EC2 returned a capacity error, such as `InsufficientInstanceCapacity`, for the sizes and zones it tried. | Try another Region with a high score, another group, or a Capacity Block. Several `no capacity` results in a row are a stronger signal than one. |
| `Quota limit` | A limit blocked the launch, such as `VcpuLimitExceeded`. This is not a lack of capacity. | Open the **Quotas** tab. See [Scenario 3](#scenario-3-i-have-a-quota-error). |
| `Not supported` | EC2 returned the `Unsupported` error code for this request. | Try the other market or another group. |
| `Error` | EC2 returned another error. The result shows EC2's reason and error code. When the code is `RequestLimitExceeded`, EC2 throttled the request: nothing was launched and the test does not count; test again in a moment. | Read the error code. Ask your administrator if it repeats. |
| `The test launch ended without a clear result. ...` | The sample could not confirm whether EC2 launched an instance, for example because EC2 did not answer in time. When time allowed, the sample looked for an instance with the launch tag of this test and asked EC2 to terminate any it found. The scheduled cleanup and the self-shutdown of the instance cover the rest. Today's counts were kept, because the request may have reached EC2. | Wait until the cooldown ends (`testLaunchCooldownMinutes`, 15 minutes by default), then test again. Until then, the page shows this result. Ask your administrator if it repeats. |
| `Something went wrong on the server. Try again.` | The server had too little time left to start the test launch safely. Nothing was launched, and today's counts were given back. | Choose **Test launch** again. |
| `Too many requests at once. Wait a moment, then choose Refresh.` | The service throttled the request before the test started. Nothing was launched, and the request does not count. | Wait a moment, then choose **Refresh** and test again. |
| `Another test of this choice and market is running in this Region. Try again in a moment.` | Another user started the same test. The page does not launch a second instance. | Wait a moment, then choose **Refresh**. |
| `Result from N min ago: ...` | A test for the same target, market, and Region ran within the cooldown, so the page shows that earlier result. This applies across the users of the deployment. | Wait until the cooldown ends for a fresh answer. |

### Zone prices and Reserve with CLI

The **Availability Zones** table has one row per zone that offers the type, with three columns: **Zone**, **Spot price / hr**, and a **Reserve with CLI** button. The Spot price is the latest Spot market price in that zone for Linux, with its share of the On-Demand list price in brackets, such as `(40% of On-Demand)`. It changes often, so the price you pay can differ. The hover text of the info button next to the heading explains this.

**Reserve with CLI** opens a dialog with an `aws ec2 create-capacity-reservation` command for that zone and type. The command asks for one instance and sets an end date one hour from the moment you open the dialog. Choose to copy it, then run it yourself with your own credentials. The page does not create the reservation. The dialog warns that a Capacity Reservation bills at the On-Demand rate from the moment it is created until it ends or you cancel it. Cancel it in the EC2 console if you no longer need it.

## Capacity Blocks tab

A Capacity Block for ML (machine learning) reserves GPU instances in one Availability Zone for a fixed number of days, starting on a future date. This tab has its own Region and type choices; the first time you open it, they start from your last used Region and type. It has two sub-tabs. Use the arrow keys to move between them.

### Search availability

| Control | Meaning |
|---|---|
| **Regions** | The Regions to search. Open the list to tick Regions, or use **Select all** and **Clear**. |
| **Instance types** | The instance types to search. The list holds only the types that support Capacity Blocks in the chosen Regions; a label such as `(2 of 3 Regions)` means that only some of the chosen Regions offer the type. |
| **Instance count** | How many instances the block holds, 1 to 64. |
| **Duration (days)** | How long the block lasts: 1 to 14 days, then 7-day steps up to 182 days, as the EC2 documentation lists. |
| **Earliest start (optional, UTC day)** | Leave it empty to see the earliest offerings. Pick a date, at most 14 days from today (UTC), to see offerings that start on or after the start of that day in UTC. |
| **Find EC2 Capacity Block** | Runs the search. It stays unavailable, with the reason next to it, until the form is complete. |
| **Create arrival alert** | Adds Capacity Block alerts from the same form. Shown only when your administrator turned on email alerts. |

**Search again** runs your last search once more, with the choices it was made with. Offerings are not cached: each search asks EC2.

The search checks each Region and type pair that EC2 offers, up to six at a time, and shows a progress bar with `N of M searched`. If the service is busy and throttles a pair, the page tries that pair once more after the others finish. One search covers up to 40 pairs. Rows appear as answers arrive, sorted by earliest start:

| Column | Meaning |
|---|---|
| **Region**, **Zone** | Where the block is. |
| **Instance type**, **Instances** | What the block holds. |
| **Start**, **End** | When the block starts and ends, in your browser's time zone. |
| **Duration** | The length of the block, such as `1 day` or `14 days`. |
| **Upfront fee** | The fee that EC2 returns for the offering, in its currency. `Not listed` means EC2 returned no fee. |
| **Per instance-hour** | The upfront fee divided by the hours and the instances of the block, to compare offerings of different sizes. |
| **Buy** | **Buy in the EC2 console** opens the Capacity Reservations page of the EC2 console in that row's Region, in a new tab, where you buy the Capacity Block. |

This page does not buy anything. Buy in the EC2 console. Another customer can buy an offering before you act, so check again just before you buy. The fees are what EC2 lists for the offering; what you pay can differ, for example with taxes.

A chosen pair whose Region does not list that type for Capacity Blocks is not searched; it shows once under the progress bar, such as `p4d.24xlarge: not offered as Capacity Blocks in ap-northeast-1`. If the page could not load a Region's list, it searches that Region's pairs and shows what EC2 answers. A pair that EC2 rejects shows one line under the progress bar, such as `p5.4xlarge: not supported for Capacity Blocks (us-east-1)`, or a note that EC2 does not accept the duration or the count for that type right now. When the search finds nothing, the table reads `No blocks match. Try fewer instances, another length, or more Regions.` with an **Alert me when available** link.

**Recent searches** above the form shows your last five searches as chips, kept in this browser only. **Create arrival alert** opens a dialog with the number of Region and type pairs, the instance count, and the duration, then adds one alert for each pair. It skips the alerts you already have and stops at your alert limit.

### Max capacity

Pick a **Region**, an **Instance type**, and a **Duration (days)**, then choose **Find max capacity**. The page finds the largest instance count, from 1 to 64, that has an offering, and shows the earliest start, end, zone, and upfront fee for that count. The page sends up to three requests that narrow the range, so the answer takes roughly 10 to 20 seconds. `No capacity available now` means EC2 lists no block of that length for a count from 1 to 64. Try a shorter duration or another Region, or create an arrival alert on **Search availability**.

## Quotas tab

The **Quotas** tab shows the quotas for the Region in its header: the On-Demand and Spot vCPU quotas for the P, G and VT, Trn, and Inf instance families, and the Capacity Block quotas per family. Each group has its own table and a short explanation. When you used an instance type before, the quota that applies to its family comes first in each group, highlighted. Move the mouse over a quota name, focus it with the keyboard, or tap it to show a tip that explains what the usage counts. Press Esc to close the tip.

| Column | Meaning |
|---|---|
| **Quota** | The quota name as Service Quotas reports it. A `not adjustable` tag means you cannot request an increase. Capacity Block rows carry a `Limit only` label. |
| **Limit** | The quota value with its unit, such as `192 vCPUs`. An `AWS default` tag means Service Quotas returned no applied value for your account, so the page shows the AWS default value. |
| **Used in the last hour (this account)** | On-Demand and Spot rows only. The highest usage in the last hour of the AWS account where the sample is deployed, from the Amazon CloudWatch usage metric, with a bar and the share of the limit. From 80% the bar turns yellow and the cell says `Near limit`; from 100% it turns red and says `Over limit`. |
| **Link** | **View in Service Quotas** opens the quota in the Service Quotas console, in a new tab, for that Region. |

When a quota is at or over its limit, a notice above the tables says so: a launch that needs more of it fails until usage drops or the limit rises. AWS does not publish usage metrics for Capacity Block quotas, so their table shows the limit only. Usage metrics can lag actual use by a few minutes. **Refresh** asks Service Quotas and CloudWatch again. The tab checks one Region at a time, and it is read-only. To request an increase, use the **View in Service Quotas** link.

## Alerts tab

This tab appears when your administrator turned on email alerts. An alert emails you when a signal turns from not available to available. Alerts only read signals; they do not run test launches or reserve anything.

1. In the form, pick a **Region**. It starts from your last used choice.
2. Pick a **Signal**:
   - **Capacity Block offering**, a number of **Days**, and an **Instance count** from 1 to 64. The alert turns available when EC2 lists a Capacity Block offering for that count and length.
   - **Spot placement score** and **Score at least**, 1 to 10. The next list becomes **Score group**: pick a group that the Region sells, such as `g6e · 4 GPUs`. The alert turns available when the Region score of that group reaches your threshold. It covers the families that your administrator chose for Spot scores, and it follows the 15-minute refresh.
3. For a Capacity Block alert, pick an **Instance type**. Choose **Add alert**. To add Capacity Block alerts for several Regions and types at once, use **Create arrival alert** on the **Capacity Blocks** tab.
4. With your first alert, a yellow notice asks you to confirm your subscription. Open the email from AWS Notifications and choose **Confirm subscription**. You confirm once. The email goes to the address of your user.

Until you confirm, you receive no alert email. The alert keeps its state, so if the signal is still available at the first check after you confirm, the email goes out then. Amazon Simple Notification Service (Amazon SNS) deletes a subscription that stays unconfirmed for 48 hours. If that happens, delete your alerts and add them again to get a new confirmation email.

| Column | Meaning |
|---|---|
| **Region**, **Instance type** | What the alert checks. For a Spot score alert, the **Instance type** column shows the score group. |
| **Signal** | `Capacity Block, N instances, N days` or `Spot score at least N`. |
| **State** | `Waiting for first check`, `Not available`, `Available now`, or `EC2 does not accept this (...)` with the reason, for example when EC2 does not offer that duration or instance count for the type. A rejected alert sends no email; delete it and add one that EC2 accepts. |
| **Last checked** | How long ago the last check ran, or `Not checked yet`. |
| **Delete** | Removes the alert, after a dialog. |

The note at the top of the tab starts with the check interval that your administrator set, for example `Alerts are checked every 15 minutes.`. The check runs on a schedule, so a signal that comes and goes between two checks sends no email. After an alert sends an email, it sends again only after the signal turns not available and then available again. When you delete your last alert, the page also removes your email subscription. To stop alert emails to your address, choose the unsubscribe link in an alert email.

## Users tab

The **Users** tab is a guide only: it calls no API and shows no user data. It explains how to add, update, and delete users in the Amazon Cognito console, which is where users are managed. Choose **Open Amazon Cognito console** to open the user pool of this deployment in a new tab. That needs AWS permissions to open the user pool; if you do not have them, ask your administrator. Ordinary users do not need this tab.

## Decision playbook

### Scenario 1: a multi-week training run that needs reserved capacity

You need several instances of one type for three weeks, and the capacity must be there when the run starts.

1. On **Capacity Blocks**, tick the Regions and the types you can use, set **Duration (days)** to `21` and **Instance count** to your count, and choose **Find EC2 Capacity Block**. Check **Quotas** for enough Capacity Block quota.
2. Compare **Start**, **Per instance-hour**, and the zone, then choose **Buy in the EC2 console** on the row that fits. Buy soon; offerings can go to another customer.
3. If nothing matches, try **Max capacity** for one Region, a nearby duration such as `14` or `28`, or choose **Alert me when available**. When the email arrives, choose **Search again** or the recent-search chip.

### Scenario 2: a flexible Spot batch job

Your job can run in a range of Regions and zones, and it can stop and restart.

1. On **Spot score**, find the column of the GPU group you need and look for Regions with a high (`7 to 10`) score. Compare the Spot prices in the tooltips and in the Region detail. Check **Quotas** for free Spot quota in that Region.
2. Choose **Test launch** in the Region detail of the best Region (it opens the **On-Demand and Spot** tab), pick the group (or one size), choose Spot, and run a test launch. The result tells you the zone and size EC2 picked.
3. If no Region scores high enough, add a **Spot placement score** alert on the **Alerts** tab, with the threshold you accept, for example `7`.

### Scenario 3: I have a quota error

A test launch or your own launch returned `Quota limit` or an error such as `VcpuLimitExceeded`, or the page said that your quota has too little free.

1. Open **Quotas** for the Region where it happened. The quota for your last used type comes first in each group.
2. Compare **Used in the last hour (this account)** with **Limit**. `Near limit` or `Over limit` means little or no headroom. Active Capacity Reservations count toward the On-Demand quota.
3. Choose another Region in the header to look for headroom, or choose **View in Service Quotas** to request an increase. A `not adjustable` quota cannot be raised. Usage can lag by a few minutes; choose **Refresh** after you end instances.

## Cost and safety notes

- A test launch starts a real instance for a few seconds, then terminates it. An On-Demand test is billed for the minimum billing period of the instance type. A Spot test can also incur a charge. Test sparingly on the largest instance types.
- The cooldown limits each group or size, market, and Region to one launch in the window, across users. The daily caps limit your volume and the volume of the whole deployment.
- The page shows list prices and the latest Spot prices. What you pay can differ, for example with discounts, Savings Plans, or taxes.
- A Capacity Block that you buy in the EC2 console is a purchase. A command you copy with **Reserve with CLI** creates a Capacity Reservation that bills at the On-Demand rate from creation until it ends or you cancel it. The page does not track or cancel either one.
- Each answer is point in time. A `Launched` result or an offering does not reserve anything, and a Spot placement score is a likelihood.

## Troubleshooting

| Symptom | What to do |
|---|---|
| The temporary password does not work | It expires after seven days. Ask your administrator to send a new one. Type it exactly as it appears in the email. |
| The authenticator code is rejected | Check that the time on your phone is set automatically. Enter the current code before it changes. If you lost the device, ask your administrator. |
| Forgotten password | Choose **Forgot your password?** on the sign-in page and use the code sent to your email address. |
| The page shows `Sign in to continue.` | Choose **Sign in**. |
| `Couldn't load the page.` | The page could not reach the API. Choose **Try again**. If it repeats, ask your administrator. |
| The page sends you to the sign-in page after a long idle time, or an action fails with `Your session expired.` | Your session ended. A session lasts up to 12 hours by default, and closing the browser tab ends it. Sign in again, then repeat the action; the expired request did not reach the API function, so it did not launch anything. |
| A red message with **Retry** | That part of the tab did not load. Choose **Retry**. The message names the part and gives the reason. |
| The page says `Setting up Spot scores for this deployment.` or that the groups are being set up | After a first deploy, or after your administrator changed the Spot score families, the deployment builds the groups on the first request. This takes about a minute. While the tab is open, the page reads the groups again every minute and updates on its own. |
| The table says `This deployment has no Spot score groups` | The groups are built, but none of the families chosen for Spot scores is sold in the enabled Regions. Ask your administrator to check `spotScoreFamilies`. |
| A cell reads `Spot score after the next daily update` | The Region sells a size of the group but joins the group at the next daily update (00:30 UTC), shown in your local time. Nothing to do. The cell shows `–`, and the text appears on hover. |
| A group or family has no column | Your administrator chooses the families that get Spot scores. Ask them to add the family to `spotScoreFamilies`. Supply, prices, quotas, and test launches still cover it. |
| A cell shows `–` but the Region sells the group | The score comes at the next refresh (every 15 minutes), or the deployment's daily budget of Spot score requests has no room yet and the request waits. It is not an error. |
| A cell shows `•` and reads `AWS returned no Spot score for this Region` | AWS answered the request but left this Region out. It is not an error and not a limit, and waiting does not change it. Run a test launch to check the Region. |
| The Spot score is high, but the test launch fails | This is normal. A score is an estimate for that moment, and capacity can be gone by the time you launch. Try again, or try another Region. See [How to use the Spot placement score](#how-to-use-the-spot-placement-score). |
| The Spot score is low, but the test launch works | This is normal too. A low score can still launch. Use the test launch as the real check. |
| `No default VPC in this Region` | Test launches use the default VPC, and this Region has none. Ask your administrator, or test another Region. |
| `Your ... quota has ... free, and the smallest size needs ...` | No size of the group fits your free vCPU quota in that market, and no instance was launched. See [Scenario 3](#scenario-3-i-have-a-quota-error). |
| A test launch says you reached a daily limit | Your administrator caps the test launches per user per day, and for the whole deployment. Every request that reached EC2 counts. The count resets at 00:00 UTC (the page shows the time in your local time). Try again after the reset, or ask your administrator to raise `maxTestLaunchesPerUserPerDay` or `maxTestLaunchesPerDay`. |
| `Not enabled` for a Region | The account has not enabled that Region (an opt-in Region). Ask your administrator. |
| `The Region did not answer.` for a Region | The Region's EC2 endpoint did not reply in time. The deployment skips that Region for 5 minutes. Try again after a few minutes; if it persists, tell your administrator, who can leave the Region out. |
| `The service is busy. Wait a moment and choose Retry.` | Many people use the page at the same time, and the API rate limit applies to everyone together. Wait a moment, then choose **Retry**. If it repeats, ask your administrator to raise the rate limit. |
| `Too many requests at once. Wait a moment, then choose Refresh.` | An action, such as a test launch, met the API rate limit. The page does not repeat actions that start or change something, so nothing ran twice. Wait a moment, then choose it again. |
| `This was refreshed less than a minute ago, so you see that answer.` | Someone refreshed the same data in the last minute, and everyone shares it. Choose **Refresh** again after the seconds in the note. |
| The data looks older than I expect | Read the `Updated` label. Spot scores refresh every 15 minutes. Other data older than its cache time loads again when you open the tab or come back to the page. |
| The **Alerts** tab is missing, or the **On-Demand and Spot** tab has no test launch panel | Your administrator turned that feature off. |
| No alert emails | Confirm the subscription email from AWS Notifications, and check your spam folder. An alert emails you only when its state changes from not available to available. Check **State** and **Last checked** on the **Alerts** tab. |
| The confirmation email is older than 48 hours, or you used the unsubscribe link | SNS removed the subscription. Delete your alerts and add them again to get a new confirmation email. |
| `Your email address is not verified.` when you add an alert | Ask your administrator to mark your email address as verified. |
| `You already have this alert.` | An alert with the same Region, type, signal, and values exists. A Spot score alert for the same Region, score group, and threshold counts as the same alert. |
| `Capacity Block alerts are not available for this instance type.` | Pick a type of a Capacity Block family. |
| `<type>: not supported for Capacity Blocks (<Region>)` on the **Capacity Blocks** tab | EC2 does not sell Capacity Blocks for that type in that Region. Pick another type. |
| `EC2 does not accept a block of N days for this type now` | EC2 rejected the duration at that moment. Try a shorter duration. |
| **Find EC2 Capacity Block** stays unavailable | Read the reason next to it: choose at least one Region and type, an instance count from 1 to 64, an earliest start at most 14 days from today (UTC), and at most 40 Region and type pairs. |

## Glossary

| Term | Meaning |
|---|---|
| Capacity Block | Capacity Blocks for ML: GPU instances that you buy for a fixed period, from 1 day up to 182 days, starting on a future date in one Availability Zone. |
| Score group | One instance family with one GPU count, such as `g6e · 4 GPUs`. The Spot score of a group is the Region-level chance that a Spot request for any listed size is fulfilled. |
| Spot placement score | A score from 1 to 10 that EC2 returns for a Region. A higher score means a Spot request is more likely to succeed. It is a likelihood, not a promise. |
| Daily update | The run at 00:30 UTC that adds new sizes, Regions, and groups to the Spot score groups. |
| List price | The public price that AWS publishes, before discounts, Savings Plans, or taxes. |
| Quota | A limit on how much of a resource your account can use in a Region, such as the vCPUs of running P instances. A quota can block a launch even when capacity exists. |
| Test launch | A launch of one instance that the page terminates right after the launch call, to learn whether capacity existed at that moment. The administrator guide and the API call it a launch probe. |
| Cooldown | The minutes during which a repeat test for the same group or size, market, and Region returns the earlier result. |
