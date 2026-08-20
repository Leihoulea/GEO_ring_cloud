# GEO 云产品本地下载、Xftp 上传与服务器复核操作说明

## 1. 适用范围

本说明用于将2024年4月起的 GEO 云产品先下载到本地周转目录，再通过
Xftp 8 上传到课题组服务器。当前约束如下：

- 服务器不能访问外网，因此不能直接从 NOAA、EUMETSAT 等数据源下载。
- 本地负责下载、初检和生成 SHA-256 清单。
- Xftp 负责 SFTP 传输。
- 服务器负责逐文件大小和 SHA-256 复核。
- 原始文件在服务器复核 `PASS` 且用户明确确认前不得删除。
- 本项目从2024年4月开始扩展；该时段风云卫星使用 FY-4B，不把2024年3月混入本批次。

## 2. 总体流程

```text
建立本地批次
  → 远端清单 inventory
  → 下载到 .part
  → 文件格式初检
  → 原子改名为正式文件
  → 生成大小和 SHA-256 传输清单
  → Xftp 上传
  → 服务器逐文件复核
  → 将复核报告带回本地
  → 用户确认允许清理
  → 单独执行本地清理
```

任何 `.part` 文件都表示批次尚未完成。不要上传 `.part`，也不要把它改名为正式
文件。

## 3. 建议批次顺序

1. 先完成一天的 GOES-16/18 试批，验证全流程。
2. 试批通过后下载2024年4月整月 GOES。
3. 下载2024年4月整月 Meteosat-0deg/IODC。
4. Himawari-9 按3天一个批次滚动处理。
5. FY-4B 从2024年4月开始，按现有数据来源在本地下载后纳入同一传输流程。
6. CM SAF、DSCOVR EPIC 和 CERES 等独立参考数据分别建立批次，不与 GEO 主批次混放。

### CLAAS-3 为什么没有普通的一键直下载复选框

CLAAS-3 是 CM SAF 基于 MSG/SEVIRI 的云属性数据记录，不是另一颗独立卫星。本项目使用的 Level-2 瞬时产品包括：

- `CMA`：云掩膜与云概率；
- `CTX`：云顶高度、气压和温度；
- `CPP`：云相态、光学厚度、有效半径和云水路径等。

CM SAF 官方要求注册、登录并提交订单，完成后再通过 HTTPS 或 SFTP 交付。因此仪表盘把 CLAAS-3 显示为“订单型数据源”，并提供官方订单页入口，但不会把它加入 GOES/Himawari 那种匿名对象存储直下载流程。这样可以避免生成误导性的 `0/0` 清单。

官方入口：<https://wui.cmsaf.eu/safira/action/viewDoiDetails?acronym=CLAAS_V003>

推荐顺序：

1. 在 CM SAF 注册并登录；
2. 对 2024 年目标日期分别选择 `CMA`、`CTX`、`CPP` 的 Instantaneous（METEOSAT disk）产品；
3. 保留 CM SAF 返回的订单编号和 HTTPS/SFTP 交付信息；
4. 将订单交付信息接入本工具的后续“订单下载”步骤；
5. 下载完成后仍按 SHA-256 清单上传服务器，本地文件不会被自动删除。

每个批次使用独立目录，例如：

```text
<本地周转根目录>/202404_goes/
<本地周转根目录>/202404_meteosat/
<本地周转根目录>/20240401_20240403_himawari/
<本地周转根目录>/202404_fy4b/
```

## 4. 启动本地下载

脚本位置：

```text
third_report/code/geo_cloud_download/geo_ring_cloud_transfer_batch.ps1
```

推荐直接在仪表板的“创建一键下载批次”区域选择日期、卫星和并行数。清单阶段采用：

- 按天列远端目录，再在本地筛选24个整点目标；
- 默认8个日级清单任务并行，可选1至16；
- 日期、平台、算法版本和网络模式完全一致时复用清单缓存；
- 勾选“强制刷新清单”才会忽略缓存重新查询；
- NOAA S3 与 EUMETSAT 均固定为 `direct_only`，程序主动清除代理环境变量。

以2024年4月2日至30日的 GOES-16/18 两类产品为例，远端目录请求量由原来的
约2784次降为约116次。线程数只影响速度，不影响清单内容，因此改变线程数仍可复用
同一份有效缓存。

下载并行数默认4。GOES/Himawari 的 S3 文件按4 MiB Range 分段下载；网络中断时保留
`.part`，下次从已完成字节继续，不再整文件清零重下。可在仪表板选择1至16路下载，
建议先用4路；只有在单文件增长速度稳定且本地磁盘压力不高时再尝试6或8路。Meteosat
通过 EUMETSAT Data Store 一键下载，不依赖 AWS，并发自动封顶8以遵守服务限制。

