# 不開 App 也保存每日資產快照

程式會在開啟 App、帳本操作完成後更新當日快照。另有 GitHub Actions 每天台灣時間 **18:17** 保存一次，即使 Streamlit 休眠、手機沒開 App、電腦關機也能執行。GitHub Actions 需要先完成下方一次性設定；只有更新 App 程式還不會啟用這個功能。

## 快照記錄什麼

- 當時帳本的現金、持有股數、各股票可取得的最近收盤價、總資產、淨投入與目標。
- 「快照日期」是保存當天；每支股票另外保留實際報價日期。週末／休市日通常沿用前一交易日收盤價，所以兩天的市值可能相同。
- 原有帳本的總資產走勢保持原範圍，另外登記的「既有庫存」不混入這條走勢。
- 同一天可保存多次，圖表以當天更新時間最後一筆顯示；快照保留在 Google Sheets 的 `Snapshots_v2` 工作表。
- 需要的報價不完整、帳本讀取失敗或持續有同時寫入時，該次排程失敗，不以零價或部分報價保存。原有歷史仍保留。

排程只保存執行當下的狀態，**不會補造部署前或執行失敗日期的歷史資產**。例如 10/19、10/20 的排程都有成功，就各有一筆那天帳本搭配最近收盤價的快照；若兩天排程都失敗，這兩天就沒有快照。

## 一次性設定

1. 將這次更新推送到 GitHub 專案的預設分支（目前為 `main`），確認已有 `.github/workflows/daily-snapshot.yml`。排程工作流程必須存在於預設分支，才能依排程執行。[GitHub 官方排程說明](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)
2. 開啟專案網頁 → **Settings → Secrets and variables → Actions → Secrets → New repository secret**，新增下表的兩個 Secret。GitHub Actions 的 Secrets 與 Streamlit Secrets 是分開設定的，不會自動共用。[GitHub 官方 Secrets 設定說明](https://docs.github.com/en/actions/how-tos/write-workflows/choose-what-workflows-do/use-secrets)

| Secret 名稱 | 內容 |
| --- | --- |
| `GCP_KEY_JSON` | 與 App 使用相同 Google 服務帳戶的**完整原始 JSON**，包含外層 `{}`。不要貼 Streamlit 的 TOML 區塊、檔名或檔案路徑。 |
| `SPREADSHEET_ID` | 與 App 相同的 Google 試算表 ID。例如網址 `https://docs.google.com/spreadsheets/d/這一段是ID/edit#gid=0`，只填 `/d/` 與 `/edit` 之間的 ID。不要填試算表名稱或整個網址。 |

3. 確認該試算表已分享給 JSON 中的 `client_email`，權限為「編輯者」，並使用與 App 相同、可用的 Google Sheets API 設定。排程需要寫入 `Snapshots_v2`；不會改寫基準 A1 或交易紀錄 `Events`。
4. 如果 App 原本有指定基準工作表，開啟同一頁的 **Variables → New repository variable**，加入相同設定。沒有指定且 App 已正常辨識帳本時，可先留空。

| Variable 名稱 | 內容 |
| --- | --- |
| `BASELINE_WORKSHEET_ID` | 基準工作表的數字 `gid`，例如 `0`；優先於名稱。App 的「備份與資料健康」也會顯示基準工作表 ID。 |
| `BASELINE_WORKSHEET_NAME` | 僅未指定 ID 時使用；須為原本存放 A1 帳本的分頁名稱，不是整份試算表名稱。 |

金鑰只填入 GitHub 的 Secret 設定，**不要上傳到 Git、不要貼到聊天中**。排程程式只讀取環境變數，不會尋找本機 `gcp_key.json` 或 `.streamlit/secrets.toml`。

## 手動測試一次

1. GitHub 專案 → **Actions → 每日資產快照 → Run workflow**，選擇預設分支後執行。
2. 等待綠色勾勾。最後一步正常時只會顯示類似 `2026-10-21 written`；同日內容沒有變更時顯示 `unchanged`。
3. 重新整理 App，查看「最新資產快照時間」與「背景排程最後成功」，並確認資產走勢出現今日資料。即使今天 App 已保存過快照，背景排程仍保留當日成功執行紀錄。

設定完成後每天 18:17 的排程會使用相同設定。GitHub 可能延遲執行；這個時間不是準點保證。排程每次最多執行十分鐘，失敗時可在修正設定後用 **Run workflow** 重試。[GitHub 官方排程說明](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)

## 更新提醒與排程中斷

App 的「最新資產快照時間」顯示目前圖表最新保存的日期／時間；「背景排程最後成功」另外顯示 GitHub Actions 最近一次成功保存的時間，避免今天打開 App 的自動更新掩蓋背景排程已中斷。超過兩天未更新時，App 會顯示提醒，這是 App 自訂的檢查門檻。

公開 GitHub 專案若連續 **60 天沒有儲存庫活動**，GitHub 會自動停用排程；這不是每 60 天固定停一次，開啟 Streamlit App 也不算 GitHub 儲存庫活動。若 Actions 顯示排程已停用，可進入 **Actions → 每日資產快照 → Enable workflow**，再手動執行一次確認。規則與其他排程限制見 [GitHub 官方說明](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)。

## 失敗時先檢查

公開日誌只輸出錯誤類別，不輸出金鑰、帳號或資產數字。

| 日誌錯誤類別 | 建議檢查 |
| --- | --- |
| `ConfigurationError` | 兩個必要 Secret 是否存在、JSON 是否完整、試算表是否只填 ID，或工作表 ID 是否為數字。 |
| `SpreadsheetNotFound` / `PermissionError` / `APIError` | 試算表 ID、服務帳戶分享權限、Google Sheets API，以及暫時的連線或配額問題。 |
| `LedgerError` | App 是否也能正常讀取帳本、基準工作表設定、當天行情是否可用。 |
| `Conflict` | 有其他操作同時修改帳本；程式已重新讀取並最多重試一次，可稍後手動重跑。 |
| `WriteUncertain` | 寫入結果無法確認；先重新開啟 App／檢查試算表，再手動重跑。程式會辨識同日相同快照，避免盲目重送。 |

程式使用與 App 相同的驗證與快照儲存函式。Workflow 只需要讀取儲存庫程式的 GitHub 權限；Google 試算表的寫入權限來自服務帳戶。使用的官方 Actions 已固定至經查證的版本：[checkout v7.0.1](https://github.com/actions/checkout/releases/tag/v7.0.1)、[setup-python v7.0.0](https://github.com/actions/setup-python/releases/tag/v7.0.0)。
