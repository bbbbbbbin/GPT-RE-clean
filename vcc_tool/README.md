# PlusVCC（本地 exe 工具）

把「生成 Plus 短链」「AiLink VCC 开卡 / 查卡」「ChatGPT 账号 2FA」放在一个本地网页里，打包成单文件 exe。

- 短链：先查 `plus-1-month-free` 优惠，只有 eligible 才建 checkout，输出
  `https://chatgpt.com/checkout/{processor_entity}/{checkout_session_id}`
  - checkout 建单必须带 `openai-sentinel-token`（flow=`chatgpt_checkout`），否则返回
    `400 Our systems have detected unusual activity`。token 由 `core/sentinel.py` 造 p，
    再用 **Node.js** 跑 `sentinel/sdk.js` 生成，所以 exe 里打包了 `core`、`config` 和
    `sentinel/*.js`，运行机器上需要有 node（或设置 `NODE_EXECUTABLE`）
  - 「短链」页有「自检 sentinel」按钮，先点它确认能出 token（会显示是否含 SO、耗时）
- 开卡：AiLink 开放 API（`https://card.ailinkvcc.com`），自带 HMAC 签名
- 查卡：卡列表 / 余额 / 状态 / 授权与交易记录（可按类型、状态、时间筛选）
- 卡号 CVV：接口只给密文，本地用 Webhook Secret 派生密钥 AES-256-GCM 解密
- 2FA：粘贴 ChatGPT 的 2FA 密钥（Base32 或 `otpauth://`），本地按 RFC 6238
  生成 6 位动态码，30 秒自动刷新 + 倒计时，可多账号保存

## 直接运行（开发）

```powershell
# 在仓库根目录执行以下命令
.\.venv\Scripts\python.exe -m vcc_tool.launcher
```

会随机端口启动 `http://127.0.0.1:xxxx` 并自动开浏览器。固定端口：

```powershell
.\.venv\Scripts\python.exe -m vcc_tool.launcher --port 8899 --no-browser
```

## 打包 exe

```powershell
powershell -ExecutionPolicy Bypass -File .\vcc_tool\build_exe.ps1
```

输出 `dist\PlusVCC.exe`（约 40-70 MB 取决于 curl_cffi）。双击运行，自动开浏览器。

打包脚本会先自动停掉正在运行的旧实例（先 `/api/quit`，2 秒后仍有残留才强杀），
避免 `dist\PlusVCC.exe` 被占用导致「拒绝访问」。

## 运行中怎么「暂停」以便替换 exe

程序没有常驻后台任务，退出即完全停止，三种方式任选：

1. 页面右上角 **「退出程序」**（推荐，会提示端口已释放）
2. 命令行：`powershell -ExecutionPolicy Bypass -File .\vcc_tool\stop.ps1`
3. 强制：`Get-Process PlusVCC | Stop-Process -Force`

退出后覆盖 `dist\PlusVCC.exe`，再双击启动即可（新实例会随机端口并打开新页面，
以新页面为准，旧标签页不会自动重连）。配置存在 `%APPDATA%\PlusVCC\config.json`，
退出/替换都不会丢。

## 配置

- 页面「设置」里填 接口域名 / API Key / API Secret / Webhook Secret
- 保存在 `%APPDATA%\PlusVCC\config.json`，Secret 用 Windows DPAPI 加密（仅当前用户可解）
- 日志：`%APPDATA%\PlusVCC\app.log`
- 若访问 AiLink 需要代理，在「设置」里填「接口代理」；短链代理是独立的

## 接口对照

| 功能 | 接口 |
| --- | --- |
| 卡 BIN / 配额 | `POST /openapi/v1/card-bins/query` |
| 账户余额 | `POST /openapi/v1/account/balance/query` |
| 申请开卡 | `POST /openapi/v1/cards/apply` |
| 开卡任务状态 | `POST /openapi/v1/cards/apply-status/query` |
| 查询卡 | `POST /openapi/v1/cards/query` |
| 卡号/CVV（密文） | `POST /openapi/v1/cards/private-info/query` |
| 卡交易/授权 | `POST /openapi/v1/card-transactions/query` |
| 3DS 验证码 | `POST /openapi/v1/cards/verification-code/query` |

2FA 完全在本地计算（`vcc_tool/totp.py`），不联网、不经过任何服务端。

签名：`HMAC-SHA256(API Secret, "POST\nPATH\nTIMESTAMP\nNONCE\nSHA256_HEX(body)")`，
请求头 `X-VCC-API-KEY / X-VCC-TIMESTAMP / X-VCC-NONCE / X-VCC-SIGNATURE`。

## 注意

- 开卡会扣费（开卡手续费 + 首充），提交前确认金额。
- 卡号/CVV 解密失败的常见原因：Webhook Secret 填错、商户后台未配置 Webhook Secret。
- 接口返回 `HTTP 403` 且正文是 openresty 页面：多半是商户后台配了 API IP 白名单，
  在「设置 → 接口代理」换一个出口 IP，或让客服把当前 IP 加白。
- 直连 `card.ailinkvcc.com` 被重置时（本机网络环境），程序会自动改走本地代理，默认顺序
  `http://127.0.0.1:7890`（Clash）→ `:7891` → `socks5h://127.0.0.1:10808` → `:1080`；
  想指定就填「设置 → 接口代理」。
- 短链的「链式上游代理」同样默认 `http://127.0.0.1:7890`，端口没监听时会自动换到上面能用的那个，
  并在结果里提示实际用的地址。