### 4.1 一天 GOES 试批

在 PowerShell 中执行：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File ".\third_report\code\geo_cloud_download\geo_ring_cloud_transfer_batch.ps1" `
  -BatchRoot "<本地批次目录>" `
  -ServerRoot "/data04/1/dhr" `
  -StartDate "2024-04-02" `
  -EndDate "2024-04-02" `
  -Platforms "GOES-16,GOES-18" `
  -InventoryWorkers 8 `
  -DownloadWorkers 4 `
  -S3RangeMiB 4
```

### 4.2 GOES 整月批次

在一天试批完成后，对同一批次目录续跑：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File ".\third_report\code\geo_cloud_download\geo_ring_cloud_transfer_batch.ps1" `
  -BatchRoot "<本地批次目录>" `
  -ServerRoot "/data04/1/dhr" `
  -StartDate "2024-04-02" `
  -EndDate "2024-04-30" `
  -Platforms "GOES-16,GOES-18" `
  -InventoryWorkers 8 `
  -DownloadWorkers 4 `
  -S3RangeMiB 4
```

已经存在且能正常读取的文件会被跳过，不会重新下载。

### 4.3 Meteosat 批次

确认 EUMETSAT 凭据配置可用，然后执行。该流程同样固定直连，不使用代理：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File ".\third_report\code\geo_cloud_download\geo_ring_cloud_transfer_batch.ps1" `
  -BatchRoot "<本地批次目录>" `
  -ServerRoot "/data04/1/dhr" `
  -StartDate "2024-04-02" `
  -EndDate "2024-04-30" `
  -Platforms "Meteosat-0deg,Meteosat-IODC" `
  -InventoryWorkers 8 `
  -DownloadWorkers 2
```

凭据只读取到进程环境变量，程序结束时自动清除，不写入传输清单。

## 5. 判断本地批次是否可以上传

只有同时满足以下条件，批次才是 `READY_FOR_XFTP_UPLOAD`：

- 下载进程已经结束；
- 批次目录中不存在 `.part`；
- 下载清单中的正式文件均存在；
- NetCDF/HDF/ZIP 初检没有产生阻断错误；
- `transfer` 目录中已生成传输 JSON、CSV 和中文上传计划；
- 每个正式文件已经记录 `size_bytes` 和 `sha256`。

主要文件如下：

```text
<本地批次目录>/transfer/batch_status.json
<本地批次目录>/transfer/geo_ring_cloud_transfer_<日期>_manifest.json
<本地批次目录>/transfer/geo_ring_cloud_transfer_<日期>_files.csv
<本地批次目录>/transfer/geo_ring_cloud_transfer_<日期>_upload_plan_cn.md
```

## 6. Xftp 上传目录映射

Xftp 使用已经配置好的 `dhr@210.45.127.28` SSH 密钥登录。建议使用二进制/SFTP
传输、保留目录层级，并将并发保持在2至4个。

| 本地平台目录 | 服务器目录 |
| --- | --- |
| `GOES-16` | `/data04/1/dhr/GOES16/Cloud/GOES-16` |
| `GOES-18` | `/data04/1/dhr/GOES-18_cloud` |
| `Himawari-9` | `/data04/1/dhr/H09/cloud` |
| `Meteosat-0deg` | `/data04/1/dhr/Meteosat-0deg` |
| `Meteosat-IODC` | `/data04/1/dhr/Meteosat-IODC` |
| `FY4B` | `/data04/1/dhr/FY4B` |
| `CMSAF` | `/data04/1/dhr/CM SAF` |

上传时同时传输以下控制文件：

- `geo_ring_cloud_transfer_batch.py`
- 对应批次的 `*_manifest.json`
- 对应批次的 `*_files.csv`

如果传输中断，选择续传；不要无条件覆盖已经完成且大小一致的文件。

## 7. 服务器端复核

进入上传了验证工具和清单的目录，执行：

```bash
python3 geo_ring_cloud_transfer_batch.py verify \
  --manifest geo_ring_cloud_transfer_<日期>_manifest.json \
  --report server_verification.json \
  --location server
```

验证工具只读取文件并计算大小和 SHA-256，不修改原始数据。退出码和报告含义：

- 退出码 `0` 且报告 `status=PASS`：所有文件存在、大小一致、SHA-256 一致。
- 退出码 `2` 或报告 `status=FAIL`：至少一个文件缺失或不一致，必须重新上传失败项。

将 `server_verification.json` 用 Xftp 下载回：

```text
<本地批次目录>/transfer/server_verification.json
```

