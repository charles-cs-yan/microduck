# Microduck 程式碼地圖（給「跟隨人」模組用）

來源：github.com/MHDuck/microduck @ b979aed（2026-11-03）。上游另有 gitee moheng_robot/microduck_rust（見 `microduck/AGENTS.md`）。主體是 Rust workspace，Python 主要在 `scripts/`、`speech/`、`vision_follow/`。

## 架構
- 7 個 daemon，各自一個 unix socket，JSON-RPC 2.0 / NDJSON（`docs/design/architecture.md`）。
- **robotd**（`/run/robotd.sock`）：唯一能動馬達的程序。50 Hz 控制迴圈，15 顆 FT-HLS 舵機（14 個策略關節＋嘴巴 ID 34），IMU 在 i2c4（`/dev/i2c-imu`）。負責跌倒保護、deadman、權限仲裁。
- **mediad**：相機（IMX219，經 ISP，預設 720p30）＋ WebRTC（console `:8080`、signalling `:8443`）＋感知。偵測結果以 notification 推給 WebRTC peer（`mediad/src/main.rs` 約 380 行、`mediad/src/detect.rs`）。
- **tofd**：頭部 8×8 ToF 深度（`/run/tofd/tof.sock`，`tof.stream`），多數鴨子沒裝。
- configd / updaterd / btd：robotd 掛掉時仍可用的救援路徑。padd：手把。
- 發佈：`/opt/robot/daemon/releases/<ver>/` ＋ `current` symlink，updaterd 驗簽＋健康閘門＋回滾。
- 設定：`/etc/robot/robotd.toml`（範本 `deploy/robotd.toml`：`[safety]` `[detect]` `[media]` `[audio]` …）。

## 控制 API（`duck-ipc-proto/src/lib.rs`）
- `robot.move {vx, vy, vyaw}`：連續型，用 notification 持續送（m/s、rad/s，vyaw 正值左轉）。
- `robot.stop`：速度歸零（不卸力）。
- `robot.enable {on, toggle, stand_up}`：開關策略。
- `robot.look {x, y, z, neck_pitch}`：trunk 座標系注視點（公尺；地面約在 z=-0.12），daemon 端做 IK，回傳 `clamped`。
- `robot.head`：關節空間頭部目標。
- `robot.state` / `robot.subscribe` / `robot.health` / `robot.do`（skills：sit_toggle、kick_left/right、ground_pick、roulade）/ `robot.sound` / `robot.mouth` / `robot.speech` / `robot.vision`。
- 安全：`deadman_ms` 預設 500 ms，沒收到速度意圖就停（站著，不卸力）。
- 參考客戶端：`scripts/keyboard-control.py`（`send("robot.move", …)`、`call("robot.stop")`）。

## 現有視覺
- `duck-detect/`：單類別「偵測其他 Microduck」模型，`duck_detect.rknn`（NPU）/ `.onnx`（CPU）。**沒有人體偵測**。
- `[detect] hz` 預設 2.0，是熱限制：全速時 95 °C、CPU 降頻到 408 MHz。
- NPU（0.8 TOPS INT8）在 Armbian 預設關閉，要跑 `scripts/setup-npu.sh` 並重開機。duck 模型 NPU 實測 p50 25.7 ms / p95 58.4 ms；CPU 約 60 ms/幀（`docs/project/npu-bringup.md`）。
- `vision_follow/`：獨立 Python＋瀏覽器模組（`:8091`，`vision-follow.service`），重用 mediad WebRTC。用 ArUco（DICT_5X5_50、ID0、50 mm）或 duck 偵測。**只做前後跟隨**，不轉身、不橫移、不避障。停走式約 0.5 Hz，前進上限 0.12、後退 0.10 m/s。安全邏輯（零速確認、舊圖不觸發、IMU／匯流排檢查）值得沿用。
- `docs/ideas/autonomous_behavior.md`：規劃中的感知融合（相機＝方向、ToF＝距離、BLE＝身分）。

## 現有語音
- `speech/`：離線 sherpa-onnx 關鍵詞辨識（KWS），目前白名單只有「前進、後退、停止、坐下、起來」，不是任意 ASR。
- 加關鍵詞要改 `keywords_raw.txt` 並重跑 `prepare.py`。
- 語音前進固定 +0.10 m/s、最長 3 秒；robotd 合併語音意圖（`robotd/src/speech.rs`），手動控制會取消語音行走。
- `pet-detect/`：麥克風偵測摸頭，robotd 會咕咕叫。

## 其他可用互動素材
叫聲庫（`robot.sound` tags）、嘴巴開合、頭部注視、坐下／站起、踢、撿東西、roulade、theremin、多鴨合唱（chorale，預設關閉）。

