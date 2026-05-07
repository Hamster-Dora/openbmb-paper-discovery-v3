# OpenBMB Paper Discovery

本项目用于按关键词抓取论文、入库、消费打分，并支持 `launchd` 定时任务与飞书提醒。

## 1) 环境要求

- macOS（项目内含 `launchd` 配置）
- Python 3.11+
- 已安装 Git

## 2) 安装与初始化

```bash
cd /Users/zhangzhixian/Desktop/openbmb-paper-discovery-v3

python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

然后手动编辑 `.env`，填入你自己的 API Key / Webhook（`.env` 不要上传）。

## 3) 手动执行（按日期回溯抓取）

```bash
cd /Users/zhangzhixian/Desktop/openbmb-paper-discovery-v3

/usr/bin/caffeinate -s zsh -lc '
venv/bin/python main_manual.py --start_date 2025-07-01 --end_date 2025-07-10
'
```

可选：只跑部分标签（省配额）

```bash
venv/bin/python main_manual.py --start_date 2025-07-01 --end_date 2025-07-10 --tags "LLM agent,tool learning"
```

## 4) 自动任务（launchd）管理与重新加载

若你已将 plist 放到 `~/Library/LaunchAgents/`，可用下面命令重载：

```bash
uid=$(id -u)

launchctl bootout gui/$uid "$HOME/Library/LaunchAgents/com.openbmb.paper-discovery.reminder.plist" 2>/dev/null
launchctl bootout gui/$uid "$HOME/Library/LaunchAgents/com.openbmb.paper-discovery.nightly.plist" 2>/dev/null

launchctl bootstrap gui/$uid "$HOME/Library/LaunchAgents/com.openbmb.paper-discovery.reminder.plist"
launchctl bootstrap gui/$uid "$HOME/Library/LaunchAgents/com.openbmb.paper-discovery.nightly.plist"

launchctl enable gui/$uid/com.openbmb.paper-discovery.reminder
launchctl enable gui/$uid/com.openbmb.paper-discovery.nightly

launchctl print gui/$uid/com.openbmb.paper-discovery.reminder
launchctl print gui/$uid/com.openbmb.paper-discovery.nightly
```

**命令原理解释：**
这一串命令本质上就是“重新确保 launchd 已注册并启用”，它不会改你的业务逻辑，也不会改数据库内容。
- `bootout`：把旧的同名任务从当前用户的 launchd 域里卸掉。
- `bootstrap`：把 plist 重新加载进来。
- `enable`：确保它不是 disabled 状态。
- `print`：检查它现在是不是已经挂在 launchd 里了。

它的作用是修复“任务掉了/没加载”，重新注册 reminder（触发条件 18:00）和 nightly（触发条件 01:00），确认之后它们还会到点自动触发。它**不会**立刻跑 nightly 任务、不会清数据库、不会改 `.env` 或筛选逻辑、也不会改 plist 内容。

**什么时候需要重跑这组命令：**
平时不需要每天都重跑。只有在以下情况才需要：
1. `launchctl print ...` 显示 missing。
2. 到点了日志完全没动。
3. 重启/换环境后怀疑任务掉了。
4. 你改了 plist 内容后想重新加载。

**仅测试 reminder：**
如果只想测试 reminder，不要触发 nightly（因为那会立刻开始消费现在的 PENDING），可运行：
```bash
uid=$(id -u)
launchctl kickstart -k gui/$uid/com.openbmb.paper-discovery.reminder
```
若提示中有包含类似 `当前仍有 **11013** 篇 PENDING 待消化...` 的内容，说明 reminder 功能畅通，且 launchd 任务已恢复。

## 5) 30秒每日自检清单

每天你可以直接跑这一段一键验证命令：

```bash
cd /Users/zhangzhixian/Desktop/openbmb-paper-discovery-v3

launchctl print gui/$(id -u)/com.openbmb.paper-discovery.reminder >/dev/null && echo "reminder ok" || echo "reminder missing"
launchctl print gui/$(id -u)/com.openbmb.paper-discovery.nightly >/dev/null && echo "nightly ok" || echo "nightly missing"

./venv/bin/python -c "from database.models import Paper; print('pending =', Paper.select().where(Paper.status=='PENDING').count())"

