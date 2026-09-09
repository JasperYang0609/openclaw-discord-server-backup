# Gemini 健康報告交接｜OWASP Top 10:2025 Gate

狀態：`PASS`（2026-09-09）。完整測試 464/464、secret/risky-call scan 與 `git diff --check` 均通過；未新增相依。

- A01 Broken Access Control：PASS — manifest 路徑必須為 installer 明確保存的絕對安全路徑；讀取驗證 owner、0600、單一 hard link、regular file、symlink 與 16 KiB 上限。
- A02 Security Misconfiguration：PASS — 配置 Gemini 時以 Gemini 為 active；未配置時才走 legacy Qwen；不可信 Gemini fail closed。
- A03 Software Supply Chain Failures：PASS — 未新增相依，沿用標準函式庫與既有封裝完整性清單。
- A04 Cryptographic Failures：NOT_APPLICABLE_WITH_EVIDENCE — 不處理密碼／金鑰；只讀索引執行憑證。
- A05 Injection：PASS — provider/model/mode/dimensions 固定 allowlist，無 shell interpolation；risky-call scan 通過。
- A06 Insecure Design：PASS — Qwen 冷備援只驗證保存身分、不要求每日 freshness、不納入 active overall status。
- A07 Authentication Failures：NOT_APPLICABLE_WITH_EVIDENCE — 本地報告器沒有登入表面。
- A08 Data/Software Integrity Failures：PASS — rowsAfter 必須為正整數且等於 chunksAvailable；當日 indexedAt、provider、model、dimensions 與 mode 交叉驗證。
- A09 Logging and Alerting Failures：PASS — 不可信 Gemini manifest 顯示紅燈與白話影響；健康 Gemini 顯示筆數與 Qwen 冷備援狀態。
- A10 Mishandling of Exceptional Conditions：PASS — wrong provider/model/mode/rows/date、permission、oversize、symlink ancestor 負向測試全部通過。

ASVS v5.0.0：本機 CLI，無 Web/API 表面，register 為 `N/A_WITH_REASON`；由本地檔案信任邊界與負向測試替代。
