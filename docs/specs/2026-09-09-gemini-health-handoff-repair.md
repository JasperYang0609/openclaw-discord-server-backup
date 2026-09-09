# Gemini 健康報告交接修復規格

## 問題

07:05 報告仍以 Qwen 當作目前索引來源。Qwen 已轉為冷備援且不再每日更新，因此舊憑證被正確判定為過期，卻造成 Gemini 正常時的假紅燈。

## 核准範圍

- 新增 Gemini incremental manifest 的固定路徑設定與嚴格驗證。
- 只接受 `google-gemini`、`gemini-embedding-001`、`incremental`、當地當日且帶時區的 `indexedAt`。
- `rowsAfter` 必須為正整數並等於 `chunksAvailable`；manifest 必須由目前使用者持有、權限為 0600、沒有 symlink 且大小受限。
- Gemini 設定存在時，以 Gemini 作為每日搜尋索引健康來源；Qwen 顯示為冷備援，不再要求當日新鮮度，也不影響整體綠燈。
- 未設定 Gemini 時維持既有 Qwen 相容行為。
- 不修改 Discord 原始備份、Gemini 索引內容或 Qwen 資產。

## 驗收

- 當日可信 Gemini manifest + Qwen 冷備援時報告為正常。
- 昨日、未來、錯 provider/model/mode、筆數不一致、權限錯誤、過大或 symlink manifest 一律不得綠燈。
- installer、managed runner、renderer、完整測試與正式設定 readback 通過。