stat -f 'reminder log: %Sm' -t '%Y-%m-%d %H:%M:%S' data/logs/launchd_reminder.err
stat -f 'nightly log:  %Sm' -t '%Y-%m-%d %H:%M:%S' data/logs/launchd_nightly.err
```

**每天看输出时主要判断 4 件事：**
1. **看任务是否还挂着：** 如果前两行都显示 `ok`，说明自动化还活着。
2. **看库里 pending 是多少：**
   - 若等于 `0`：今晚可以放心关机。
   - 若 `>0`：如果你希望夜里自动消费，今晚别关机。
3. **看 reminder 和 nightly 的 log 时间：** 确认时间是否正常在变化。如果时间长期不变，就要警惕 launchd 任务掉了。

## 6) 数据库清理与重置方案

**操作前提：** 一定先停掉正在跑的 `closed_loop` / `main_cron` 相关任务，再删库。避免边清边写。
可通过以下命令确认（如果没有没有任何返回，说明当前没有在跑）：
```bash
ps -ax | grep "core/closed_loop.py" | grep -v grep
```
*注：若担心删除时被自动任务抢占，可临时先 unload nightly，删完再 load 回来。*

### 方案 A：彻底清空（推荐，最干净）
这会把库文件直接删掉，下次程序会自动新建空库。走此方案第一次查可能提示库不存在，运行一次项目脚本后会自动建库。

```bash
cd /Users/zhangzhixian/Desktop/openbmb-paper-discovery-v3
# 删库与预算（预算重置可从0开始计）
rm -f data/openbmb_papers.db
rm -f data/budget_usage.json
# 验证：检查 db 文件是否不在了
ls -lah data
```

### 方案 B：稳妥可回滚方案（保留库文件，只删表数据）
此方案只删数据，不动表结构、索引、迁移逻辑。

1. **备份数据库（强烈建议，以便秒回滚）：**
   ```bash
   cp data/openbmb_papers.db "data/openbmb_papers.db.bak.$(date +%Y%m%d_%H%M%S)"
   ```
  
2. **清空 papers 表记录（保留表结构）：**
   ```bash
   sqlite3 data/openbmb_papers.db "DELETE FROM papers;"
   sqlite3 data/openbmb_papers.db "SELECT COUNT(*) FROM papers;"
   ```
   *第二条应返回 `0`。*
3. **（可选）重置预算计数：** 把 `data/budget_usage.json` 清空或删除，程序会重建当天状态。
4. **手动重建验证（先小范围确认链路可用）：**
   ```bash
   venv/bin/python main_manual.py --start_date 2025-06-01 --end_date 2025-06-01
   sqlite3 data/openbmb_papers.db "SELECT COUNT(*) FROM papers;"
   sqlite3 data/openbmb_papers.db "SELECT status, COUNT(*) FROM papers GROUP BY status;"
   ```
   *只要总数 > 0，就说明“手动收集可重建”正常。*
5. **自动化重建演练：**
   手动模拟 nightly：`venv/bin/python main_cron.py --job all`。看日志是否出现常规轨、消费唤醒开始、closed_loop 第等执行信息。
6. **周一（自动验证点）：**
   核对 `launchd_nightly.err` 01:00 日志；确认 `papers` 总量从 0 变 >0；确认 status 里有 PENDING（之后会被 consumer 消化）。
7. **回滚方案（如果不满意）：**
   ```bash
   cp data/openbmb_papers.db.bak.YYYYmmdd_HHMMSS data/openbmb_papers.db
   ```
  
## 7) 自动化运行常见现实场景

1. **只清理数据库记录，会不会影响自动化？**
   不会影响定时机制本身，但会影响“跑的时候有没有数据可处理”。表清空后，reminder 不再提示有 PENDING，nightly 的 consumer 会很快结束。到了周一/每月 1 号，producer 会继续自动抓取并重新入库。注意最好在没有任务跑的时候清理，且 `budget_usage.json` 状态可能仍会影响消费。
2. **不定时关机会不会影响？**
   会影响触发，但不影响功能本身。关机时不会运行，开机且已登录才会运行，睡眠唤醒后有时会补跑但不可信赖。合理策略：没有 PENDING 时关机问题不大，有 PENDING 且希望夜里自动消费时尽量别关机。
3. **换网络（公司/家里）会不会影响？**
   会影响抓取和 PDF 下载成功率，但不会影响 launchd 到点触发。最需注意的是你的代理/翻墙节点在计划执行时是否对 Semantic Scholar、arXiv/PDF源站、LLM Center、飞书 webhook 可用。检查家里网络是否导致更多 `pdf_unreachable`，以及公司内外网切换后 `LLM_CENTER_API_ROOT` 是否仍可达。可通过查看 `data/logs/launchd_nightly.err` 和 `launchd_reminder.err` 排查。只要 `print` 能看到服务，说明任务还挂着。

## 8) GitHub 上传与隐私说明

- 已忽略：`.env`、`PRD.md`、`TODO.md`、本地日志与临时数据（以 `.gitignore` 为准）。
- 可以上传：代码、`.env.example`、`whiteboard_exported_image.png`、`launchd` 示例配置。
- 若误传过密钥，请立即在对应平台轮换（revoke + regenerate）。

## 9) 维护声明（建议）

本仓库默认用于代码与流程复现，不包含生产密钥与私有业务文档。  
在公开仓库场景下，请仅提交可公开的信息；涉及账号、预算、内部流程细节的内容请保留在本地或私有仓库。

## 10) Windows 部署说明（PowerShell）

说明：Windows 可完整运行抓取、入库、消费与提醒逻辑；但 macOS 的 `launchd` 命令不适用，需改用 Windows 任务计划程序（Task Scheduler）。

### 10.1 选择安装路径并初始化

将下面路径替换为你自己的安装目录（示例为 `D:\apps\openbmb-paper-discovery-v3`）：

```powershell
# 你可以改成任意目录
$InstallDir = "D:\apps\openbmb-paper-discovery-v3"
$ParentDir  = Split-Path $InstallDir -Parent