仪表板读取到该文件后，才会把“服务器复核”显示为通过。

## 8. 本地数据什么时候删除

不是“Xftp显示上传完成”就立即删除。本地清理必须经过四道门：

1. Xftp 上传队列无失败；
2. `server_verification.json` 为 `PASS`；
3. 在服务器抽样打开至少一个 GOES、Meteosat、Himawari 或 FY-4B 文件；
4. 用户在仪表板点击“确认允许清理”，或以其他方式明确确认。

仪表板的“确认允许清理”只生成审计标记，不会删除文件。真正删除批次必须作为后续
独立操作执行，并再次核对精确目录。建议服务器 `PASS` 后至少保留本地批次到下一个
批次成功上传；空间紧张时，也至少等待服务器复核和抽样读取完成。

## 9. 启动中文仪表板

执行：

```powershell
conda run -n pytorch python `
  .\third_report\code\geo_cloud_download\geo_ring_cloud_transfer_dashboard.py `
  --batch-root "<本地批次目录>" `
  --port 8765
```

浏览器打开：

```text
http://127.0.0.1:8765
```

页面每5秒刷新，展示：

- 一键创建新批次，并选择日期、卫星、清单并行数和下载并行数；
- 清单缓存是否复用，以及固定直连网络模式；
- 当前阶段和总流程门禁；
- 文件完成数、总体百分比、已完成字节数和磁盘余量；
- 当前 `.part` 文件及增长速度；
- GOES、Meteosat、Himawari、FY-4B 分平台汇总；
- 最近完成记录和错误；
- Xftp 人工确认、服务器复核状态和本地清理许可。

## 10. 常见异常处理

### `.part` 长时间为0字节

先看下载进程和日志是否仍在重试。不要手工改名。若进程结束，可使用原命令续跑；
S3 程序会从 `.part` 的现有字节位置继续，并跳过已经通过校验的正式文件。Meteosat
当前由 EUMDAC 数据流提供，失败重试时可能需要重传当前单文件。

### 页面显示“状态文件失败”，但下载日志仍增长

以实际下载进程和 `download_s3_range.log` 为准。重复启动调度器可能产生状态竞争；新版
脚本使用批次锁阻止同一目录的重复调度。等待活动进程结束后再续跑。

### 服务器验证失败

查看 `server_verification.json` 中 `status=FAIL` 的路径，仅重新上传这些文件。重新运行
验证，直到全部为 `PASS`。不要因为总文件数一致就跳过 SHA-256 复核。

### 本地空间不足

不要扩大当前批次。先完成上传和服务器复核，再由用户确认清理已经通过的批次。对于
Himawari，优先缩小为1至3天一个批次。

## 11. 自动 SFTP 上传（推荐）

自动上传固定写入以下专用目录，不与课题组其他数据混放：

```text
/data04/1/dhr/geo_ring_cloud_auto_upload
```

目录内保留原有卫星和产品层级；每个批次的传输清单、验证工具和服务器复核报告放在：

```text
/data04/1/dhr/geo_ring_cloud_auto_upload/_control/<批次编号>/
```

首次启动仪表板前，在自己的 PowerShell 中执行一次：

```powershell
ssh-add "$env:USERPROFILE\.ssh\id_ed25519_node05"
```

口令只输入给 Windows `ssh-agent`，不要写入脚本、网页或配置文件。然后用下列参数启动仪表板：

```powershell
python .\third_report\code\geo_cloud_download\geo_ring_cloud_transfer_dashboard.py `
  --batch-root "<本地批次目录>" `
  --ssh-target "dhr@210.45.127.28" `
  --identity-file "$env:USERPROFILE\.ssh\id_ed25519_node05" `
  --auto-upload-root "/data04/1/dhr/geo_ring_cloud_auto_upload" `
  --port 8765
```

新建批次时，“全自动流水线”默认勾选。下载器每产生一个正式文件，持续上传器会连续检查两次文件大小和修改时间；只有两次一致才进入上传队列。文件名以 `.part` 结尾的临时文件永远不会上传。对于已经在运行的旧批次，可点击“接管当前下载并自动上传”，无需等整批下载结束，也无需以后再手工点击。

边下边传阶段只使用一路 SFTP，以降低对下载的干扰。下载结束并生成传输清单后，上传器会先用2路、再用3路、最终升至最多4路；最后一组文件少于4个时，页面显示的是实际活动路数。网卡通常支持全双工，因此收发可以同时进行，但两者仍可能争用本地磁盘、路由器/校园出口、CPU 校验和 TCP 确认包带宽，并不是完全互不影响。