## 客戶端寫法細節（2026-11-22 讀碼）

### `scripts/keyboard-control.py`（直接連 robotd socket）
- 連線：`AF_UNIX` 連 `/run/robotd.sock`，`timeout=1.0`，用 `makefile("rb")` 逐行讀；第一個呼叫是 `call("hello", {"api_version": 18})`（約 728 行）。板上 robotd 是 v18、robotctl 是 v17。
- 兩種送法（約 664–716 行）：
  - `send()`：沒有 id 的 notification，不等回覆；`robot.move`、`robot.head`、`robot.pose`、`robot.mouth` 都用它。
  - `call()`：帶 id，3 秒內等對應 id 的回覆，等待時順手吃掉 `robot.state` 推播；`result.accepted == false` 表示被拒，`reason` 是原因。
- 主迴圈每一拍只要 `enabled` 就重送 `robot.move`（約 1181 行），靠持續重送來讓 deadman 不觸發。HANDOVER 說的「鬆開後保持」就是這個造成的。
- 起身：`robot.stop` → `robot.pose {active:false}` → `robot.enable {on:true, toggle:false, stand_up:true}`。
- 結束時（約 1230–1264 行）：依序送 `robot.pose` 關閉、`robot.head` 歸零、`robot.stop`，**接著 `robot.relax` 並 `systemctl stop robotd`**。跟隨模組**不要**照抄這段，結束時只送 `robot.stop`，讓鴨子站著。
- 狀態訂閱：`robot.subscribe {hz:10}` 之後 robotd 會推 `{"method":"robot.state","params":…}`。

### `vision_follow/`（瀏覽器 → mediad WebRTC data channel → robotd）
- 不直接連 robotd socket。瀏覽器透過 mediad 的 WebRTC 控制通道（signalling `:8443`）送同樣格式的 JSON-RPC；`serve.py` 只負責提供網頁和 `/aruco/detect`（收 JPEG、用 OpenCV 找 ArUco），不碰相機也不碰馬達。
- `core.js` 是純邏輯，沒有 I/O：
  - `Tracker`：目標選擇（先選離畫面中心最近的，之後追同一個 ID，位置差小於 0.65、大小比例在 0.45～2.2 之間才算同一目標）、要連續兩幀確認、頭部 yaw 積分（有死區和速率限制）、用施密特帶判斷前進或後退（`targetSize` ±`sizeBand`），加速度上限 0.15 m/s²，目標丟失就立即歸零。
  - `FollowCycle`（停走式）：`stopping` → 收到零速度已被接受 → `settling` 1 秒 → 等穩定後的新畫面 → `detecting` → `moving`（最長 2.5 秒，加速度 0.3）→ 再回到 `stopping`。每張偵測結果只能用一次，舊圖不能延長移動。
  - `stateProblem()` / `imuEvidenceProblem()`：只有 policy 是 walk 或 stand、沒倒地、沒卸力、IMU 就緒、`consecutive_stale_blocks < 5`、bus `consecutive_errors == 0` 時才允許動。**這兩個函式可以直接改寫成 Python 沿用。**
- `app.js`：每 125 ms 查一次 `robot.health`，每 1.8 秒查一次 `robot.mode`（不是 walk 就停）；頁面進入背景、迴圈卡住超過 0.4 秒都會停。停止只送 `robot.move {0,0,0}`，**不卸力**（第 145 行有註解說明）。
- 板上目前沒有 `vision-follow.service`，也沒有 `~/microduck-vision`（見 HANDOVER 盤點）。

### 對我們的影響
- 板上 IMU 約一半讀數是 stale，代表 `imu_fresh=false` 會經常出現；照 `stateProblem` 的規則，要看 health 的 `consecutive_stale_blocks` 才能判斷能不能動，不能只看 `imu_fresh`。
- 舵機匯流排偶發錯誤（consecutive=1）會讓 vision_follow 的閘門把移動停下來；如果充飽電後還很頻繁，跟隨會一直被打斷。
- 我們的 `follow/` 用 Python 直接連 socket（照 keyboard-control 的 send/call 寫法），安全閘門照 core.js 的寫法，結束時只送 stop、不 relax。

## 對人體跟隨的含意
- 偵測：比照 duck-detect，在 mediad 內加人體偵測器（例如 YOLOv8n person → RKNN），結果走同一條 sighting 通道；不要另開相機。
- 控制：走 `robot.move`＋`robot.look`，依賴 robotd deadman 作為最後保護，但客戶端仍要主動 stop。
- 熱與算力是主要限制：必須上 NPU，偵測頻率要實測決定。
