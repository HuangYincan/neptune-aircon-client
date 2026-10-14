# Neptune Aircon Client · 尼普顿空调客户端

用 Python 查询和控制尼普顿物联网空调：读取状态、设置温度、切换模式、调节风速和开关，并在临时授权即将到期时自动重新授权。运行时只依赖 Python 标准库，可作为命令行工具或接入自建后端。

这是根据实际遥控网页分析实现的非官方客户端。**尼普顿是空调服务提供商；ihome 是本次验证使用的公寓管理入口**。当前授权导入适配 Mac 微信中经 ihome 打开的尼普顿遥控页面，尚未验证其他公寓入口、其他微信版本或所有尼普顿设备。

## 快速开始

需要 Python 3.10+，并且你有权控制该设备。

1. 在 Mac 微信中打开 ihome → 空调遥控，进入尼普顿控制页面，确保正常登录。
2. 从遥控页面地址中的 `assetCode` 参数获取自己的设备编号。
3. 克隆本仓库，在终端查询状态：

```bash
git clone https://github.com/HuangYincan/neptune-aircon-client.git
cd neptune-aircon-client
export AIRCON_ASSET_CODE="YOUR_ASSET_CODE"

python3 aircon.py --asset-code "$AIRCON_ASSET_CODE" cache-info
python3 aircon.py --asset-code "$AIRCON_ASSET_CODE" status
python3 aircon.py --asset-code "$AIRCON_ASSET_CODE" profile
```

`cache-info` 只读取本地缓存，不联网。它会显示设备编号和授权期限，分享输出时请删去设备编号。当前支持观察到的 Chromium Simple Cache v5；缓存被清理或微信升级后，可能需要重新进入遥控页面。

## 控制命令

| 功能 | 子命令 | 验证情况 |
|---|---|---|
| 状态查询 | `status` | 已在线验证 |
| 温度能力查询 | `profile` | 已在线验证，CLI 仅输出温度范围 |
| 设置温度 | `set-temp 23` | 请求已在线接受，实际变温尚未验证 |
| 切换模式 | `set-mode cool` | 已实现，尚未实测 |
| 调整风速 | `set-wind low` | 已实现，尚未实测 |
| 开关设备 | `power on` / `power off` | 已实现，尚未实测 |
| 授权续期检查 | `check-refresh` | 已在线验证 |
| 使用最早缓存签名续期 | `check-refresh --oldest` | 原临时令牌过期后仍成功查询状态 |

完整命令示例：

```bash
python3 aircon.py --asset-code "$AIRCON_ASSET_CODE" set-temp 23
python3 aircon.py --asset-code "$AIRCON_ASSET_CODE" set-mode cool
python3 aircon.py --asset-code "$AIRCON_ASSET_CODE" set-wind low
python3 aircon.py --asset-code "$AIRCON_ASSET_CODE" power on
```

模式支持 `auto`、`cool`、`dry`、`heat`、`fan`；风速支持 `auto`、`low`、`medium`、`high`、`turbo`。控制前检查设备锁定与在线状态，温度设置还会检查能力接口返回的范围。

控制输出包含 `commandAcknowledgement` 和 `observedStatus`。设备可能异步执行，成功应答不代表实际空调已经改变。2026 年 10 月 14 日的温度验证使用了原有设定值 23℃，因此只证明控制请求被接受。

## 安装命令行入口

可直接运行源码，也可以安装到 Python 环境：

```bash
python3 -m pip install .
neptune-aircon --asset-code "$AIRCON_ASSET_CODE" status
```

安装时需提供构建依赖 `setuptools>=68`；运行时不依赖第三方包。兼容保留旧入口名 `ihome-aircon`。

## 自动授权续期

客户端从本机微信缓存中选择与设备匹配的合法登录记录。每次设备请求前检查令牌期限，剩余不超过 60 秒时原样复用已有签名重新登录；成功后核对设备编号并更新进程内会话。续期失败则停止本次设备请求。

```bash
# 强制现在重新授权，再只读查询状态
python3 aircon.py --asset-code "$AIRCON_ASSET_CODE" --refresh status

# 比较授权期限，并用新令牌只读验证
python3 aircon.py --asset-code "$AIRCON_ASSET_CODE" check-refresh

# 选择最早匹配记录，检验临时令牌过期后的续期
python3 aircon.py --asset-code "$AIRCON_ASSET_CODE" check-refresh --oldest
```

已验证原临时令牌到期约 76 分钟后，对应签名仍可换取新令牌。**签名的最长有效期未知，自动续期并不代表永久免登录**。旧签名被拒绝时，需要重新从正常 ihome 入口进入尼普顿遥控页取得授权。

`--no-auto-refresh` 关闭默认续期；`--direct` 关闭当前进程的环境代理，HTTPS 证书验证保持开启。CLI 每次启动仍从微信缓存初始化，不把续期凭据保存到磁盘。

## 接入自建后端

在同一进程中复用一个客户端，让多次操作共享刷新后的会话：

```python
from aircon import AirconClient, discover_session, public_profile, public_status

client = AirconClient(discover_session("YOUR_ASSET_CODE"))
print(public_status(client.status()))
print(public_profile(client.profile()))

# 需要调整温度时调用：
# acknowledgement = client.command("temperature", {"temperature": 23})
# print(public_status(acknowledgement))
```

当前只提供 Python 客户端和 CLI。独立部署到其他服务器时，仍需提供合法初始授权；项目没有实现服务器端首次登录或旧签名失效后的上游登录恢复。自建控制 API 应设置访问认证，并把上游凭据保留在后端。

## 隐私与安全

公开源码不包含实际设备编号、姓名、学号、令牌、签名值、Cookie 或原始抓包。客户端只读匹配设备的微信缓存，会话留在内存中，CLI 对状态、能力和控制应答进行字段过滤。库接口返回的原始数据需通过 `public_status()` / `public_profile()` 过滤后再展示。

签名参数可能关联个人身份，控制 URL 的路径也包含临时令牌。不要公开完整授权 URL、微信缓存、请求日志或会话对象。详见 [SECURITY.md](SECURITY.md)。

## 测试与协议记录

```bash
python3 -m unittest -v
```

本地测试使用合成凭据和模拟网络，覆盖请求格式、设备范围、锁定阻断、续期顺序、续期失败、响应输出过滤和错误信息脱敏，不会控制真实空调。

- [PROTOCOL.md](PROTOCOL.md)：入口、认证、接口与验证范围。
- [evidence.json](evidence.json)：网页资源身份与脱敏验证摘要。
- [aircon.py](aircon.py)：客户端、缓存读取和 CLI。
- [test_aircon.py](test_aircon.py)：离线测试。

## 许可

客户端代码采用 [MIT License](LICENSE)。仓库不分发尼普顿或 ihome 的原始前端资源；网页资源哈希仅用于定位协议分析证据。本项目与尼普顿及 ihome 的运营方没有官方关联。
