# Value 人工标注器

本地网页工具，选择一个 LeRobot 数据集后，按 20 个 action 一个 chunk 编辑累计进度曲线。无需启动 value 模型或 GPU。

## 启动

在数据集根目录执行：

```bash
bash ~/value_label_annotator/run.sh --port 8003
```

打开 `http://localhost:8003`。`run.sh` 使用 uv 获取独立的 pyarrow 依赖，不需要修改 openpi 的训练环境。

后续数据可以通过网页中的绝对路径打开，或修改启动时的列表根目录：

```bash
bash ~/value_label_annotator/run.sh --root /path/to/datasets --port 8003
```

如果 VS Code 通过 Remote SSH 连接这台机器，在「端口」面板转发 8003，然后在本机浏览器访问转发地址。默认服务只监听 `127.0.0.1`。停止服务使用 Ctrl+C。


### 同一 Wi-Fi / 局域网访问

先保存当前标注，在启动服务的终端按 Ctrl+C 停止旧服务，再指定监听所有 IPv4 网卡。将 `/path/to/datasets` 替换为服务器上的实际数据集目录：

```bash
bash ~/value_label_annotator/run.sh --root /mnt/data/haolei/2026-09-btin-datasets/2026-09-G1D01-dagger-boin-40mm/boltinsert_value_validate_datasets --host 0.0.0.0 --port 8003
# bash ~/value_label_annotator/run.sh --root /path/to/datasets --host 0.0.0.0 --port 8003
```

在服务器上执行 `hostname -I` 查看内网 IP。其他设备在浏览器打开 `http://服务器内网IP:8003`。例如，服务器内网 IP 为 `10.10.1.41` 时，访问 `http://10.10.1.41:8003`。IP 以服务器当前配置为准；`0.0.0.0` 是监听地址，不是浏览器访问地址。

访问设备需要能连通服务器，服务器防火墙需允许 TCP 8003 端口。同一 Wi-Fi 下的访客网络或设备隔离也可能阻止访问。如果服务运行在远程服务器上，应使用远程服务器的内网 IP，而非运行浏览器的电脑 IP。其他设备上的 `localhost` 和 `127.0.0.1` 指向设备自身。

数据和视频保存在服务器上，网页中填写的数据集路径也是服务器路径。访问期间服务器和服务进程需要保持运行。

### Mac 一键启动并共享给同一 Wi-Fi 用户

如果 Mac 能通过 `ssh haolei@ch-pub-2` 连接服务器，但 Wi-Fi 内设备无法直接访问服务器 IP，可以让 Mac 作为共享入口。在 **Mac 终端**执行以下命令，下载并运行脚本：

```bash
curl -fL https://raw.githubusercontent.com/LeiH5180/value_label_annotator/main/start_mac.sh -o "$HOME/start_value_label_annotator.sh" && bash "$HOME/start_value_label_annotator.sh"
```

脚本使用 Mac 已有的 SSH 配置连接 `haolei@ch-pub-2`，复用服务器上已经运行的标注服务；如果服务未运行，则使用以下数据集目录启动服务：

```text
/mnt/data/haolei/2026-09-btin-datasets/2026-09-G1D01-dagger-boin-40mm/boltinsert_value_validate_datasets
```

脚本将 Mac 所有 IPv4 网卡的 `18003` 端口转发到服务器的 `127.0.0.1:8003`，并显示 Wi-Fi 访问网址。本机访问 `http://localhost:18003`，其他用户访问 `http://Mac的Wi-Fi-IP:18003`，无需各自连接 SSH。新启动的服务器服务仅监听回环地址；已有服务的监听配置保持不变。

保持 Mac 联网、唤醒和终端运行。按 Ctrl+C 或关闭终端会停止共享，服务器服务继续运行；脚本启动的服务日志保存在服务器的 `~/value_label_annotator/.cache/shared-server.log`。停止该后台服务时，先保存标注，再在服务器使用 `ss -ltnp 'sport = :8003'` 找到进程 PID，并执行 `kill <PID>`。

如果其他人无法访问，检查 Mac 防火墙是否允许 SSH 入站连接，以及 Wi-Fi 是否启用了访客隔离或设备隔离。如果本地端口被占用，可执行 `LOCAL_PORT=18004 bash "$HOME/start_value_label_annotator.sh"` 使用另一端口；连接目标可通过 `SSH_TARGET` 环境变量修改。需要更换默认数据集时，编辑下载脚本中的 `DATASET_ROOT`；复用已有服务时仍沿用已有服务的数据集配置。

