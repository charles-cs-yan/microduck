# follow/ — Microduck 跟隨／互動模組

robotd 的客戶端：只透過 `/run/robotd.sock` 送意圖，不碰馬達、HOME、跌倒或策略程式。
協定以板上 robotd 實際的 **API v18** 為準（上游 rev `a9be46e`），不是本機上游最新的 v20。
主程式只用標準函式庫，Python ≥ 3.9（板上 trixie 是 3.13）；語音和相機各自在上游的 venv 裡以子行程執行。

## 檔案
| 檔案 | 作用 | 會讓鴨子動嗎 |
|---|---|---|
| `app.py` | 主程式：idle / gaze / follow / home / greet / dance 與姿態模式；`--home-turn DEG`（預設 180，0 = 不轉直接跟） | 透過 MotionGuard；預設 dry-run |
| `motion.py` | `MotionGuard`：唯一會送 `robot.move`、`robot.head`、`robot.mouth`、`robot.stop`、`robot.sound` 的地方 | **會**（`dry_run=True` 時不會） |
| `safety.py` | 純函式：`Limits`、限幅、`motion_problem` 閘門（從 core.js 移植，v18 欄位） | 否 |
| `tracker.py` | 純函式：`Tracker`、`FollowCycle`，逐行移植自 `vision_follow/core.js` | 否 |
| `turning.py` | 純函式：依 `robot.state.odom.yaw` 閉環原地轉向 | 否 |
| `greet.py` | 純函式：打招呼與跳舞的關鍵影格（頭、嘴、聲音、身體 pose），可循環 | 否 |
| `sources.py` | 偵測來源（回放、相機子行程）、指令佇列、鍵盤備援 | 否 |
| `speech.py` | 英文語音子行程 → 指令佇列 | 否 |
| `voice/` | `keywords_en.txt`、`models.py`（模型登錄）、`fetch_model.py`（下載＋驗證）、`kws_prepare.py`（編譯關鍵詞）、`kws_listen.py`（收音＋辨識）、`audio.py`（左聲道＋增益）、`mic_check.py`（上機診斷）、`setup_venv.sh`（鴨子上的語音環境） | 否 |
| `vision/aruco_worker.py` | 相機 → 上游 `ArucoDetector` → JSON 行（在 vision venv 執行） | 否 |
| `vision/exposure.py` | mediad 軟體自動曝光的 Python 移植（停 mediad 時用） | 否（只寫相機曝光） |
| `vision/camera_check.py` | 上線後的相機驗證：幀率、亮度、marker、存 JPEG | 否 |
| `vision/relay_worker.py` | 備案：上游瀏覽器中繼（mediad 照跑），偵測結果同時交給 follow | 否 |
| `status.py` | 操作員狀態列：一行即時狀態，事件印在上方 | 否 |
| `stand.py` | **操作員工具：讓鴨子站起來**（倒地或 held 時），獨立於 follow.app，要輸入 yes | **會（改變姿態）** |
| `posture.py` | robotd 能做的姿態與技能（sit_toggle、ground_pick、roulade、kick；pose 訓練範圍；起身流程） | 否（由 MotionGuard 送） |
| `robotd_client.py` | JSON-RPC/NDJSON 連線 | 本身不會 |
| `probe.py` | 唯讀探測 | 否 |
| `deploy.sh` | 複製到鴨子 `~/microduck-follow/` 並在鴨子上跑離線測試 | 否 |
| `show.sh` | 演出一鍵：唯讀檢查（電池 < 7.6 V 拒絕）→ sudo 停 mediad → `follow.app --camera /dev/video0 --voice --live --record …`；結束一定恢復 mediad。`--no-voice`、`SHOW_DRY=1` | 透過 follow.app（會問 yes） |
| `standby.sh` | **開機待命**：開機後在背景跑 `follow.app --live --scripted --standby`，等操作台輸入 yes 才連 robotd；`--stop`、`--status`；紀錄在 `~/microduck-follow/standby.log` | 透過 follow.app（要操作台 yes） |
| `console.sh` | 開機待命的操作台（筆電或手機 SSH App 都行）：即時看紀錄；打 yes 解鎖後單鍵操作、不用 Enter；`q` 只離開操作台（鴨子停下），`Q` 結束 show | 透過 follow.app |
| `standby_install.sh` | 開機待命開關：在使用者 crontab 加／刪一行 `@reboot`（不用 sudo）；`--remove`、`--show` | 否 |
| `vision/setup_venv.sh` | 鴨子上建 `~/microduck-vision/.venv`（OpenCV），搭配上游 `vision_follow/push.sh` | 否 |
| `tests/` | 假 robotd ＋ 簡單模擬世界的離線測試 | 否 |

