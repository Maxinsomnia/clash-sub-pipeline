# Clash Sub Pipeline

<!-- STATS_BADGE_START -->
![Update](https://img.shields.io/badge/Updated-2026--11--19_07%3A29%3A34-green.svg?style=flat-square)
![Nodes](https://img.shields.io/badge/Valid_Nodes-280-orange.svg?style=flat-square)
![Sources](https://img.shields.io/badge/Active_Sources-9-blue.svg?style=flat-square)
<!-- STATS_BADGE_END -->

> 基于 [lanzm/MetaFetch](https://github.com/lanzm/MetaFetch) (MIT) fork 二次开发。
> 融合了 MetaFetch 的抓取/智能池、AutoMergePublicNodes 的源管理、V2RayAggregator 的测速筛选、
> free-proxy-airport 的订阅自检与 Pages 分发，以及本地 `clash_node_update` 的 TCP 测速思路。
> 由 GitHub Actions 每 4 小时自动抓取公开节点 → 测速筛选 → 生成 Clash 订阅。

## 与上游 MetaFetch 的差异

- ✅ **真实测速**：抓取去重后对每个节点做并发 TCP 探测（延迟写回节点并按速度排序），死节点自动剔除
  （Hysteria2/UDP 与私有节点豁免，可用 Repository Variables 配置）。
- ✅ **延迟可见**：节点名附带 `⚡123ms` 标签，方便在客户端里肉眼挑快的。
- ✅ **AI 智能分流**：新增 `🤖 AI 服务` 策略组，OpenAI / Claude / Anthropic / Gemini / Cursor 等域名优先走代理；
  新增 `🛑 广告拦截` 组。
- ✅ **订阅自检**：CI 推送前校验 YAML 结构、必需策略组、关键规则、策略组悬空引用；
  并用**真正的 Mihomo 内核**（`clash -t`）加载配置，捕获 SS2022 非法密钥等会让客户端报"输入不合法"的致命错误。
- ✅ **GitHub Pages 分发**：`docs/` 目录输出 `clash.yaml` / `clash.b64` / `nodes.txt`，可在仓库 Settings → Pages
  里设置为「Deploy from a branch → master / docs」获得稳定公开订阅链接。
- ✅ **源扩充**：新增你 star 过的 `chengaopan/AutoMergePublicNodes`、`mahdibland/V2RayAggregator`、
  `sunmiao4458/free-proxy-airport` 等已实测可达的源。

## 订阅链接

> ⚠️ **优先用 Pages 链接**：`raw.githubusercontent.com` 的 CDN 有时会缓存旧内容（实测滞后可达数十分钟），
> 遇到"输入不合法"或节点没更新时先换 Pages 链接。Pages 链接已启用并稳定。

| 客户端 | 订阅地址（推荐度从高到低） |
| :--- | :--- |
| **Clash / ClashX Meta / Mihomo（推荐）** | `https://maxinsomnia.github.io/clash-sub-pipeline/clash.yaml` |
| Clash（备用） | `https://raw.githubusercontent.com/Maxinsomnia/clash-sub-pipeline/master/list.meta.yml` |
| Shadowrocket / v2rayN / Quantumult X | `https://maxinsomnia.github.io/clash-sub-pipeline/clash.b64` |
| 明文节点（Sub-Store 等） | `https://maxinsomnia.github.io/clash-sub-pipeline/nodes.txt` |

> 也可走 jsDelivr CDN 加速：`https://cdn.jsdelivr.net/gh/Maxinsomnia/clash-sub-pipeline@master/list.meta.yml`

## 工作原理

```
sources.yaml (17 个源，默认启用 10 个)
        │
        ▼
core/fetcher.py   并发抓取（异步 httpx，动态日期 URL、递归源、私有源）
        │
        ▼
core/parser.py    解析 ss / vmess / vless / trojan / hysteria2 → Node
        │
        ▼
core/processor.py 去重 / 校验 / 名称清洗 / 地区识别
        │
        ▼
core/tester.py    ★ 并发 TCP 测速 → 标记 _latency_ms → 剔除死节点
        │
        ▼
core/generator.py 智能池（地区分组 + 质量评分，评分纳入实测延迟）→ Clash YAML + 策略组 + 规则
        │
        ▼
CI 自检 → 提交到仓库 + docs/ (Pages) → 客户端自动更新
```

## 本地运行

```bash
pip install -r requirements-dev.txt

# 完整流水线（含真实测速）
python main.py

# 仅跑测试
python -m unittest discover tests -v
```

### 按你自己的网络筛选节点（推荐）

GitHub Actions 的 runner 在海外，测出的可用性代表的是海外网络。要在国内用，
用下面的脚本做一次**本地真实 HTTP 代理测速**，生成只含本机可用节点的配置：

```bash
# 默认从 Pages 订阅读取，结果写入 local-clash.yaml
python scripts/local_probe.py

# 指定源和输出，可调节并发/超时
python scripts/local_probe.py \
  --url https://maxinsomnia.github.io/clash-sub-pipeline/clash.yaml \
  --output my-clash.yaml --workers 40 --timeout 5000
```

脚本会自动从 `/Applications/ClashX Meta.app` 提取内置 Mihomo 内核（可用
`MIHOMO_BIN` 覆盖），对每个节点发起真实 HTTP 请求，剔除失败节点并清理策略组
里的悬空引用。把输出的**文件**通过 ClashX Meta 的「配置 → 导入 → 本地文件」加载。
建议每天或感觉节点变慢时重跑一次。

## 可配置项

### Repository Variables（Settings → Secrets and variables → Actions → Variables）

| 变量 | 默认值 | 说明 |
| :--- | :--- | :--- |
| `SPEEDTEST_ENABLED` | `true` | 是否开启测速；关掉可恢复上游纯聚合行为 |
| `SPEEDTEST_TIMEOUT` | `3` | 单个节点 TCP 连接超时（秒） |
| `SPEEDTEST_WORKERS` | `120` | 并发探测数 |
| `SPEEDTEST_MAX_NODES` | `2000` | 最多探测多少节点，防止 CI 超时 |
| `SPEEDTEST_DROP_DEAD` | `true` | 是否剔除不可达节点 |
| `ANNOTATE_LATENCY` | `true` | 是否在节点名附加 `⚡xxms` |
| `KERNEL_PROBE_ENABLED` | `true` | 是否开启内核级真实 HTTP 代理探测（剔除 TCP 通但代理不可用的节点） |
| `KERNEL_PROBE_TIMEOUT` | `6000` | 单节点 HTTP 探测超时（毫秒） |
| `KERNEL_PROBE_WORKERS` | `40` | 内核探测并发数 |
| `KERNEL_PROBE_MAX_NODES` | `400` | 最多做真实代理探测的节点数 |
| `KERNEL_PROBE_DROP_DEAD` | `true` | 是否剔除真实探测失败的节点 |

自动选择池容量、地区组上限、测速间隔与 lazy 模式在 `sources.yaml` 的 `settings.smart_pool`
中配置（`max_pool_size` / `region_group_limit` / `test_interval` / `test_timeout` / `lazy`）。

### Secrets

- `PRIVATE_SOURCES`：私有订阅源（YAML 或 list），可选。私有节点会以 👑 标记并豁免测速。
- `TG_BOT_TOKEN` / `TG_CHAT_ID`：Telegram 通知，可选。

## 配置源文件

编辑 [`sources.yaml`](sources.yaml)：每个源支持 `name`、`url`、`enabled`、`ignore`、`recursive`、
`verify_tls`、`note`。URL 中的 `%Y` `%m` `%d` 会自动替换为当天日期（适合日抛源）。
`settings.smart_pool` 控制智能池各地区配额和总容量。

## 已知边界

- 两级探测：**TCP 连通**（快速预筛）+ **Mihomo 内核真实 HTTP 代理请求**（剔除协议/认证/出口失效的节点）。
  第二级在 CI 的海外 runner 上运行，代表的仍是海外可用性；本地网络请用 `scripts/local_probe.py` 再筛一次。
- 自动选择策略组设为 `lazy: true`、300 秒间隔、24 节点上限，地区组每组最多 12 个，
  避免客户端启动时对数百节点并发测速导致大面积「失败」显示。
- Hysteria2 走 UDP，TCP 探测无法覆盖，这类节点默认保留（豁免）。
- 公开免费节点随时可能失效或被滥用，请勿用于敏感账号或生产环境。
- 请遵守所在地区的法律法规和相关服务条款。

## License

MIT（继承自上游 MetaFetch，见 [LICENSE](LICENSE)）。