下载默认启用自动调速：从4路开始，以页面选择的“自动模式并行上限”为上限。调速器按已完成文件的实际字节吞吐和失败比例逐级探测；吞吐继续增长时加一路，吞吐明显下降或错误增多时减一路。GOES/Himawari最高可设16路，Meteosat为保护 EUMETSAT 接口自动限制在8路。取消“下载自动调速”后，所选数字就是固定并行数。`logs/download_parallelism_status.json` 保存当前路数、活动路数、速率和调速原因，仪表板会直接显示这些信息。

下载完成并生成 `READY_FOR_XFTP_UPLOAD` 清单后，持续上传器自动转入最终对账和全量复核。也可以不启用全自动流水线，等清单就绪后点击“完成后开始／续传上传”。两种模式都会：

1. 检查 SSH 免交互认证和服务器目标目录；
2. 将每个文件上传为 `<文件名>.part`；
3. 中断后按服务器 `.part` 大小续传；
4. 完成后原子改名为正式文件；
5. 在服务器逐文件计算 SHA-256；
6. 把 `server_verification.json` 自动取回本地；
7. 只有复核全部通过时才显示 `PASS`。

如果服务器已经存在同名正式文件但大小与清单不一致，自动上传会停止，不会覆盖。处理异常后再次点击即可续传。自动上传流程没有本地删除命令；即使服务器复核通过，也只会解锁“清理许可”，不会自行删除本地数据。

### FY4B 官方应用本地导入

FY4B 由官方应用下载完成后，在仪表盘的“FY4B 官方应用本地导入”区域填写**包含 `.NC` 文件的目录**，再先点击“预览 FY4B 映射”。无需手填批次标识：系统依据官方文件名中的最早和最晚日期及变量集合自动生成控制批次名，例如 `fy4b_20240601_20240630_clm` 或 `fy4b_20240601_20240630_clm-cth`；相同来源再次提交会安全续传，不同来源恰好覆盖同一日期和变量集合时会自动加上来源指纹以避免混淆。已存在的历史日期型控制批次不会被重命名；同一来源再次提交时仍会安全续传该历史批次。当前支持已核验的 FY4B AGRI L2 官方命名规则；系统从文件名的产品标识和 `NOM_YYYYMMDDHHMMSS` 提取时次，只有能同时识别变量、日期和小时的文件才会进入上传清单。

服务器正式路径不是控制批次名，而是：

```text
/data04/1/dhr/geo_ring_cloud_auto_upload/FY4B/<变量>/<YYYYMMDD>/<HH>/<原始文件名>
```

例如 `FY4B-_AGRI--_N_DISK_1050E_L2-_CLM-_MULT_NOM_20240501080000_...NC` 映射为：

```text
/data04/1/dhr/geo_ring_cloud_auto_upload/FY4B/CLM/20240501/08/FY4B-_AGRI--_N_DISK_1050E_L2-_CLM-_MULT_NOM_20240501080000_...NC
```

预览只读扫描，不创建批次、不计算 SHA-256、不上传。点击“创建 FY4B 上传批次”后，系统会再次执行同一套命名校验，随后先显示“本地 SHA-256 清单准备”的逐文件/逐体积进度，再进入服务器连接、远端已完成文件预检、`.part` 断点续传和服务器复核流程；不会复制、移动或删除官方应用下载的原始文件。

### 11.1 上传状态异常的处理

Windows 有时会短暂占用仪表盘的小型状态文件。程序会自动重试状态写入；即使状态暂时写不进去，正在传输的原始数据也不会因此停止。仪表盘另外检查上传器 PID：若 PID 已退出，显示“已停止”，可点击“接管当前下载并自动上传”从服务器 `.part` 安全续传；若 PID 仍存在但超过15分钟没有状态心跳，显示“疑似卡住”，此时先查看 `transfer/continuous_upload.stderr.log`，不要重复启动第二个上传器。

## 12. 多任务、下载磁盘与通知

### 12.1 同时查看旧批上传和新批下载

页面顶部的“多任务中心”会扫描各本地下载盘中的 `GEO_Cloud_2024_batches`
目录。每个批次分别显示下载、上传和服务器复核状态。创建新批次后，旧批次不会从
页面消失；点击任意任务即可切换下方详细视图，上传等操作始终绑定当前选中的批次。

### 12.2 选择下载磁盘

“创建一键下载批次”中的“下载磁盘”下拉框会列出当前系统可访问的磁盘、可用空间和
总空间。程序只允许写入所选磁盘根目录下的 `GEO_Cloud_2024_batches`，不接受任意目录，
以减少误写其他项目目录的风险。磁盘门禁失败时，页面会分别显示：