mkdir $ParentDir -Force | Out-Null
cd $ParentDir
git clone https://github.com/Hamster-Dora/openbmb-paper-discovery-v3.git
cd $InstallDir

# 建议 Python 3.11
py -3.11 -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt

copy .env.example .env
notepad .env
```

> `.env` 内填写你自己的 API Key/Webhook，且不要提交到 GitHub。

### 10.2 手动执行（按日期回溯）

```powershell
cd D:\apps\openbmb-paper-discovery-v3
.\venv\Scripts\python.exe .\main_manual.py --start_date 2025-07-01 --end_date 2025-07-10
```

可选：只跑部分 tags（更省配额）

```powershell
.\venv\Scripts\python.exe .\main_manual.py --start_date 2025-07-01 --end_date 2025-07-10 --tags "LLM agent,tool learning"
```

### 10.3 自动任务（Task Scheduler）

下面命令会创建两个每天执行的任务：

```powershell
$InstallDir = "D:\apps\openbmb-paper-discovery-v3"
$Py = "$InstallDir\venv\Scripts\python.exe"

# 每天 01:00：regular+conference(按日期判断)+consumer
schtasks /Create /F /SC DAILY /ST 01:00 /TN "OpenBMB-Nightly" `
  /TR "`"$Py`" `"$InstallDir\main_cron.py`" --job all"

# 每天 18:00：智能提醒
schtasks /Create /F /SC DAILY /ST 18:00 /TN "OpenBMB-Reminder" `
  /TR "`"$Py`" `"$InstallDir\main_cron.py`" --job reminder"
```

查看任务：

```powershell
schtasks /Query /TN "OpenBMB-Nightly" /V /FO LIST
schtasks /Query /TN "OpenBMB-Reminder" /V /FO LIST
```

手动触发测试：

```powershell
schtasks /Run /TN "OpenBMB-Nightly"
schtasks /Run /TN "OpenBMB-Reminder"
```

### 10.4 Windows 日常自检（简版）

```powershell
$InstallDir = "D:\apps\openbmb-paper-discovery-v3"
cd $InstallDir

schtasks /Query /TN "OpenBMB-Reminder" /FO LIST | findstr /I "Status"
schtasks /Query /TN "OpenBMB-Nightly" /FO LIST  | findstr /I "Status"

.\venv\Scripts\python.exe -c "from database.models import Paper; print('pending =', Paper.select().where(Paper.status=='PENDING').count())"
```