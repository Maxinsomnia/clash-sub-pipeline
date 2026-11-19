# 候选订阅源质量报告

- 生成时间（UTC）：2026-11-19T00:05:55.190842+00:00
- 探测环境：`github-actions-overseas`
- 评估数量：5，达标：0
- 阈值：最少解析节点 5，最低存活率 25%

> ⚠️ Probed from GitHub Actions runner (overseas). Alive rates reflect overseas reachability, NOT mainland China. Re-run locally on a Chinese network before promoting a source.

| 仓库 | URL | 解析 | 测试 | 存活 | 存活率 | 最快(ms) | 结论 |
| :--- | :--- | ---: | ---: | ---: | ---: | ---: | :--- |
| Loyalsoldier/clash-rules | https://raw.githubusercontent.com/Loyalsoldier/clash-rule... | 0 | 0 | 0 | 0% | - | ⚠️ no parsable nodes |
| Loyalsoldier/v2ray-rules-dat | https://raw.githubusercontent.com/Loyalsoldier/v2ray-rule... | 0 | 0 | 0 | 0% | - | ⚠️ no parsable nodes |
| Loyalsoldier/v2ray-rules-dat | https://raw.githubusercontent.com/Loyalsoldier/v2ray-rule... | 0 | 0 | 0 | 0% | - | ⚠️ no parsable nodes |
| Loyalsoldier/v2ray-rules-dat | https://raw.githubusercontent.com/Loyalsoldier/v2ray-rule... | 0 | 0 | 0 | 0% | - | ⚠️ no parsable nodes |
| Loyalsoldier/v2ray-rules-dat | https://raw.githubusercontent.com/Loyalsoldier/v2ray-rule... | 0 | 0 | 0 | 0% | - | ⚠️ no parsable nodes |

## 审核说明

达标仅表示在该探测环境可用。确认要纳入时，用报告中的 `key` 字段执行：

```bash
python scripts/source_scout.py promote --approve '<key>'
```

promote 只会写入 `enabled: false` 的候选项，需你手动确认后才生效。