## 模式與指令
| 模式 | 做什麼 | 鍵盤 | 語音 |
|---|---|---|---|
| idle | 站著不動，不送速度也不送頭部 | `s` 或 Enter | STOP |
| gaze | 頭追 marker，身體不動 | `g` | LOOK AT ME |
| follow | 頭追 marker；停走式前後跟隨；目標在正前方但頭轉超過 0.30 rad 時先原地轉身對正 | `f` | FOLLOW ME |
| home | 原地左轉 180°，完成後自動進入 follow（跟著主持人下台） | `h` | LET'S GO HOME / GO HOME |
| greet | 頭看左邊觀眾＋greet 叫聲＋嘴巴開合，再看右邊＋chirp，回正；結束後回到原本模式 | `w` | SAY HELLO |
| dance（跳舞） | 撒錢槍用：原地不走，身體左右傾 ±0.15 rad、蹲低 1.5 cm 點頭、頭反向擺、嘴巴開合、laugh／chirp／coo；4 秒一輪循環到下一個指令或 30 秒；從注視進來回到注視；離開時 pose `active:false`、嘴巴關。語音 "LET'S GO HOME" 在跳舞時也接受 | `d` | LET'S PARTY／SHAKE IT（"LET'S DANCE" 模型抓不到；單獨 "DANCE" 會誤觸發）。跟隨中用說的：先停，3 秒內站穩就跳 |
| LET'S GO（只有語音） | 待命時 = follow；注視／跳舞時 = home；跟隨中不做事。是 "LET'S GO HOME" 的開頭，模型聽到完整句會回報較長的那個 | — | LET'S GO |
| sit | 坐下：站穩時送 `robot.do sit_toggle`，等 policy 變 `sit` | `x` | SIT DOWN |
| skill（站起來） | 坐著 → `sit_toggle` 站起來；held／倒地／卸力 → 啟用策略起身（同 `stand.py`，舊 robotd 自動改送 `{on:true}`）。**從地上起身只接受鍵盤、要連按兩次**，電池要 > 7.5 V | `u` | GET UP（只在坐著時有效） |
| crouch（趴下） | robotd 沒有趴下技能，也沒有趴姿策略：用訓練範圍內最深的蹲低 z −0.025 m＋前傾 pitch 0.15 rad，1.5 秒滑下去後保持；離開時送 `active:false` 回正 | `p` | LIE DOWN |
| skill（撿東西） | `robot.do ground_pick` 一次，等它站回 walk/stand | `1` | — |
| skill（翻滾） | `robot.do roulade`（向前翻滾約 1 秒），**連按兩次** | `2` | — |
| skill（左／右踢） | `robot.do kick_left／kick_right`，**連按兩次**。板上 robotd 記錄 `kicks=false`（沒有踢的模型），robotd 會拒絕並說明原因 | `3`／`4` | — |
| — | 結束程式（送 stop，鴨子站著） | `q` | — |

