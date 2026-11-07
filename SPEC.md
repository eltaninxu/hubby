# Hubby — the wallet keeper that can say no

> PayPal AI Hackathon(Devpost)参赛项目规格。写给跑腿的:照这份做,做完按第 9 节验收,验收过了交给 Eltanin 推仓库、配视频。
> 截止:2026-11-12 12:00 PT = 成都 2026-11-13 04:00。留 11/12 整天给视频和 Devpost 文案,代码 11/11 晚必须冻结。

## 0. 一句话

用户用一段人话给 Hubby 立规矩和月预算;之后每次想买东西先问它。它判(LLM),它付(PayPal 沙箱真下单、真收款),它记账,超了它拒付。**LLM 负责讲理,代码负责硬顶**:月预算上限由代码强制,LLM 说"批"也过不去。

这不是概念图,是我们家正在跑的规矩(Royola Day 210 把吃喝的闸交给 Eltanin)做成产品。demo 里直接用这套真规矩演。

## 1. 比赛硬要求(缺一样不计分)

- PayPal 开发者平台沙箱接入,且 PayPal 是项目核心(钱真的经它手)
- 任意 AI 模型/工具
- 公开 GitHub 仓库 + 开源协议(MIT)+ 评委能照着跑起来的说明
- 一个评委能真跑的 demo:托管 URL **或** `docker compose up` 即起;mockup 不算
- < 3 分钟视频,公开传 YouTube/Vimeo(Eltanin 做,不在跑腿范围)
- 一段文字说明功能 + 用了哪些工具(Eltanin 写,跑腿提供 README 即可)

## 2. 范围

**做(全部,一样不少):**

1. 规矩与预算:一段自然语言规矩 + 月预算上限(数字)。存库。可改。
2. 购买请求:聊天框里一句话("奶茶 $4.5" / "want a second bubble tea today, $4.5")。
3. 判:LLM 读规矩 + 近 30 天账本 + 今天日期,给出结构化裁决 `approve | decline | ask`(ask = 缺信息,问一句)。每个裁决带一句人话理由,理由要引用到具体规矩或具体账本行("上一杯奶茶是 11/06,三天一杯,11/09 才到")。
4. 付:`approve` → 调 PayPal Orders v2 建单(intent=CAPTURE,USD)→ 返回 approve 链接 → 买家(沙箱 personal 号)在 PayPal 页面点确认 → 回到我们的 return_url → 代码 capture → 账本写 `paid` + order_id + capture_id。买家取消 → 账本写 `cancelled`。
5. 硬顶:本月 `paid` 合计 + 本次金额 > 月预算 → 代码直接 `decline`,理由固定格式 "Hard cap: $X of $Y used this month",**不经 LLM**。LLM 的 approve 在这一步之前被覆盖。
6. 账本页:表格,每行 = 一次请求:时间、原话、金额、类别、裁决、理由、PayPal order/capture id、状态。顶部:本月已付 / 预算,按类别小计。
7. 评委可跑:`docker compose up` + `.env` 即起;README 写清沙箱怎么拿钥匙、怎么登买家号付款。
8. 托管:跑在我们 VPS 上一个端口,Caddy 反代到一个子域(域名由 Royola 定,先按 `hubby.royola.dev` 占位)。

**不做(写在这儿是为了别做):**

- 不做注册/登录/多用户。单用户,一个 `.env` 一个家。
- 不做 Telegram/微信接入。网页聊天框就够,评委打得开。
- 不做 Payouts、退款、订阅、争议。只有 Orders create → approve → capture。
- 不做 webhook。capture 走 return_url 同步触发;return 没回来的单子状态留 `pending`,账本页显示,不自动轮询。
- 不做移动端样式。桌面浏览器能看就行。
- 不做"规矩解析成结构化策略"。规矩就是一段文本,原样喂 LLM。唯一结构化的是月预算数字。
- 不做 i18n。界面英文,规矩文本用户想写中文也能写(LLM 看得懂)。

## 3. 技术栈(定死,别换)

- Python 3.12,FastAPI + Uvicorn,Jinja2 模板 + 一点原生 JS(不上前端框架)
- SQLite(单文件 `data/hubby.db`),用 `sqlite3` 标准库或 SQLModel 二选一,选了就不换
- LLM:Anthropic Messages API,`anthropic` 官方 SDK,tool use 拿结构化裁决。默认模型 `claude-sonnet-5-5`,环境变量 `LLM_MODEL` 可换;`ANTHROPIC_BASE_URL` 可设(走家里 LiteLLM 时用)。写代码前先加载 `claude-api` skill 看当前 SDK 用法,别凭记忆写。
- PayPal:直接 `httpx` 打 REST,不引 SDK。沙箱 base `https://api-m.sandbox.paypal.com`。
  - `POST /v1/oauth2/token`,Basic(client_id:secret),`grant_type=client_credentials`,token 缓存到过期前 60s
  - `POST /v2/checkout/orders`,body:`intent: CAPTURE`,`purchase_units[0].amount = {currency_code: USD, value: "4.50"}`,`purchase_units[0].description` = 用户原话截 127 字,`payment_source.paypal.experience_context = {return_url, cancel_url, user_action: PAY_NOW}`;从响应 `links` 里取 `rel == "payer-action"`(没有就取 `approve`)
  - `POST /v2/checkout/orders/{id}/capture`,成功看 `status == "COMPLETED"`,取 `purchase_units[0].payments.captures[0].id`
  - 以上三条在 Day 1 用一个独立脚本 `scripts/paypal_smoke.py` 先跑通再写别的(见第 8 节)
