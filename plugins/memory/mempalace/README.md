# MemPalace Memory Provider cho Hermes Agent

Provider memory local-first cho Hermes Agent sử dụng [mempalace_rust](https://github.com/tranquangdang46/mempalace_rust) qua MCP protocol.

## Kiến trúc

```
Hermes Agent → MemPalaceMemoryProvider → MemPalaceMCPClient → mpr mcp (subprocess)
                                                                    ↓
                                                              PalaceDb
                                                          (SQLite + vector)
```

## Yêu cầu

- **mpr binary** — MemPalace Rust CLI
- Python ≥ 3.9 (no extra packages needed)

### Cài đặt mpr

```bash
# Từ source
cd ~/Projects/mempalace_rust
cargo install --path crates/cli

# Kiểm tra
mpr --version
```

## Quick Start

### 1. Kích hoạt trong config.yaml

```yaml
memory:
  provider: mempalace
```

### 2. Plugin tự động init

Khi Hermes khởi động, plugin sẽ tự động:
1. Phát hiện `mpr` binary
2. Khởi tạo palace với lightweight settings (`mpr init --no-llm --search-strategy contains --yes`)
3. Kết nối MCP persistent (`mpr mcp` subprocess)
4. Inject 3 tools cho agent

Hoặc chạy thủ công:

```bash
hermes memory setup mempalace
```

## Tools

| Tool | MCP Backend | Mô tả | Timeout |
|------|-------------|-------|---------|
| `memory_search` | `mempalace_hybrid_search` | Search memories (BM25 + vector + KG + RRF fusion) | 15s |
| `memory_save` | `mempalace_add_drawer` | Store important info permanently | 15s |
| `memory_status` | `mempalace_health` | Check palace health and stats | 15s |

## Environment Variables

| Variable | Default | Mô tả |
|----------|---------|-------|
| `MEMPALACE_PALACE_PATH` | `$HERMES_HOME/mempalace/` | Palace directory |
| `MPR_PATH` | (auto-detect) | Explicit path to mpr binary |

## Lightweight Init

Plugin sử dụng lightweight init để không cần download model hay LLM:

```bash
mpr init <dir> --no-llm --search-strategy contains --yes
```

- `--no-llm`: Không cần ollama/gemma
- `--search-strategy contains`: Rust substring matching, 0MB disk
- `--yes`: Non-interactive

## Troubleshooting

| Vấn đề | Kiểm tra | Fix |
|--------|----------|-----|
| "mpr binary not found" | `which mpr` | Cài đặt mpr |
| "Failed to connect" | `mpr mcp` chạy thủ công | Kiểm tra palace đã init |
| Palace chưa init | `ls ~/.hermes/mempalace/.mempalace/` | Chạy mpr init thủ công |

## So sánh với các provider khác

| Tính năng | MemPalace | ByteRover | Holographic | Honcho |
|-----------|-----------|-----------|-------------|--------|
| Local-first | ✅ | ✅ | ✅ | ❌ |
| Hybrid search | ✅ BM25+vec+KG+RRF | ❌ | ✅ FTS5 | ❌ |
| Knowledge Graph | ✅ | ❌ | ❌ | ❌ |
| Init time | **< 1s** | N/A | N/A | account |
| MCP persistent | ✅ | ❌ (subprocess) | ✅ (in-process) | ❌ |
| Protocol | MCP stdio | subprocess CLI | native Python | REST API |
