# Microduck 工作區

這個資料夾是 Microduck（MHDuck）機器鴨的開發工作區。
目標：做一個 Python 模組，讓鴨子能和人互動、自動跟隨人，並完成舞台展示：
「跟我走 / follow me」→ 跟著主持人走上台 → 台上互動 → 「我們回家 / let's go home」→ 一起下台。

## 目錄
- `microduck/`：上游程式碼（git clone https://github.com/MHDuck/microduck）。上游規則見 `microduck/AGENTS.md`。
- `docs/codebase-map.md`：程式碼地圖（架構、IPC API、現有視覺與語音、限制）。
- `docs/stage-demo.md`：舞台展示需求與風險。
- `docs/show-runbook.md`：演出當天的一頁操作手冊。
- `docs/vendor-docs/`：使用者提供的繁中文件（斷線處理、入門指南）；已知錯誤見 `HANDOVER.md`「文件說明」。
- `follow/`：新寫的跟隨／互動模組（robotd 客戶端、安全閘門、MotionGuard、唯讀 probe），說明見 `follow/README.md`。板上 robotd 是 API v18，協定以上游 rev `a9be46e` 為準。

## 機器人
- 主機：Radxa Zero 3W（RK3566），Armbian；這是**雙足**機器人（14 個策略關節＋嘴巴），不是四足。
- 位址：`192.168.50.135`（家裡 Wi-Fi，可能會變；變了先到路由器確認）。
- 登入：`ssh radxa@192.168.50.135`。請使用 SSH 金鑰；不要把密碼寫進任何檔案或指令。
- 控制介面：`/run/robotd.sock`，JSON-RPC 2.0（NDJSON）。`robot.move {vx,vy,vyaw}`、`robot.look`、`robot.stop`、`robot.enable`、`robot.state`、`robot.health`。
- robotd 有 deadman（預設 500 ms 沒收到速度意圖就停下、保持站立）。

## 安全規則（一定要遵守）
1. **任何會讓馬達或姿態改變的動作，執行前都要先問我並等我確認**：
   `robot.enable`、`robot.move`、`robot.do`、`robot.pose`、`robot.init`、`robot.relax`、
   `keyboard-control.py`、`standup-test.py`、`servo_*.py`、`systemctl start|restart robotd`、重開機或關機。
   原因：鴨子會站起或倒下，需要有人在旁邊扶著。
2. 唯讀檢查可以直接執行，例如：`uptime`、`systemctl status`、`journalctl`、`robotctl health`、
   `python3 -m follow.probe`、`dmesg`、`cat` 設定檔、`ls`。
3. 不修改 robotd 的馬達、HOME、跌倒、策略、安全相關程式（上游 AGENTS.md 的規定）。新功能一律當作 robotd 的客戶端，透過 socket 送意圖。
4. 跟隨模組自己的限制：前進 ≤ 0.12 m/s 起步（客戶操作上限 0.30）、不用 vy、目標遺失或程式結束時一定送 `robot.stop`。
5. 偵測頻率受熱限制（約 2 Hz）；修改前先量 `robotctl health` 的 cpu 溫度。
6. 電池到 7.2 V 就停止行走並提醒我充電（2026-11-25 使用者決定；原廠建議 7.5 V，7.5 V 以下要盡快充電）。
7. 不要格式化 TF 卡、不要重刷系統、不要改網路設定，除非我明確要求。

## 溝通
- 用繁體中文回覆。
- 每次改動鴨子上的檔案前，先說明要改什麼、怎麼回復。