- Docker:一个 `Dockerfile` + `docker-compose.yml`,SQLite 挂 volume,端口 `8080`
- 协议:MIT,`LICENSE` 文件放仓库根

## 4. 数据模型

```
settings  (单行)
  id=1, rules TEXT, monthly_cap_usd REAL, currency TEXT DEFAULT 'USD', updated_at

ledger
  id, created_at, raw_text, amount_usd REAL, category TEXT,
  decision TEXT  -- approve | decline | ask
  reason TEXT,
  status TEXT    -- declined | pending | paid | cancelled | capture_failed
  paypal_order_id TEXT NULL, paypal_capture_id TEXT NULL, paid_at NULL
```

类别由 LLM 给,自由文本但要求小写单词(`food`, `drink`, `sweets`, `transport`, `other`…),账本页按它小计。

## 5. 路由

```
GET  /                 聊天页(左:对话;右:本月预算条 + 最近 5 条账)
POST /api/request      {text} → 走第 6 节流程 → 返回 {decision, reason, amount, category, approve_url?}
GET  /ledger           账本页
GET  /settings         表单:rules 文本域 + monthly_cap 数字
POST /settings         保存
GET  /paypal/return    ?token=<order_id> → capture → 更新账本 → 跳回 / 带一条 flash("Paid $4.50 · capture 3AB…")
GET  /paypal/cancel    ?token=<order_id> → 状态 cancelled → 跳回 / 带 flash
GET  /healthz          {"ok": true, "paypal": "sandbox", "llm": "<model>"}
```

## 6. 一次请求的流程(照这个顺序,别调换)

1. 收到 `text`。
2. **抽金额**:LLM 第一轮(或同一轮 tool)从原话里抽 `amount_usd`、`category`、`item`。抽不到金额 → `ask`,理由 "How much is it?",落账本 `decision=ask, status=declined`,结束。
3. **硬顶**:`sum(amount_usd where status='paid' and month=this month) + amount_usd > monthly_cap_usd` → `decline`,理由 `Hard cap: $used of $cap used this month`。落账本,结束。不调 LLM 裁决。
4. **LLM 裁决**:system prompt 固定(见 6.1),user 内容 = 规矩全文 + 近 30 天账本(每行一句:日期、item、金额、类别、状态)+ 今天日期 + 本次请求。要求通过 tool `decide` 返回 `{decision, reason, category, amount_usd}`。`reason` ≤ 2 句,必须点名引用规矩原文的某一句或账本的某一行。
5. `decline` / `ask` → 落账本,返回。
6. `approve` → 建 PayPal 订单 → 落账本 `status=pending, paypal_order_id` → 返回 `approve_url`。前端把它渲染成一个按钮 "Pay $4.50 with PayPal"(新窗口打开)。
7. 买家在 PayPal 沙箱页确认 → `GET /paypal/return?token=…` → capture → `status=paid, paypal_capture_id, paid_at` → flash。capture 非 COMPLETED → `capture_failed`,理由存 PayPal 返回的 `name`/`message`。

### 6.1 system prompt(初稿,跑腿可微调措辞,不改结构)

```
You are Hubby, a household wallet keeper. The user has handed you their spending rules and a monthly cap.
Your job on each purchase request: decide approve / decline / ask.
- Judge strictly by the rules text and the ledger. Do not invent rules. Do not moralize.
- If the rules are silent on this item and it fits the cap, approve.
- "ask" only when a fact you need is missing (price, what it is). One question, no more.
- Your reason must quote or point to the specific rule sentence or the specific ledger line that drove the decision, in one or two sentences, warm but firm.
- Dates matter: compute "N days since last X" from the ledger yourself.
Return your decision only via the `decide` tool.
```

## 7. 种子数据(首次启动写入,README 里说明可改)

规矩(英文,是我们家 Day 210 那套的直译):

```
One sweet thing per day: bubble tea OR dessert, not both. If one is already paid today, the other is a no.
Bubble tea at most once every 3 days, counted from the last paid bubble tea. No exceptions for "it's on sale" or "it expires".
Ask before buying, not after. Requests for things already bought get declined and logged.
Meals are fine. Fried chicken drumsticks are always fine.
Anything over $20 in one go needs a one-line reason in the request.
```

月预算:`120`。