### 检查服务状态

在服务器终端查看监听地址：

```bash
ss -ltnp 'sport = :8003'
```

- 没有 `LISTEN` 行：8003 端口没有服务监听。
- 显示 `127.0.0.1:8003`：仅服务器本机可直接访问，远程浏览器可通过 SSH 端口转发访问。
- 显示 `0.0.0.0:8003`：服务监听所有 IPv4 网卡，可通过服务器 IP 访问，仍需满足网络和防火墙条件。

检查网页是否响应：

```bash
curl --noproxy '*' --max-time 3 -I http://127.0.0.1:8003/
```

返回 HTTP 响应表示服务可访问；显示 `Failed to connect` 表示该地址连接失败。也可以将 `127.0.0.1` 替换为服务器内网 IP，从访问设备测试连通性。若使用了其他端口，上述命令和访问网址中的 `8003` 也需相应替换。

### 公网访问

`--host 0.0.0.0` 只改变监听范围，不会自动生成公网网址。跨网络访问需要额外配置公网入口，例如公网服务器反向代理、路由器端口映射或内网穿透，并确保访问设备能连接该入口。

当前系统没有用户登录，能访问服务的用户可以打开数据集并保存修改。对公网开放前应配置身份验证、HTTPS 和数据目录访问限制。

## 使用

1. 选择数据集及已有标签版本，点击「打开数据集」。路径指向包含 `meta/info.json`、`meta/episodes.jsonl` 和 `data/` 的目录。也支持单个 LeRobot v3 episode 的 metadata Parquet。
2. 打开数据集后设置区自动收起，给左侧 episode 列表留出更多空间。点击「数据集设置」可重新展开。每条轨迹显示「已修改 / 未修改」，可按状态筛选。页面显示 success / dagger / fail，以及固定、左腕、右腕三个相机。
3. 每个曲线点表示该 chunk 的**结束进度**，episode 起点保留原值。可以拖动点或直接输入结束进度；也可以输入 chunk 的增量。修改结束进度会调整相邻 chunk 的增量，后续结束进度保持原值；修改增量会平移后续曲线，保持后续 chunk 增量，并更新右侧显示的 episode 总进度。总进度就是末尾曲线点的值。
4. 点击曲线点定位到该 chunk 的末尾 action；「看起始 action」「看末尾 action」可检查边界画面。选中的按钮会高亮，之后通过上方箭头、键盘左右键或数值表切换 chunk 时，保持显示所选的起始或末尾 action；切换 episode 也保留此选择。曲线下方视频定位条和曲线中的竖向游标均可左右拖动，三路视频同步显示对应 action。播放中拖动定位条，松开后继续播放。
5. **页面下方**的「保存并标记已修改」将标签写入 Parquet，标记此轨迹为「已修改」，并将整条轨迹的所有 chunk 标记为 `human_annotated=true`。数值没有变也允许保存，同样代表人工确认整条轨迹。Ctrl / ⌘ + S 也可以保存。
6. 右侧「恢复此点初值」旁增加了「恢复整条初值」，与页面上方的还原按钮执行同一操作。恢复整条需二次确认，会撤回已保存修改，恢复初始化标签，并标记为「未修改」。恢复后可重新编辑或直接保存确认。如果初始时没有标签文件，恢复会删除人工创建的标签文件。

切换 episode 或关闭页面时，有未保存修改会提示。轨迹状态保存在当前标签目录的 `annotation_state.json` 中。首次保存或恢复前的初值备份保存到 `.annotation_initial/`；重新打开数据集或重启服务后仍可恢复该初值。新版初次使用时以已有标签为初值，无标签时以线性曲线为初值。

桌面窗口宽度至少 1000px、高度至少 650px 时，三路视频、完整曲线、右侧编辑区及保存按钮会适配到同一屏。右侧按「chunk 增量 → 结束进度 → episode 总进度」排列，增量为主要输入框。「查看所有 chunk 的数值」展开为浮层；较小窗口采用可滚动布局。

### 线性锚定与插值