- 含安全余量的所需空间；
- 失败检查发生时的可用空间；
- 当前实时可用空间；
- 按当前空间重新计算的尚缺空间；
- 可展开的完整错误信息。

### 12.3 空间感知批次队列

填写日期、平台、下载磁盘和并行参数后，点击“加入空间感知队列”。队列会持久保存到当前
批次父目录的 `_geo_ring_cloud_control/batch_queue.json`。仪表板关闭后任务不会丢失；再次
启动仪表板时，调度器会继续每15秒检查一次。

队列按加入顺序寻找第一个同时满足两个门禁的待处理批次：没有其他下载批次正在运行，并且目标
磁盘可用空间不少于保守估算值。因此，前一项所在磁盘空间不足时，后面位于其他磁盘且空间充足
的项目仍可先启动。页面会显示预计所需、当前可用和仍缺空间。空间不足时只等待，
不会提前创建数据目录，也不会反复创建失败批次。已有批次如果曾完成清单统计，则优先采用该
批次实际磁盘门禁值；新批次使用“平台×天数”的保守估算并加20%安全余量。实际清单生成后，
下载器仍会执行一次精确门禁；若精确门禁未通过，队列会回到“等待磁盘空间”而不是盲目重试。

“取消等待”只允许取消尚未启动的队列项，只修改控制状态，不会停止已启动的下载，也不会删除、
移动或覆盖任何本地数据。直接点击“立即创建并启动”仍可跳过排队，但已有下载运行时该按钮会
禁用，应改用队列自动续批。

### 12.4 电脑弹窗通知

仪表板进程会直接调用 Windows 通知中心；即使内置浏览器阻止 Web 通知，下载、上传或
服务器复核从原状态变为完成或失败时仍可收到系统弹窗。也可以点击页面右上角“开启电脑
通知”，在支持的外部浏览器中再启用一层 Web 通知。首次启动只建立当前状态基线，不会
把历史完成任务全部重新提醒一遍。仪表板后台进程必须保持运行；页面可以最小化或关闭。

### 12.5 可选邮件通知

邮件通知适合把完成或失败消息推送到手机邮箱。新版使用独立监控进程：关闭网页不会停止
监控，状态变化会先写入持久队列，SMTP 暂时失败时按照1、5、15、60、180分钟逐级重试。
首次启动只建立状态基线，不会把历史完成任务全部重新发送。推荐直接点击仪表板中的
“配置邮件”，输入发件地址、手机接收地址和邮箱服务商生成的客户端专用密码。页面只监听
`127.0.0.1`；专用密码会由 Windows DPAPI 按当前用户加密后保存到：

```text
%LOCALAPPDATA%\GeoRingCloud\email_notification.json
```

该文件只保存 DPAPI 密文，无法由其他 Windows 用户或复制到另一台电脑后直接解密，也不会
进入 Git。保存时程序会立即发送一封测试邮件。高级用户也可以在启动仪表板前，在同一个
PowerShell 窗口用临时环境变量覆盖本机加密配置：

```powershell
$env:GEO_RING_NOTIFY_SMTP_HOST = "<SMTP服务器>"
$env:GEO_RING_NOTIFY_SMTP_PORT = "587"
$env:GEO_RING_NOTIFY_SMTP_USER = "<SMTP用户名>"
$env:GEO_RING_NOTIFY_SMTP_PASSWORD = "<SMTP应用专用密码>"
$env:GEO_RING_NOTIFY_EMAIL_FROM = "<发件地址>"
$env:GEO_RING_NOTIFY_EMAIL_TO = "<手机接收邮件的地址>"
$env:GEO_RING_NOTIFY_SMTP_STARTTLS = "1"
```

如果邮件服务使用465端口的隐式 TLS，再设置：

```powershell
$env:GEO_RING_NOTIFY_SMTP_SSL = "1"
```

请使用邮箱服务商生成的 SMTP 应用专用密码，不要把邮箱登录密码写入脚本。通知事件队列不会
保存 SMTP 主机、邮箱地址或密码；网页只显示掩码后的收件地址。仪表板显示“邮件通知已启用”后，先点击
“发送测试邮件”；手机确认收到后再依赖正式通知。队列和监控健康状态位于所选批次父目录的：

```text
_geo_ring_cloud_control/notifications/notification_state.json
```

这个文件只含状态快照、事件摘要、重试次数和时间，不含凭据。独立监控进程入口是
`geo_ring_cloud_notification_service.py`；仪表板启动时会自动检查并启动它。若希望电脑重启后
在未打开仪表板的情况下也自动恢复，可在配置邮箱后再创建当前用户级 Windows 计划任务。
