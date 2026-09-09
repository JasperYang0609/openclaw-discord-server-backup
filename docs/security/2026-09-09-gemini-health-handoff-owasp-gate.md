# Gemini 健康報告交接｜OWASP Top 10:2025 Gate

實作與驗證前狀態為 `BLOCKED`。

- A01 Broken Access Control：BLOCKED — manifest 路徑與檔案身分、owner、mode、symlink 檢查。
- A02 Security Misconfiguration：BLOCKED — Gemini/Qwen 模式明確且 fail closed。
- A03 Software Supply Chain Failures：BLOCKED — 不新增相依，執行 dependency audit。
- A04 Cryptographic Failures：NOT_APPLICABLE_WITH_EVIDENCE — 不處理密碼／金鑰；只讀索引執行憑證。
- A05 Injection：BLOCKED — provider/model/mode 固定 allowlist，無 shell interpolation。
- A06 Insecure Design：BLOCKED — 冷備援不再冒充 active，也不以過期 Qwen 阻擋正常 Gemini。
- A07 Authentication Failures：NOT_APPLICABLE_WITH_EVIDENCE — 本地報告器沒有登入表面。
- A08 Data/Software Integrity Failures：BLOCKED — rows/chunks/date/schema 欄位交叉驗證。
- A09 Logging and Alerting Failures：BLOCKED — 不可信 Gemini manifest 必須顯示異常，不得靜默綠燈。
- A10 Mishandling of Exceptional Conditions：BLOCKED — missing、stale、future、tamper、permission、oversize、symlink 負向測試。

ASVS v5.0.0：本機 CLI，無 Web/API 表面，register 為 `N/A_WITH_REASON`；由本地檔案信任邊界與負向測試替代。