姿態指令的保護（2026-11-23 第三輪審查後）：
- 只在**站穩不動**時做：policy 是 `stand`，或 `walk` 但實際速度≈0；robot.state 要在 1 秒內；程式沒有鎖住。正在跟隨／回家時，按鍵會先停下，站穩後要再下一次。
- **`robot.stop` 不會取消技能**（robotd 只把速度歸零、技能照做完），所以只要 robotd 回報 rise／standup／roulade／ground_pick／kick，就拒絕所有姿態指令——不管 follow 自己在哪個模式（按 `s` 離開技能模式也一樣擋）。
- 坐下送出後、robotd 回報 `sit` 之前，不接受其他姿態指令（`sit_toggle` 是切換，重複送會把剛坐下的鴨子又站起來）。
- 從坐姿站起來（`u`／GET UP）：坐著、沒倒地、狀態新鮮、IMU 正常、電池 > 7.5 V。
- 從地上起身（只有鍵盤 `u` 連按兩次）：只在 held 或倒地／卸力時；robotd 忙、坐著、保護狀態都拒絕（保護時請用 `stand.py`，它會問 yes）；IMU 與電池同 `stand.py`。
- 連按兩次要**連續**：中間插了別的指令，或模式變了（例如倒地回待命），就要重來。
- 跟隨或回家途中不接受姿態語音（主持人對觀眾說 "sit down" 也會被聽到）；撿東西、翻滾、踢只接受鍵盤。
- 趴下用的 pose 在 robotd **沒有 deadman**：放開一定在鎖內送 `active:false`，送 `active:true` 前也在同一把鎖內重新確認，所以放開之後不會冒出舊的 `active:true`；app 停止更新時 MotionGuard 會主動送 `active:false`。
- 技能等待中不套用「倒地即停」（翻滾時 IMU 本來就會判倒地），改成等它站回 walk/stand；保護狀態或逾時就回待命並提示。dry-run 都不送。

## 安全規則（程式裡寫死的部分）
- 前進 ≤ 0.12 m/s、後退 ≤ 0.10 m/s、轉向 ≤ 0.5 rad/s（轉身實際用 0.15–0.40）；`Limits` 不允許超過 0.30 m/s。`vy` 一律是 0。
- 行走／轉身閘門：policy 是 `walk` 或 `stand`、沒倒地、沒卸力、state 與 health 在 0.5 秒內、`robot.mode` 在 3.8 秒內確認 `walk`、`healthy`、IMU ready 且連續重複 < 5、匯流排連續錯誤 = 0、電池 > 7.6 V、CPU < 90 °C、馬達 < 65 °C。
- 電池一到 7.6 V，本次執行鎖住不再走並提示充電（原廠參考值 7.5 V；robotd 電壓有約 10 秒平滑，抓不到起步壓降，見斷線處理指南 §1.1、§2.1）。最低只能設到 7.5 V。
- 頭與嘴：有限幅；只在站穩時送。電池低時還能轉頭、動嘴，只是不走。
- 呼叫端 0.4 秒沒更新就停送（在 robotd 500 ms deadman 前面再一層）。控制迴圈停頓 > 0.4 秒、視訊 10 秒沒新畫面都會停。
- 閘門一失敗：清掉意圖、送有確認的 `robot.stop`。兩種恢復方式：
  - **自動恢復（只限資料過期類）**：state／health／mode 過期、迴圈停頓、視訊沒新畫面、IMU 短暫重複、匯流排單次錯誤——狀況解除後，重新經過「確認零速 → 穩定 1 秒 → 新畫面」才可能再走。
  - **回到待命、要重新下指令**：倒地、卸力、策略變成 walk/stand 以外、保護、健康檢查不過、CPU／馬達過熱、跟隨或回家時電池過低。就算鴨子被扶起來也不會自己再走（和上游一樣）。