账本预埋两行(让 demo 第一句就能被拒):`2026-11-06 bubble tea $4.50 drink paid`、`2026-11-07 Pepsi $1.20 sweets paid`。注意预埋行日期要相对"今天"算(今天-1、今天),别写死,不然 11/12 评委跑的时候三天早过了。

## 8. 日程(跑腿按天交付,每天末尾在群里报一句"今天那条验收过了没")

- **11/08(Day 1)**:`scripts/paypal_smoke.py` —— 用 `.env` 的沙箱钥匙:拿 token → 建 $1.00 单 → 打印 approve 链接 → 人工在浏览器用买家号付 → 脚本第二段传 order_id 做 capture → 打印 `COMPLETED` + capture_id。**这一步不通,后面全停,立刻在群里说。** 同日起 FastAPI 骨架 + SQLite + `/healthz`。
- **11/09(Day 2)**:第 6 节全流程跑通(含硬顶、LLM 裁决、建单、return 回来 capture);聊天页能用;账本页能看。
- **11/10(Day 3)**:settings 页;种子数据;Dockerfile + compose;README(评委视角:拿钥匙 → 填 .env → compose up → 用买家号付一笔);部署到 VPS 端口,Caddy 条目给 Royola 加。
- **11/11(Day 4)**:修 bug、把第 9 节 1–7 全过一遍录屏证据(截图或 asciinema 都行)发群里;**当晚代码冻结**,之后只改 README 错字。
- 11/12:Eltanin 录视频、写 Devpost;跑腿待命修阻塞性 bug。

## 9. 验收(Eltanin 亲自过,每条要能复现)

1. 干净机器 `git clone` → 填 `.env`(按 `.env.example`)→ `docker compose up` → 浏览器 `localhost:8080` 出聊天页,`/healthz` 返回 ok。
2. 发 "bubble tea $4.50" → 返回 `decline`,理由里出现上一杯的日期和"3 days"。账本多一行 declined。
3. 发 "fried chicken drumstick $6" → `approve`,出现 PayPal 按钮;点开,用买家沙箱号登录付款;回到 `/`,flash 显示 Paid 和 capture id;账本该行 `paid`,`paypal_capture_id` 非空;PayPal 沙箱卖家后台能看到这笔。
4. 发 "a cake" → `ask`,理由是问价格,不建单。
5. 把月预算改成 `7`,再发 "lunch $8" → `decline`,理由是 `Hard cap: $6.00 of $7 used this month` 这个格式,**账本理由里没有 LLM 的话**。
6. 发 "dessert $5" 在同一天已有 paid 的 sweets 之后 → `decline`,理由引用 "One sweet thing per day"。
7. 买家在 PayPal 页点取消 → 回到 `/paypal/cancel` → 账本该行 `cancelled`,本月已付不变。
8. README 从头读一遍,一个评委没见过我们家也能照着跑到第 3 条。
9. 仓库里 `grep -r "sk-\|EL[A-Za-z0-9_-]\{60,\}\|BAA2"` 零命中;`.env` 在 `.gitignore`;`.env.example` 里只有变量名和说明。

## 10. 环境变量(`.env.example` 原样抄)

```
PAYPAL_ENV=sandbox
PAYPAL_CLIENT_ID=
PAYPAL_CLIENT_SECRET=
PAYPAL_RETURN_BASE=http://localhost:8080      # 部署时换成 https://hubby.royola.dev
ANTHROPIC_API_KEY=
ANTHROPIC_BASE_URL=                            # 可空
LLM_MODEL=claude-sonnet-5-5
MONTHLY_CAP_USD=120
```

真钥匙在 Eltanin 那边 `~/.config/eltanin/secrets.env`(`PAYPAL_SANDBOX_CLIENT_ID` / `PAYPAL_SANDBOX_SECRET`,沙箱买家卖家号也在),跑腿要用就从那儿读,**不复制进仓库目录,不贴群里**。

## 11. 仓库

- 本地目录 `/home/ubuntu/hubby/`(这份 SPEC 就在这儿)。跑腿在这儿 `git init`、开发、提交。
- GitHub:`github.com/eltaninxu/hubby`,public,由 Eltanin 用自己的 gh 号建远端并推;跑腿不需要 gh 权限,提交到本地 main 即可,Eltanin 来推。
- 提交信息英文,小写动词开头。

## 12. 视频脚本骨架(Eltanin 用,跑腿不管,放这儿是让跑腿知道 demo 要演哪几步、别把那几步做丑)

0:00 聊天页空白,打字 "bubble tea $4.50" → 被拒,理由念出来
0:25 打 "fried chicken drumstick $6" → 批 → 点 PayPal 按钮 → 买家付款 → 回来 Paid + capture id
1:10 切到账本页:两行,一拒一付,预算条动了
1:30 settings 页:把 cap 改成 7 → 回去打 "lunch $8" → Hard cap 拒,强调"这一步没问 AI"
2:00 切 PayPal 沙箱卖家后台,那笔 $6 在
2:20 一句话收尾:the LLM argues, the code enforces, PayPal moves the money
2:40 完
