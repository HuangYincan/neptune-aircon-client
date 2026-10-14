# 尼普顿空调网页协议记录

2026-10-14，通过 Mac 微信 4.1.15 加载的遥控网页与 Chromium Simple Cache v5 分析协议。本次公寓管理入口为 ihome，空调遥控和游客认证由尼普顿服务提供。记录对应当时观察到的网页版本，不代表服务提供商的公开 API 契约。

## 入口与授权

```text
ihome 公寓服务页面
  → GET https://yun.chinasinew.com/mob/general/air
  → HTTP 302 到 https://wx.hzxwwl.com/prod/air/#/rentStatus?...签名参数...
  → GET https://gateway.hzxwwl.com/api/asset/ac/tourist/auth/login?...签名参数...
  → 返回 assetCode、tmpAuthToken、expireTime
  → 前端写入 Request-Tmp-Auth-Token Cookie
  → #/zdInternetControl?assetCode=...
```

签名参数为 `xm`、`xh`、`qsId`、`timestamp`、`platformType`、`sign`，由上游入口提供。它们可能关联姓名、学号或房间身份；不能作为公开配置。前端没有包含签名生成逻辑，客户端只原样使用用户已有的合法签名。

网页检测 `Request-Tmp-Auth-Token` Cookie，缺失时跳转至通用入口，因此复制最终遥控地址到其他浏览器通常不能继承授权。

观察到临时令牌的 `expireTime` 为 895 和 893。Cookie 逻辑将其乘以 1000 后加到当前时间，单位为秒，约 15 分钟。这是观察值，并非固定服务承诺。缓存读取用响应 `Date` 计算过期时间，不把文件修改时间或签名 `timestamp` 当成令牌签发时间。

## 接口

基础地址为 `https://gateway.hzxwwl.com/api/`。`{token}` 是临时令牌，应当避免记录完整控制 URL。

| 功能 | 方法 | 路径后缀 | 参数 |
|---|---|---|---|
| 查询能力 | GET | `iot/device/dynamic/control/{token}/getProfile` | `assetCode` |
| 查询状态 | GET | `iot/device/dynamic/control/{token}/getRunningStatusInfo` | `assetCode` |
| 温度 | POST | `iot/device/dynamic/control/{token}/temperature` | `assetCode`, `temperature` |
| 模式 | POST | `iot/device/dynamic/control/{token}/model` | `assetCode`, `model` |
| 风速 | POST | `iot/device/dynamic/control/{token}/windSpeed` | `assetCode`, `windSpeed` |
| 开关 | POST | `iot/device/dynamic/control/{token}/switching` | `assetCode`, `run` |
| 左右扫风 | POST | `iot/device/dynamic/control/{token}/windLeftRight` | `assetCode`, `run` |
| 上下扫风 | POST | `iot/device/dynamic/control/{token}/windUpDown` | `assetCode`, `run` |
| 电辅热 | POST | `iot/device/dynamic/control/{token}/electricHeat` | `assetCode`, `run` |

GET 使用查询串，POST 使用 `application/x-www-form-urlencoded;charset=UTF-8`。模式：自动 1、制冷 2、除湿 3、制热 4、送风 5；风速：自动 0、低 1、中 2、高 3、强劲 4；开关：开 1、关 0。CLI 只提供状态、能力、温度、模式、风速和开关命令。

普通账号的 `REQ-NPD-TOKEN` 可能由原前端拦截器发送，游客登录接口被排除。本次用户的默认查询和温度请求没有额外提供普通账号令牌，已经成功。客户端兼容可选 `REA_AIR_NPD_TOKEN` 环境变量，但不会把它用于游客登录；其他设备和接口的认证要求尚未确认。

屏显和睡眠功能使用普通 `/iot/device/command/screen-display`、`/iot/device/command/power-saving`，未确认游客临时授权兼容性，未加入客户端。

外层 `code=0000` 和 `success=true` 仅说明服务应答情况。客户端还要求状态、能力和控制应答中的 `data.success=true`，控制前检查锁定及离线状态，温度请求核对设备返回的范围。仍需实际观察设备执行结果。

## 续期

`AirconClient` 在设备请求前发现剩余期限不超过 60 秒时，复用原签名调用游客登录接口。返回设备必须与选定的 `assetCode` 一致，刷新失败则停止设备请求。会话更新和设备请求通过 `threading.RLock` 串行处理；刷新凭据只留在当前进程。

`check-refresh` 比较旧、新到期时间，报告延长秒数、剩余期限、令牌是否改变，并用新令牌只读查询状态。`check-refresh --oldest` 选择最早匹配缓存，可检验原临时令牌到期后的签名复用。仅返回成功或令牌改变不能证明延长授权，需确认期限明显延后。

用户在本地终端验证了以下结果，公开记录仅保留结论和相对期限：

| 验证 | 结果 | 限制 |
|---|---|---|
| 状态查询 | 在线、未锁定，查询成功 | 仅对应本次设备 |
| `set-temp 23` | 控制应答成功，后续查询仍为 23℃ | 使用原有设定值，未证明实际变温 |
| 期限内续期 | 到期时间延长约 481 秒，剩余约 890 秒 | 不能证明签名长期有效 |
| 原令牌到期后续期 | 到期后约 76 分钟仍取得新令牌，剩余约 894 秒，查询成功 | 未验证跨天或永久有效 |

临时令牌过期不会必然让对应签名立即失效。签名的最长有效期未知。旧签名被拒绝后，需要重新走正常 ihome 入口获得新的签名。

客户端没有实现上游登录恢复。前端读取名为 `ihome` 的 Cookie，不足以证明单独该 Cookie 可完成上游认证；完整会话组合及微信环境依赖仍未确认。独立服务器首次授权和跨长期签名失效恢复仍需另行解决。

## 资源证据

分析的 JavaScript 为 `app.398cbb78cbd976934e3e.js`，SHA-256：

```text
aa2f28702ce025cdcc68fb66c18080a53eacf4e1546a644ab6f6c24e1981219b
```

从缓存提取 gzip 内容时验证了完整性。REA 静态解析了 133786 个 AST 节点，语义图受每文件 20000 节点上限限制，覆盖为 partial。请求格式还通过原始代码交叉核对，图中缺失关系不能作为不存在的证据。

`evidence.json` 的符号位置以解码 JavaScript 的 UTF-16 code units 计。该文件仅含资源身份、非个人协议参数和验证摘要，不包含原始响应、设备编号、凭据或本机缓存位置。

## 本地验证

运行 `python3 -m unittest -v`。测试使用合成身份字段和模拟 API，覆盖协议、缓存到期时间、设备约束、自动续期和隐私输出，不发送真实控制请求。

尚未在线验证：实际变温、其他写操作、其他设备和入口、跨天签名有效期、旧签名失效后的上游会话恢复。