- 跟隨中每段移動時也持續讀畫面，**目標一不見就立刻煞停**（不會走完 2.5 秒；畫面永遠不會延長一段移動）。
- 轉身：航向 2 秒沒變化（卡住或 dry-run）、往反方向轉超過 0.15 rad（odom 正負號不對）、轉過頭 0.3 rad、超過時限（180° 約 16 秒）、沒有 odom、閘門失敗 → 停下回 idle。
- 送出執行緒或健康執行緒發生任何非預期例外 → 立刻停、記錄、鎖住（直到重新啟動程式），執行緒本身不會靜默死掉。
- `robot.move` 在鎖內、重新確認意圖後才送，確保已確認的 `robot.stop` 之後不會冒出舊的 move（2026-11-23 審查 #1）。
- 結束時（例外、Ctrl-C、SIGTERM/SIGHUP）送 `robot.stop`，用過嘴巴就把嘴巴關上；鴨子保持站立。
- **永遠不送 `robot.relax`、`robot.init`**（測試會檢查）。跟隨／注視／回家／打招呼的流程也不送 `robot.enable`、`do`、`pose`。只有操作員明確下的姿態指令（上表 `x u p d 1 2 3 4`）和 `stand.py` 才會送 `robot.do`／`robot.pose`／`robot.enable`，而且都先確認停下、各自有閘門。`stand.py` 要輸入 yes，從不卸力；電池 ≤ 7.5 V、IMU 不正常、狀態過期、已在起身中都會拒絕。
- **只有這台鴨子的即時相機能驅動它**：`--live` 必須同時有 `--camera`，而且啟動時要在終端機輸入 `yes`。其他情況一律 dry-run（不送任何馬達指令、也不出聲）。

## 在鴨子上跑
```bash
cd ~/microduck-follow/current
python3 -m follow.probe --seconds 30                       # 唯讀
python3 -m follow.app                                      # dry run：鍵盤、狀態、閘門
python3 -m follow.app --replay detections.jsonl            # dry run：回放偵測
python3 -m follow.app --camera /dev/videoN                 # dry run：真相機（mediad 要先停）
python3 -m follow.app --camera /dev/videoN --voice         # 再加英文語音
python3 -m follow.app --camera /dev/videoN --live          # 真的會動（會先問 yes）
python3 -m follow.app --relay --live                       # 備案：瀏覽器中繼（mediad 不停）
python3 -m follow.stand                                    # 讓鴨子站起來（會先問 yes）
```
### 開機待命（11/26 新增，還沒上機）
```bash
bash follow/standby_install.sh          # 開：之後每次開機自動待命（只加一行 crontab）
bash follow/standby_install.sh --remove # 關
bash follow/console.sh                  # 操作台：扶好鴨子 → yes → 跟 show.sh 一樣的按鍵
```
- 開機時什麼都不會動：robotd 本來就是 Limp，follow.app 收到操作台的 `yes` 之前連 robotd 都不連。
- show 不再跟著 SSH 一起結束，所以改成：**操作台或筆電語音斷線就送 stop**（跟以前 SSH 斷掉的效果一樣）。
- 電池 ≤ 7.0 V 不啟動；當掉會重啟，最多 3 次；按 `q` 或被結束就不重啟。
- `start_show.ps1` 會自動判斷：鴨子在待命就開操作台，沒有就照舊跑 show.sh。待命時直走距離用 standby.sh 的 `STANDBY_WALK_M`（預設 2），不是 `-WalkM`。

終端機最下面一行是即時狀態（模式·階段、原因、電壓、CPU、相機、語音、上一句指令），事件印在它上面。輸出導到檔案（`| tee show.log`）時改成每 5 秒一行。

### 相機（決定：演出時停 mediad、直接讀；還沒在鴨子上驗證）
mediad 把相機畫面留在自己的 appsink，只給它內建的偵測器用，**沒有對外取畫面的 API**（板上版本和上游最新版都一樣；設計文件規劃的 `get_frame` 還沒實作）。
2026-11-23 決定：演出時停掉 mediad，由 `vision/aruco_worker.py --device` 用 `gst-launch` 直接讀 rkisp 節點，辨識和控制都在鴨子上，不依賴 Wi-Fi 和電腦。備案是上游的瀏覽器中繼。

停掉 mediad 也就停掉了它的**軟體自動曝光**（rkaiq 只在開始串流時調一次）。所以 `vision/exposure.py` 移植了 mediad `exposure.rs` 的同一套迴圈（目標亮度 90、死區 12%、快門→類比增益→長快門→數位增益），用 `--camera` 時**預設就開**（`--no-auto-exposure` 關掉），在 worker 裡用 `v4l2-ctl` 寫曝光（只動相機）。11/23 上機實測：停 mediad 後感光元件停在 159 lines／1×，平均亮度只有 3–11，所以一定要開。

