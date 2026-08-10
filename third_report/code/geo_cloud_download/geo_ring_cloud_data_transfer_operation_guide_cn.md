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

当传输清单状态为 `READY_FOR_XFTP_UPLOAD` 时，页面上的“开始／续传自动上传”按钮会解锁。点击一次后，程序将：

1. 检查 SSH 免交互认证和服务器目标目录；
2. 将每个文件上传为 `<文件名>.part`；
3. 中断后按服务器 `.part` 大小续传；
4. 完成后原子改名为正式文件；
5. 在服务器逐文件计算 SHA-256；
6. 把 `server_verification.json` 自动取回本地；
7. 只有复核全部通过时才显示 `PASS`。

如果服务器已经存在同名正式文件但大小与清单不一致，自动上传会停止，不会覆盖。处理异常后再次点击即可续传。自动上传流程没有本地删除命令；即使服务器复核通过，也只会解锁“清理许可”，不会自行删除本地数据。