- 默认锚定 episode 起点和终点。点击曲线点后，点击曲线旁的「线性锚定」；或直接在点上右键，切换锚定 / 取消。锚点显示橙色菱形，默认锚点也可取消。
- 至少选定两个锚点后，点击「线性插值」。以当前锚点进度为端点，按曲线横轴的真实时间，对每对相邻锚点之间的点分段线性插值。锚点数值和最外侧锚点之外的数值不变。
- 修改过的点不会自动成为锚点。手动调整数值后，可再次点击「线性插值」。插值会更新相应 chunk 增量，点击「保存」才写入 Parquet。
- 锚点选择按数据集标签版本和 episode 保存在当前浏览器中，切换轨迹或刷新后保留；锚点选择本身不算标签数值修改。恢复整条初值会将锚点重置为起点和终点。升级默认锚点功能后，已有锚点选择会保留，并一次性补上起点和终点。

升级了后端代码后，需要先保存当前编辑，在启动服务的终端按 Ctrl+C，再重新启动并刷新网页。界面会识别旧版服务并提示重启。

## 视频与 action 对齐

chunk 按 **data Parquet 中保留下来的连续 20 行 action** 划分，末尾允许少于 20 行。不能用 `frame_index // 20` 划分。

原视频保留全部帧，Parquet 删除静止帧后，其 `frame_index` 和 `timestamp` 可以跳号。点击 action 时，根据该行实际 `timestamp` 定位对应的原视频帧。为避免 float32 时间戳与 WebM 毫秒时间编码的舍入导致显示前一帧，视频定位落在目标帧内部；界面仍显示数据记录的原始时间戳。播放时，用视频时间查找最近一个已经发生的保留 action，更新曲线和 chunk。播放完整原视频时，被删除的静止帧仍会出现；视频时间落在两个保留 action 之间时，仍显示前一个 action 的索引。

曲线横轴使用真实视频时间。无标签时，进度按实际时间从 0 线性增加到 1。

AV1 源视频会按需转换成 H.264；不支持 H.264 的浏览器自动选择 VP9 WebM。转换结果缓存到 `.cache/videos/`，保持原帧率、帧数和时间线，视频准备可能需要几秒。支持 HTTP Range。缓存可删除，之后会重新生成。

## 读取与写入标签

已有逐 step 标签 `value_labels/<标签版本>/episodes/episode_XXXXXX.parquet`：

- 初始 chunk 增量取 `value_delta` 的第 0、20、40……行；与 `openpi-value/scripts/build_value_vis.py` 的 `progress[k] = Σ(j<k) delta[j]` 语义一致。
- 从 0 累加成边界进度，不强制归一化、单调递增或限制到 `[0,1]`，保留原预测结果。
- 保存**直接修改当前选中标签版本的原 Parquet**。进度边界 `p[k]` 改变时，更新所有受影响的 chunk 起始行的 `value_delta = p[k+1] - p[k]`。修改一个内部边界通常影响相邻两个 chunk 的增量。
- 其余 step 的 `value_delta` 和原有列保持原值；`human_annotated` 布尔列表示人工确认。点击保存后，所有 chunk 起始行都标记为 `true`，包括数值没有改动的 chunk；chunk 级标签文件的所有行都标记为 `true`。原文件的 schema metadata 保留，加入人工编辑时间等 metadata。
- 文件使用临时文件和原子替换保存，同一文件有其他页面的新修改时会拒绝覆盖，提示重新载入。
- 同步更新当前标签版本的 `manifest.json` 中 episode 增量统计。

无标签的 episode 首次保存后，文件新建在 `value_labels/human_value_h20/episodes/` 下。如果打开的数据集已有选定标签版本、但缺少某个 episode 的文件，则新文件创建在该版本目录下。

新文件一行一个 chunk，主要字段如下：

| 字段 | 含义 |
| --- | --- |
| episode_index, chunk_index | episode 和 chunk 编号 |
| start_step, end_step_exclusive | 在保留 action 序列中的范围 `[start,end)` |
| start_index, last_index | 对应 data 的全局索引 |
| start_frame_index, last_frame_index | 对应完整原视频的帧号 |
| start_timestamp, end_timestamp | 对应完整原视频的时间范围 |
| valid_action_count | 实际 action 数，最多 20 |
| progress_start, progress_end | chunk 起止进度 |
| value_delta, progress_delta | 结束进度减去起点进度 |
| human_annotated | 是否经人工确认；保存 episode 后，其所有 chunk 均为 true |

chunk 级新文件用于人工验证。若现有训练或自动标注代码假定标签文件与 data 的行数相同，需要在加载时区别 chunk 文件与逐 step 文件。

## 验证

测试只使用临时数据集，不写入实际训练数据：

```bash
cd ~/value_label_annotator
uv run --no-project --with pyarrow python -m unittest -v test_server.py
```