上線後的驗證（約 10 分鐘，`vision/camera_check.py`，不會讓鴨子動）：
1. 唯讀找相機節點與旋轉：`systemctl cat mediad`、`journalctl -u mediad -b | grep -i -E 'device|rotat'`、`v4l2-ctl --list-devices`；確認 `systemctl show mediad -p Requires,BindsTo,PartOf` 停 mediad 不會連帶停 robotd。
2. **經使用者同意**後 `sudo systemctl stop mediad`（復原：`sudo systemctl start mediad`）。
3. `~/microduck-vision/.venv/bin/python follow/vision/camera_check.py --device /dev/videoN`：拿到幾張、實際幀率、亮度、看不看得到 marker；`first.jpg`／`last.jpg` 拿回筆電看旋轉。
4. 亮處、暗處各放一次 marker，看亮度有沒有收斂到 90 附近（自動曝光預設開）。
5. 都過了才用 `python3 -m follow.app --camera /dev/video0`；不過就改走瀏覽器中繼。

11/23 已確認：`/dev/video0`、UYVY 1280x720 用 `gst-launch` 讀得到；**`--rotate 90` 正確**（照片順時針轉 90° 後筆電螢幕在上）；停 mediad 不會停 robotd。還沒做：裝 `~/microduck-vision`（OpenCV）後跑 `camera_check.py` 看 marker 與自動曝光收斂。

需要：`~/microduck-vision/`（上游 `aruco_detector.py` ＋ `.venv` 含 `opencv-contrib-python-headless`）、`gst-launch-1.0`、`v4l2-ctl`（mediad 的擷取路徑本來就要這兩個）。

### 英文語音（用鴨子真實錄音驗證過辨識；還沒在鴨子上即時跑過）
**模型：預設是 sherpa-onnx 官方的英文 KWS（GigaSpeech 3.3M，BPE）**，上游的中英雙語模型（zh-en 3M）用 `--kws-model zh-en` 仍可選。理由（2026-11-23，詳見 `voice/models.py`）：
- 鴨子麥克風錄到的主持人 "follow me"，zh-en 在門檻 0.05、加權 3、加發音變體都辨識不到；GigaSpeech 在增益 10 以上、每組參數都辨識到。
- GigaSpeech：6 句合成指令全對；沒說話的錄音（放大到 100 倍）、6 句干擾句＋鴨子噪音都沒有誤觸發；指令縮到鴨子收音音量＋噪音時 5 句中 4 句辨識到（"let's go home" 漏掉——現場要講清楚，或用鍵盤 `h`）。

**收音**（`voice/audio.py`，11/23 在鴨子上量的）：`hw:microducki2s3,0` 最少 2 聲道，**只有左聲道有麥克風**；說話聲約 -45 dBFS、背景約 -50 dBFS（沒有類比前級）。所以直接錄 S32 立體聲、取左聲道、數位增益預設 20（5 倍不夠、10 倍剛好）。mediad 開著也能錄。

步驟（鴨子上，要網路，不用 sudo）：
1. `bash ~/microduck-follow/current/follow/voice/setup_venv.sh`：建 `~/microduck-follow/venv-voice`（sherpa-onnx 1.13.8、numpy），下載並驗證 SHA256 後解壓模型到 `~/microduck-follow/models/kws-gigaspeech`。
2. `~/microduck-follow/venv-voice/bin/python ~/microduck-follow/current/follow/voice/mic_check.py`：按 Enter 後錄 5 秒，印出各聲道音量／訊噪比和各增益下辨識到的指令。
3. `python3 -m follow.app --voice …`。
只有改了 `keywords_en.txt` 才要重編：`PY follow/voice/kws_prepare.py [--model zh-en]`。下載工具 `voice/fetch_model.py` 只用標準函式庫（不需要系統有 bzip2），`--archive` 可離線用。
「STOP」誤觸發只會停下（安全）；「FOLLOW ME」誤觸發會開始跟隨，所以台上仍要有人拿鍵盤備援。

## 測試
```bash
python3 -X dev -W error::ResourceWarning -m unittest discover -s follow/tests -t .
# 視覺整合測試（真的 OpenCV：合成 marker → aruco_worker → Tracker），用有 OpenCV 的 venv：
~/mdev/vision-venv/bin/python -m unittest follow.tests.test_integration_vision
```
2026-11-23：WSL（Python 3.12）181 個全過（7 個整合測試在沒有 OpenCV 時略過）；vision venv 的整合測試 7 個全過（含瀏覽器中繼端到端：上游 serve.py 原封不動），驗證了 `--rotate 90` 的座標換算、marker 大小對前進／後退方向、別的 ID 不會被追、節流、亮度量測。Windows uv Python 3.15 也全過。

開發機的環境（WSL，2026-11-23 建好；沒有 sudo，所以 pip 是用官方 get-pip.py 裝進 venv）：
- `~/mdev/speech-venv`：上游 `speech/requirements.txt`（sherpa-onnx 1.13.8、numpy 1.26.4）
- `~/mdev/vision-venv`：上游 `vision_follow/requirements.txt`（opencv-contrib-python-headless 4.12.0.88、numpy 2.2.6）——兩者 numpy 版本衝突，所以分開，和鴨子上一樣
- `~/mdev/speech/`：上游 speech 的副本，`prepare.py` 下載的官方模型在 `models/`
- `~/mdev/node`：官方 Node.js v22.12.0（SHA256 驗過），給逐步比對用

**JS／Python 逐步比對**（`tests/test_differential.py`，找得到 Node 才跑：`PATH=~/mdev/node/bin:$PATH`）：同一批隨機情境（固定種子）同時跑上游 `core.js` 和 `tracker.py`，每一步比對輸出和內部狀態（容許誤差 1e-9）。2026-11-23：84,726 步完全一致，其中 6,351 次有效輸出、2,215 次非零速度、2,883 步在移動段。`python3 -m follow.tests.diff.sanity` 統計覆蓋率，並注入三種細微差異確認比對抓得到（三個都抓到）。上游原本的 26 個 JS 測試在同一個 Node 上也全過。

**2026-11-23 獨立審查**：另一個代理人只看程式碼，找到 7 項：
1. 嚴重：已確認的 stop 之後可能冒出舊的 move；
2. 倒地被扶起後會自動恢復跟隨；
3. 轉向沒有反方向檢查；
4. 慢速 stop 會縮短穩定時間；
5. 執行緒例外會靜默死掉；
6. 相機子行程可能殘留；
7. 移動中不檢查目標遺失。

全部修正，每一項都有回歸測試，並在暫存副本拿掉修正確認測試會失敗。

## 錄影與回放
`python3 -m follow.app --camera … --record rehearsal.jsonl` 會把每張新畫面的偵測結果存下來（任何模式都錄），格式就是 `--replay` 吃的格式，排練完拿回筆電重播調參數。
測試裡有一個簡單的模擬世界：身體航向依實際送出的 `vyaw` 積分，marker 在畫面上的位置由「marker 方位 − 身體航向 − 頭 yaw」算出。

沒驗證到的：
- gst-launch 讀 rkisp、`v4l2-ctl` 寫曝光、ALSA 麥克風、瀏覽器實際連 mediad——這些只有鴨子上有。
- **`odom.yaw` 的正負號**：程式假設「正 vyaw → yaw 變大」。第一次轉身前要有人扶著做一次小角度轉身確認；反了的話轉身會在 0.15 rad 內自己停下並說明原因。
- 全部還沒在鴨子上跑過；walk 策略能不能用 0.15 rad/s 原地轉，也要實測。

## 部署
```bash
bash follow/deploy.sh radxa@192.168.50.135
```
只會寫入鴨子的 `~/microduck-follow/`，不啟動程式、不動服務、不用 sudo。復原：`ssh radxa@HOST 'rm -rf ~/microduck-follow'`。
